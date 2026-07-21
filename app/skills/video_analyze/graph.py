"""拆视频 skill：单条文案 → 结构化拆解（structure / why_hot / framework / diff_hint）。

两条入口（被 app/api/analyze.py 调用）：
- ``structure_video(task_id, transcript)`` **同步**：UGC 安全 → LLM 结构化 → 输出过审 →
  写 ``analyze_task(status='done', progress=100, result)`` → 返回结构。UGC 或输出命中安全
  返回 ``{blocked: True}``，**不写 result**（Java 决策退/不退，与 P2 interview blocked 同口径）。
- ``analyze_video_link(task_id, url)`` **后台**（FastAPI BackgroundTasks）：set running+updated_at
  → transcribe(url)（带心跳）→ 结构化 → done+result。转写 DataSourceError → failed+error。

模型档位：``video_analyze`` 走 glm-4.7 thinking off（MODEL_FOR，轻量抽取/归纳——本 skill
是单条结构化，非深度归纳，按 §5 选 thinking off）。

进度语义（LOAD-BEARING，Task 3.3 按比例退款依赖）：单条 progress 0→100，``100`` 仅在结构化
完成并写 result 后赋。无中间值（单条无中间条目概念）。

心跳：``transcribe`` 可轮询阿里云长达 10min，Java running-timeout 是 5min——``_transcribe_with_heartbeat``
在轮询间隙每 ``HEARTBEAT_INTERVAL``（60s）touch 一次 ``updated_at = now()``，防止 Java
把正在转写的任务判为停滞。

模块级别名 ``chat`` / ``check`` / ``transcribe`` / ``update_task`` / ``heartbeat`` 是测试
monkeypatch 目标（app.skills.video_analyze.graph.*），与 script_gen / card_gen 同模式。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.datasource import DataSourceError
from app.datasource.transcribe import transcribe as _transcribe
from app.llm.client import glm_client
from app.safety.content_safety import check as _check
from app.skills.analyze_store import heartbeat as _heartbeat
from app.skills.analyze_store import update_task as _update_task

log = logging.getLogger(__name__)

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat
check = _check
transcribe = _transcribe
update_task = _update_task
heartbeat = _heartbeat

# 心跳间隔：transcribe 轮询期间每 N 秒 touch updated_at，短于 Java running-timeout 5min。
HEARTBEAT_INTERVAL = 60.0


# ---- 结构化输出 schema ----------------------------------------------------

VIDEO_STRUCTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "structure": {"type": "string", "description": "视频文案结构拆解（开场/正文/结尾分段与作用）"},
        "why_hot": {"type": "string", "description": "爆火原因分析（受众/情绪/节奏/时机）"},
        "framework": {"type": "string", "description": "可复用的叙事框架（抽象套路）"},
        "diff_hint": {"type": "string", "description": "迁移到本账号的差异化提示"},
    },
    "required": ["structure", "why_hot", "framework", "diff_hint"],
}

_STRUCT_FIELDS = ("structure", "why_hot", "framework", "diff_hint")


# ---- prompt ----------------------------------------------------------------

def _build_messages(transcript: str) -> list[dict[str, str]]:
    system = (
        "你是一名口播视频拆解分析师。给你一段视频的完整转写文案，请输出结构化拆解："
        "structure（文案结构：开场钩子/正文/CTA 各自的作用与衔接）、"
        "why_hot（爆火原因：受众、情绪、节奏、时机）、"
        "framework（可复用的叙事框架，抽象成套路）、"
        "diff_hint（迁移到其他账号时的差异化建议）。"
        "基于转写文本，不要臆造，不得包含违禁内容。"
    )
    user = f"视频转写文案：\n{transcript}\n\n请输出结构化拆解（四个字段均为文本）。"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---- 内部：结构化 + 双向安全 -----------------------------------------------

async def _structure_transcript(transcript: str) -> dict[str, Any] | None:
    """UGC 已过审前提下，调 LLM 结构化 → 输出过审 → 返回结构 dict；命中返回 None。

    调用方负责先对 transcript 做 UGC 安全检查（本函数不重复 UGC 检查，避免双查）。
    返回 None 表示 LLM 输出命中安全——上游按 blocked 处理（不写 result）。
    """
    messages = _build_messages(transcript)
    result = await chat("video_analyze", messages, json_schema=VIDEO_STRUCTURE_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    # LLM 输出过审（文本部分会展示给用户，§5.1 硬不变量）
    text = " ".join(str(result.get(k, "")) for k in _STRUCT_FIELDS)
    if not await check(text):
        return None
    return {k: result.get(k, "") for k in _STRUCT_FIELDS}


# ---- 心跳包裹的转写 --------------------------------------------------------

async def _transcribe_with_heartbeat(task_id: int, download_url: str) -> str:
    """transcribe + 周期心跳：长转写（最长 10min）期间每 HEARTBEAT_INTERVAL touch updated_at。

    用 asyncio.shield 保护内层 task 不被 wait_for 超时取消——超时仅跳出本轮 wait，
    下一轮继续 await 同一 task，transcribe 真实进度不丢。
    """
    task = asyncio.create_task(transcribe(download_url))
    while True:
        await heartbeat(task_id)  # 本轮先 touch，再 wait
        if task.done():
            return await task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            continue  # 超时但 task 未完，循环再 touch heartbeat


# ---- 入口 1：同步结构化（/ai/analyze/video/text） ---------------------------

async def structure_video(task_id: int, transcript: str) -> dict[str, Any]:
    """UGC 安全 → LLM 结构化 → 输出过审 → 写 done+result → 返回结构。

    返回:
      - 成功: {structure, why_hot, framework, diff_hint}
      - blocked: {blocked: True}  （UGC 或 LLM 输出命中安全，不写 result）
    """
    # 1. UGC 安全（transcript 是用户输入/转写文本，§5.1 先审后用）
    if not await check(transcript):
        return {"blocked": True}

    # 2. LLM 结构化 + 输出过审
    result = await _structure_transcript(transcript)
    if result is None:
        return {"blocked": True}

    # 3. 写 done+result+progress=100+updated_at（update_task 内部保证 updated_at=now()）
    await update_task(task_id, status="done", progress=100, result=result)
    return result


# ---- 入口 2：后台异步（/ai/analyze/video/link） ----------------------------

async def analyze_video_link(task_id: int, url: str) -> None:
    """后台：set running → transcribe(带心跳) → 结构化 → done+result；失败 → failed+error。

    endpoint 在返回 202 前已写一次 running，本函数开头再写一次 running+updated_at，
    保证 background 启动瞬间 updated_at 是最新的（Java 看到的是 running 而非 stale-queued）。
    """
    await update_task(task_id, status="running", progress=0)
    try:
        transcript = await _transcribe_with_heartbeat(task_id, url)
        result = await _structure_transcript(transcript)
    except DataSourceError as e:
        await update_task(task_id, status="failed", error=str(e))
        return
    except Exception as e:  # noqa: BLE001 — LLM/未预期错误统一 failed，不让任务卡 running
        log.exception("video_link unexpected error, marking failed")
        await update_task(task_id, status="failed", error=f"{type(e).__name__}: {e}")
        return

    if result is None:
        # LLM 输出命中安全——按 failed 退款（粘文案版无重写路径，blocked 即终止）
        await update_task(
            task_id, status="failed",
            error="transcript or structured output blocked by content safety",
        )
        return

    await update_task(task_id, status="done", progress=100, result=result)
