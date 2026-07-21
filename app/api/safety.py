"""POST /ai/safety/check 路由：封装 safety.check，供 Java 对 UGC 过审。

设计文档 §5.1：UGC（KB 卡片内容、选题标题等用户直接编辑文本）与 LLM 输出同样要过
内容安全。Task 1.2 Java KB CRUD / 选题等消费本端点。router 整体 verify_service_token 守卫。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import verify_service_token
from app.safety.content_safety import check as safety_check

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(verify_service_token)])


class SafetyRequest(BaseModel):
    text: str


class SafetyResponse(BaseModel):
    safe: bool


@router.post("/safety/check", response_model=SafetyResponse)
async def post_safety_check(req: SafetyRequest) -> SafetyResponse:
    safe = await safety_check(req.text)
    return SafetyResponse(safe=safe)
