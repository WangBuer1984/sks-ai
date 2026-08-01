"""阿里云内容安全（Green 文本审核）封装。

设计文档 §5.1：LLM 输出 + UGC 均过审；命中违禁自动重写一次，仍命中返回特定错误码、
Java 走退款流程。本方法只回答「文本是否安全」（True=安全）。

用 aliyunsdkcore 的 CommonRequest（RPC 风格，自动签名），替代手写 ACS ROA。
手写 ROA 调 /green/text/scan 时阿里云返回 400 MissingAction（端点期望 RPC Action 参数）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest

from app.config import settings

log = logging.getLogger(__name__)

_SCENE = "antispam"
_DOMAIN = "green.cn-shanghai.aliyuncs.com"
_VERSION = "2017-08-23"
_ACTION = "TextScan"


def _is_safe(body: dict) -> bool:
    """解析 Green TextScan 响应：suggestion 全 pass 才安全。"""
    if body.get("code") != 200:
        return False
    data = body.get("data") or []
    if not data:
        return False
    for task in data:
        for r in task.get("results", []):
            if r.get("suggestion") != "pass":
                return False
    return True


async def check(text: str, *, client=None) -> bool:
    """文本是否安全（True=安全）。

    用 aliyunsdkcore CommonRequest（RPC 风格，自动签名 + Action 参数）。
    AcsClient.do_response_with_exception 是同步调用——用 asyncio.to_thread 避免阻塞事件循环。
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
        req.add_query_param("Content", json.dumps(
            {
                "scenes": [_SCENE],
                "tasks": [{"dataId": str(uuid.uuid4()), "content": text}],
            },
            ensure_ascii=False,
        ))
        # AcsClient 是同步的——放线程里跑，不阻塞事件循环
        resp = await asyncio.to_thread(acs.do_action_with_exception, req)
        return _is_safe(json.loads(resp.decode()))
    except Exception as e:
        # fail-closed：宁可让 Java 走重试/退款，也不放行未审内容。
        log.warning(
            "content safety check failed (fail-closed → blocked): %s",
            e,
        )
        return False
