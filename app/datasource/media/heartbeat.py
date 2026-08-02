"""长任务 + 周期心跳共享实现。

提取自 ``video_analyze/graph.py`` 的 ``_transcribe_with_heartbeat`` 与
``account_analyze/graph.py`` 的 ``_run_with_heartbeat``（两者逻辑等价）。

用 ``asyncio.shield`` 保护内层 task 不被 ``wait_for`` 超时取消——超时仅跳出本轮
``wait``，下一轮继续 await 同一 task，长任务真实进度不丢。

设计要点：
- ``run_with_heartbeat`` 接受已构造的 coroutine，调用方在调用点构建 coro（便于
  在测试中 monkeypatch 模块级 ``transcribe`` / ``account_top_videos`` 别名后，
  patched 函数被实际调用）。
- ``transcribe_with_heartbeat`` 是便捷封装，内部用本模块 import 的 ``transcribe``；
  适合不需要 patch transcribe 的调用方。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from app.datasource.media.constants import HEARTBEAT_INTERVAL
from app.datasource.media.types import MediaRef
from app.datasource.transcribe import transcribe as _transcribe
from app.skills.analyze_store import heartbeat as _heartbeat


async def run_with_heartbeat(
    task_id: int,
    coro: Coroutine[Any, Any, Any],
    *,
    interval: float = HEARTBEAT_INTERVAL,
) -> Any:
    """任意长协程 + 周期心跳：每 ``interval`` 秒 touch 一次 ``updated_at = now()``。

    ``asyncio.shield`` 保证内层 task 不被 ``wait_for`` 超时取消——超时仅跳出本轮
    wait，下一轮继续 await 同一 task。``coro`` 由调用方在调用点构建（便于
    patch 模块级别名）。
    """
    task = asyncio.create_task(coro)
    while True:
        await _heartbeat(task_id)  # 本轮先 touch，再 wait
        if task.done():
            return await task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            continue  # 超时但 task 未完，循环再 touch heartbeat


async def transcribe_with_heartbeat(
    task_id: int,
    media: MediaRef | str,
    *,
    interval: float = HEARTBEAT_INTERVAL,
) -> str:
    """transcribe + 周期心跳（便捷封装）。

    内部用本模块 import 的 ``transcribe``；如需在测试中 patch 调用方模块的
    ``transcribe`` 别名，请改用 ``run_with_heartbeat(task_id, transcribe(media), ...)``。
    """
    return await run_with_heartbeat(task_id, _transcribe(media), interval=interval)


__all__ = ["run_with_heartbeat", "transcribe_with_heartbeat"]
