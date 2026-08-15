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

    async def _download(urls, *, headers=None, total_timeout=None, client=None):
        cap["download_url"] = urls[0] if urls else None
        cap["download_headers"] = headers
        return dl_path

    async def _convert(src):
        cap.setdefault("convert_calls", []).append(src)
        return wav_path

    def _duration(wav):
        return 10.0

    async def _slice(wav, segment_duration=270, overlap=3, *, duration=None):
        cap.setdefault("slice_called", True)
        cap["slice_duration"] = duration
        return [tmp_path / "seg_0.wav", tmp_path / "seg_1.wav"]

    async def _recognize(wav_path, *, title=None, author=None):
        cap.setdefault("recognize_calls", []).append(
            {"wav": str(wav_path), "title": title, "author": author}
        )
        return "你好"

    def _merge(parts, overlap=3):
        cap["merge_parts"] = list(parts)
        return "".join(parts)

    monkeypatch.setattr(tr, "download_urls", _download)
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
    # 段级有界并发：按文件名映射，禁止用共享 iter（会竞态）。
    by_name = {"seg_0.wav": "段落一", "seg_1.wav": "段落二"}

    async def _recognize(wav_path, *, title=None, author=None):
        cap.setdefault("recognize_calls", []).append({"wav": str(wav_path)})
        return by_name[Path(wav_path).name]

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
    # Task 8a 默认注入 decode_channels_media；本测显式卸掉 seam，断言守卫仍在。
    monkeypatch.setattr(tr, "decode_media", None)
    assert tr.decode_media is None
    ref = MediaRef(
        platform="wechat_channels",
        download_url="https://x/a.mp4",
        headers={},
        decode_key="some-decode-key",
    )
    with pytest.raises(DataSourceError, match="channels decode not enabled"):
        await tr.transcribe(ref)


@pytest.mark.asyncio
async def test_transcribe_decode_media_injected_and_called(monkeypatch, tmp_path):
    """decode_key 非空时走注入的 decode_media（to_thread），再进入 convert。"""
    cap = _common_seams(monkeypatch, tmp_path)
    called: dict = {}

    def _fake_decode(src, key):
        called["src"] = src
        called["key"] = key
        out = tmp_path / "decoded.mp4"
        out.write_bytes(b"\x00\x00\x00\x20ftypisom")
        return out

    monkeypatch.setattr(tr, "decode_media", _fake_decode)
    ref = MediaRef(
        platform="wechat_channels",
        download_url="https://x/a.mp4",
        headers={},
        decode_key="910035402",
    )
    text = await tr.transcribe(ref)
    assert text == "你好"
    assert called["key"] == "910035402"
    assert Path(called["src"]).name == "dl_source.mp4"
    # convert 收到的是 decode 产出，而非原始下载文件
    assert cap["convert_calls"][0].name == "decoded.mp4"


@pytest.mark.asyncio
async def test_transcribe_channels_missing_decode_key_skips_decode(monkeypatch, tmp_path):
    """无 decode_key → 跳过 WASM，直进 convert（未加密或 TikHub 缺字段）。"""
    cap = _common_seams(monkeypatch, tmp_path)
    called = {"n": 0}

    def _fake_decode(src, key):
        called["n"] += 1
        return src

    monkeypatch.setattr(tr, "decode_media", _fake_decode)
    ref = MediaRef(
        platform="wechat_channels",
        download_url="https://x/a.mp4",
        headers={},
        decode_key=None,
    )
    text = await tr.transcribe(ref)
    assert text == "你好"
    assert called["n"] == 0
    assert len(cap["convert_calls"]) == 1


