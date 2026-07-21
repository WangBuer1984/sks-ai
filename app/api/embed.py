"""POST /ai/embed 路由：供 Java KB 写卡用（Task 1.2 Java 消费）。

放本任务而非 Task 1.3：Task 1.2 的 Java KB CRUD（B 层卡 embedding 必填）就要消费它，
避免依赖倒挂。router 整体以 verify_service_token 守卫——/health 是唯一免 token 端点。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import verify_service_token
from app.rag.embedding import embed

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(verify_service_token)])


class EmbedRequest(BaseModel):
    text: str


class EmbedResponse(BaseModel):
    embedding: list[float]


@router.post("/embed", response_model=EmbedResponse)
async def post_embed(req: EmbedRequest) -> EmbedResponse:
    vec = await embed(req.text)
    return EmbedResponse(embedding=vec)
