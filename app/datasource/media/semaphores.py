"""四路信号量懒单例：ASR / download / convert / channels decode。

懒创建（首次调用时构造 ``asyncio.Semaphore``）——不在 import 时构造，避免在
测试 runner 捕获到错误的 event loop。每个 getter 跨调用返回同一实例。

**设计约束**：``recognize_wav``（Task 4）**不**在内部 acquire ``asr_sem``。
速率限制由 facade（Task 5 ``transcribe.py``）独占持有，避免双重 acquire /
死锁。本模块仅提供 getter；使用方在 Task 5 才接入。

初值（PRD §11.2，线上修订 2026-08）：
  - ASR 并发：5（官方限流仅 RPM=100，见
    https://help.aliyun.com/zh/model-studio/qwen3-asr-flash ；ASR 非瓶颈——下载才是，
    见下。5 路远在 100 RPM 之下，留余量给多任务）
  - download 并发：2（**实测**：抖音/视频号 CDN 均按客户端聚合限速 ~0.7/1.1 MB/s，
    10 路并发聚合反降到 0.36/0.39 且大量超时——并发连接越多越慢。2 = 少量流水线重叠
    不触发争用惩罚；数据见 docs/spikes/cdn-download-concurrency.md）
  - convert（ffmpeg）并发：4
  - channels decode（node WASM）并发：2（峰值内存保护）
"""

from __future__ import annotations

import asyncio

_asr_sem: asyncio.Semaphore | None = None
_download_sem: asyncio.Semaphore | None = None
_convert_sem: asyncio.Semaphore | None = None
_decode_sem: asyncio.Semaphore | None = None


def get_asr_semaphore() -> asyncio.Semaphore:
    """ASR 调用并发信号量（默认 5）。跨调用返回同一实例；懒创建。

    官方 qwen3-asr-flash 限流仅 RPM=100（无并发路数列），5 路远在其下。ASR 非瓶颈
    （下载才是），5 留 RPM 余量给多任务。
    """
    global _asr_sem
    if _asr_sem is None:
        _asr_sem = asyncio.Semaphore(5)
    return _asr_sem


def get_download_semaphore() -> asyncio.Semaphore:
    """媒体下载并发信号量（默认 2）。跨调用返回同一实例；懒创建。

    实测抖音/视频号 CDN 客户端聚合限速，并发越多越慢（10 路 0.36/0.39 < 串行 0.7/1.1）。
    2 = 少量流水线重叠不触发争用；详情见 docs/spikes/cdn-download-concurrency.md。
    """
    global _download_sem
    if _download_sem is None:
        _download_sem = asyncio.Semaphore(2)
    return _download_sem


def get_convert_semaphore() -> asyncio.Semaphore:
    """音频转换（ffmpeg）并发信号量（默认 4）。跨调用返回同一实例；懒创建。"""
    global _convert_sem
    if _convert_sem is None:
        _convert_sem = asyncio.Semaphore(4)
    return _convert_sem


def get_decode_semaphore() -> asyncio.Semaphore:
    """视频号 WASM decode 并发信号量（默认 2）。跨调用返回同一实例；懒创建。"""
    global _decode_sem
    if _decode_sem is None:
        _decode_sem = asyncio.Semaphore(2)
    return _decode_sem
