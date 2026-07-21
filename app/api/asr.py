"""POST /ai/asr 路由：语音回答转文字（阿里云一句话识别，≤60s 短音频同步）。

Java（Task 2.2）调本端点把访谈/补卡的语音回答转文字。识别失败返回 502
（Java 提示用户改用文字输入）；ALIYUN_ASR_KEY 未配置返回 503（懒初始化失败，
per-request，不在 import 期崩溃）。

router 整体 verify_service_token 守卫——与其他 /ai/* 路由同模式（内网端点）。
无流式：同步返回 {text}。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.api.deps import verify_service_token
from app.datasource.asr import ASRNotConfigured, ASRRecognitionError, transcribe_short

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(verify_service_token)])

# 一句话识别上限（PRD/设计文档：短音频 ≤60s）
_MAX_AUDIO_BYTES = 60 * 1024 * 1024  # 60s 上限的宽松字节兜底（实际按时长由阿里云校验）


class ASRResponse(BaseModel):
    text: str


@router.post("/asr", response_model=ASRResponse)
async def post_asr(audio: UploadFile = File(...)) -> ASRResponse:
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "EMPTY_AUDIO"},
        )
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "AUDIO_TOO_LARGE"},
        )
    # 从文件名/content-type 推断格式（联调期按阿里云支持的 format 字符串核对）
    fmt = _infer_format(audio)
    try:
        text = await transcribe_short(audio_bytes, fmt)
    except ASRNotConfigured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "ASR_NOT_CONFIGURED"},
        )
    except ASRRecognitionError as e:
        log.warning("asr recognition failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "ASR_FAILED"},
        )
    return ASRResponse(text=text)


def _infer_format(audio: UploadFile) -> str:
    """从 content_type 推断阿里云一句话识别的 format 字符串。

    联调注意：阿里云 paraformer-realtime-v2 的 format 取值（如 wav/mp3/opus）
    需用真实样本核对。此处按常见 MIME 做宽松映射，未命中默认 wav。
    """
    ct = (audio.content_type or "").lower()
    name = (audio.filename or "").lower()
    if "wav" in ct or name.endswith(".wav"):
        return "wav"
    if "mp3" in ct or name.endswith(".mp3"):
        return "mp3"
    if "opus" in ct or name.endswith(".opus"):
        return "opus"
    if "m4a" in ct or name.endswith(".m4a"):
        return "m4a"
    return "wav"
