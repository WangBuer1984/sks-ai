"""FastAPI 入口：随口说 Python AI 服务（仅内网，不暴露公网）。

/health 免鉴权（内网健康检查）；/ai/* 全部以 verify_service_token 守卫
（Java→Python 唯一出口带 X-Service-Token）。后续 skill 路由按 task 逐步挂载。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.card_gen import router as card_gen_router
from app.api.embed import router as embed_router
from app.api.safety import router as safety_router
from app.api.script_gen import router as script_gen_router
from app.db import close_pool, init_pool

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup：建 asyncpg 池 + 注册 pgvector（懒初始化兜底，真实环境由本处显式起池）
    try:
        await init_pool()
    except Exception:  # noqa: BLE001 — DB 不可达不应阻断 /health 与 /ai/* 鉴权层启动
        # 不阻断启动（/health 仍可探活、/ai/* 鉴权层可用），但必须留痕：
        # 否则配置错的 DATABASE_URL 会让 /health 假 UP，把坏连接推迟到首个 RAG 调用才暴露。
        log.exception("init_pool failed; /health stays UP, RAG/kb_card/analyze_task endpoints will fail on demand")
    yield
    # shutdown：关池
    await close_pool()


app = FastAPI(title="sks-ai", version="0.1.0", lifespan=lifespan)
app.include_router(embed_router)
app.include_router(safety_router)
app.include_router(script_gen_router)
app.include_router(card_gen_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "UP"}
