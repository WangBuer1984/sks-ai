"""阿里云内容安全增强版（Green 2.0 / 2022-03-02）文本审核封装。

用 aliyunsdkcore 的 CommonRequest（RPC 风格，自动签名）。
旧版 Green 1.0（2018-05-09 TextScan，ROA 路径 /green/text/scan）手写签名 →
阿里云 400 MissingAction → 404 InvalidAction.NotFound——端点已不支持旧版 ROA。
切到 2.0 TextModeration（RPC，Action=TextModeration，域 green-cip.*）。

响应格式（2.0）：{"Code":200,"Data":{"Result":[{"Confidence":0,"Label":"non_label","Suggestion":"pass"}]}}
判定：Code==200 且所有 Result[].Suggestion=="pass" 才安全。
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
_SERVICE = "content_detection"


def _is_safe(body: dict) -> bool:
    """解析 TextModeration 2.0 响应：Code==200 且所有 Suggestion==pass 才安全。"""
    if body.get("Code") != 200:
        return False
    data = body.get("Data") or {}
    results = data.get("Result") or []
    if not results:
        return False
    for r in results:
        if r.get("Suggestion") != "pass":
            return False
    return True


async def check(text: str, *, client=None) -> bool:
    """文本是否安全（True=安全）。

    用 aliyunsdkcore CommonRequest（RPC，自动签名 + Action）。
    AcsClient.do_action_with_exception 是同步——用 asyncio.to_thread 不阻塞事件循环。
    失败（网络/签名/内容命中）按不安全处理（fail-closed），但留痕排查。
    """
    if not settings.ALIYUN_ACCESS_KEY_ID or not settings.ALIYUN_ACCESS_KEY_SECRET:
        log.warning("content safety skip: ALIYUN AK not configured (fail-closed → blocked)")
        return False
    try:
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
        req.add_query_param("ServiceParameters", json.dumps(
            {"content": text}, ensure_ascii=False,
        ))
        resp = await asyncio.to_thread(acs.do_action_with_exception, req)
        return _is_safe(json.loads(resp.decode()))
    except Exception as e:
        log.warning(
            "content safety check failed (fail-closed → blocked): %s",
            e,
        )
        return False
