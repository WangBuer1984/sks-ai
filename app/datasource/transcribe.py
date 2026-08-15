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

P0 不变量（5min 超时 → DataSourceError）：
  ``transcribe`` 外层包 ``asyncio.wait_for(_transcribe_inner(media), timeout=300)``。
  ``wait_for`` 超时抛裸 ``asyncio.TimeoutError``，skill 层只 catch ``DataSourceError`` +
  宽 ``except Exception``，故此处必须捕获并翻译为 ``DataSourceError("transcribe timed out …")``。
  超时时内层 task 被 cancel，CPython 仍执行内层 ``finally`` → temp 文件被 unlink。

配置校验在 ``transcribe()`` 中、``wait_for`` **之前**执行，缺 key / 缺 ffmpeg 立即 fail-fast
（不在 20min 超时后才报错）。

seam 全部模块级绑定（与 ``tikhub.py`` 同模式），测试 monkeypatch 目标：
``app.datasource.transcribe.download_url`` / ``.convert_to_wav`` /
``.get_audio_duration`` / ``.slice_audio`` / ``.recognize_wav`` /
``.merge_transcript_parts`` / ``.gc_stale_tmp`` / ``._ffmpeg_available``。
转写墙钟超时从 ``settings.TRANSCRIBE_TIMEOUT`` 读取（默认 300s，fail-fast）。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.config import settings
from app.datasource import DataSourceError
from app.datasource.media import MediaRef
from app.datasource.media.audio import convert_to_wav, get_audio_duration, slice_audio
from app.datasource.media.channels_decode import decode_channels_media
from app.datasource.media.constants import WAV_SIZE_LIMIT
from app.datasource.media.download import download_url, gc_stale_tmp
from app.datasource.media.merge import merge_transcript_parts
from app.datasource.media.qwen_asr import recognize_wav
from app.datasource.media.semaphores import (
    get_asr_semaphore,
    get_convert_semaphore,
    get_decode_semaphore,
    get_download_semaphore,
)

log = logging.getLogger(__name__)

_SHORT_DURATION_LIMIT = 300.0

# 0.0–1.0：转写管线内相对进度（供 video/link 映射到任务 progress，账号拆解可不传）。
ProgressCallback = Callable[[float], Awaitable[None]]

# channels decode 可插拔 seam：Task 8a 注入 ``decode_channels_media``；
# 测试可 monkeypatch 为 None / fake。缺注入且 ``decode_key`` 非空 → 守卫报错。
decode_media: Callable[[Path, str], Path] | None = decode_channels_media


def _ffmpeg_available() -> bool:
    """``ffmpeg`` 与 ``ffprobe`` 均在 PATH 才视为可用。"""
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _is_configured() -> bool:
    """转写可用：DashScope key 配置 且 ffmpeg/ffprobe 在 PATH。"""
    return bool(settings.ALIYUN_ASR_KEY) and _ffmpeg_available()


async def _emit_progress(cb: ProgressCallback | None, frac: float) -> None:
    """best-effort 进度回调；失败不拖垮管线。"""
    if cb is None:
        return
    try:
        await cb(max(0.0, min(1.0, frac)))
    except Exception:  # noqa: BLE001
        log.warning("transcribe on_progress failed", exc_info=True)


async def transcribe(
    media: MediaRef | str,
    *,
    on_progress: ProgressCallback | None = None,
) -> str:
    """下载 → 转码 → 时长分支 → 识别 → 拼接，返回完整文案。

    入参 ``media``：``MediaRef``（携带下载直链/请求头/解码键/标题/作者）或裸 str
    URL（裸 str 时构造 ``MediaRef(platform="unknown", download_url=media, headers={})``，
    **绝不猜测 douyin Referer/UA**——裸 str 视为无特殊请求头）。

    ``on_progress``：可选，接收 ``0.0–1.0`` 管线相对进度（download→asr），供前端进度条。

    P0：外层 ``wait_for(timeout=settings.TRANSCRIBE_TIMEOUT)``，超时翻译为
    ``DataSourceError``（非裸 TimeoutError）。配置校验（key + ffmpeg）在 ``wait_for``
    之前，缺一即 fail-fast。
    """
    # 配置校验优先——缺 key / 缺 ffmpeg 立即失败，不进入 20min 超时后才报错。
    if not settings.ALIYUN_ASR_KEY:
        raise DataSourceError("ALIYUN_ASR_KEY not configured")
    if not _ffmpeg_available():
        raise DataSourceError("ffmpeg/ffprobe not found on PATH")
    timeout = settings.TRANSCRIBE_TIMEOUT
    try:
        return await asyncio.wait_for(
            _transcribe_inner(media, on_progress=on_progress), timeout=timeout
        )
    except asyncio.TimeoutError as e:
        # P0：裸 TimeoutError 不可泄漏到 skill 层（只 catch DataSourceError）。
        raise DataSourceError(
            f"transcribe timed out after {timeout}s"
        ) from e


