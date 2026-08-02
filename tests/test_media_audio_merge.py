"""audio.py + merge.py 测试。

merge 测试：纯文本，无 ffmpeg，对齐 clever-hans `_merge_transcript_parts`。
audio 测试：本地有 ffmpeg 时跑真实集成（``@pytest.mark.skipif`` 守卫）；
失败路径用 monkeypatch ``shutil.which("ffmpeg")`` → ``None`` 触发 ``DataSourceError``。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.datasource import DataSourceError
from app.datasource.media.merge import find_overlap_text, merge_transcript_parts

# 延迟 import audio 模块，让 merge 测试在该模块尚未实现时也能给出清晰的 ImportError。
audio = pytest.importorskip("app.datasource.media.audio")
convert_to_wav = audio.convert_to_wav
get_audio_duration = audio.get_audio_duration
slice_audio = audio.slice_audio

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_skip_no_ffmpeg = pytest.mark.skipif(not _HAVE_FFMPEG, reason="no ffmpeg/ffprobe installed")


# ---------------------------------------------------------------------------
# Step 1: merge 纯文本测试（对齐 clever-hans）
# ---------------------------------------------------------------------------


def test_find_overlap_text():
    assert find_overlap_text("大家来到我的频道", "来到我的频道今天") == "来到我的频道"
    assert find_overlap_text("大家好", "欢迎来到") == ""


def test_merge_transcript_parts_overlap():
    parts = ["前面文字来到我的频道", "来到我的频道后面继续"]
    assert "来到我的频道" in merge_transcript_parts(parts, overlap=3)
    # 不应重复整段 overlap


def test_merge_transcript_parts_empty():
    assert merge_transcript_parts([]) == ""


def test_merge_transcript_parts_single():
    assert merge_transcript_parts(["only one part"]) == "only one part"


def test_find_overlap_text_max_len_cap():
    # overlap 上限 50：超过 50 的公共前后缀不应被识别
    tail = "x" * 60
    head = "x" * 60
    # max_len = min(60, 60, 50) = 50 → 返回 50 个 x
    assert find_overlap_text(tail, head) == "x" * 50


# ---------------------------------------------------------------------------
# Step 2: audio 集成测试（ffmpeg/ffprobe 守卫）
# ---------------------------------------------------------------------------


def _gen_wav(path: Path, *, seconds: float, rate: int = 16000, channels: int = 1) -> None:
    """用 ffmpeg lavfi 生成静音 wav（用于集成测试 fixture）。"""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=r={rate}:cl={'mono' if channels == 1 else 'stereo'}",
            "-t", str(seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


def _gen_mp4(path: Path, *, seconds: float = 1.0) -> None:
    """用 ffmpeg 生成带音轨的极小 mp4（用于 convert_to_wav 跨格式 round-trip）。"""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", f"color=c=red:s=16x16:d={seconds}",
            "-shortest", "-c:v", "libx264", "-c:a", "aac",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


@_skip_no_ffmpeg
async def test_convert_to_wav_roundtrip_wav(tmp_path, monkeypatch):
    """44.1k 立体声 wav → 16k mono wav。"""
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    src = tmp_path / "src_44k_stereo.wav"
    _gen_wav(src, seconds=1.0, rate=44100, channels=2)

    out = await convert_to_wav(src)
    assert out.exists()
    # 验证输出确实是 16k mono
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
         "-of", "default=noprint_wrappers=1", str(out)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert "sample_rate=16000" in probe.stdout
    assert "channels=1" in probe.stdout


@_skip_no_ffmpeg
async def test_convert_to_wav_roundtrip_mp4(tmp_path, monkeypatch):
    """mp4 (含音轨) → 16k mono wav。"""
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    src = tmp_path / "src.mp4"
    _gen_mp4(src, seconds=1.0)

    out = await convert_to_wav(src)
    assert out.exists()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
         "-of", "default=noprint_wrappers=1", str(out)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert "sample_rate=16000" in probe.stdout
    assert "channels=1" in probe.stdout


@_skip_no_ffmpeg
def test_get_audio_duration_positive(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    wav = tmp_path / "dur.wav"
    _gen_wav(wav, seconds=2.0, rate=16000, channels=1)
    dur = get_audio_duration(wav)
    assert dur > 0
    # 容差：2s ± 0.3
    assert abs(dur - 2.0) < 0.3


def test_get_audio_duration_missing_file_returns_zero(tmp_path):
    # 不依赖 ffmpeg：缺失文件应返回 0.0，不抛异常
    assert get_audio_duration(tmp_path / "nope.wav") == 0.0


@_skip_no_ffmpeg
async def test_slice_audio_yields_multiple_segments(tmp_path, monkeypatch):
    """>270s wav 切片应产生 ≥2 段。"""
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    wav = tmp_path / "long.wav"
    # 280s > 270 + 3 = 273 → 触发切片
    _gen_wav(wav, seconds=280.0, rate=16000, channels=1)

    segments = await slice_audio(wav, segment_duration=270, overlap=3)
    assert len(segments) >= 2
    for seg in segments:
        assert seg.exists()


@_skip_no_ffmpeg
async def test_slice_audio_short_returns_original(tmp_path, monkeypatch):
    """短音频 (< segment_duration + overlap) 直接返回 [wav]。"""
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    wav = tmp_path / "short.wav"
    _gen_wav(wav, seconds=2.0, rate=16000, channels=1)

    segments = await slice_audio(wav, segment_duration=270, overlap=3)
    assert segments == [wav]


@_skip_no_ffmpeg
async def test_slice_audio_duration_zero_estimates_and_slices(tmp_path, monkeypatch):
    """ffprobe→0.0 时不得当「够短」：按体积估时长后仍切片（抓真实 slice_audio，不 mock）。"""
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    monkeypatch.setattr(audio, "get_audio_duration", lambda _p: 0.0)

    wav = tmp_path / "long_unknown.wav"
    _gen_wav(wav, seconds=280.0, rate=16000, channels=1)

    segments = await slice_audio(wav, segment_duration=270, overlap=3)
    assert len(segments) >= 2
    for seg in segments:
        assert seg.exists()


async def test_slice_audio_reuses_provided_duration(tmp_path, monkeypatch):
    """facade 传入 duration>0 时不再调用 get_audio_duration。"""
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    calls = {"n": 0}

    def _boom(_p):
        calls["n"] += 1
        raise AssertionError("should not ffprobe when duration provided")

    monkeypatch.setattr(audio, "get_audio_duration", _boom)
    wav = tmp_path / "short.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 40)
    # 真短：duration=10 ≤ 273 → 直接返回 [wav]，不切
    segs = await slice_audio(wav, segment_duration=270, overlap=3, duration=10.0)
    assert segs == [wav]
    assert calls["n"] == 0


async def test_slice_audio_duration_zero_oversized_unestimable_raises(
    tmp_path, monkeypatch
):
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    monkeypatch.setattr(audio, "get_audio_duration", lambda _p: 0.0)
    monkeypatch.setattr(audio, "_estimate_pcm_wav_duration", lambda _p: 0.0)

    wav = tmp_path / "huge.wav"
    wav.write_bytes(b"\x00" * (11 * 1024 * 1024))
    with pytest.raises(DataSourceError, match="10MB"):
        await slice_audio(wav, segment_duration=270, overlap=3)


async def test_convert_to_wav_no_ffmpeg_raises_datasource_error(
    tmp_path, monkeypatch
):
    """monkeypatch shutil.which("ffmpeg") → None → DataSourceError("ffmpeg …")。"""
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda _: None)

    # 随便给一个存在的文件作为 src；ffmpeg 检查应在 subprocess 之前
    src = tmp_path / "src.wav"
    src.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    with pytest.raises(DataSourceError, match="ffmpeg"):
        await convert_to_wav(src)
