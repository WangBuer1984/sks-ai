"""智谱 embedding-3，1024 维（OpenAI 兼容 /embeddings 端点）。

设计文档 §5：向量统一用智谱 embedding-3，**固定 1024 维**——与 pgvector
kb_card.embedding vector(1024) 列绑定，换模型需全库重算向量并改列定义。
"""

from __future__ import annotations

import httpx

from app.config import settings

# 模型型号仅出现在 llm/ 与本处（向量与对话同厂商、同 key）。维度与 schema 列绑定。
EMBEDDING_MODEL = "embedding-3"
EMBEDDING_DIM = 1024


async def embed(text: str, *, client: httpx.AsyncClient | None = None) -> list[float]:
    """对文本算 1024 维向量。client 可注入用于测试（MockTransport）。

    返回长度恒为 1024；运行期 assert 保证与 schema 列对齐——若智谱侧返回维度不符
    会在此抛出，避免脏向量写进 kb_card.embedding 后无法召回。
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=60.0)
    try:
        resp = await client.post(
            f"{settings.ZHIPU_BASE_URL}embeddings",
            headers={
                "Authorization": f"Bearer {settings.ZHIPU_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": EMBEDDING_MODEL, "input": text},
        )
        resp.raise_for_status()
        vec = resp.json()["data"][0]["embedding"]
        assert len(vec) == EMBEDDING_DIM, (
            f"embedding-3 返回维度 {len(vec)} 与 schema 列 vector(1024) 不符"
        )
        return [float(x) for x in vec]
    finally:
        if own:
            await client.aclose()