_TEMP_DIR_PREFIXES = ("sks_asr_wav_", "sks_asr_slice_")


async def _transcribe_inner(
    media: MediaRef | str,
    *,
    on_progress: ProgressCallback | None = None,
) -> str:
    """管线编排本体：own temps 列表 + try/finally 清理。

    ``transcribe`` 的 ``wait_for`` 超时会 cancel 本协程，CPython 仍运行本 ``finally``
    → ``temps`` 中的下载/转码/切片产物均被 unlink（``missing_ok=True``）；
    ``sks_asr_wav_*`` / ``sks_asr_slice_*`` 父目录一并 rmtree，避免空目录堆 inode。
    """
    await asyncio.to_thread(gc_stale_tmp)

    # str → MediaRef（裸 str：unknown 平台、空 headers，绝不猜 douyin 头）。
    ref = media if isinstance(media, MediaRef) else MediaRef(
        platform="unknown", download_url=media, headers={}
    )

    log.info(
        "transcribe start: url=%s platform=%s decode_key=%s",
        ref.download_url[:80], ref.platform, bool(ref.decode_key),
    )
    t_total = time.monotonic()
    temps: list[Path] = []
    try:
        await _emit_progress(on_progress, 0.05)
        # 下载：裸 str 时 headers 为 {} → ``ref.headers or None`` 传 None 给 download_url。
        t0 = time.monotonic()
        async with get_download_semaphore():
            src = await download_url(ref.download_url, headers=ref.headers or None)
        temps.append(src)
        log.info("transcribe step download done: elapsed=%.2fs", time.monotonic() - t0)
        await _emit_progress(on_progress, 0.30)

        # channels decode：有 decode_key 才解密（to_thread + decode_sem）；缺 key 直进 ffmpeg。
        if ref.decode_key:
            if decode_media is None:
                raise DataSourceError(
                    f"channels decode not enabled: decode_key={ref.decode_key[:8]}…"
                )
            t0 = time.monotonic()
            async with get_decode_semaphore():
                src = await asyncio.to_thread(decode_media, src, ref.decode_key)
            temps.append(src)
            log.info("transcribe step decode done: elapsed=%.2fs", time.monotonic() - t0)
            await _emit_progress(on_progress, 0.42)

        # 转码到 WAV 16k mono。
        t0 = time.monotonic()
        async with get_convert_semaphore():
            wav = await convert_to_wav(src)
        temps.append(wav)
        log.info("transcribe step convert_wav done: elapsed=%.2fs", time.monotonic() - t0)
        await _emit_progress(on_progress, 0.55)

        # WAV 就绪后尽早丢掉下载/decode 中间文件（峰值磁盘，尤其视频号百 MB 级）。
        for early in list(temps[:-1]):
            try:
                early.unlink(missing_ok=True)
            except OSError:
                pass
        temps[:] = [wav]

        # ffprobe 同步 subprocess：必须 to_thread，避免堵事件循环/心跳。
        t0 = time.monotonic()
        duration = await asyncio.to_thread(get_audio_duration, wav)
        size = wav.stat().st_size
        log.info(
            "transcribe step probe done: duration=%.1fs size=%d elapsed=%.2fs",
            duration, size, time.monotonic() - t0,
        )
        await _emit_progress(on_progress, 0.60)

        # 时长分支（contract 表）：
        #   0 < d <= 300 → 单段 recognize；若 wav>10MB（unexpected）→ DataSourceError
        #   d > 300 或 d == 0.0（ffprobe 失败/未知）→ 切片路径（slice_audio 估时长）
        if 0 < duration <= _SHORT_DURATION_LIMIT:
            if size > WAV_SIZE_LIMIT:
                raise DataSourceError(
                    f"wav exceeds 10MB within 300s — unexpected: {wav.name} ({size} bytes)"
                )
            t0 = time.monotonic()
            await _emit_progress(on_progress, 0.70)
            async with get_asr_semaphore():
                text = await recognize_wav(
                    wav, title=ref.title, author=ref.author
                )
            log.info("transcribe step asr(single) done: elapsed=%.2fs", time.monotonic() - t0)
            await _emit_progress(on_progress, 0.95)
        else:
            # 切片单段 270s≈8.24MB 天然 <10MB，无需逐段再检。
            # 传入已测 duration，避免 slice_audio 再跑一次 ffprobe。
            t0 = time.monotonic()
            async with get_convert_semaphore():
                segs = await slice_audio(wav, duration=duration)
            log.info("slice done: %d segs elapsed=%.2fs",
                     len(segs), time.monotonic() - t0)
            await _emit_progress(on_progress, 0.65)
            # 真实切片（非原 wav）纳入 temps；识别后尽早删以压峰值磁盘。
            real_slices = [s for s in segs if s.resolve() != wav.resolve()]
            temps.extend(real_slices)

            done_n = 0
            seg_lock = asyncio.Lock()

            async def _recognize_seg(i: int, seg: Path) -> str:
                nonlocal done_n
                tseg = time.monotonic()
                async with get_asr_semaphore():
                    part = await recognize_wav(
                        seg, title=ref.title, author=ref.author
                    )
                log.info(
                    "transcribe step asr(seg %d/%d) done: elapsed=%.2fs",
                    i + 1, len(segs), time.monotonic() - tseg,
                )
                async with seg_lock:
                    done_n += 1
                    # 0.65 → 0.92 按已完成段数推进
                    await _emit_progress(
                        on_progress, 0.65 + 0.27 * done_n / max(len(segs), 1)
                    )
                if seg.resolve() != wav.resolve():
                    try:
                        seg.unlink(missing_ok=True)
                    except OSError:
                        pass
                    try:
                        temps.remove(seg)
                    except ValueError:
                        pass
                return part

            # 有界并发：吃满 asr_sem（默认 3）。return_exceptions 隔离单段失败——
            # 失败段跳过不参与 merge；全部失败才 raise。gather 返回序 = 输入序 → 保序。
            t0 = time.monotonic()
            raw = await asyncio.gather(
                *[_recognize_seg(i, seg) for i, seg in enumerate(segs)],
                return_exceptions=True,
            )
            parts = [r for r in raw if isinstance(r, str) and r.strip()]
            if not parts:
                raise DataSourceError(
                    f"all ASR segments failed for {ref.download_url[:50]}"
                )
            log.info(
                "transcribe step asr(all segs) done: n_ok=%d/%d elapsed=%.2fs",
                len(parts), len(segs), time.monotonic() - t0,
            )
            t0 = time.monotonic()
            text = merge_transcript_parts(parts)
            log.info("transcribe step merge done: elapsed=%.2fs", time.monotonic() - t0)
            await _emit_progress(on_progress, 0.95)

        if not text.strip():
            raise DataSourceError(
                f"asr produced empty transcript for {ref.download_url[:50]}"
            )
        log.info(
            "transcribe done: url=%s text_len=%d elapsed=%.2fs",
            ref.download_url[:80], len(text), time.monotonic() - t_total,
        )
        await _emit_progress(on_progress, 1.0)
        return text
    finally:
        dirs: set[Path] = set()
        for p in temps:
            try:
                if p.parent.name.startswith(_TEMP_DIR_PREFIXES):
                    dirs.add(p.parent)
                p.unlink(missing_ok=True)
            except OSError:
                pass
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
