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
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        from app.skills.interview.graph import set_checkpointer

        # 单连接 autocommit 模式——与 langgraph 官方 from_conn_string 同口径
        # （其内部即 AsyncConnection.connect(..., autocommit=True, prepare_threshold=0, row_factory=dict_row)）。
        # 为什么 autocommit：saver.setup() 跑 CREATE INDEX CONCURRENTLY，该语句不能在事务块内执行；
        # psycopg 默认 autocommit=False 会把每条语句包进事务 → ActiveSqlTransaction。autocommit 后
        # 裸 execute 不开事务，CONCURRENTLY 通过；运行时 checkpoint 操作仍用 conn.transaction()
        # 显式开事务保证原子性（不受影响）。不能用 pool 的 configure 回调设：AsyncConnection 的
        # autocommit 属性只读，须 await set_autocommit()，而 configure 是同步回调无法 await。
        # 单连接而非 pool：saver 自带 asyncio.Lock 串行化所有 checkpoint 操作，单连接即够，无并发诉求。
        conn = await AsyncConnection.connect(
            settings.DATABASE_URL,
            autocommit=True, prepare_threshold=0, row_factory=dict_row,
            connect_timeout=3.0,
        )
        saver = AsyncPostgresSaver(conn=conn)
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
