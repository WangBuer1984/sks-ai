"""asyncpg 连接池 + pgvector 类型注册。

仅用于 RAG / kb_card / analyze_task 的读写（检索 + LangGraph checkpointer + 异步任务状态）。
**不做 migration**——Java/Flyway 拥有 schema 演进（V1__core_schema.sql 建表，含
kb_card.embedding vector(1024)）。本池在 init hook 注册 pgvector，使读写 vector 列透明。
"""

from __future__ import annotations

import asyncpg
from pgvector.asyncpg import register_vector

from app.config import settings

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """每个新连接注册 pgvector 类型编解码器。"""
    await register_vector(conn)


async def init_pool(dsn: str | None = None, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    """创建全局连接池。通常由 FastAPI startup 调用；测试可不调用。"""
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        dsn=dsn or settings.DATABASE_URL,
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
    )
    return _pool


async def get_pool() -> asyncpg.Pool:
    """懒初始化并返回全局池。"""
    if _pool is None:
        await init_pool()
    assert _pool is not None
    return _pool


async def close_pool() -> None:
    """关闭全局池（FastAPI shutdown / 测试清理）。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
