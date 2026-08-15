"""阿里云内容安全增强版（Green 2.0 / 2022-03-02）文本审核封装。

**产品口径（谁必须过审）**：
  - **必须**：用户自己输入的文本、用户自己录音经 ASR 得到的文本。
  - **不必**：解析账号 / 解析视频；文案生成 / 访谈问题 / 归因等创作链路
    （大模型自身已做合规，不再叠阿里云）。

用 aliyunsdkcore 的 CommonRequest（RPC 风格，自动签名）。
旧版 Green 1.0（2018-05-09 textScan，ROA 路径 /green/text/scan）手写签名 →
阿里云 400 MissingAction → 404 InvalidAction.NotFound——端点已不支持旧版 ROA。
切到 2.0 TextModeration（RPC，Action=TextModeration，域 green-cip.*）。

响应格式（2.0）：
  安全：{"Code":200,"Data":{"reason":"","descriptions":"","labels":""}}
  命中：{"Code":200,"Data":{"reason":"涉政","labels":"political"}}
  错误：{"Code":408,"Message":"..."}（如服务未开通）
  超长：{"Code":400,"Message":"content is too long(>600)"} —— 非违规，须本地分片。

判定：Code==200 且 Data.reason/labels 为空 → 安全；非空 → 命中。
``comment_detection`` 单次 content ≤600 字符；长文本按片审核，任一片命中即拦截。
"""

from __future__ import annotations

import asyncio
import json
import logging

from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest

from app.config import settings

log = logging.getLogger(__name__)

_DOMAIN = "green-cip.cn-shanghai.aliyuncs.com"
_VERSION = "2022-03-02"
_ACTION = "TextModeration"
_SERVICE = "comment_detection"
# 官方：comment_detection 的 content 限定在 600 字符以内。
_MAX_CONTENT_CHARS = 600


def _chunks(text: str, size: int = _MAX_CONTENT_CHARS) -> list[str]:
    """按 ``size`` 切分（strip 后）；空串返回 []。"""
    t = text.strip()
    if not t:
        return []
    return [t[i : i + size] for i in range(0, len(t), size)]


def _is_safe(body: dict) -> bool:
    """解析 TextModeration 2.0 响应：Code==200 且 reason/labels 为空才安全。"""
    if body.get("Code") != 200:
        return False
    data = body.get("Data") or {}
    # 安全时 Data 为 {"reason":"","descriptions":"","labels":""}（全空）。
    # 命中时 reason/labels 非空（如 reason="涉政", labels="political"）。
    if data.get("reason") or data.get("labels"):
        return False
    # 若有 Result 数组（部分响应格式），检查 Suggestion。
    for r in (data.get("Result") or []):
        if r.get("Suggestion") not in (None, "pass"):
            return False
    return True


def _moderate_sync(text: str) -> dict:
    """同步调用 TextModeration（由 ``check`` 经 ``asyncio.to_thread`` 调度）。"""
    acs = AcsClient(
        settings.ALIYUN_ACCESS_KEY_ID,
        settings.ALIYUN_ACCESS_KEY_SECRET,
        "cn-shanghai",
    )
    req = CommonRequest()
    req.set_domain(_DOMAIN)
    req.set_version(_VERSION)
    req.set_action_name(_ACTION)
    req.set_method("POST")
    req.set_accept_format("json")
    # TextModeration 2.0 参数：Service（检测场景）+ ServiceParameters（JSON 含 content）
    req.add_query_param("Service", _SERVICE)
    req.add_query_param(
        "ServiceParameters",
        json.dumps({"content": text}, ensure_ascii=False),
    )
    resp = acs.do_action_with_exception(req)
    return json.loads(resp.decode())


async def check(text: str, *, client=None) -> bool:
    """文本是否安全（True=安全）。

    长文本按 ``_MAX_CONTENT_CHARS`` 分片逐段审核（任一段命中 → False）。
    ``client`` 保留兼容位（旧 Green 1.0 httpx 测试）；当前实现走 AcsClient，忽略该参。

    失败（网络/签名/内容命中）按不安全处理（fail-closed），但留痕排查。
    """
    del client  # 兼容旧签名；不再使用 httpx mock transport
    if not text or not text.strip():
        # 空文本无需审核（Aliyun API 对空内容返回 400 "content is blank" → 误拦截）
        return True
    if not settings.ALIYUN_ACCESS_KEY_ID or not settings.ALIYUN_ACCESS_KEY_SECRET:
        log.warning("content safety skip: ALIYUN AK not configured (fail-closed → blocked)")
        return False

    parts = _chunks(text)
    try:
        for i, part in enumerate(parts):
            body = await asyncio.to_thread(_moderate_sync, part)
            if not _is_safe(body):
                log.warning(
                    "content safety blocked chunk %d/%d "
                    "(reason/labels non-empty or Code!=200): %s",
                    i + 1, len(parts),
                    json.dumps(body, ensure_ascii=False)[:500],
                )
                return False
        return True
    except Exception as e:
        # fail-closed：宁可让 Java 走重试/退款，也不放行未审内容。
        log.warning(
            "content safety check failed (fail-closed → blocked): %s",
            e,
        )
        return False
