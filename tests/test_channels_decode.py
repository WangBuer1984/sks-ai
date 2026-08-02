"""视频号 decode：资产存在性 + 失败路径（monkeypatch）+（有 node 时）WASM 冒烟。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.datasource import DataSourceError
from app.datasource.media import channels_decode as cd
from app.datasource.media.channels_decode import _CLI, _WASM_DIR, decode_channels_media


def test_wechat_wasm_assets_present():
    assert _CLI.is_file()
    assert (_WASM_DIR / "lib" / "wasm-decrypt.js").is_file()
    assert (_WASM_DIR / "wasm" / "decrypt.js").is_file()
    assert (_WASM_DIR / "wasm" / "wasm_video_decode.js").is_file()
    assert (_WASM_DIR / "wasm" / "wasm_video_decode.wasm").is_file()


def test_decode_empty_key_errors(tmp_path: Path):
    src = tmp_path / "e.mp4"
    src.write_bytes(b"x" * 64)
    with pytest.raises(DataSourceError, match="empty decode_key"):
        decode_channels_media(src, "  ")


def test_decode_requires_node(monkeypatch, tmp_path: Path):
    src = tmp_path / "e.mp4"
    src.write_bytes(b"x" * 64)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(DataSourceError, match="requires node"):
        decode_channels_media(src, "910035402")


def test_decode_cli_missing(monkeypatch, tmp_path: Path):
    src = tmp_path / "e.mp4"
    src.write_bytes(b"x" * 64)
    monkeypatch.setattr(cd, "_CLI", tmp_path / "missing_cli.js")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")
    with pytest.raises(DataSourceError, match="CLI missing"):
        decode_channels_media(src, "k")


def test_decode_wasm_missing(monkeypatch, tmp_path: Path):
    src = tmp_path / "e.mp4"
    src.write_bytes(b"x" * 64)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")
    fake_dir = tmp_path / "empty_wasm"
    fake_dir.mkdir()
    monkeypatch.setattr(cd, "_WASM_DIR", fake_dir)
    monkeypatch.setattr(cd, "_CLI", tmp_path / "cli.js")
    (tmp_path / "cli.js").write_text("//")
    with pytest.raises(DataSourceError, match="WASM missing"):
        decode_channels_media(src, "k")


def test_decode_src_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")
    with pytest.raises(DataSourceError, match="encrypted file not found"):
        decode_channels_media(tmp_path / "nope.mp4", "k")


def test_decode_timeout_unlinks_out(monkeypatch, tmp_path: Path):
    src = tmp_path / "e.mp4"
    src.write_bytes(b"x" * 64)
    out = src.with_name("e_decoded.mp4")
    out.write_bytes(b"orphan")

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="node", timeout=1)

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(cd.subprocess, "run", _boom)
    with pytest.raises(DataSourceError, match="timed out"):
        decode_channels_media(src, "k")
    assert not out.exists()


def test_decode_nonzero_exit_unlinks_out(monkeypatch, tmp_path: Path):
    src = tmp_path / "e.mp4"
    src.write_bytes(b"x" * 64)
    out = src.with_name("e_decoded.mp4")

    def _fail(*a, **k):
        out.write_bytes(b"bad")
        return SimpleNamespace(returncode=1, stderr="boom", stdout="")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(cd.subprocess, "run", _fail)
    with pytest.raises(DataSourceError, match="channels decode failed"):
        decode_channels_media(src, "k")
    assert not out.exists()


def test_decode_bad_ftyp_unlinks_out(monkeypatch, tmp_path: Path):
    src = tmp_path / "e.mp4"
    src.write_bytes(b"x" * 64)
    out = src.with_name("e_decoded.mp4")

    def _ok_bad_ftyp(*a, **k):
        out.write_bytes(b"\x00" * 16)
        return SimpleNamespace(returncode=0, stderr="", stdout="ok")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(cd.subprocess, "run", _ok_bad_ftyp)
    with pytest.raises(DataSourceError, match="ftyp"):
        decode_channels_media(src, "k")
    assert not out.exists()


@pytest.mark.skipif(not shutil.which("node"), reason="node not on PATH")
def test_decode_channels_media_wasm_ftyp(tmp_path: Path):
    """用 spike 固化的加密头 + 配对 decode_key 验证 ftyp（若样例文件存在）。"""
    spike = (
        Path(__file__).resolve().parents[1]
        / ".superpowers/sdd/2026-08-02-qwen-asr-media-pipeline/spikes"
    )
    enc = spike / "enc_head.bin"
    meta = spike / "meta.json"
    if not enc.is_file() or not meta.is_file():
        pytest.skip("local spike enc_head.bin/meta.json not present")
    import json

    key = json.loads(meta.read_text())["decode_key"]
    src = tmp_path / "enc.bin"
    src.write_bytes(enc.read_bytes())
    out = decode_channels_media(src, key)
    assert out.is_file()
    assert out.read_bytes()[4:8] == b"ftyp"