# ---- contract 新增测试 ------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_timeout_raises_datasource_error(monkeypatch, tmp_path):
    cap = _common_seams(monkeypatch, tmp_path)
    # 极短超时 + 慢 recognize → wait_for 必然超时。
    # 超时常量已移至 ``settings.TRANSCRIBE_TIMEOUT``（P3-1），patch settings 而非模块常量。
    monkeypatch.setattr(tr.settings, "TRANSCRIBE_TIMEOUT", 0.05)

    async def _slow_recognize(wav_path, *, title=None, author=None):
        await asyncio.sleep(1.0)
        return "不应到达"

    monkeypatch.setattr(tr, "recognize_wav", _slow_recognize)

    # 用真实 temp 文件验证超时后 finally 仍然 unlink。
    dl_path = tmp_path / "dl_to.mp4"
    dl_path.write_bytes(b"x")
    wav_path = tmp_path / "wav_to.wav"
    wav_path.write_bytes(b"y")

    async def _download(urls, *, headers=None, total_timeout=None, client=None):
        return dl_path

    async def _convert(src):
        return wav_path

    monkeypatch.setattr(tr, "download_urls", _download)
    monkeypatch.setattr(tr, "convert_to_wav", _convert)

    with pytest.raises(DataSourceError, match="timed out"):
        await tr.transcribe("https://x/a.mp4")

    # P0：超时路径 temps 仍被清理（finally 在 cancel 时执行）。
    assert not dl_path.exists()
    assert not wav_path.exists()


@pytest.mark.asyncio
async def test_transcribe_queue_wait_excluded_from_wall_clock(monkeypatch, tmp_path):
    """排队等下载槽位不计入 TRANSCRIBE_TIMEOUT，且下载后立即让出槽位。

    回归（线上实测拆账号 10 条）：item 并发 10 > download_sem 2，尾部条目在队列里
    干等；旧实现把排队算进 wait_for，第 8 条含排队的 step elapsed 已达 252s，
    第 9/10 条不做任何实际工作就撞满 300s。
    """
    cap = _common_seams(monkeypatch, tmp_path)
    monkeypatch.setattr(tr.settings, "TRANSCRIBE_TIMEOUT", 0.2)

    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(tr, "get_download_semaphore", lambda: sem)

    async def _recognize(wav_path, *, title=None, author=None):
        # 下载结束即 release：ASR 阶段不得仍占着下载并发。
        cap["sem_locked_during_asr"] = sem.locked()
        return "你好"

    monkeypatch.setattr(tr, "recognize_wav", _recognize)

    await sem.acquire()  # 占满槽位，模拟前序条目正在下载
    task = asyncio.create_task(tr.transcribe("https://x/a.mp4"))
    await asyncio.sleep(0.4)  # 排队 0.4s，远超 0.2s 墙钟
    assert not task.done(), "排队期间不得启动墙钟"
    sem.release()

    assert await task == "你好"
    assert cap["sem_locked_during_asr"] is False


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

    async def _download(urls, *, headers=None, total_timeout=None, client=None):
        return dl_path

    async def _convert(src):
        return wav_path

    async def _boom(wav_path, *, title=None, author=None):
        raise DataSourceError("asr boom")

    monkeypatch.setattr(tr, "download_urls", _download)
    monkeypatch.setattr(tr, "convert_to_wav", _convert)
    monkeypatch.setattr(tr, "recognize_wav", _boom)

    with pytest.raises(DataSourceError, match="asr boom"):
        await tr.transcribe("https://x/a.mp4")

    # 下载产物 + 转码产物均被 unlink（temps 清空）。
    assert not dl_path.exists()
    assert not wav_path.exists()


# ---- 转写缓存：按 raw_id 复用，避免同一条视频反复整下 ----------------------


def _reusable_seams(monkeypatch, tmp_path: Path) -> dict:
    """``_common_seams`` + 每次调用重建临时文件。

    管线的 finally 会 unlink 下载/转码产物，所以在同一个测试里真跑第二遍时，
    共用一份 fixture 文件的桩会撞 FileNotFoundError。缓存类测试必须跑两遍，故单列。
    """
    cap = _common_seams(monkeypatch, tmp_path)
    dl_path = tmp_path / "dl_source.mp4"
    wav_path = tmp_path / "audio_16k_mono.wav"

    async def _download(urls, *, headers=None, total_timeout=None, client=None):
        cap.setdefault("download_calls", []).append(urls[0] if urls else None)
        dl_path.write_bytes(b"src-bytes")
        return dl_path

    async def _convert(src):
        wav_path.write_bytes(b"wav-bytes")
        return wav_path

    monkeypatch.setattr(tr, "download_urls", _download)
    monkeypatch.setattr(tr, "convert_to_wav", _convert)
    return cap


