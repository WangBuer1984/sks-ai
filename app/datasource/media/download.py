"""媒体下载层：给定直链 + 可选请求头 → 写入 temp 文件 → 返回 ``Path``。

纯下载：不 import ``tikhub``，无平台感知（高 fallback / header 策略属于
``tikhub`` 的 ``video_meta_to_media_ref`` 与 Task 6 ``resolve_media``，不在此层）。
失败语义：HTTP 非 2xx / 传输异常 / 超时 → 统一抛 ``DataSourceError``，
Task 3.2/3.3 捕获后翻译为退款/重试策略（PRD §11.3）。

模块级 seam：``download_url`` 接受可选 ``client: httpx.AsyncClient | None``，
测试注入 ``httpx.MockTransport`` 客户端，绝不发真实网络请求（见 tests/test_media_download.py）。
与 ``app/datasource/tikhub.py`` 同一模式。

临时文件命名前缀 ``sks_asr_dl_``，落盘目录由 ``settings.ASR_TMP_DIR`` 决定
（空 → 系统 tempfile 目录）。``gc_stale_tmp`` 清扫陈旧文件（默认 >2h）。

下载用 ``stream`` + ``aiter_bytes`` 流式落盘，避免 ``resp.content`` 整包入内存
（视频号/长视频可达数百 MB，``download_sem=2`` 并发时 RSS 会爆炸）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

import httpx

from app.config import settings
from app.datasource import DataSourceError

log = logging.getLogger(__name__)

# 连接 30s；read 30s = 块间无数据上限（stall 早死），write/pool 600s 不动（不砍 total）。
# httpx read 是「上一块到下一块的最大间隔」非「整段总时长」——0.7MB/s 持续有块流每块间隔
# <1s 永不触 30s；真 stall（0 字节）30s 即 ReadTimeout → DataSourceError，不烧到旧 600s。
# 实测抖音/视频号 CDN 聚合限速 ~0.7/1.1 MB/s，长视频正常下载 50s+ 能完（块密集，不触 read）。
_DOWNLOAD_TIMEOUT = httpx.Timeout(600.0, connect=30.0, read=30.0)
_CHUNK_SIZE = 256 * 1024
# 临时文件名前缀——``gc_stale_tmp`` 按此前缀匹配清扫，避免误删其他模块的 temp 文件。
_TMP_PREFIX = "sks_asr_dl_"
# convert / slice 的 mkdtemp 目录前缀（见 audio.py）；须一并清扫，否则只漏 inode。
_GC_PREFIXES = (_TMP_PREFIX, "sks_asr_wav_", "sks_asr_slice_")

# 进程内共享下载客户端（keepalive），拆账号 TOP N 复用连接；测试仍可注入 client。
_shared_client: httpx.AsyncClient | None = None
_shared_client_lock: asyncio.Lock | None = None


def _unlink_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _client_lock() -> asyncio.Lock:
    global _shared_client_lock
    if _shared_client_lock is None:
        _shared_client_lock = asyncio.Lock()
    return _shared_client_lock


async def _get_shared_client() -> httpx.AsyncClient:
    """懒单例 AsyncClient；跨 download_url 调用复用连接。"""
    global _shared_client
    async with _client_lock():
        if _shared_client is None or _shared_client.is_closed:
            _shared_client = httpx.AsyncClient(
                timeout=_DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                ),
            )
        return _shared_client


async def download_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Path:
    """下载直链到 temp 文件，返回 ``Path``。

    - ``headers``：下载所需请求头（Referer/UA 等），默认 None。
    - ``client``：测试注入 ``MockTransport`` 客户端；生产传 None → 用进程内
      共享 client（keepalive，拆账号多视频复用）。
    - 流式写盘（``aiter_bytes``），不把整包 body 留在内存。
    - 非 2xx / 传输异常 / 超时 → ``DataSourceError``（绝不冒泡裸 httpx/网络异常）。
    """
    if client is None:
        client = await _get_shared_client()

    log.info("download start: url=%s", url[:100])
    t0 = time.monotonic()
    tmp_path: str | None = None
    try:
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code // 100 != 2:
                    raise DataSourceError(
                        f"download failed for {url}: HTTP {resp.status_code}"
                    )

                fd, tmp_path = tempfile.mkstemp(
                    prefix=_TMP_PREFIX,
                    dir=settings.ASR_TMP_DIR or None,
                )
                written = 0
                f = None
                try:
                    f = os.fdopen(fd, "wb")
                    fd = -1  # ownership transferred
                    async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                        f.write(chunk)
                        written += len(chunk)
                finally:
                    if f is not None:
                        f.close()
                    elif fd >= 0:
                        os.close(fd)
        except httpx.HTTPError as exc:
            raise DataSourceError(f"download transport error for {url}: {exc}") from exc
        except OSError as exc:
            raise DataSourceError(
                f"failed to write tmp file {tmp_path!r} for {url}: {exc}"
            ) from exc

        assert tmp_path is not None
        log.info(
            "download done: url=%s -> %s (%d bytes) elapsed=%.2fs",
            url[:100], tmp_path, written, time.monotonic() - t0,
        )
        return Path(tmp_path)
    except Exception:
        # 含 DataSourceError / 传输中断：清掉半截文件，避免 ASR_TMP 堆残骸。
        _unlink_quiet(tmp_path)
        raise


def gc_stale_tmp(*, max_age_hours: float = 2.0) -> int:
    """清扫陈旧 ASR 临时文件/目录，返回删除条目数。

    扫描目录：``settings.ASR_TMP_DIR``（空 → 系统 tempfile 目录）。
    - 前缀：``sks_asr_dl_``（下载文件）、``sks_asr_wav_`` / ``sks_asr_slice_``（mkdtemp 目录）。
    - 删 mtime 早于 ``max_age_hours`` 的普通文件；目录用 ``rmtree``。
    - best-effort：缺目录返回 0；单条 ``OSError`` 被吞，继续清扫其余。
    """
    target_dir = settings.ASR_TMP_DIR or tempfile.gettempdir()
    cutoff = time.time() - max_age_hours * 3600.0
    deleted = 0

    try:
        entries = os.listdir(target_dir)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    except OSError as exc:
        log.warning("gc_stale_tmp: cannot list %s: %s", target_dir, exc)
        return 0

    for name in entries:
        if not name.startswith(_GC_PREFIXES):
            continue
        path = os.path.join(target_dir, name)
        try:
            st = os.stat(path)
            if st.st_mtime >= cutoff:
                continue
            if stat.S_ISREG(st.st_mode):
                os.unlink(path)
                deleted += 1
            elif stat.S_ISDIR(st.st_mode):
                # ignore_errors：并发 transcribe finally/早删可能已清掉同路径。
                shutil.rmtree(path, ignore_errors=True)
                deleted += 1
        except FileNotFoundError:
            # listdir → 他协程已 unlink/rmtree：竞态，静默跳过。
            continue
        except OSError as exc:
            log.warning("gc_stale_tmp: cannot stat/delete %s: %s", path, exc)
            continue

    if deleted:
        log.info("gc_stale_tmp: deleted %d stale entr(y/ies) under %s", deleted, target_dir)
    return deleted
