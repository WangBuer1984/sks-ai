"""POST /ai/script_gen + POST /ai/rewrite_sentence 路由。

Java（Task 1.4）通过 AiClient 调本端点，扣额度链路在 Java 侧闭环。
router 整体 verify_service_token 守卫——与 embed/safety 路由同模式。
无流式：同步返回完整 JSON（含 {blocked: true} 情况）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import verify_service_token
from app.skills.script_gen.graph import generate_script
from app.skills.script_gen.rewrite import rewrite_sentence

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(verify_service_token)])


# ---- /ai/script_gen -------------------------------------------------------

class TopicRequest(BaseModel):
    title: str
    rationale: str = ""


class ScriptGenRequest(BaseModel):
    user_id: int
    topic: TopicRequest
    profile: dict[str, Any] = {}
    platform: str = "douyin"


class SectionSentences(BaseModel):
    sentences: list[dict[str, Any]] = []


class ScriptGenResponse(BaseModel):
    hook: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    cta: dict[str, Any] | None = None
    cited_card_ids: list[int] = []
    blocked: bool = False


@router.post("/script_gen", response_model=ScriptGenResponse, response_model_exclude_unset=True)
async def post_script_gen(req: ScriptGenRequest) -> ScriptGenResponse:
    result = await generate_script(
        user_id=req.user_id,
        topic=req.topic.model_dump(),
        profile=req.profile,
        platform=req.platform,
    )
    return ScriptGenResponse(**result)


# ---- /ai/rewrite_sentence -------------------------------------------------

class RewriteSentenceRequest(BaseModel):
    sentence: str
    section: str
    full_script: dict[str, Any] = {}
    profile: dict[str, Any] = {}


class RewriteSentenceResponse(BaseModel):
    text: str | None = None
    blocked: bool = False


@router.post("/rewrite_sentence", response_model=RewriteSentenceResponse, response_model_exclude_unset=True)
async def post_rewrite_sentence(req: RewriteSentenceRequest) -> RewriteSentenceResponse:
    result = await rewrite_sentence(
        sentence=req.sentence,
        section=req.section,
        full_script=req.full_script,
        profile=req.profile,
    )
    return RewriteSentenceResponse(**result)
