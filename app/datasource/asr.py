"""阿里云一句话识别（短音频 ≤60s，同步）。

与 P3 拆解转写的「录音文件识别」（长音频异步批量）是不同 API——本模块只做
短音频同步一句话识别，供访谈/补卡的语音回答用（POST /ai/asr 消费）。

SDK 选型：dashscope（阿里云百炼官方 SDK）的 paraformer-realtime 实时识别，
以同步收集模式封装（start → 喂完整段音频 → stop → 取累计文本），实现「一次性
同步返回」。一句话识别的纯 REST 入口在阿里云文档间版本变动较大，dashscope 是
当前推荐且长期维护的统一入口。

模块级 seam `transcribe_short` 是测试 monkeypatch 目标
（app.datasource.asr.transcribe_short）——测试 mock 它，不发真实网络请求。
真实 阿里云 调用需联调期用 ALIYUN_ASR_KEY 核对（与 content_safety 签名同口径）。

懒初始化：未配置 ALIYUN_ASR_KEY 时不在 import 期崩溃，per-request 返回
ASRNotConfigured，由端点翻译为 503。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)


class ASRNotConfigured(RuntimeError):
    """ALIYUN_ASR_KEY 未配置——懒初始化失败，per-request 报错。"""


class ASRRecognitionError(RuntimeError):
    """一句话识别失败（网络/鉴权/音频格式等）。"""


def _is_configured() -> bool:
    return bool(getattr(settings, "ALIYUN_ASR_KEY", ""))


def _sync_transcribe(audio_bytes: bytes, fmt: str) -> str:
    """同步阻塞调用 dashscope paraformer-realtime，收集完整文本后返回。

    在 asyncio 端点中经 asyncio.to_thread 调用，避免阻塞事件循环。
    联调注意：paraformer-realtime-v2 走 WebSocket，需用真实 key 核对回调时序
    与音频格式约束（sample_rate / format）。此处实现按 dashscope 文档的
    同步收集模式，结构正确但未用真实 key 验证。
    """
    # 延迟 import：未配置 key 时不在 import 期触发 dashscope 加载
    import dashscope
    from dashscope.audio.asr import Recognition, RecognitionCallback

    dashscope.api_key = settings.ALIYUN_ASR_KEY

    collected: dict[str, Any] = {"text": "", "error": None}
    done = threading.Event()

    class _Collector(RecognitionCallback):
        def on_open(self) -> None:  # noqa: D401
            pass

        def on_result(self, result) -> None:  # noqa: D401
            # 累计增量句子文本
            sent = getattr(result, "get_sentence", lambda: None)()
            if sent is not None:
                txt = getattr(sent, "text", "") or ""
                if txt:
                    collected["text"] += txt

        def on_error(self, result) -> None:  # noqa: D401
            collected["error"] = result
            done.set()

        def on_close(self) -> None:  # noqa: D401
            done.set()

    callback = _Collector()
    # paraformer-realtime-v2 支持常见短音频格式；sample_rate 按音频实际值
    recognition = Recognition(
        model="paraformer-realtime-v2",
        callback=callback,
        format=fmt,
        sample_rate=16000,
    )
    try:
        recognition.start()
        recognition.send_audio_frame(audio_bytes)
        recognition.stop()
        # 等待 on_close/on_error（stop 内部已等回调，但显式 wait 兜底边界时序）
        done.wait(timeout=30.0)
    except Exception as e:  # noqa: BLE001
        raise ASRRecognitionError(f"asr transport failed: {e}") from e

    if collected["error"] is not None:
        raise ASRRecognitionError(f"asr recognition error: {collected['error']}")
    return collected["text"] or ""


async def transcribe_short(audio_bytes: bytes, fmt: str) -> str:
    """一句话识别（短音频 ≤60s，同步返回文本）。

    未配置 ALIYUN_ASR_KEY → ASRNotConfigured；识别失败 → ASRRecognitionError。
    端点（app/api/asr.py）翻译为 503/502。
    """
    if not _is_configured():
        raise ASRNotConfigured("ALIYUN_ASR_KEY not configured")
    return await asyncio.to_thread(_sync_transcribe, audio_bytes, fmt)
