"""微信视频号加密 MP4 解密（Task 8a）。

算法（spike 已用真实 TikHub ``fetch_video_detail`` + CDN 头验证）：
  decode_key → 微信官方 WASM ``WxIsaac64`` 生成 131072 字节密钥流 → reverse
  → 与文件前 128KiB XOR；其余明文保留；成功时 ``bytes[4:8] == b'ftyp'``。

实现：通过 ``node`` 调用同包内 ``wechat_wasm/decrypt_cli.js``（不引入纯 Python
Isaac64 近似实现——已实测与 WASM 输出不一致）。

签名对齐 ``transcribe.decode_media`` seam：``(Path, str) -> Path``。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from app.datasource import DataSourceError

log = logging.getLogger(__name__)

_WASM_DIR = Path(__file__).resolve().parent / "wechat_wasm"
_CLI = _WASM_DIR / "decrypt_cli.js"
_KEYSTREAM_SIZE = 131_072


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def decode_channels_media(src: Path, decode_key: str) -> Path:
    """解密视频号加密文件，写出 ``*_decoded.mp4`` 并返回路径。

    要求 PATH 上有 ``node``；缺 node / CLI / WASM 资产 → ``DataSourceError``。
    解密失败（非 ftyp）由 CLI 非零退出，翻译为 ``DataSourceError``。
    失败路径会尽量删除已写出的 ``out``，避免孤儿文件依赖 2h GC。
    """
    key = (decode_key or "").strip()
    if not key:
        raise DataSourceError("channels decode: empty decode_key")
    node = shutil.which("node")
    if not node:
        raise DataSourceError("channels decode requires node.js on PATH")
    if not _CLI.is_file():
        raise DataSourceError(f"channels decode CLI missing: {_CLI}")
    wasm_bin = _WASM_DIR / "wasm" / "wasm_video_decode.wasm"
    if not wasm_bin.is_file():
        raise DataSourceError(f"channels decode WASM missing: {wasm_bin}")

    src = Path(src)
    if not src.is_file():
        raise DataSourceError(f"channels decode: encrypted file not found: {src}")

    out = src.with_name(f"{src.stem}_decoded.mp4")
    log.info("channels decode start: src=%s", src.name)
    t0 = time.monotonic()
    try:
        try:
            proc = subprocess.run(
                [node, str(_CLI), str(src), key, str(out)],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as e:
            _unlink_quiet(out)
            raise DataSourceError("channels decode timed out") from e
        except OSError as e:
            _unlink_quiet(out)
            raise DataSourceError(f"channels decode failed to spawn node: {e}") from e

        if proc.returncode != 0:
            _unlink_quiet(out)
            err = (proc.stderr or proc.stdout or "").strip()[:300]
            raise DataSourceError(
                f"channels decode failed: {err or f'exit {proc.returncode}'}"
            )
        if not out.is_file():
            raise DataSourceError("channels decode produced no output file")

        # 轻量二次校验（CLI 内已 assertMp4；此处防写盘残缺）。只读 8 字节，禁全量入内存。
        with out.open("rb") as fh:
            head = fh.read(8)
        if len(head) < 8 or head[4:8] != b"ftyp":
            _unlink_quiet(out)
            raise DataSourceError("channels decode: output missing MP4 ftyp signature")
    except DataSourceError:
        raise
    except Exception:
        _unlink_quiet(out)
        raise

    log.info(
        "channels decode ok: src=%s out=%s keystream=%d elapsed=%.2fs",
        src.name, out.name, _KEYSTREAM_SIZE, time.monotonic() - t0,
    )
    return out
