"""转写门面：编排 Qwen ASR 媒体管线（download → convert → duration 分支 → recognize）。

**与旧阿里云长音频异步 POP 路径彻底切割**：本模块不再走旧版录音文件识别异步
submit→poll→fetch POP API，不再持有域名/版本/区域/轮询常量，也不再暴露提交/取结果
模块级 seam。编排逻辑统一交给 Task 2–4 的媒体子模块：
``download`` / ``audio`` / ``qwen_asr`` / ``merge`` / ``semaphores``。

接口（对齐 brief / contract）：
  - ``async def transcribe(media: MediaRef | str) -> str``
  - ``def _is_configured() -> bool`` —— ``ALIYUN_ASR_KEY`` 且 ffmpeg/ffprobe 在 PATH
  - 模块级 ``decode_media: Callable[[Path, str], Path] | None = None`` —— 可插拔
    channels decode seam；Task 8a 注入，默认 None + 守卫（绝不引用未定义符号）。

P0 不变量（20min 超时 → DataSourceError）：
  ``transcribe`` 外层包 ``asyncio.wait_for(_transcribe_inner(media), timeout=1200)``。
  ``wait_for`` 超时抛裸 ``asyncio.TimeoutError``，skill 层只 catch ``DataSourceError`` +
  宽 ``except Exception``，故此处必须捕获并翻译为 ``DataSourceError("transcribe timed out …")``。
  超时时内层 task 被 cancel，CPython 仍执行内层 ``finally`` → temp 文件被 unlink。

配置校验在 ``transcribe()`` 中、``wait_for`` **之前**执行，缺 key / 缺 ffmpeg 立即 fail-fast
（不在 20min 超时后才报错）。

seam 全部模块级绑定（与 ``tikhub.py`` 同模式），测试 monkeypatch 目标：
``app.datasource.transcribe.download_url`` / ``.convert_to_wav`` /
``.get_audio_duration`` / ``.slice_audio`` / ``.recognize_wav`` /
``.merge_transcript_parts`` / ``.gc_stale_tmp`` / ``._ffmpeg_available`` /
``._TRANSCRIBE_TIMEOUT``。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Callable, Optional, Union

from app.config import settings
from app.datasource import DataSourceError
from app.datasource.media import MediaRef
from app.datasource.media.audio import convert_to_wav, get_audio_duration, slice_audio
from app.datasource.media.download import download_url, gc_stale_tmp
from app.datasource.media.merge import merge_transcript_parts
from app.datasource.media.qwen_asr import recognize_wav
from app.datasource.media.semaphores import (
    get_asr_semaphore,
    get_convert_semaphore,
    get_download_semaphore,
)

log = logging.getLogger(__name__)

# 20min 硬上限（单条转写墙钟）。module-level 以便测试 monkeypatch 缩短。
_TRANSCRIBE_TIMEOUT = 1200

# 10MB（qwen3-asr-flash 单次 wav 体积上限）——整段守卫；切片单段 270s≈8.24MB 天然满足。
_WAV_SIZE_LIMIT = 10 * 1024 * 1024
_SHORT_DURATION_LIMIT = 300.0

# channels decode 可插拔 seam：Task 8a 注入 channels 解码函数；默认 None + 守卫。
# 绝不引用未定义的 ``decode_channels_media`` 符号——本 seam 即是接入点。
decode_media: Optional[Callable[[Path, str], Path]] = None


def _ffmpeg_available() -> bool:
    """``ffmpeg`` 与 ``ffprobe`` 均在 PATH 才视为可用。"""
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _is_configured() -> bool:
    """转写可用：DashScope key 配置 且 ffmpeg/ffprobe 在 PATH。"""
    return bool(settings.ALIYUN_ASR_KEY) and _ffmpeg_available()


async def transcribe(media: Union[MediaRef, str]) -> str:
    """下载 → 转码 → 时长分支 → 识别 → 拼接，返回完整文案。

    入参 ``media``：``MediaRef``（携带下载直链/请求头/解码键/标题/作者）或裸 str
    URL（裸 str 时构造 ``MediaRef(platform="unknown", download_url=media, headers={})``，
    **绝不猜测 douyin Referer/UA**——裸 str 视为无特殊请求头）。

    P0：外层 ``wait_for(timeout=1200)``，超时翻译为 ``DataSourceError``（非裸 TimeoutError）。
    配置校验（key + ffmpeg）在 ``wait_for`` 之前，缺一即 fail-fast。
    """
    # 配置校验优先——缺 key / 缺 ffmpeg 立即失败，不进入 20min 超时后才报错。
    if not settings.ALIYUN_ASR_KEY:
        raise DataSourceError("ALIYUN_ASR_KEY not configured")
    if not _ffmpeg_available():
        raise DataSourceError("ffmpeg/ffprobe not found on PATH")
    try:
        return await asyncio.wait_for(
            _transcribe_inner(media), timeout=_TRANSCRIBE_TIMEOUT
        )
    except asyncio.TimeoutError as e:
        # P0：裸 TimeoutError 不可泄漏到 skill 层（只 catch DataSourceError）。
        raise DataSourceError(
            f"transcribe timed out after {_TRANSCRIBE_TIMEOUT}s"
        ) from e


async def _transcribe_inner(media: Union[MediaRef, str]) -> str:
    """管线编排本体：own temps 列表 + try/finally 清理。

    ``transcribe`` 的 ``wait_for`` 超时会 cancel 本协程，CPython 仍运行本 ``finally``
    → ``temps`` 中的下载/转码/切片产物均被 unlink（``missing_ok=True``）。
    """
    gc_stale_tmp()

    # str → MediaRef（裸 str：unknown 平台、空 headers，绝不猜 douyin 头）。
    ref = media if isinstance(media, MediaRef) else MediaRef(
        platform="unknown", download_url=media, headers={}
    )

    temps: list[Path] = []
    try:
        # 下载：裸 str 时 headers 为 {} → ``ref.headers or None`` 传 None 给 download_url。
        async with get_download_semaphore():
            src = await download_url(ref.download_url, headers=ref.headers or None)
        temps.append(src)

        # channels decode 可插拔 seam：decode_key 非空但 decode_media 未注入 → 故障。
        if ref.decode_key:
            if decode_media is None:
                raise DataSourceError("channels decode not enabled")
            src = decode_media(src, ref.decode_key)
            temps.append(src)

        # 转码到 WAV 16k mono。
        async with get_convert_semaphore():
            wav = await convert_to_wav(src)
        temps.append(wav)

        duration = get_audio_duration(wav)
        size = wav.stat().st_size

        # 时长分支（contract 表）：
        #   0 < d <= 300 → 单段 recognize；若 wav>10MB（unexpected）→ DataSourceError
        #   d > 300 或 d == 0.0（ffprobe 失败/未知）→ 切片路径
        if 0 < duration <= _SHORT_DURATION_LIMIT:
            if size > _WAV_SIZE_LIMIT:
                raise DataSourceError(
                    "wav exceeds 10MB within 300s — unexpected"
                )
            async with get_asr_semaphore():
                text = await recognize_wav(
                    wav, title=ref.title, author=ref.author
                )
        else:
            # 切片单段 270s≈8.24MB 天然 <10MB，无需逐段再检。
            async with get_convert_semaphore():
                segs = await slice_audio(wav)
            temps.extend(segs)
            parts: list[str] = []
            for seg in segs:
                async with get_asr_semaphore():
                    parts.append(
                        await recognize_wav(seg, title=ref.title, author=ref.author)
                    )
            text = merge_transcript_parts(parts)

        if not text.strip():
            raise DataSourceError("asr produced empty transcript")
        return text
    finally:
        # 任何路径（成功/异常/cancel）均 unlink temps；missing_ok 容忍已被删的。
        for p in temps:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
