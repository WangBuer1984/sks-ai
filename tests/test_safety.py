"""阿里云内容安全 check() 测试：mock httpx，绝不发真实网络请求。"""

import json

import httpx
import pytest

from app.safety.content_safety import check


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def _safe_response(request):
    return httpx.Response(
        200,
        json={
            "code": 200,
            "msg": "OK",
            "requestId": "r-1",
            "data": [
                {
                    "code": 200,
                    "msg": "OK",
                    "taskId": "t-1",
                    "dataId": "d-1",
                    "results": [{"scene": "antispam", "suggestion": "pass", "label": "normal", "rate": 99.0}],
                }
            ],
        },
    )


async def _block_response(request):
    return httpx.Response(
        200,
        json={
            "code": 200,
            "msg": "OK",
            "requestId": "r-2",
            "data": [
                {
                    "code": 200,
                    "msg": "OK",
                    "taskId": "t-2",
                    "dataId": "d-2",
                    "results": [{"scene": "antispam", "suggestion": "block", "label": "spam", "rate": 99.0}],
                }
            ],
        },
    )


async def _review_response(request):
    return httpx.Response(
        200,
        json={
            "code": 200,
            "msg": "OK",
            "requestId": "r-3",
            "data": [
                {
                    "code": 200,
                    "msg": "OK",
                    "taskId": "t-3",
                    "dataId": "d-3",
                    "results": [{"scene": "antispam", "suggestion": "review", "label": "politics", "rate": 80.0}],
                }
            ],
        },
    )


async def test_check_returns_true_for_clean():
    client = _mock_client(_safe_response)
    try:
        assert await check("正常文本", client=client) is True
    finally:
        await client.aclose()


async def test_check_returns_false_for_blocked():
    client = _mock_client(_block_response)
    try:
        assert await check("违禁文本", client=client) is False
    finally:
        await client.aclose()


async def test_check_returns_false_for_review():
    client = _mock_client(_review_response)
    try:
        assert await check("可疑文本", client=client) is False
    finally:
        await client.aclose()


async def test_check_sends_text_as_content():
    captured = {}

    async def handler(request):
        body = json.loads(request.content)
        captured["body"] = body
        return await _safe_response(request)

    client = _mock_client(handler)
    try:
        await check("hello world", client=client)
    finally:
        await client.aclose()

    assert "tasks" in captured["body"]
    assert captured["body"]["scenes"] == ["antispam"]
    assert any(t["content"] == "hello world" for t in captured["body"]["tasks"])


async def test_check_returns_false_on_api_error():
    async def handler(request):
        return httpx.Response(200, json={"code": 500, "msg": "Internal", "data": []})

    client = _mock_client(handler)
    try:
        assert await check("x", client=client) is False
    finally:
        await client.aclose()
