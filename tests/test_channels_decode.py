"""视频号 decode：CLI 资产存在性 +（有 node 时）真实 WASM 冒烟。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.datasource import DataSourceError
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


@pytest.mark.skipif(not shutil.which("node"), reason="node not on PATH")
def test_decode_channels_media_wasm_ftyp(tmp_path: Path):
    """用 spike 固化的加密头 + 配对 decode_key 验证 ftyp（若样例文件存在）。

    样例默认在 SDD spikes 目录且被 gitignore；本地 spike 后可跑通。
    无样例时跳过——CI 仍靠 mock 路径与资产存在性测试。
    """
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
    # 拷到 tmp，避免污染 spikes
    src = tmp_path / "enc.bin"
    src.write_bytes(enc.read_bytes())
    out = decode_channels_media(src, key)
    assert out.is_file()
    assert out.read_bytes()[4:8] == b"ftyp"