@pytest.fixture(autouse=True)
def _clear_transcript_cache():
    """缓存是模块级状态，测试间必须隔离，否则用例互相污染。"""
    tr.clear_transcript_cache()
    yield
    tr.clear_transcript_cache()


@pytest.mark.asyncio
async def test_transcribe_caches_by_raw_id(monkeypatch, tmp_path):
    """第二次转同一条视频直接命中缓存：不下载、不识别。

    线上实测（logs 15:48–16:49）同一条 10.3MB 视频被整下 4 次——用户重试拆视频，
    每次重跑全链路。
    """
    cap = _common_seams(monkeypatch, tmp_path)
    ref = MediaRef(
        platform="douyin",
        download_url="https://x/a.mp4",
        headers={},
        raw_id="7412345678901234567",
    )

    first = await tr.transcribe(ref)
    assert first == "你好"
    assert len(cap["recognize_calls"]) == 1

    # 换一套直链（CDN 签名会变）但同一个 raw_id → 仍应命中。
    again = MediaRef(
        platform="douyin",
        download_url="https://y/other-signed-url.mp4",
        headers={},
        raw_id="7412345678901234567",
    )
    second = await tr.transcribe(again)
    assert second == "你好"
    assert len(cap["recognize_calls"]) == 1, "命中缓存不应再走 ASR"


@pytest.mark.asyncio
async def test_transcribe_cache_key_isolates_platform_and_id(monkeypatch, tmp_path):
    """不同 raw_id / 不同平台各自独立，不得串味。"""
    cap = _reusable_seams(monkeypatch, tmp_path)
    await tr.transcribe(MediaRef(platform="douyin", download_url="u", headers={}, raw_id="a"))
    await tr.transcribe(MediaRef(platform="douyin", download_url="u", headers={}, raw_id="b"))
    await tr.transcribe(
        MediaRef(platform="wechat_channels", download_url="u", headers={}, raw_id="a")
    )
    assert len(cap["recognize_calls"]) == 3


@pytest.mark.asyncio
async def test_transcribe_without_raw_id_not_cached(monkeypatch, tmp_path):
    """无 raw_id（含裸 str URL）不缓存——URL 带时效签名，不能当身份。"""
    cap = _reusable_seams(monkeypatch, tmp_path)
    await tr.transcribe("https://x/a.mp4")
    await tr.transcribe("https://x/a.mp4")
    assert len(cap["recognize_calls"]) == 2


@pytest.mark.asyncio
async def test_transcribe_failure_not_cached(monkeypatch, tmp_path):
    """失败不进缓存：多为瞬态（CDN 慢流 / ASR 抖动），下次该真重试。"""
    cap = _reusable_seams(monkeypatch, tmp_path)
    calls = {"n": 0}

    async def _flaky(wav_path, *, title=None, author=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DataSourceError("cdn stalled")
        cap.setdefault("recognize_calls", []).append({"wav": str(wav_path)})
        return "你好"

    monkeypatch.setattr(tr, "recognize_wav", _flaky)
    ref = MediaRef(platform="douyin", download_url="u", headers={}, raw_id="z")

    with pytest.raises(DataSourceError):
        await tr.transcribe(ref)
    assert await tr.transcribe(ref) == "你好"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_transcribe_cache_hit_skips_download_slot(monkeypatch, tmp_path):
    """命中缓存必须绕开下载信号量——排在满队列后面就白缓存了。"""
    _common_seams(monkeypatch, tmp_path)
    ref = MediaRef(platform="douyin", download_url="u", headers={}, raw_id="q")
    await tr.transcribe(ref)

    sem = asyncio.Semaphore(1)
    await sem.acquire()  # 占满，未命中缓存者必被挂起
    monkeypatch.setattr(tr, "get_download_semaphore", lambda: sem)

    assert await asyncio.wait_for(tr.transcribe(ref), timeout=1.0) == "你好"
