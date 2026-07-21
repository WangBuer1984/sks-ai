"""阿里云内容安全（Green 文本审核）封装。

设计文档 §5.1：LLM 输出 + UGC 均过审；命中违禁自动重写一次，仍命中返回特定错误码、
Java 走退款流程。本方法只回答「文本是否安全」（True=安全）。

API 形状：阿里云内容安全 1.0 Green /green/text/scan（文档：
https://help.aliyun.com/document_detail/53425.html ）
  请求体 JSON：{"scenes":["antispam"],"tasks":[{"dataId":uuid,"content":text}]}
  响应：data[0].results[0].suggestion ∈ {pass, review, block}
  判定：suggestion=="pass" → 安全；"review"/"block" → 不安全。

认证用 阿里云 AccessKey 的 ACS ROA 风格签名（HMAC-SHA1）。本封装内含签名实现，
但**签名细节需在联调期用真实 key 核对**（无 key 无法验证签名正确性）；
测试用 httpx.MockTransport 校验请求体形状 + 响应解析逻辑，不发真实网络请求。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import uuid

import httpx

from app.config import settings

_PATH = "/green/text/scan"
_SCENE = "antispam"


def _build_headers(body: bytes) -> dict[str, str]:
    """构造阿里云 ACS ROA 风格签名头（HMAC-SHA1）。

    StringToSign = METHOD + "\\n" + Accept + "\\n" + Content-MD5 + "\\n"
                   + Content-Type + "\\n" + Date + "\\n"
                   + CanonicalizedAcsHeaders + "\\n" + CanonicalizedResource
    此处 METHOD=POST, Resource=_PATH。签名键 = AccessKeySecret + "&"。
    联调注意：若阿里云侧拒绝签名，改用官方 SDK alibabacloud-green20220302。
    """
    access_key_id = settings.ALIYUN_ACCESS_KEY_ID
    access_key_secret = settings.ALIYUN_ACCESS_KEY_SECRET
    content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    date = datetime.datetime.now(datetime.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    nonce = str(uuid.uuid4())
    accept = "application/json"
    content_type = "application/json"
    x_acs_version = "2017-08-23"

    canonical_headers = (
        f"x-acs-signature-method:HMAC-SHA1\n"
        f"x-acs-signature-nonce:{nonce}\n"
        f"x-acs-version:{x_acs_version}\n"
    )
    string_to_sign = "\n".join([
        "POST",
        accept,
        content_md5,
        content_type,
        date,
        canonical_headers + _PATH,
    ])
    signature = base64.b64encode(
        hmac.new(
            (access_key_secret + "&").encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode()
    return {
        "Accept": accept,
        "Content-Type": content_type,
        "Content-MD5": content_md5,
        "Date": date,
        "x-acs-version": x_acs_version,
        "x-acs-signature-method": "HMAC-SHA1",
        "x-acs-signature-nonce": nonce,
        "Authorization": f"acs {access_key_id}:{signature}",
    }


def _is_safe(body: dict) -> bool:
    """解析 Green /green/text/scan 响应：suggestion 全 pass 才安全。"""
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


async def check(text: str, *, client: httpx.AsyncClient | None = None) -> bool:
    """文本是否安全（True=安全）。client 可注入用于测试。"""
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        body = json.dumps(
            {
                "scenes": [_SCENE],
                "tasks": [{"dataId": str(uuid.uuid4()), "content": text}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        url = f"{settings.ALIYUN_CONTENT_SAFETY_ENDPOINT}{_PATH}"
        resp = await client.post(url, content=body, headers=_build_headers(body))
        resp.raise_for_status()
        return _is_safe(resp.json())
    except httpx.HTTPError:
        # 网络层失败按不安全处理（保守：宁可让 Java 走重试/退款，也不放行未审内容）。
        return False
    finally:
        if own:
            await client.aclose()
