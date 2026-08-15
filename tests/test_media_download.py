"""download.py 测试：httpx MockTransport，绝不发真实网络请求。

覆盖：
  - 成功：stream GET → 写入 temp 文件、Path.exists()、内容匹配、前缀 sks_asr_dl_。
  - 多块大体量内容仍完整落盘（流式路径）。
  - 4xx/5xx → DataSourceError。
  - 传输异常（httpx.ConnectError / timeout）→ DataSourceError，不冒泡裸异常。
  - headers 透传到请求（MockTransport handler 捕获 request.headers）。
  - gc_stale_tmp：一个新鲜 + 一个陈旧（mtime > 2h）→ 仅删陈旧，count==1；
    缺目录不崩；删不掉的文件被吞掉。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.datasource import DataSourceError
from app.datasource.media import download as dl
from app.datasource.media.download import download_url, gc_stale_tmp, _DOWNLOAD_TIMEOUT


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_download_read_timeout_is_30s_not_600():
    """read=30s 锁：stall 30s 即 DataSourceError，不烧到旧 600s。

    httpx read 是「块间无数据上限」非整段总时长——0.7MB/s 持续流每块间隔 <1s 永不触
    30s；真 stall（0 字节）30s 即 ReadTimeout。connect 维持 30s；write/pool 留 600s
    不动（不把整段 total 砍成 30s，否则 50s+ 的正常长视频下载会被误杀）。
    """
    assert _DOWNLOAD_TIMEOUT.read == 30.0, "read 必须 30s：stall 早死，不能回到 600s"
    assert _DOWNLOAD_TIMEOUT.connect == 30.0
    # total 不被砍：write/pool 仍 600s（正常长视频下载 50s+ 不被误杀）
    assert _DOWNLOAD_TIMEOUT.write == 600.0
    assert _DOWNLOAD_TIMEOUT.pool == 600.0


async def test_download_url_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    async def handler(request: httpx.Request):
        return httpx.Response(200, content=b"fake-bytes")

    client = _mock_client(handler)
    try:
        path = await download_url("https://cdn.example/a.mp4", client=client)
    finally:
        await client.aclose()

    assert isinstance(path, Path)
    assert path.exists()
    assert path.read_bytes() == b"fake-bytes"
    assert path.name.startswith("sks_asr_dl_")


async def test_download_url_streams_large_body(tmp_path, monkeypatch):
    """>1 个 chunk（256KiB）的 body 须完整落盘（覆盖 aiter_bytes 路径）。"""
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    payload = bytes((i % 256) for i in range(300 * 1024))

    async def handler(request: httpx.Request):
        return httpx.Response(200, content=payload)

    client = _mock_client(handler)
    try:
        path = await download_url("https://cdn.example/big.mp4", client=client)
    finally:
        await client.aclose()

    assert path.read_bytes() == payload


async def test_download_url_reuses_shared_client(tmp_path, monkeypatch):
    """未注入 client 时复用进程内共享 AsyncClient。"""
    import app.datasource.media.download as dl

    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    monkeypatch.setattr(dl, "_shared_client", None)

    async def handler(request: httpx.Request):
        return httpx.Response(200, content=b"ok")

    shared = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    monkeypatch.setattr(dl, "_shared_client", shared)

    p1 = await download_url("https://cdn.example/a.mp4")
    p2 = await download_url("https://cdn.example/b.mp4")
    assert p1.read_bytes() == b"ok"
    assert p2.read_bytes() == b"ok"
    assert dl._shared_client is shared
    assert not shared.is_closed
    await shared.aclose()


async def test_download_url_forwards_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request):
        captured["ua"] = request.headers.get("User-Agent", "")
        captured["referer"] = request.headers.get("Referer", "")
        return httpx.Response(200, content=b"ok")

    client = _mock_client(handler)
    try:
        await download_url(
            "https://cdn.example/a.mp4",
            headers={"User-Agent": "test-ua/1.0", "Referer": "https://www.douyin.com/"},
            client=client,
        )
    finally:
        await client.aclose()

    assert captured["ua"] == "test-ua/1.0"
    assert captured["referer"] == "https://www.douyin.com/"


@pytest.mark.parametrize("status", [404, 500])
async def test_download_http_error_raises_datasource_error(status, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    async def handler(request: httpx.Request):
        return httpx.Response(status, content=b"nope")

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await download_url("https://cdn.example/missing.mp4", client=client)
    finally:
        await client.aclose()


async def test_download_transport_error_raises_datasource_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    def handler(request: httpx.Request):
        raise httpx.ConnectError("connection refused")

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await download_url("https://cdn.example/down.mp4", client=client)
    finally:
        await client.aclose()


async def test_download_timeout_raises_datasource_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    def handler(request: httpx.Request):
        raise httpx.ReadTimeout("read timed out")

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await download_url("https://cdn.example/slow.mp4", client=client)
    finally:
        await client.aclose()


def test_gc_stale_tmp_deletes_only_old_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))

    fresh = tmp_path / "sks_asr_dl_fresh"
    fresh.write_bytes(b"fresh")
    old = tmp_path / "sks_asr_dl_old"
    old.write_bytes(b"old")
    old_wav_dir = tmp_path / "sks_asr_wav_abc"
    old_wav_dir.mkdir()
    (old_wav_dir / "audio_16k_mono.wav").write_bytes(b"x")
    old_slice_dir = tmp_path / "sks_asr_slice_xyz"
    old_slice_dir.mkdir()
    (old_slice_dir / "seg_0.wav").write_bytes(b"y")

    now = time.time()
    # 陈旧：mtime > 2h 前
    os.utime(old, (now - 3 * 3600, now - 3 * 3600))
    os.utime(old_wav_dir, (now - 3 * 3600, now - 3 * 3600))
    os.utime(old_slice_dir, (now - 3 * 3600, now - 3 * 3600))
    # 新鲜：当前
    os.utime(fresh, (now, now))

    count = gc_stale_tmp(max_age_hours=2.0)
    assert count == 3
    assert fresh.exists()
    assert not old.exists()
    assert not old_wav_dir.exists()
    assert not old_slice_dir.exists()


def test_gc_stale_tmp_safe_on_missing_dir(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(missing))
    # 不应抛异常
    assert gc_stale_tmp() == 0


def test_gc_stale_tmp_swallows_unlink_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    old = tmp_path / "sks_asr_dl_undeletable"
    old.write_bytes(b"x")
    now = time.time()
    os.utime(old, (now - 5 * 3600, now - 5 * 3600))

    real_unlink = os.unlink

    def boom(path):
        # 命中我们的目标文件就抛 OSError，其余正常
        if str(path).endswith("sks_asr_dl_undeletable"):
            raise OSError("permission denied")
        real_unlink(path)

    monkeypatch.setattr(os, "unlink", boom)
    # 不应抛异常；删不掉返回 0
    assert gc_stale_tmp() == 0


def test_gc_stale_tmp_ignores_vanished_entries(tmp_path, monkeypatch, caplog):
    """listdir 后被并发 finally 删掉 → FileNotFoundError 静默，不刷 warning。"""
    import logging

    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    vanished = tmp_path / "sks_asr_wav_gone"
    vanished.mkdir()
    now = time.time()
    os.utime(vanished, (now - 5 * 3600, now - 5 * 3600))

    real_stat = os.stat

    def race_stat(path, *a, **k):
        if str(path).endswith("sks_asr_wav_gone"):
            raise FileNotFoundError(path)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", race_stat)
    with caplog.at_level(logging.WARNING, logger="app.datasource.media.download"):
        assert gc_stale_tmp() == 0
    assert not any("cannot stat/delete" in r.message for r in caplog.records)


# ---- download_urls 多节点合计预算 -----------------------------------------
# per-URL 上限挡不住「节点多 × 每个都慢」：_asr_source_urls 现在最多给 10 个候选，
# 10×120s 远超 transcribe 的 300s 墙钟 → 外层超时先到，错误不指明卡在下载。


async def test_download_urls_stops_when_overall_budget_exhausted(tmp_path, monkeypatch):
    """预算耗尽即停，不把剩余节点挨个试完。"""
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    attempts: list[str] = []

    async def _fake_download(url, *, headers=None, client=None, total_timeout=None):
        attempts.append(url)
        # 每次「耗掉」全部预算：模拟慢流跑满 per-URL 上限。
        clock["t"] += 100.0
        raise DataSourceError(f"download timed out after {total_timeout}s: {url}")

    clock = {"t": 1000.0}
    monkeypatch.setattr(dl, "download_url", _fake_download)
    monkeypatch.setattr(dl.time, "monotonic", lambda: clock["t"])

    with pytest.raises(DataSourceError, match="overall budget"):
        await dl.download_urls(
            [f"https://cdn{i}/v.mp4" for i in range(10)], overall_timeout=180.0
        )

    assert len(attempts) == 2, f"180s 预算最多试 2 个 100s 的节点，实际试了 {len(attempts)}"


async def test_download_urls_shrinks_per_url_budget_to_remaining(tmp_path, monkeypatch):
    """末个节点只能拿到剩余预算，不能再独享完整 per-URL 上限。"""
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    budgets: list[float] = []
    clock = {"t": 0.0}

    async def _fake_download(url, *, headers=None, client=None, total_timeout=None):
        budgets.append(total_timeout)
        clock["t"] += 150.0
        raise DataSourceError("boom")

    monkeypatch.setattr(dl, "download_url", _fake_download)
    monkeypatch.setattr(dl.time, "monotonic", lambda: clock["t"])

    with pytest.raises(DataSourceError):
        await dl.download_urls(
            ["https://a/v.mp4", "https://b/v.mp4"], total_timeout=120.0, overall_timeout=180.0
        )

    assert budgets == [120.0, 30.0], f"第二个节点应只剩 30s，实际 {budgets}"


async def test_download_urls_unbounded_when_overall_none(tmp_path, monkeypatch):
    """overall_timeout=None → 回到「每个节点各享 per-URL 上限」的老行为。"""
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    budgets: list[float] = []

    async def _fake_download(url, *, headers=None, client=None, total_timeout=None):
        budgets.append(total_timeout)
        raise DataSourceError("boom")

    monkeypatch.setattr(dl, "download_url", _fake_download)

    with pytest.raises(DataSourceError, match="all 3 urls failed"):
        await dl.download_urls(
            ["https://a/1", "https://b/2", "https://c/3"],
            total_timeout=120.0,
            overall_timeout=None,
        )

    assert budgets == [120.0, 120.0, 120.0]


async def test_download_urls_succeeds_on_second_node_within_budget(tmp_path, monkeypatch):
    """预算够时正常降级到下一个节点——别把可用的 fallback 也掐了。"""
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    good = tmp_path / "ok.mp4"
    good.write_bytes(b"ok")

    async def _fake_download(url, *, headers=None, client=None, total_timeout=None):
        if url.endswith("/1"):
            raise DataSourceError("first node dead")
        return good

    monkeypatch.setattr(dl, "download_url", _fake_download)

    assert await dl.download_urls(["https://a/1", "https://b/2"]) == good
