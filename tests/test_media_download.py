"""download.py 测试：httpx MockTransport，绝不发真实网络请求。

覆盖：
  - 成功：GET 返回字节 → 写入 temp 文件、Path.exists()、内容匹配、前缀 sks_asr_dl_。
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
from app.datasource.media.download import download_url, gc_stale_tmp


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


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

    now = time.time()
    # 陈旧：mtime > 2h 前
    os.utime(old, (now - 3 * 3600, now - 3 * 3600))
    os.utime(old_wav_dir, (now - 3 * 3600, now - 3 * 3600))
    # 新鲜：当前
    os.utime(fresh, (now, now))

    count = gc_stale_tmp(max_age_hours=2.0)
    assert count == 2
    assert fresh.exists()
    assert not old.exists()
    assert not old_wav_dir.exists()


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
