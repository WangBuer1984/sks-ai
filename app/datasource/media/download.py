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
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.datasource import DataSourceError

log = logging.getLogger(__name__)

# httpx 请求超时（秒）。长音频可能较大，60s 覆盖首字节 + 传输；超大文件按需再调。
_DOWNLOAD_TIMEOUT = 60.0
# 临时文件名前缀——``gc_stale_tmp`` 按此前缀匹配清扫，避免误删其他模块的 temp 文件。
_TMP_PREFIX = "sks_asr_dl_"


async def download_url(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Path:
    """下载直链到 temp 文件，返回 ``Path``。

    - ``headers``：下载所需请求头（Referer/UA 等），默认 None。
    - ``client``：测试注入 ``MockTransport`` 客户端；生产传 None，内部按
      timeout/``follow_redirects=True`` 新建。
    - 非 2xx / 传输异常 / 超时 → ``DataSourceError``（绝不冒泡裸 httpx/网络异常）。
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=_DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        )

    try:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            # 传输/超时/协议异常 → 数据源故障（不冒泡裸 httpx 异常）。
            raise DataSourceError(f"download transport error for {url}: {exc}") from exc

        if resp.status_code // 100 != 2:
            raise DataSourceError(
                f"download failed for {url}: HTTP {resp.status_code}"
            )

        fd, tmp_path = tempfile.mkstemp(
            prefix=_TMP_PREFIX,
            dir=settings.ASR_TMP_DIR or None,
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(resp.content)
        except OSError:
            # 写盘失败也归一为数据源故障（磁盘满/权限等），调用方按统一路径处理。
            raise DataSourceError(f"failed to write tmp file for {url}")

        log.info("downloaded %s -> %s (%d bytes)", url, tmp_path, len(resp.content))
        return Path(tmp_path)
    finally:
        if own_client:
            await client.aclose()


def gc_stale_tmp(*, max_age_hours: float = 2.0) -> int:
    """清扫陈旧的 ``sks_asr_dl_*`` 临时文件，返回删除数量。

    扫描目录：``settings.ASR_TMP_DIR``（空 → 系统 tempfile 目录）。
    - 仅删前缀 ``sks_asr_dl_`` 且 mtime 早于 ``max_age_hours`` 小时前的文件。
    - best-effort：缺目录返回 0；单文件 ``OSError`` 被吞，继续清扫其余。
    - 不会 crash，确保可安全周期调用（Task 7 心跳 / 启动钩子）。
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
        if not name.startswith(_TMP_PREFIX):
            continue
        path = os.path.join(target_dir, name)
        try:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_mtime < cutoff:
                os.unlink(path)
                deleted += 1
        except OSError as exc:
            log.warning("gc_stale_tmp: cannot stat/delete %s: %s", path, exc)
            continue

    if deleted:
        log.info("gc_stale_tmp: deleted %d stale file(s) under %s", deleted, target_dir)
    return deleted
