"""POST /ai/attribution/single + POST /ai/attribution/weekly 路由。

归因是 FREE review aid（PRD：flop「看归因」不扣费；周归因是定时聚合，非用户扣费）——
无额度链路，Java 侧直接调本端点（仍带 X-Service-Token，内网信任边界）。

router 整体 verify_service_token 守卫——与 script_gen / card_gen / analyze 路由同模式。
无流式（硬不变量）：同步返回完整 JSON（含 {blocked: true} 情况）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import verify_service_token
from app.skills.attribution.graph import attribution_single, attribution_weekly

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(verify_service_token)])


# ---- /ai/attribution/single ------------------------------------------------

class AttributionSingleResponse(BaseModel):
    diagnosis: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    blocked: bool = False


class AttributionSingleRequest(BaseModel):
    script: str
    play_count: int
    baseline: float


@router.post(
    "/attribution/single",
    response_model=AttributionSingleResponse,
    response_model_exclude_unset=True,
)
async def post_attribution_single(req: AttributionSingleRequest) -> AttributionSingleResponse:
    result = await attribution_single(
        script=req.script,
        play_count=req.play_count,
        baseline=req.baseline,
    )
    return AttributionSingleResponse(**result)


# ---- /ai/attribution/weekly ------------------------------------------------

class WeeklyScriptItem(BaseModel):
    """单条 script 子项（Java 组装；skill prompt 只取已知字段，其余忽略）。

    请求体本身用 ``list[dict[str, Any]]``（见 AttributionWeeklyRequest）以放行 Java 透传的
    额外字段（title 等）；本模型仅文档化契约。
    """

    script: str = ""
    play_count: int = 0
    review_state: str = "unknown"
    baseline: float | None = None
    model_config = {"extra": "allow"}


class AttributionWeeklyRequest(BaseModel):
    user_id: int
    scripts: list[dict[str, Any]] = Field(default_factory=list)


class AttributionWeeklyResponse(BaseModel):
    summary: str | None = None
    wins: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    next_focus: str | None = None
    blocked: bool = False


@router.post(
    "/attribution/weekly",
    response_model=AttributionWeeklyResponse,
    response_model_exclude_unset=True,
)
async def post_attribution_weekly(req: AttributionWeeklyRequest) -> AttributionWeeklyResponse:
    result = await attribution_weekly(
        user_id=req.user_id,
        scripts=req.scripts,
    )
    return AttributionWeeklyResponse(**result)
