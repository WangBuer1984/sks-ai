"""POST /ai/interview/step + GET /ai/interview/result 路由。

Java（Task 2.2）调本端点驱动定位访谈。校准是免费的（PRD §4.2），无额度逻辑。
router 整体 verify_service_token 守卫——与 script_gen/card_gen 同模式。
无流式：一次 /step 请求返回一次 JSON（含 {blocked:true} 情况）。

数据流：
- /step 同步推进状态机一轮，返回 {stage, question?, profile_draft?, done, blocked?}
- /result 只读从 checkpoint 取 summarize 产出，不推进状态机——Java 的 confirm 靠它取数
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import verify_service_token
from app.skills.interview.graph import fetch_result, interview_step
from app.skills.interview.sample_opening import sample_opening

router = APIRouter(prefix="/ai/interview", tags=["ai"], dependencies=[Depends(verify_service_token)])


# ---- /ai/interview/step ---------------------------------------------------

class InterviewStepRequest(BaseModel):
    user_id: int
    session_id: str
    user_reply: str | None = None
    materials: str | None = None


class InterviewStepResponse(BaseModel):
    stage: str | None = None
    question: str | None = None
    profile_draft: dict[str, Any] | None = None
    done: bool = False
    blocked: bool = False


@router.post("/step", response_model=InterviewStepResponse, response_model_exclude_unset=True)
async def post_interview_step(req: InterviewStepRequest) -> InterviewStepResponse:
    result = await interview_step(
        user_id=req.user_id,
        session_id=req.session_id,
        user_reply=req.user_reply,
        materials=req.materials,
    )
    return InterviewStepResponse(**result)


# ---- /ai/interview/result（只读）-----------------------------------------

class InterviewResultResponse(BaseModel):
    profile: dict[str, Any] | None = None
    a_cards: list[dict[str, Any]] | None = None
    found: bool = True


@router.get("/result", response_model=InterviewResultResponse, response_model_exclude_unset=True)
async def get_interview_result(thread_id: str = Query(...)) -> InterviewResultResponse:
    """只读：取最新 checkpoint 的 summarize 产出，不推进状态机。"""
    data = await fetch_result(thread_id=thread_id)
    if data is None:
        return InterviewResultResponse(found=False)
    return InterviewResultResponse(
        profile=data.get("profile"),
        a_cards=data.get("a_cards"),
        found=True,
    )


# ---- /ai/interview/sample-opening（只读，试试效果）-----------------------

class SampleOpeningRequest(BaseModel):
    user_id: int
    thread_id: str
    topic: str | None = None


class SampleOpeningResponse(BaseModel):
    found: bool = True
    topic: str | None = None
    without: str | None = None
    with_: str | None = Field(default=None, alias="with")

    model_config = {"populate_by_name": True}


@router.post("/sample-opening", response_model=SampleOpeningResponse, response_model_exclude_unset=True)
async def post_sample_opening(req: SampleOpeningRequest) -> SampleOpeningResponse:
    """只读：取 checkpoint profile，产「无档案/有档案」两版开场钩子，不推进状态机。"""
    data = await sample_opening(thread_id=req.thread_id, topic=req.topic)
    if data is None:
        return SampleOpeningResponse(found=False)
    return SampleOpeningResponse(found=True, **data)
