"""拆账号 skill：TikHub TOP10 → 逐条转写+结构化 → 规律归纳 → 三层结果。

入口（被 app/api/analyze.py 调用）：
- ``analyze_account(task_id, url)`` **后台**（FastAPI BackgroundTasks）：
  set running → account_top_videos(url, n=10) → 有界并发（默认 3）逐条：心跳 +
  transcribe + 结构化 → 写 benchmark_video + 进度递增 → 全部完成后规律归纳 →
  三层 result。异常 → partial/failed。

模型档位（MODEL_FOR）：
- account_analyze_item: glm-4.5-air（轻量抽取，逐条结构化快省）。
- account_analyze_summary: glm-4.7 thinking off（thinking 开与结构化输出在 GLM-4.7
  冲突——function_calling 触发 1210、json_schema 返回散文、json_mode 不强制 schema；
  关 thinking 保 function_calling 强制字段类型，见 git log）。

进度语义（LOAD-BEARING，Task 3.3 按比例退款依赖）：
``progress = floor(已完成条数 / 总条数 × 100)``，整数 0-100。「已完成」= 该条转写+结构化
全跑完并写 benchmark_video 行。转写完但结构化没做**不**算。逐条完成后递增更新 progress。

终态语义：
- ``done``：全部条目成功 + summary 成功。
- ``partial``：部分条目失败（终态，Python 不再更新该行）——progress=已完成比例，
  result 仍写三层（在成功条目上归纳）+ videos 摘要，error 写失败条数。Java 按比例退款一次。
- ``failed``：全量 scrape DataSourceError / 所有条目失败 / summary 致命错误 → 全额退款。

心跳：``account_top_videos``（视频号可多页）与每条 ``transcribe`` 均用
``run_with_heartbeat`` 包裹，轮询间隙 touch updated_at，防 Java 5min
running-timeout 误判（与 video_analyze 共享实现，见
``app.datasource.media.heartbeat``）。

**不做阿里云内容安全**：拆账号解析的是竞品/对标视频转写与结构化产出，过审会误杀
（引流话术、超长文案等）；内容安全留给文案生成等用户可见创作链路。

模块级别名 ``chat`` / ``transcribe`` / ``account_top_videos`` / ``update_task`` /
``insert_benchmark_video`` / ``heartbeat`` 是测试 monkeypatch 目标。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.datasource import DataSourceError
from app.datasource.media.constants import ACCOUNT_ITEM_CONCURRENCY, HEARTBEAT_INTERVAL
from app.datasource.media.heartbeat import run_with_heartbeat
from app.datasource.tikhub import account_top_videos as _account_top_videos
from app.datasource.tikhub import video_meta_to_media_ref
from app.datasource.tikhub import VideoMeta
from app.llm.client import glm_client
from app.skills.analyze_store import heartbeat as _heartbeat
from app.skills.analyze_store import insert_benchmark_video as _insert_benchmark_video
from app.skills.analyze_store import update_task as _update_task
from app.datasource.transcribe import transcribe as _transcribe

log = logging.getLogger(__name__)

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat
transcribe = _transcribe
account_top_videos = _account_top_videos
update_task = _update_task
insert_benchmark_video = _insert_benchmark_video
heartbeat = _heartbeat

# 心跳间隔：与 video_analyze 一致，短于 Java running-timeout 5min。
# 自 ``app.datasource.media.constants`` import（共享常量）；调用 ``run_with_heartbeat``
# 时传 ``interval=HEARTBEAT_INTERVAL``，故测试 monkeypatch ``ag.HEARTBEAT_INTERVAL`` 仍生效。
_TOP_N = 10
# 有界并发：模块级别名便于测试 monkeypatch 为 1（串行）验证进度语义。
_ITEM_CONCURRENCY = ACCOUNT_ITEM_CONCURRENCY

_ITEM_FIELDS = ("structure", "why_hot", "framework", "diff_hint")
_SUMMARY_FIELDS = ("account_profile", "patterns", "migration_advice")


# ---- 结构化输出 schema ----------------------------------------------------

_ITEM_SCHEMA: dict[str, Any] = {
    "title": "account_analyze_item",
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
    "title": "account_analyze_summary",
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


# ---- 内部：原视频链接 ------------------------------------------------------

def _video_url(v: VideoMeta) -> str | None:
    """构造该条的作品链接，无法构造返 None（不编造）。

    抖音：``aweme_id`` 拼标准作品页。视频号：一律 None——列表项只给到
    ``media.full_url``（带鉴权 token 的加密 CDN 直链，需 decode_key 且短时失效），
    拼网页形态（``channels.weixin.qq.com/web/pages/feed?eid=…|?oid=…`` /
    ``weixin.qq.com/sph/<短码>``）需要 ``export_id`` / ``oid`` / sph 短码或
    ``object_id``+``object_nonce_id`` 配对，``fetch_user_videos`` 是否带这些字段
    未验证（spec §3.1 backlog）。前端据此让详情态输入框留空，结果区照常。
    """
    if v.platform == "douyin" and v.aweme_id:
        return f"https://www.douyin.com/video/{v.aweme_id}"
    return None


# ---- 内部：单条结构化 ------------------------------------------------------

async def _structure_item(transcript: str) -> dict[str, Any]:
    """单条结构化（account_analyze_item）。不做内容安全。"""
    messages = _build_item_messages(transcript)
    result = await chat("account_analyze_item", messages, json_schema=_ITEM_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    return {k: result.get(k, "") for k in _ITEM_FIELDS}


async def _summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    """规律归纳（account_analyze_summary，thinking off——与结构化输出冲突，见 models.py 注释）。不做内容安全。"""
    messages = _build_summary_messages(items)
    result = await chat("account_analyze_summary", messages, json_schema=_SUMMARY_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    return {k: result.get(k, "") for k in _SUMMARY_FIELDS}


# ---- 心跳包裹 -------------------------------------------------------------
# 实现已提取至 ``app.datasource.media.heartbeat.run_with_heartbeat``（与
# video_analyze 共享）。调用时在调用点构建 coro（``account_top_videos(...)`` /
# ``transcribe(ref)``），便于测试 monkeypatch ``ag.account_top_videos`` /
# ``ag.transcribe`` 别名后 patched 函数被实际调用；并传
# ``interval=HEARTBEAT_INTERVAL``（模块级，可被测试 patch 缩短）。


# ---- 入口：后台异步 --------------------------------------------------------

async def analyze_account(task_id: int, url: str) -> None:
    """后台：running → TOP10 → 有界并发转写+结构化→benchmark_video → summary → done/partial/failed。

    状态语义见模块 docstring。endpoint 在 202 前已写一次 running，本函数开头再写一次
    保证 background 启动瞬间 updated_at 最新。
    """
    await update_task(task_id, status="running", progress=0)

    # 1. 全量 scrape（含视频号多页）——须心跳，慢网下可 1–3min。
    # DataSourceError → failed（Java 全额退款）
    try:
        videos = await run_with_heartbeat(
            task_id, account_top_videos(url, n=_TOP_N), interval=HEARTBEAT_INTERVAL
        )
    except DataSourceError as e:
        # 数据源失败也打日志——以前只写 DB error 列，sks-ai 日志看不到，
        # 加上 Java poller 用固定文案覆盖 error，排查时 DB 和 log 双盲。
        log.warning("account_analyze failed (DataSourceError): %s", e)
        await update_task(task_id, status="failed", error=str(e))
        return
    if not videos:
        log.warning("account_analyze failed: no videos found for account")
        await update_task(task_id, status="failed", error="no videos found for account")
        return

    total = len(videos)
    done = 0
    # 按输入顺序占位——并发完成顺序不影响回填次序（保序回填）。
    results: list[tuple[dict[str, Any], str] | None] = [None] * total
    item_sem = asyncio.Semaphore(_ITEM_CONCURRENCY)
    # insert 串行锁：保原「逐条串行避免 DB 连接池争用」不变量。insert 留在 _process_item
    # 内是为了「该条转写+结构化+写行全成即递增 progress」——原先 insert+进度递增放在 gather
    # 之后的串行回填循环，导致整段并发慢转写期间 progress 恒 0（12min 0%、视频号同路径复现）。
    insert_lock = asyncio.Lock()

    async def _process_item(idx: int, v: VideoMeta) -> None:
        async with item_sem:
            try:
                ref = video_meta_to_media_ref(v)
                transcript = await run_with_heartbeat(
                    task_id, transcribe(ref), interval=HEARTBEAT_INTERVAL
                )
                item = await _structure_item(transcript)
            except DataSourceError as e:
                log.warning("account item transcribe failed, skipping: %s", e)
                return  # continue 语义
            except Exception:  # noqa: BLE001 — 单条 LLM/未预期错误跳过，不拖垮整任务
                log.exception("account item unexpected error, skipping")
                return  # continue 语义
            # insert 逐条串行（锁）——与原回填循环同语义，仅迁入并发条目内以便逐条递增进度。
            async with insert_lock:
                try:
                    await insert_benchmark_video(
                        task_id,
                        v.title,
                        v.play_count,
                        v.collect_count or v.fav_count,
                        transcript,
                        item,
                        description=v.description or "",
                        tags=v.tags or [],
                        published_at=v.published_at,
                        like_count=v.like_count,
                        comment_count=v.comment_count,
                        share_count=v.share_count,
                        collect_count=v.collect_count or v.fav_count,
                        duration_sec=v.duration_sec,
                        author=v.author or "",
                        video_url=_video_url(v),
                    )
                except Exception:  # noqa: BLE001 — benchmark 写失败不抹掉已做的结构化
                    log.exception("insert_benchmark_video failed, continuing")
            results[idx] = (item, transcript)
            # 进度递增：该条转写+结构化+写行全成（LOAD-BEARING：Java 按比例退款 + 前端进度条）。
            # done+=1 无 await 夹在读写之间，asyncio 单线程下原子；progress 经 insert_lock 串行化单调。
            nonlocal done
            done += 1
            progress = int(done * 100 / total)
            await update_task(task_id, progress=progress)

    await asyncio.gather(*(
        _process_item(i, v) for i, v in enumerate(videos)
    ))

    # 按输入顺序累积结构化结果给 summary（保序；insert+进度递增已在 _process_item 内逐条完成）。
    structured: list[dict[str, Any]] = []
    video_summary: list[dict[str, Any]] = []
    for i, v in enumerate(videos):
        r = results[i]
        if r is None:
            continue
        item, transcript = r
        structured.append(item)
        video_summary.append({
            "title": v.title,
            "description": v.description or "",
            "tags": list(v.tags or []),
            "published_at": v.published_at,
            "play_count": v.play_count,
            "like_count": v.like_count,
            "comment_count": v.comment_count,
            "share_count": v.share_count,
            "collect_count": v.collect_count or v.fav_count,
            "fav_count": v.collect_count or v.fav_count,  # 兼容旧 Java 读 fav
            "duration_sec": v.duration_sec,
        })

    # 全部条目失败 → failed
    if done == 0:
        await update_task(
            task_id, status="failed",
            error=f"all {total} items failed during transcribe/structure",
        )
        return

    progress = int(done * 100 / total)

    # 2. 规律归纳（thinking off，见 models.py 不变量）——summary 失败 → partial（条目明细已写 benchmark）
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
