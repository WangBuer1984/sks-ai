"""三路信号量懒单例：ASR / download / convert。

懒创建（首次调用时构造 ``asyncio.Semaphore``）——不在 import 时构造，避免在
测试 runner 捕获到错误的 event loop。每个 getter 跨调用返回同一实例。

**设计约束**：``recognize_wav``（Task 4）**不**在内部 acquire ``asr_sem``。
速率限制由 facade（Task 5 ``transcribe.py``）独占持有，避免双重 acquire /
死锁。本模块仅提供 getter；使用方在 Task 5 才接入。

初值（PRD §11.2）：
  - ASR 并发：3（DashScope qwen3-asr-flash 同步接口，按量计费，限流保守）
  - download 并发：5
  - convert（ffmpeg）并发：4
"""

from __future__ import annotations

import asyncio

_asr_sem: asyncio.Semaphore | None = None
_download_sem: asyncio.Semaphore | None = None
_convert_sem: asyncio.Semaphore | None = None


def get_asr_semaphore() -> asyncio.Semaphore:
    """ASR 调用并发信号量（默认 3）。跨调用返回同一实例；懒创建。"""
    global _asr_sem
    if _asr_sem is None:
        _asr_sem = asyncio.Semaphore(3)
    return _asr_sem


def get_download_semaphore() -> asyncio.Semaphore:
    """媒体下载并发信号量（默认 5）。跨调用返回同一实例；懒创建。"""
    global _download_sem
    if _download_sem is None:
        _download_sem = asyncio.Semaphore(5)
    return _download_sem


def get_convert_semaphore() -> asyncio.Semaphore:
    """音频转换（ffmpeg）并发信号量（默认 4）。跨调用返回同一实例；懒创建。"""
    global _convert_sem
    if _convert_sem is None:
        _convert_sem = asyncio.Semaphore(4)
    return _convert_sem
