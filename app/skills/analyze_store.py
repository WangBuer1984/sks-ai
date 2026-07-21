"""analyze_task / benchmark_video 直写工具（asyncpg，无 ORM）。

设计文档 §4.3：Python skill 把进度/结果**直接写回 Java 拥有的 analyze_task 表**
（单库，单 task_id 跨整条链）。本模块是两个 skill 共用的 DB 薄层。

**LOAD-BEARING 不变量**（Task 3.3 按比例退款依赖）：
- 每次 UPDATE analyze_task **必须显式 `SET updated_at = now()`**——PG 无自动更新触发器，
  Java 的 running-timeout（5min 无 updated_at）判定靠这列。漏写会让 Java 把正在跑的任务
  判为停滞并退款给用户，造成「Python 在干活、用户被退款」的对账错配。本模块所有 UPDATE
  都把 `updated_at = now()` 放在 SET 子句首位，不可删。
- `progress` 取值整数 0-100 = floor(已完成条数 / 总条数 × 100)；「已完成」= 该条转写+结构化
  全跑完，转写完但结构化没做**不**算完成。

模块级 `get_pool` 是测试 monkeypatch 目标（指向 app.db.get_pool 的别名），
测试注入假 pool 断言 SQL；生产由 main.py init_pool 起池。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.db import get_pool  # noqa: F401  — 模块级别名，测试 monkeypatch 目标

log = logging.getLogger(__name__)


async def update_task(
    task_id: int,
    *,
    status: str | None = None,
    progress: int | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """UPDATE analyze_task，显式 SET updated_at = now()（每次必带，见模块 docstring）。

    只更新传入的字段；updated_at 永远写。error 截断到 300 字符（analyze_task.error
    VARCHAR(300)）。result 以 jsonb 落库（$N::jsonb）。

    DB 不可达时抛异常——上游 background 编排器用 try 包住并尝试写 failed（best-effort）。
    """
    pool = await get_pool()
    sets: list[str] = ["updated_at = now()"]  # 首位不可删
    args: list[Any] = []
    idx = 1
    if status is not None:
        sets.append(f"status = ${idx}")
        args.append(status)
        idx += 1
    if progress is not None:
        sets.append(f"progress = ${idx}")
        args.append(progress)
        idx += 1
    if result is not None:
        sets.append(f"result = ${idx}::jsonb")
        args.append(json.dumps(result, ensure_ascii=False))
        idx += 1
    if error is not None:
        sets.append(f"error = ${idx}")
        args.append(error[:300])
        idx += 1
    args.append(task_id)
    sql = (
        f"UPDATE analyze_task SET {', '.join(sets)} WHERE id = ${idx}"
    )
    await pool.execute(sql, *args)


async def heartbeat(task_id: int) -> None:
    """仅 touch updated_at = now()，用于长转写期间保活（防 Java running-timeout 误判）。

    单语句 `UPDATE analyze_task SET updated_at = now() WHERE id = $1`——
    本模块的 update_task 也会带 updated_at，但心跳在 transcribe 轮询间隙**独立**调用，
    不写其他字段，避免干扰 progress/status 语义。
    """
    pool = await get_pool()
    await pool.execute(
        "UPDATE analyze_task SET updated_at = now() WHERE id = $1",
        task_id,
    )


async def insert_benchmark_video(
    task_id: int,
    title: str,
    play_count: int,
    fav_count: int,
    transcript: str,
    structure: dict[str, Any],
) -> None:
    """写 benchmark_video 行（TOP20 明细）。analyze_task_id FK→analyze_task(id)。"""
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO benchmark_video "
        "(analyze_task_id, title, play_count, fav_count, transcript, structure) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
        task_id,
        title,
        play_count,
        fav_count,
        transcript,
        json.dumps(structure, ensure_ascii=False),
    )
