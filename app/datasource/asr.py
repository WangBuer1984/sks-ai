"""阿里云一句话识别（短音频 ≤60s，同步）。

与 P3 拆解转写的「录音文件识别」（长音频异步批量）是不同 API——本模块只做
短音频同步一句话识别，供访谈/补卡的语音回答用（POST /ai/asr 消费）。

SDK 选型：dashscope（阿里云百炼官方 SDK）的 paraformer-realtime-v2 实时识别，
以同步收集模式封装（start → 喂完整段音频 → stop → 取累计文本）。

<b>WebM → PCM 转码</b>：浏览器 MediaRecorder 恒发 audio/webm（Opus 编码 + WebM 容器）。
paraformer-realtime-v2 的 send_audio_frame 吃原始 PCM 帧，不认 WebM/WAV 容器头。
用 pydub（依赖 ffmpeg）把 WebM → raw PCM（16kHz mono 16-bit），再以 format=pcm 送出。

<b>回调契约</b>：dashscope ``RecognitionCallback`` 钩子是 ``on_event``（不是 ``on_result``）；
``get_sentence()`` 返回 ``dict | list[dict]``，文本在 ``sentence["text"]``。
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


def _sentence_text(sentence: Any) -> str:
    """从 dashscope get_sentence() 的返回值抽出文本。

    SDK 契约是 Dict[str, Any] | List[Dict]，不是带 .text 属性的对象。
    """
    if sentence is None:
        return ""
    if isinstance(sentence, dict):
        return str(sentence.get("text") or "")
    if isinstance(sentence, list):
        parts = [
            str(s.get("text") or "")
            for s in sentence
            if isinstance(s, dict) and s.get("text")
        ]
        return "".join(parts)
    return str(getattr(sentence, "text", "") or "")


def _to_pcm(audio_bytes: bytes, source_fmt: str) -> tuple[bytes, str]:
    """把浏览器录的 WebM/Opus 转成 paraformer 认的原始 PCM（16kHz mono 16-bit）。

    paraformer-realtime-v2 的 send_audio_frame 是流式接口——期望**原始 PCM 帧**，
    不认 WAV 文件头（RIFF/WAVE header 会被当音频数据解码 → 报错/空文本）。
    pydub + ffmpeg 把 WebM → raw PCM（16kHz mono 16-bit），以 format=pcm 送 paraformer。
    """
    if source_fmt == "pcm":
        return audio_bytes, "pcm"
    try:
        from pydub import AudioSegment

        container_fmt = "webm" if source_fmt == "opus" else source_fmt
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format=container_fmt)
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        # 导出 raw PCM（无 WAV 头），paraformer 流式接口直接吃
        pcm_buf = io.BytesIO()
        audio.export(pcm_buf, format="raw")
        pcm_bytes = pcm_buf.getvalue()
        log.warning("asr: converted %s(%s)→pcm (16kHz mono 16-bit): %d→%d bytes",
                    source_fmt, container_fmt, len(audio_bytes), len(pcm_bytes))
        return pcm_bytes, "pcm"
    except Exception as e:
        log.warning("asr: pydub convert failed (%s→pcm), falling back to original: %s", source_fmt, e)
        return audio_bytes, source_fmt


def _sync_transcribe(audio_bytes: bytes, fmt: str) -> str:
    """同步阻塞调用 dashscope paraformer-realtime-v2，收集完整文本后返回。

    WebM/Opus 先转 raw PCM 再送（流式接口不认容器头）。
    """
    import dashscope
    from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

    dashscope.api_key = settings.ALIYUN_ASR_KEY

    # WebM → PCM 转码（paraformer 流式接口只认原始 PCM，不认 WAV 头）
    audio_data, actual_fmt = _to_pcm(audio_bytes, fmt)
    sr = 16000 if actual_fmt == "pcm" else 48000

    collected: dict[str, Any] = {"text": "", "partial": "", "error": None}
    done = threading.Event()

    class _Collector(RecognitionCallback):
        def on_open(self) -> None:  # noqa: D401
            log.warning("asr callback: on_open")

        def on_event(self, result) -> None:  # noqa: D401
            # dashscope RecognitionCallback 的钩子是 on_event（不是 on_result）。
            # get_sentence() 返回 Dict 或 List[Dict]，字段是 sentence["text"]——
            # 用 getattr(sent, "text") 对 dict 恒为空，会把真实识别结果吞掉。
            sentence = result.get_sentence() if hasattr(result, "get_sentence") else None
            text = _sentence_text(sentence)
            is_end = False
            if isinstance(sentence, dict):
                is_end = RecognitionResult.is_sentence_end(sentence)
            elif isinstance(sentence, list) and sentence:
                is_end = all(
                    isinstance(s, dict) and RecognitionResult.is_sentence_end(s)
                    for s in sentence
                )
            log.warning(
                "asr callback: on_event text='%s' is_end=%s sentence=%s",
                text[:100] if text else "(empty)",
                is_end,
                str(sentence)[:300],
            )
            if not text:
                return
            # 中间包是同一句的增量草稿；句末才累加，避免 "你好"+"你好呀" 拼成重复。
            if is_end or isinstance(sentence, list):
                collected["text"] += text
                collected["partial"] = ""
            else:
                collected["partial"] = text

        def on_error(self, result) -> None:  # noqa: D401
            collected["error"] = result
            log.warning("asr callback: on_error type=%s", type(result).__name__)
            done.set()

        def on_complete(self) -> None:  # noqa: D401
            log.warning("asr callback: on_complete")
            done.set()

        def on_close(self) -> None:  # noqa: D401
            log.warning("asr callback: on_close")
            done.set()

    callback = _Collector()
    recognition = Recognition(
        model="paraformer-realtime-v2",
        callback=callback,
        format="pcm",
        sample_rate=16000,
    )
    try:
        recognition.start()
        # 分批发送：paraformer 流式接口期望小帧（100ms = 3200 bytes @ 16kHz 16-bit mono）
        chunk_size = 3200
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            recognition.send_audio_frame(chunk)
            import time
            time.sleep(0.01)  # 10ms 间隔，给 paraformer 处理时间
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
        # dump 全部属性看实际结构
        err_dict = {k: v for k, v in vars(err_obj).items() if not k.startswith('_')} if hasattr(err_obj, '__dict__') else {}
        log.warning("asr recognition error: type=%s, attrs=%s (fmt=%s, sr=%d)",
                    type(err_obj).__name__, str(err_dict)[:500], actual_fmt, sr)
        raise ASRRecognitionError(f"asr recognition error: {type(err_obj).__name__} attrs={str(err_dict)[:200]}")
    # 句末未到但已有 partial（超时/提前 close）时，用 partial 兜底，避免白白丢结果。
    result_text = collected["text"] or collected["partial"] or ""
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
