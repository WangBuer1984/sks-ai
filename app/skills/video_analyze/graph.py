"""拆视频 skill：单条文案 → 结构化拆解（structure / why_hot / framework / diff_hint）。

两条入口（被 app/api/analyze.py 调用）：
- ``structure_video(task_id, transcript)`` **同步**：LLM 结构化 → 写
  ``analyze_task(status='done', progress=100, result)`` → 返回结构。
- ``analyze_video_link(task_id, url)`` **后台**（FastAPI BackgroundTasks）：set running+updated_at
  → transcribe(url)（带心跳）→ 结构化 → done+result（result 含四字段 + ``transcript`` 全文）。
  转写 DataSourceError → failed+error。

**不做阿里云内容安全**：解析视频的转写/LLM 产出是业务分析对象（含竞品引流话术等），
过审会误杀正常拆解；内容安全留给文案生成等用户可见创作链路。

模型档位：``video_analyze`` 走 glm-4.7 thinking off（MODEL_FOR，轻量抽取/归纳——本 skill
是单条结构化，非深度归纳，按 §5 选 thinking off）。

进度语义：
- ``video/link``（异步）：启动 5 → resolve 20 → 转写管内 20–85（里程碑 + 每 3s 缓增，
  避免长时间停在 20）→ 结构化前 90 → done 100。失败仍全额退，中间值不参与按比例退款。
- ``video/text``（同步）：一次写 done+100（无轮询条）。
- 拆账号仍用「已完成条数/总数」口径（LOAD-BEARING，按比例退款）。

心跳：``transcribe``（Qwen 管线，最长约 20min）期间 ``run_with_heartbeat``
每 ``HEARTBEAT_INTERVAL``（60s）touch 一次 ``updated_at = now()``，防止 Java
5min running-timeout 把正在转写的任务判为停滞。实现见
``app.datasource.media.heartbeat``（与 account_analyze 共享）。

模块级别名 ``chat`` / ``transcribe`` / ``update_task`` / ``heartbeat`` 是测试
monkeypatch 目标（app.skills.video_analyze.graph.*），与 script_gen / card_gen 同模式。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from app.datasource import DataSourceError
from app.datasource.media.constants import HEARTBEAT_INTERVAL
from app.datasource.media.heartbeat import run_with_heartbeat
from app.datasource.tikhub import resolve_media as _resolve_media
from app.datasource.transcribe import transcribe as _transcribe
from app.llm.client import glm_client
from app.skills.analyze_store import heartbeat as _heartbeat
from app.skills.analyze_store import update_task as _update_task

log = logging.getLogger(__name__)

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat
transcribe = _transcribe
update_task = _update_task
heartbeat = _heartbeat
resolve_media = _resolve_media

# 心跳间隔：长转写期间每 N 秒 touch updated_at，短于 Java running-timeout 5min。
# 自 ``app.datasource.media.constants`` import（共享常量）；调用 ``run_with_heartbeat``
# 时传 ``interval=HEARTBEAT_INTERVAL``，故测试 monkeypatch ``vg.HEARTBEAT_INTERVAL`` 仍生效。

# 转写阶段进度：对齐前端 3s 轮询缓增；真实里程碑可超越缓增上限。
_PROGRESS_CREEP_INTERVAL = 3.0
_PROGRESS_CREEP_CAP = 72
_TRANSCRIBE_PROGRESS_LO = 20
_TRANSCRIBE_PROGRESS_HI = 85


# ---- 结构化输出 schema ----------------------------------------------------

VIDEO_STRUCTURE_SCHEMA: dict[str, Any] = {
    "title": "video_analyze",
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
        "基于文本，不臆造。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"转写文案：\n{transcript}\n\n请输出四字段结构化拆解。"},
    ]


# ---- 内部：结构化 ----------------------------------------------------------

async def _structure_transcript(transcript: str) -> dict[str, Any]:
    """调 LLM 结构化，返回四字段 dict。不做内容安全。"""
    messages = _build_messages(transcript)
    result = await chat("video_analyze", messages, json_schema=VIDEO_STRUCTURE_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    return {k: result.get(k, "") for k in _STRUCT_FIELDS}


# ---- 心跳包裹的转写 --------------------------------------------------------
# 实现已提取至 ``app.datasource.media.heartbeat.run_with_heartbeat``（与
# account_analyze 共享）。此处调用时在调用点构建 ``transcribe(media)`` coro，
# 便于测试 monkeypatch ``vg.transcribe`` 别名后 patched 函数被实际调用；并传
# ``interval=HEARTBEAT_INTERVAL``（模块级，可被测试 patch 缩短）。


# ---- 入口 1：同步结构化（/ai/analyze/video/text） ---------------------------

async def structure_video(task_id: int, transcript: str) -> dict[str, Any]:
    """LLM 结构化 → 写 done+result → 返回结构。

    不做阿里云内容安全；不再返回 ``{blocked: True}``。
    """
    result = await _structure_transcript(transcript)
    await update_task(task_id, status="done", progress=100, result=result)
    return result


# ---- 入口 2：后台异步（/ai/analyze/video/link） ----------------------------

async def analyze_video_link(task_id: int, url: str) -> None:
    """后台：running → resolve → transcribe(心跳+进度) → 结构化 → done；失败 → failed。

    转写期间：管线里程碑映射 20–85，并每 3s 缓增（单调不回退），避免进度条长期卡死。
    """
    await update_task(task_id, status="running", progress=5)
    progress_state = {"p": 5}
    progress_lock = asyncio.Lock()

    async def _set_progress(p: int) -> None:
        """单调推进任务 progress（永不回退）。"""
        p = max(0, min(99, int(p)))  # 100 仅在最终 done 写入
        async with progress_lock:
            if p <= progress_state["p"]:
                return
            progress_state["p"] = p
            await update_task(task_id, progress=p)

    async def _on_transcribe_frac(frac: float) -> None:
        lo, hi = _TRANSCRIBE_PROGRESS_LO, _TRANSCRIBE_PROGRESS_HI
        await _set_progress(lo + int(max(0.0, min(1.0, frac)) * (hi - lo)))

    async def _creep_during_transcribe() -> None:
        """下载/转码等长等待无里程碑时，每 3s +2，封顶 72，防止用户以为卡死。"""
        try:
            while True:
                await asyncio.sleep(_PROGRESS_CREEP_INTERVAL)
                async with progress_lock:
                    cur = progress_state["p"]
                    if cur >= _PROGRESS_CREEP_CAP:
                        continue
                    nxt = min(cur + 2, _PROGRESS_CREEP_CAP)
                    if nxt > cur:
                        progress_state["p"] = nxt
                        await update_task(task_id, progress=nxt)
        except asyncio.CancelledError:
            raise

    creep_task: asyncio.Task[None] | None = None
    try:
        ref = await resolve_media(url)
        await _set_progress(20)
        creep_task = asyncio.create_task(_creep_during_transcribe())
        transcript = await run_with_heartbeat(
            task_id,
            transcribe(ref, on_progress=_on_transcribe_frac),
            interval=HEARTBEAT_INTERVAL,
        )
        await _set_progress(90)
        result = await _structure_transcript(transcript)
        # transcript 随 result 落库：前端结果页要展示文案全文，而链接流的转写以前用完即丢。
        # 注意是在结构化**之后**注入，不进 VIDEO_STRUCTURE_SCHEMA——否则等于让 LLM 复述全文。
        result["transcript"] = transcript
    except DataSourceError as e:
        # 数据源失败（下载/转写/ASR）也打日志——以前只写 DB error 列，sks-ai 日志看不到，
        # 加上 Java poller 用固定文案覆盖 error，排查时 DB 和 log 双盲。
        log.warning("video_link failed (DataSourceError): %s", e)
        await update_task(task_id, status="failed", error=str(e))
        return
    except Exception as e:  # noqa: BLE001 — LLM/未预期错误统一 failed，不让任务卡 running
        log.exception("video_link unexpected error, marking failed")
        await update_task(task_id, status="failed", error=f"{type(e).__name__}: {e}")
        return
    finally:
        if creep_task is not None:
            creep_task.cancel()
            with suppress(asyncio.CancelledError):
                await creep_task

    await update_task(task_id, status="done", progress=100, result=result)
