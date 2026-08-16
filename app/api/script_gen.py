"""POST /ai/script_gen + POST /ai/rewrite_sentence 路由。

Java（Task 1.4）通过 AiClient 调本端点，扣额度链路在 Java 侧闭环。
router 整体 verify_service_token 守卫——与 embed/safety 路由同模式。
无流式：同步返回完整 JSON（含 {blocked: true} 情况）。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import verify_service_token
from app.skills.script_gen.graph import generate_script
from app.skills.script_gen.rewrite import rewrite_sentence

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(verify_service_token)])


# ---- /ai/script_gen -------------------------------------------------------

class TopicRequest(BaseModel):
    title: str
    rationale: str = ""


class ScriptGenRequest(BaseModel):
    """一次<b>单平台</b>生成请求。

    双平台一轮生成（spec D21）由 Java 编排：同一 generation_group 各调一次本端点，platform 不同。
    Python 保持无状态——不知道「组」的存在、不做去重也不管计费。

    ``generation_group_id`` 由 Java 传入便于排障，Python 不做组去重或计费。
    ``framework`` 进入写稿 prompt；为 null 时用默认口播结构。
    ``cited_content_ids`` 非空时跳过检索、按 id 加载（懒生成复用首版引用快照）。

    ``platform`` 只接受 douyin / channels（D13 之后全站只剩这两个）——退役值一律 422，不让脏平台走进
    prompt。注意这与 ``app.datasource`` 那套 ``douyin`` / ``wechat_channels`` 是两个命名空间，不要混用。

    ``profile`` 用定位档案的七个规范键（persona/targetAudience/differentiation/conversionPath/
    tone/redlines/contentPillars）。
    """

    user_id: int
    topic: TopicRequest
    profile: dict[str, Any] = Field(default_factory=dict)
    platform: Literal["douyin", "channels"] = "douyin"
    duration: str = "45"  # '45'|'90'|'180'（秒）；45=45秒口播 90=90秒 180=3分钟深度
    generation_group_id: int | None = None
    framework: str | None = None
    cited_content_ids: list[int] | None = None


class ScriptGenResponse(BaseModel):
    hook: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    cta: dict[str, Any] | None = None
    # 整篇内容参考（内容底仓，D2/D18）：Java 据此写 content_reference，右栏展示「本稿参考了你的这些内容」
    cited_content_ids: list[int] = Field(default_factory=list)
    cited_card_ids: list[int] = Field(default_factory=list)  # 旧 B 卡引用，保留一个兼容周期
    blocked: bool = False


@router.post("/script_gen", response_model=ScriptGenResponse, response_model_exclude_unset=True)
async def post_script_gen(req: ScriptGenRequest) -> ScriptGenResponse:
    result = await generate_script(
        user_id=req.user_id,
        topic=req.topic.model_dump(),
        profile=req.profile,
        platform=req.platform,
        duration=req.duration,
        framework=req.framework,
        generation_group_id=req.generation_group_id,
        cited_content_ids=req.cited_content_ids,
    )
    return ScriptGenResponse(**result)


# ---- /ai/rewrite_sentence -------------------------------------------------

class RewriteSentenceRequest(BaseModel):
    sentence: str
    section: str
    full_script: dict[str, Any] = Field(default_factory=dict)
    profile: dict[str, Any] = Field(default_factory=dict)


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
