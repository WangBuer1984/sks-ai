"""transcribe 门面测试：mock 媒体管线 seam（download/convert/duration/slice/
recognize/merge/gc），绝不发真实网络/ffmpeg/DashScope 请求。

覆盖（brief + contract）：
  - 短音频 MediaRef：单段 recognize，返回文本。
  - 裸 str 入参：headers 必须是 None/{}，不猜 douyin 头。
  - duration>300 → 切片路径，per-segment recognize。
  - duration==0.0（未知）→ 切片路径（不 raise）。
  - 短音频 wav>10MB → DataSourceError。
  - 未配置 ALIYUN_ASR_KEY → DataSourceError。
  - 未配置 ffmpeg → DataSourceError。
  - decode_key 但 decode_media=None → DataSourceError。
  - 超时 → DataSourceError（非裸 TimeoutError），且 temps 被清理。
  - title/author 透传给 recognize_wav。
  - recognize 抛错 → temps 被清理。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import settings
from app.datasource import DataSourceError
from app.datasource import transcribe as tr
from app.datasource.media import MediaRef


def _common_seams(monkeypatch, tmp_path: Path) -> dict:
    """注入全部 seam 的「成功默认值」。返回捕获字典供单测断言。

    各测试可在调用 _common_seams 后再 override 单个 seam。
    """
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "sk-test")
    monkeypatch.setattr(tr, "_ffmpeg_available", lambda: True)
    monkeypatch.setattr(tr, "gc_stale_tmp", lambda *a, **k: 0)

    cap: dict = {}

    dl_path = tmp_path / "dl_source.mp4"
    dl_path.write_bytes(b"src-bytes")
    wav_path = tmp_path / "audio_16k_mono.wav"
    wav_path.write_bytes(b"wav-bytes")

    async def _download(url, *, headers=None, client=None):
        cap["download_url"] = url
        cap["download_headers"] = headers
        return dl_path

    async def _convert(src):
        cap.setdefault("convert_calls", []).append(src)
        return wav_path

    def _duration(wav):
        return 10.0

    async def _slice(wav, segment_duration=270, overlap=3):
        cap.setdefault("slice_called", True)
        return [tmp_path / "seg_0.wav", tmp_path / "seg_1.wav"]

    async def _recognize(wav_path, *, title=None, author=None):
        cap.setdefault("recognize_calls", []).append(
            {"wav": str(wav_path), "title": title, "author": author}
        )
        return "你好"

    def _merge(parts, overlap=3):
        cap["merge_parts"] = list(parts)
        return "".join(parts)

    monkeypatch.setattr(tr, "download_url", _download)
    monkeypatch.setattr(tr, "convert_to_wav", _convert)
    monkeypatch.setattr(tr, "get_audio_duration", _duration)
    monkeypatch.setattr(tr, "slice_audio", _slice)
    monkeypatch.setattr(tr, "recognize_wav", _recognize)
    monkeypatch.setattr(tr, "merge_transcript_parts", _merge)
    return cap


# ---- brief 测试 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_media_ref_short_audio(monkeypatch, tmp_path):
    cap = _common_seams(monkeypatch, tmp_path)
    ref = MediaRef(
        platform="douyin",
        download_url="https://x/a.mp4",
        headers={"Referer": "https://www.douyin.com/"},
        author="张三",
        title="标题A",
    )
    text = await tr.transcribe(ref)
    assert text == "你好"
    # 短音频走单段路径，不切片。
    assert cap.get("slice_called") is not True
    assert cap["download_headers"] == {"Referer": "https://www.douyin.com/"}


@pytest.mark.asyncio
async def test_transcribe_str_is_bare_download_no_headers_guess(monkeypatch, tmp_path):
    cap = _common_seams(monkeypatch, tmp_path)
    # 裸 str：headers 必须是 None 或 {}，绝不猜 douyin Referer/UA。
    await tr.transcribe("https://x/a.mp4")
    assert cap["download_headers"] in (None, {})


@pytest.mark.asyncio
async def test_transcribe_slices_when_duration_over_300(monkeypatch, tmp_path):
    cap = _common_seams(monkeypatch, tmp_path)
    monkeypatch.setattr(tr, "get_audio_duration", lambda wav: 600.0)
    # recognize per segment returns distinct parts so merge is observable.
    parts_iter = iter(["段落一", "段落二"])

    async def _recognize(wav_path, *, title=None, author=None):
        cap.setdefault("recognize_calls", []).append({"wav": str(wav_path)})
        return next(parts_iter)

    monkeypatch.setattr(tr, "recognize_wav", _recognize)

    text = await tr.transcribe("https://x/a.mp4")
    assert cap.get("slice_called") is True
    assert len(cap["recognize_calls"]) == 2
    assert cap["merge_parts"] == ["段落一", "段落二"]
    assert text == "段落一段落二"


@pytest.mark.asyncio
async def test_transcribe_wav_over_10mb_short_duration_errors(monkeypatch, tmp_path):
    _common_seams(monkeypatch, tmp_path)
    monkeypatch.setattr(tr, "get_audio_duration", lambda wav: 100.0)
    # wav > 10MB 且短时长 → DataSourceError（unexpected）。
    big_wav = tmp_path / "big.wav"
    big_wav.write_bytes(b"\0" * (10 * 1024 * 1024 + 1))

    async def _convert(src):
        return big_wav

    monkeypatch.setattr(tr, "convert_to_wav", _convert)

    with pytest.raises(DataSourceError, match="10MB"):
        await tr.transcribe("https://x/a.mp4")


@pytest.mark.asyncio
async def test_transcribe_not_configured_missing_key(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "")
    with pytest.raises(DataSourceError, match="ALIYUN_ASR_KEY"):
        await tr.transcribe("https://x")


@pytest.mark.asyncio
async def test_transcribe_not_configured_missing_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "sk-test")
    monkeypatch.setattr(tr, "_ffmpeg_available", lambda: False)
    with pytest.raises(DataSourceError, match="ffmpeg"):
        await tr.transcribe("https://x")


@pytest.mark.asyncio
async def test_transcribe_decode_key_without_decoder_errors(monkeypatch, tmp_path):
    _common_seams(monkeypatch, tmp_path)
    # decode_media 默认 None（未注入）——确保未引用未定义符号。
    assert tr.decode_media is None
    ref = MediaRef(
        platform="shipinhao",
        download_url="https://x/a.mp4",
        headers={},
        decode_key="some-decode-key",
    )
    with pytest.raises(DataSourceError, match="channels decode not enabled"):
        await tr.transcribe(ref)


# ---- contract 新增测试 ------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_timeout_raises_datasource_error(monkeypatch, tmp_path):
    cap = _common_seams(monkeypatch, tmp_path)
    # 极短超时 + 慢 recognize → wait_for 必然超时。
    monkeypatch.setattr(tr, "_TRANSCRIBE_TIMEOUT", 0.05)

    async def _slow_recognize(wav_path, *, title=None, author=None):
        await asyncio.sleep(1.0)
        return "不应到达"

    monkeypatch.setattr(tr, "recognize_wav", _slow_recognize)

    # 用真实 temp 文件验证超时后 finally 仍然 unlink。
    dl_path = tmp_path / "dl_to.mp4"
    dl_path.write_bytes(b"x")
    wav_path = tmp_path / "wav_to.wav"
    wav_path.write_bytes(b"y")

    async def _download(url, *, headers=None, client=None):
        return dl_path

    async def _convert(src):
        return wav_path

    monkeypatch.setattr(tr, "download_url", _download)
    monkeypatch.setattr(tr, "convert_to_wav", _convert)

    with pytest.raises(DataSourceError, match="timed out"):
        await tr.transcribe("https://x/a.mp4")

    # P0：超时路径 temps 仍被清理（finally 在 cancel 时执行）。
    assert not dl_path.exists()
    assert not wav_path.exists()


@pytest.mark.asyncio
async def test_transcribe_duration_zero_uses_slice_path(monkeypatch, tmp_path):
    cap = _common_seams(monkeypatch, tmp_path)
    # ffprobe 失败 → 0.0 → 视为「未知 → 切片」，不 raise。
    monkeypatch.setattr(tr, "get_audio_duration", lambda wav: 0.0)

    text = await tr.transcribe("https://x/a.mp4")
    assert cap.get("slice_called") is True
    assert len(cap["recognize_calls"]) == 2
    assert text == "你好你好"


@pytest.mark.asyncio
async def test_transcribe_passes_title_author_to_recognize(monkeypatch, tmp_path):
    cap = _common_seams(monkeypatch, tmp_path)
    ref = MediaRef(
        platform="douyin",
        download_url="https://x/a.mp4",
        headers={"Referer": "https://www.douyin.com/"},
        title="热门视频标题",
        author="作者甲",
    )
    await tr.transcribe(ref)
    call = cap["recognize_calls"][0]
    assert call["title"] == "热门视频标题"
    assert call["author"] == "作者甲"


@pytest.mark.asyncio
async def test_transcribe_cleans_temps_on_recognize_error(monkeypatch, tmp_path):
    _common_seams(monkeypatch, tmp_path)

    dl_path = tmp_path / "dl_err.mp4"
    dl_path.write_bytes(b"x")
    wav_path = tmp_path / "wav_err.wav"
    wav_path.write_bytes(b"y")

    async def _download(url, *, headers=None, client=None):
        return dl_path

    async def _convert(src):
        return wav_path

    async def _boom(wav_path, *, title=None, author=None):
        raise DataSourceError("asr boom")

    monkeypatch.setattr(tr, "download_url", _download)
    monkeypatch.setattr(tr, "convert_to_wav", _convert)
    monkeypatch.setattr(tr, "recognize_wav", _boom)

    with pytest.raises(DataSourceError, match="asr boom"):
        await tr.transcribe("https://x/a.mp4")

    # 下载产物 + 转码产物均被 unlink（temps 清空）。
    assert not dl_path.exists()
    assert not wav_path.exists()
