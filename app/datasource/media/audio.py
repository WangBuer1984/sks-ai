"""FFmpeg 转码 WAV 16kHz 单声道 + 长音频切片。

ffmpeg 命令形态参考 clever-hans ``backend/app/core/media/audio.py``，但：
- 不定义 ``AudioConvertError`` —— 所有 ffmpeg/subprocess 失败一律抛
  ``DataSourceError``（``app.datasource`` 全局约束，便于上游按数据源故障
  统一翻译为退款/重试策略，PRD §11.3）。
- 不读 ``settings.audio_sample_rate`` / ``audio_channels`` —— 16k mono 是
  qwen ASR 规格的固定目标（非配置），硬编码 ``16000`` / ``1``。
- 临时目录用 ``settings.ASR_TMP_DIR``（空 → 系统 tempfile 目录）。

``convert_to_wav`` / ``slice_audio`` 通过 ``asyncio.to_thread`` 调度同步
subprocess，避免阻塞事件循环。``get_audio_duration`` 是同步纯函数，
失败返回 ``0.0``（不抛）；facade 须 ``asyncio.to_thread`` 调用。``0.0`` 表示
「时长未知」——``slice_audio`` 用 16k mono PCM 体积估时长，禁止把未知当「够短」。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from app.config import settings
from app.datasource import DataSourceError

log = logging.getLogger(__name__)

# qwen ASR 规格固定目标：16kHz 单声道。非配置项，硬编码。
_SAMPLE_RATE = 16000
_CHANNELS = 1
_BYTES_PER_SAMPLE = 2  # s16le
# FFmpeg 转码超时（秒）：完整转码不截断时长，5min 覆盖长音频；切片 2min。
_CONVERT_TIMEOUT = 300
_SLICE_TIMEOUT = 120
_FFPROBE_TIMEOUT = 30
# 与 facade / qwen 单次上限对齐（16k mono PCM 300s≈9.2MB）。
_WAV_SIZE_LIMIT = 10 * 1024 * 1024
# 切片临时目录前缀，便于排查；须纳入 ``gc_stale_tmp``。
_CONVERT_DIR_PREFIX = "sks_asr_wav_"
_SLICE_DIR_PREFIX = "sks_asr_slice_"


def _get_output_dir(prefix: str) -> str:
    """获取临时输出目录。优先 ``settings.ASR_TMP_DIR``，否则系统 tempfile。"""
    base = settings.ASR_TMP_DIR
    if base:
        os.makedirs(base, exist_ok=True)
        return tempfile.mkdtemp(prefix=prefix, dir=base)
    return tempfile.mkdtemp(prefix=prefix)


def _require_ffmpeg() -> None:
    """调用 subprocess 前确保 ``ffmpeg`` 在 PATH，否则抛 ``DataSourceError``。

    ``shutil.which`` 返回 None → ffmpeg 未安装，翻译为数据源故障而非
    裸 ``FileNotFoundError`` 冒泡（测试通过 monkeypatch ``shutil.which`` 触发）。
    """
    if shutil.which("ffmpeg") is None:
        raise DataSourceError("ffmpeg not found in PATH")


def _convert_sync(src_path: Path, output_dir: Optional[str] = None) -> Path:
    """同步 FFmpeg 转码（由 ``convert_to_wav`` 经 ``asyncio.to_thread`` 调用）。"""
    _require_ffmpeg()
    if output_dir is None:
        output_dir = _get_output_dir(_CONVERT_DIR_PREFIX)

    wav_path = Path(output_dir) / "audio_16k_mono.wav"
    cmd = [
        "ffmpeg", "-y", "-i", str(src_path),
        "-ar", str(_SAMPLE_RATE),
        "-ac", str(_CHANNELS),
        "-vn",
        str(wav_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_CONVERT_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise DataSourceError(f"ffmpeg convert timed out ({_CONVERT_TIMEOUT}s)") from exc
    except Exception as exc:  # FileNotFoundError 等 —— 理论上 _require_ffmpeg 已挡，兜底
        raise DataSourceError(f"ffmpeg convert error: {exc}") from exc

    if result.returncode != 0:
        raise DataSourceError(f"ffmpeg convert failed: {result.stderr[-400:]}")
    if not wav_path.exists():
        raise DataSourceError("ffmpeg convert produced no output file")
    return wav_path


async def convert_to_wav(src: Path) -> Path:
    """将任意音频/视频格式转码为 WAV 16kHz 单声道。完整转码，不截断时长。

    失败语义（一律 ``DataSourceError``，绝不冒泡裸 subprocess 异常）：
    - 无 ffmpeg（``shutil.which`` → None）→ ``DataSourceError("ffmpeg not found …")``
    - ``subprocess.TimeoutExpired`` → ``DataSourceError("ffmpeg convert timed out …")``
    - 非零退出 / 无输出文件 / 其他 subprocess 错误 → ``DataSourceError("ffmpeg …")``
    """
    return await asyncio.to_thread(_convert_sync, src)


def _parse_duration_from_ffprobe(output: str) -> Optional[float]:
    """解析 ffprobe 输出中的 ``duration=xxx`` 字段。"""
    match = re.search(r"duration=(\d+\.?\d*)", output)
    if match:
        return float(match.group(1))
    return None


def get_audio_duration(wav: Path | str) -> float:
    """用 ffprobe 获取音频时长（秒）。任何失败返回 ``0.0``，不抛异常。

    facade 以 ``0.0`` 作为「时长未知 → 走切片路径」的信号；本函数含同步
    ``subprocess.run``，协程内必须经 ``asyncio.to_thread`` 调用。
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1", str(wav)],
            capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT,
        )
        duration = _parse_duration_from_ffprobe(result.stdout)
        return duration if duration is not None else 0.0
    except Exception:
        return 0.0


