"""FastAPI 入口：随口说 Python AI 服务（仅内网，不暴露公网）。

/health 免鉴权（内网健康检查）；/ai/* 全部以 verify_service_token 守卫
（Java→Python 唯一出口带 X-Service-Token）。后续 skill 路由按 task 逐步挂载。

启动时 init_pool（asyncpg，RAG/kb/analyze 用）+ checkpointer.setup()
（AsyncPostgresSaver，LangGraph 私有检查点表——「Python 不做迁移」的唯一例外）。
两者失败均不阻断启动：/health 保持 UP，对应端点按需失败（与 init_pool 同口径）。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.analyze import router as analyze_router
from app.api.asr import router as asr_router
from app.api.attribution import router as attribution_router
from app.api.card_gen import router as card_gen_router
from app.api.embed import router as embed_router
from app.api.interview import router as interview_router
from app.api.safety import router as safety_router
from app.api.script_gen import router as script_gen_router
from app.config import settings
from app.db import close_pool, init_pool

log = logging.getLogger(__name__)


async def _init_checkpointer() -> None:
    """启动时建 AsyncPostgresSaver 并 setup() 检查点表（LangGraph 私有）。

    用同一 DATABASE_URL 的独立 psycopg 连接池（不与 asyncpg 池复用——协议不同）。
    失败不阻断启动：log.exception + /health 仍 UP，interview 端点按需失败
    （与 init_pool 失败处理同口径）。
    """
    try:
        from langgraph.checkpoint.postgres.aio import (
            AsyncConnectionPool,
            AsyncPostgresSaver,
        )

        from app.skills.interview.graph import set_checkpointer

        # psycopg 连接串与 asyncpg 同库；asyncpg 用 postgresql://，psycopg 接受同格式
        # timeout=3.0：DB 不可达时快速失败（避免 TestClient lifespan 卡 30s 默认超时）
        pool = AsyncConnectionPool(
            conninfo=settings.DATABASE_URL, min_size=1, max_size=10,
            open=False, timeout=3.0,
        )
        await pool.open()
        saver = AsyncPostgresSaver(conn=pool)
        await saver.setup()  # 建检查点表（迁移例外，LangGraph 私有）
        set_checkpointer(saver)
        log.info("interview checkpointer ready (AsyncPostgresSaver)")
    except Exception:  # noqa: BLE001 — 不阻断启动，interview 端点按需失败
        log.exception(
            "checkpointer setup failed; /health stays UP, interview endpoints will fail on demand"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup：建 asyncpg 池 + 注册 pgvector（懒初始化兜底，真实环境由本处显式起池）
    try:
        await init_pool()
    except Exception:  # noqa: BLE001 — DB 不可达不应阻断 /health 与 /ai/* 鉴权层启动
        # 不阻断启动（/health 仍可探活、/ai/* 鉴权层可用），但必须留痕：
        # 否则配置错的 DATABASE_URL 会让 /health 假 UP，把坏连接推迟到首个 RAG 调用才暴露。
        log.exception("init_pool failed; /health stays UP, RAG/kb_card/analyze_task endpoints will fail on demand")
    # checkpointer setup（非致命，失败留痕，interview 端点按需失败）
    await _init_checkpointer()
    yield
    # shutdown：关池
    await close_pool()


app = FastAPI(title="sks-ai", version="0.1.0", lifespan=lifespan)
app.include_router(embed_router)
app.include_router(safety_router)
app.include_router(script_gen_router)
app.include_router(card_gen_router)
app.include_router(interview_router)
app.include_router(asr_router)
app.include_router(analyze_router)
app.include_router(attribution_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "UP"}
