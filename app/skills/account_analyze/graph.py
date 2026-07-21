"""拆账号 skill：TikHub TOP20 → 逐条转写+结构化 → 规律归纳 → 三层结果。

入口（被 app/api/analyze.py 调用）：
- ``analyze_account(task_id, url)`` **后台**（FastAPI BackgroundTasks）：
  set running → account_top_videos(url, n=20) → 逐条：心跳 + transcribe(download_url) +
  结构化（account_analyze_item）→ 写 benchmark_video 行 + 进度递增 → 全部完成后
  规律归纳（account_analyze_summary，深度归纳/迁移建议）→ 三层 result 写 analyze_task，
  TOP20 明细写 benchmark_video。异常 → partial/failed。

模型档位（MODEL_FOR）：
- account_analyze_item: glm-4.5-air（轻量抽取，逐条结构化快省）。
- account_analyze_summary: glm-4.7 thinking on（深度归纳/归因，需全局推理）。

进度语义（LOAD-BEARING，Task 3.3 按比例退款依赖）：
``progress = floor(已完成条数 / 总条数 × 100)``，整数 0-100。「已完成」= 该条转写+结构化
全跑完并写 benchmark_video 行。转写完但结构化没做**不**算。逐条完成后递增更新 progress。

终态语义：
- ``done``：全部条目成功 + summary 成功。
- ``partial``：部分条目失败（终态，Python 不再更新该行）——progress=已完成比例，
  result 仍写三层（在成功条目上归纳）+ videos 摘要，error 写失败条数。Java 按比例退款一次。
- ``failed``：全量 scrape DataSourceError / 所有条目失败 / summary 致命错误 → 全额退款。

心跳：每条 transcribe 用 ``_transcribe_with_heartbeat`` 包裹，轮询间隙 touch updated_at，
防 Java 5min running-timeout 误判（与 video_analyze 同实现）。

模块级别名 ``chat`` / ``check`` / ``transcribe`` / ``account_top_videos`` / ``update_task`` /
``insert_benchmark_video`` / ``heartbeat`` 是测试 monkeypatch 目标。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.datasource import DataSourceError
from app.datasource.tikhub import account_top_videos as _account_top_videos
from app.datasource.tikhub import VideoMeta
from app.llm.client import glm_client
from app.safety.content_safety import check as _check
from app.skills.analyze_store import heartbeat as _heartbeat
from app.skills.analyze_store import insert_benchmark_video as _insert_benchmark_video
from app.skills.analyze_store import update_task as _update_task
from app.datasource.transcribe import transcribe as _transcribe

log = logging.getLogger(__name__)

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat
check = _check
transcribe = _transcribe
account_top_videos = _account_top_videos
update_task = _update_task
insert_benchmark_video = _insert_benchmark_video
heartbeat = _heartbeat

# 心跳间隔：与 video_analyze 一致，短于 Java running-timeout 5min。
HEARTBEAT_INTERVAL = 60.0
_TOP_N = 20

_ITEM_FIELDS = ("structure", "why_hot", "framework", "diff_hint")
_SUMMARY_FIELDS = ("account_profile", "patterns", "migration_advice")


# ---- 结构化输出 schema ----------------------------------------------------

_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "structure": {"type": "string", "description": "该条视频文案结构拆解"},
        "why_hot": {"type": "string", "description": "该条爆火原因"},
        "framework": {"type": "string", "description": "可复用叙事框架"},
        "diff_hint": {"type": "string", "description": "迁移到本账号的差异化提示"},
    },
    "required": ["structure", "why_hot", "framework", "diff_hint"],
}

_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "account_profile": {"type": "string", "description": "账号画像归纳（人设/定位/受众）"},
        "patterns": {"type": "string", "description": "规律归纳（选题/节奏/结构共性）"},
        "migration_advice": {"type": "string", "description": "迁移到本账号的具体建议"},
    },
    "required": ["account_profile", "patterns", "migration_advice"],
}


# ---- prompt ----------------------------------------------------------------

def _build_item_messages(transcript: str) -> list[dict[str, str]]:
    system = (
        "你是一名口播视频拆解分析师。给你单条视频的转写文案，输出结构化拆解："
        "structure（文案结构）、why_hot（爆火原因）、framework（可复用叙事框架）、"
        "diff_hint（迁移到其他账号的差异化提示）。基于文本，不臆造，不含违禁内容。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"转写文案：\n{transcript}\n\n请输出四字段结构化拆解。"},
    ]


def _build_summary_messages(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    items_text = "\n\n".join(
        f"视频{i+1}：结构={it.get('structure', '')}；框架={it.get('framework', '')}；"
        f"爆火原因={it.get('why_hot', '')}"
        for i, it in enumerate(items)
    )
    system = (
        "你是一名账号拆解分析师。基于该账号 TOP 视频的逐条结构化拆解，做全局归纳："
        "account_profile（账号画像：人设/定位/受众）、patterns（规律归纳：选题/节奏/结构共性）、"
        "migration_advice（迁移到其他账号的具体可执行建议）。不得包含违禁内容。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"逐条拆解：\n{items_text}\n\n请输出账号画像/规律归纳/迁移建议三层。"},
    ]


# ---- 内部：单条结构化 ------------------------------------------------------

async def _structure_item(transcript: str) -> dict[str, Any] | None:
    """单条结构化（account_analyze_item）+ LLM 输出过审。命中安全返回 None。

    调用方负责先对 transcript 做 UGC 安全检查。
    """
    messages = _build_item_messages(transcript)
    result = await chat("account_analyze_item", messages, json_schema=_ITEM_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    text = " ".join(str(result.get(k, "")) for k in _ITEM_FIELDS)
    if not await check(text):
        return None
    return {k: result.get(k, "") for k in _ITEM_FIELDS}


async def _summarize(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """规律归纳（account_analyze_summary，thinking on）+ 输出过审。命中安全返回 None。"""
    messages = _build_summary_messages(items)
    result = await chat("account_analyze_summary", messages, json_schema=_SUMMARY_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    text = " ".join(str(result.get(k, "")) for k in _SUMMARY_FIELDS)
    if not await check(text):
        return None
    return {k: result.get(k, "") for k in _SUMMARY_FIELDS}


# ---- 心跳包裹的转写（与 video_analyze 同实现，保持独立便于单测） -----------

async def _transcribe_with_heartbeat(task_id: int, download_url: str) -> str:
    """transcribe + 周期心跳：长转写期间每 HEARTBEAT_INTERVAL touch updated_at。"""
    task = asyncio.create_task(transcribe(download_url))
    while True:
        await heartbeat(task_id)
        if task.done():
            return await task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            continue


# ---- 入口：后台异步 --------------------------------------------------------

async def analyze_account(task_id: int, url: str) -> None:
    """后台：running → TOP20 → 逐条转写+结构化→benchmark_video → summary → done/partial/failed。

    状态语义见模块 docstring。endpoint 在 202 前已写一次 running，本函数开头再写一次
    保证 background 启动瞬间 updated_at 最新。
    """
    await update_task(task_id, status="running", progress=0)

    # 1. 全量 scrape——DataSourceError 直接 failed（Java 全额退款）
    try:
        videos = await account_top_videos(url, n=_TOP_N)
    except DataSourceError as e:
        await update_task(task_id, status="failed", error=str(e))
        return
    if not videos:
        await update_task(task_id, status="failed", error="no videos found for account")
        return

    total = len(videos)
    done = 0
    structured: list[dict[str, Any]] = []
    video_summary: list[dict[str, Any]] = []

    for v in videos:
        try:
            # UGC 安全（transcript 来自抖音创作者，仍按 UGC 处理 §5.1）
            transcript = await _transcribe_with_heartbeat(task_id, v.download_url)
            if not await check(transcript):
                log.warning("account item transcript blocked by safety, skipping: %s", v.title)
                continue  # 本条不算完成
            item = await _structure_item(transcript)
            if item is None:
                log.warning("account item structured output blocked by safety, skipping: %s", v.title)
                continue
        except DataSourceError as e:
            log.warning("account item transcribe failed, skipping: %s", e)
            continue
        except Exception as e:  # noqa: BLE001 — 单条 LLM/未预期错误跳过，不让一条拖垮整任务
            log.exception("account item unexpected error, skipping")
            continue

        # 写 benchmark_video 明细行 + 累计进度
        try:
            await insert_benchmark_video(
                task_id, v.title, v.play_count, v.fav_count, transcript, item,
            )
        except Exception:  # noqa: BLE001 — benchmark 写失败不抹掉已做的结构化
            log.exception("insert_benchmark_video failed, continuing")
        done += 1
        structured.append(item)
        video_summary.append({
            "title": v.title, "play_count": v.play_count, "fav_count": v.fav_count,
        })
        progress = int(done * 100 / total)
        await update_task(task_id, progress=progress)

    # 全部条目失败 → failed
    if done == 0:
        await update_task(
            task_id, status="failed",
            error=f"all {total} items failed during transcribe/structure",
        )
        return

    progress = int(done * 100 / total)

    # 2. 规律归纳（thinking on）——summary 失败/命中 → partial（条目明细已写 benchmark）
    try:
        summary = await _summarize(structured)
    except Exception as e:  # noqa: BLE001 — summary LLM 失败：条目已产出，降级 partial
        log.exception("account summary failed, marking partial")
        await update_task(
            task_id, status="partial", progress=progress,
            result={"videos": video_summary},
            error=f"summary generation failed: {type(e).__name__}: {e}",
        )
        return

    if summary is None:
        await update_task(
            task_id, status="partial", progress=progress,
            result={"videos": video_summary},
            error="summary blocked by content safety",
        )
        return

    result = {
        "account_profile": summary.get("account_profile", ""),
        "patterns": summary.get("patterns", ""),
        "migration_advice": summary.get("migration_advice", ""),
        "videos": video_summary,
    }
    final_status = "done" if done == total else "partial"
    error = None if final_status == "done" else f"{total - done} of {total} items failed"
    await update_task(
        task_id, status=final_status, progress=progress,
        result=result, error=error,
    )
