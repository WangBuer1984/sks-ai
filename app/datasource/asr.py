"""阿里云一句话识别（短音频 ≤60s，同步）。

与 P3 拆解转写的「录音文件识别」（长音频异步批量）是不同 API——本模块只做
短音频同步一句话识别，供访谈/补卡的语音回答用（POST /ai/asr 消费）。

SDK 选型：dashscope（阿里云百炼官方 SDK）的 paraformer-realtime-v2 实时识别，
以同步收集模式封装（start → 喂完整段音频 → stop → 取累计文本）。

<b>WebM → WAV 转码</b>：浏览器 MediaRecorder 恒发 audio/webm（Opus 编码 + WebM 容器）。
paraformer-realtime-v2 不认 WebM 容器（只认原始 Opus 帧），直接喂 WebM 返空文本。
用 pydub（依赖 ffmpeg）把 WebM → WAV（16kHz mono），再以 format=wav 送 paraformer。
"""

from __future__ import annotations

import asyncio
import io
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


def _to_wav(audio_bytes: bytes, source_fmt: str) -> tuple[bytes, str]:
    """把浏览器录的 WebM/Opus 转成 paraformer 认的 WAV（16kHz mono）。

    paraformer-realtime-v2 不认 WebM 容器——直接喂 WebM 返空文本。
    pydub + ffmpeg 转 WAV 后以 format=wav 送 paraformer，确定能识别。
    若 pydub/ffmpeg 不可用（如容器里没装 ffmpeg），原样返回（降级走原格式）。
    """
    if source_fmt == "wav":
        return audio_bytes, "wav"
    try:
        from pydub import AudioSegment

        # pydub.from_file 按"容器格式"推断——浏览器发的是 WebM 容器，
        # 不是裸 Opus 帧。source_fmt 是 _infer_format 的返回值（"opus"），
        # 但 ffmpeg 需要的是容器格式 "webm"，不是编解码器名 "opus"。
        container_fmt = "webm" if source_fmt == "opus" else source_fmt
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format=container_fmt)
        audio = audio.set_frame_rate(16000).set_channels(1)
        wav_buf = io.BytesIO()
        audio.export(wav_buf, format="wav")
        wav_bytes = wav_buf.getvalue()
        log.warning("asr: converted %s(%s)→wav (16kHz mono): %d→%d bytes", source_fmt, container_fmt, len(audio_bytes), len(wav_bytes))
        return wav_bytes, "wav"
    except Exception as e:
        log.warning("asr: pydub convert failed (%s→wav), falling back to original: %s", source_fmt, e)
        return audio_bytes, source_fmt


def _sync_transcribe(audio_bytes: bytes, fmt: str) -> str:
    """同步阻塞调用 dashscope paraformer-realtime-v2，收集完整文本后返回。

    WebM/Opus 先转 WAV 再送（paraformer 不认 WebM 容器）。
    """
    import dashscope
    from dashscope.audio.asr import Recognition, RecognitionCallback

    dashscope.api_key = settings.ALIYUN_ASR_KEY

    # WebM → WAV 转码
    audio_data, actual_fmt = _to_wav(audio_bytes, fmt)
    sr = 16000 if actual_fmt == "wav" else 48000

    collected: dict[str, Any] = {"text": "", "error": None}
    done = threading.Event()

    class _Collector(RecognitionCallback):
        def on_open(self) -> None:  # noqa: D401
            pass

        def on_result(self, result) -> None:  # noqa: D401
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
    recognition = Recognition(
        model="paraformer-realtime-v2",
        callback=callback,
        format=actual_fmt,
        sample_rate=sr,
    )
    try:
        recognition.start()
        recognition.send_audio_frame(audio_data)
        recognition.stop()
        if not done.wait(timeout=30.0):
            log.warning("asr timed out after 30s (fmt=%s, sr=%d, bytes=%d)", actual_fmt, sr, len(audio_data))
            return collected["text"] or ""
    except Exception as e:  # noqa: BLE001
        log.warning("asr transport failed: %s (fmt=%s, sr=%d, bytes=%d)", e, actual_fmt, sr, len(audio_data))
        raise ASRRecognitionError(f"asr transport failed: {e}") from e

    if collected["error"] is not None:
        # dashscope RecognitionResult 的 __str__ 有 bug（访问 .headers 崩），
        # 不能直接 %s 或 f-string 格式化——安全提取错误信息。
        err_obj = collected["error"]
        err_type = type(err_obj).__name__
        # 尝试安全提取错误码/message（不触发 __str__）
        err_code = getattr(getattr(err_obj, 'output', None), 'code', None)
        err_msg = getattr(getattr(err_obj, 'output', None), 'message', None)
        err_str = f"{err_type}(code={err_code}, msg={err_msg})"
        log.warning("asr recognition error: %s (fmt=%s, sr=%d)", err_str, actual_fmt, sr)
        raise ASRRecognitionError(f"asr recognition error: {err_str}")
    result_text = collected["text"] or ""
    log.warning("asr done: fmt=%s→%s, sr=%d, bytes=%d, text='%s', error=%s",
               fmt, actual_fmt, sr, len(audio_data),
               result_text[:100] if result_text else "(empty)", collected["error"])
    return result_text


async def transcribe_short(audio_bytes: bytes, fmt: str) -> str:
    """一句话识别（短音频 ≤60s，同步返回文本）。

    未配置 ALIYUN_ASR_KEY → ASRNotConfigured；识别失败 → ASRRecognitionError。
    端点（app/api/asr.py）翻译为 503/502。
    """
    if not _is_configured():
        raise ASRNotConfigured("ALIYUN_ASR_KEY not configured")
    return await asyncio.to_thread(_sync_transcribe, audio_bytes, fmt)
