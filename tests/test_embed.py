"""embedding-3 embed() 测试：mock httpx，断言 1024 维 + 调用 embedding-3。"""

import json

import httpx
import pytest

from app.rag.embedding import embed


def _vec(n):
    return [0.01 * (i % 100) for i in range(n)]


async def _embedding_response(request):
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "model": body.get("model", "embedding-3"),
            "data": [{"index": 0, "embedding": _vec(1024), "object": "embedding"}],
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        },
    )


async def test_embed_returns_1024_dims():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_embedding_response))
    try:
        vec = await embed("一段知识库卡片文本", client=client)
    finally:
        await client.aclose()
    assert len(vec) == 1024
    assert all(isinstance(x, float) for x in vec)


async def test_embed_calls_embedding3_model():
    captured = {}

    async def handler(request):
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return await _embedding_response(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await embed("abc", client=client)
    finally:
        await client.aclose()

    assert captured["body"]["model"] == "embedding-3"
    assert captured["body"]["input"] == "abc"
    assert "/embeddings" in captured["url"]


async def test_embed_asserts_dim_mismatch():
    async def handler(request):
        return httpx.Response(
            200,
            json={"model": "embedding-3", "data": [{"embedding": _vec(512)}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(AssertionError):
            await embed("x", client=client)
    finally:
        await client.aclose()