def _estimate_pcm_wav_duration(wav: Path | str) -> float:
    """按 16k mono s16le PCM 体积估时长（跳过约 44 字节头）。失败 → 0.0。"""
    try:
        size = Path(wav).stat().st_size
    except OSError:
        return 0.0
    payload = max(0, size - 44)
    rate = _SAMPLE_RATE * _CHANNELS * _BYTES_PER_SAMPLE
    if payload <= 0 or rate <= 0:
        return 0.0
    return payload / float(rate)


def _slice_segment_sync(
    src_path: Path | str, start: float, duration: float, output_dir: str, index: int
) -> Path:
    """同步切出一段音频（由 ``slice_audio`` 经 ``asyncio.to_thread`` 调用）。"""
    _require_ffmpeg()
    seg_path = Path(output_dir) / f"seg_{index}.wav"
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-t", str(duration),
        "-i", str(src_path),
        "-ar", str(_SAMPLE_RATE),
        "-ac", str(_CHANNELS),
        "-vn",
        str(seg_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_SLICE_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise DataSourceError(
            f"ffmpeg slice {index} timed out ({_SLICE_TIMEOUT}s)"
        ) from exc
    except Exception as exc:
        raise DataSourceError(f"ffmpeg slice {index} error: {exc}") from exc

    if result.returncode != 0:
        raise DataSourceError(f"ffmpeg slice {index} failed: {result.stderr[-200:]}")
    return seg_path


async def slice_audio(
    wav: Path, segment_duration: int = 270, overlap: int = 3
) -> list[Path]:
    """长音频切片。每段约 ``segment_duration`` 秒，段间 ``overlap`` 秒重叠。

    返回切片文件路径列表（按时间顺序）。若音频时长
    ``<= segment_duration + overlap``，直接返回 ``[wav]``（不切片）。

    ``duration == 0.0``（ffprobe 失败）**不得**当成「够短」：先按 PCM 体积估
    时长再切片；仍无法估计且 ``wav > 10MB`` → ``DataSourceError``（避免整文件
    直送 recognize 撞 DashScope 上限）。

    切片体积不变量：270s × 16k mono PCM ≈ 8.24MB < 10MB，
    单段天然满足 qwen ≤10MB 体积上限，无需再加每段 10MB 守卫。
    """
    duration = await asyncio.to_thread(get_audio_duration, str(wav))
    if duration <= 0:
        duration = _estimate_pcm_wav_duration(wav)
        if duration > 0:
            log.warning(
                "ffprobe duration unknown; estimated %.1fs from wav size (%s)",
                duration, Path(wav).name,
            )
        else:
            try:
                size = Path(wav).stat().st_size
            except OSError:
                size = 0
            if size > _WAV_SIZE_LIMIT:
                raise DataSourceError(
                    "audio duration unknown and wav exceeds 10MB"
                )
            return [wav]

    if duration <= segment_duration + overlap:
        # 真短：仍拦 >10MB（异常码率/坏文件），禁止整文件直送 recognize。
        try:
            size = Path(wav).stat().st_size
        except OSError:
            size = 0
        if size > _WAV_SIZE_LIMIT:
            raise DataSourceError(
                "wav exceeds 10MB within short-duration slice skip — unexpected"
            )
        return [wav]

    output_dir = _get_output_dir(_SLICE_DIR_PREFIX)
    segments: list[Path] = []
    start = 0.0
    index = 0

    while start < duration:
        seg_dur = min(segment_duration, duration - start)
        seg_path = await asyncio.to_thread(
            _slice_segment_sync, str(wav), start, seg_dur, output_dir, index
        )
        segments.append(seg_path)
        # 下一段起点：当前段末尾减 overlap，实现重叠区。
        start += segment_duration - overlap
        index += 1

    return segments
