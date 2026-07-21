"""POST /ai/card_gen 路由：补卡（抽卡 + 缺口 + 冲突检测）。

Java（Task 1.5 supplement 流程）通过 AiClient.cardGen 调本端点。router 整体
verify_service_token 守卫——与 embed/safety/script_gen 路由同模式。无流式：同步返回完整 JSON。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import verify_service_token
from app.skills.card_gen.graph import generate_cards

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(verify_service_token)])


class CardGenCard(BaseModel):
    card_type: str
    title: str
    content: dict[str, Any] = Field(default_factory=dict)


class CardGenConflict(BaseModel):
    card_id: int
    card_index: int
    reason: str


class CardGenRequest(BaseModel):
    user_id: int
    raw_text: str
    target_layer: str


class CardGenResponse(BaseModel):
    cards: list[CardGenCard] | None = None
    gaps: list[str] | None = None
    conflicts: list[CardGenConflict] | None = None
    blocked: bool = False


@router.post("/card_gen", response_model=CardGenResponse, response_model_exclude_unset=True)
async def post_card_gen(req: CardGenRequest) -> CardGenResponse:
    result = await generate_cards(
        user_id=req.user_id,
        raw_text=req.raw_text,
        target_layer=req.target_layer,
    )
    return CardGenResponse(**result)
