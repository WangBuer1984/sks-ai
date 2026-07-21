"""拆视频/拆账号 + 热榜 + precheck 路由。

五个端点（全部 verify_service_token 守卫，§5.1 内网唯一出口）：
- ``POST /ai/analyze/precheck {url}`` 同步：封装 tikhub.precheck → {reachable, video_count}
  （Java 预扣额度门槛，Task 3.3）。
- ``GET /ai/hot_board`` 同步：封装 tikhub.hot_board → 列表（Task 1.7 HotTopicJob 消费）。
- ``POST /ai/analyze/video/text {task_id, transcript}`` 同步：structure_video → 结构或
  {blocked:true}（UGC/LLM 输出命中安全，不写 result，Java 决策）。
- ``POST /ai/analyze/video/link {task_id, url}`` 202：endpoint 先写 running+updated_at，
  BackgroundTasks 跑 analyze_video_link（transcribe→结构化→done/failed）。
- ``POST /ai/analyze/account {task_id, url}`` 202：endpoint 先写 running+updated_at，
  BackgroundTasks 跑 analyze_account（TOP20→逐条→三层→done/partial/failed）。

202-before-background 不变量：endpoint 在 add_task 前显式 update_task(running)，
保证 Java 轮询看到 running 而非 stale-queued（§4.3 timeout 判定靠 updated_at，由 update_task
内部 SET updated_at=now() 保证）。BackgroundTasks 是进程内执行，Python 重启不续跑——靠 Java
轮询的超时/停滞判定兜底退款（Task 3.3）。

无流式（硬不变量）：202 返回 {task_id}，后台进度直写 analyze_task，不向 client stream。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import verify_service_token
from app.datasource import DataSourceError
from app.datasource.tikhub import hot_board as _hot_board
from app.datasource.tikhub import precheck as _precheck
from app.skills.account_analyze import graph as account_graph
from app.skills.account_analyze.graph import analyze_account
from app.skills.video_analyze import graph as video_graph
from app.skills.video_analyze.graph import analyze_video_link, structure_video

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(verify_service_token)])

# 模块级别名——测试 monkeypatch 目标（app.api.analyze.precheck / .hot_board /
# .structure_video / .analyze_video_link / .analyze_account）
precheck = _precheck
hot_board = _hot_board


# ---- /ai/analyze/precheck --------------------------------------------------

class PrecheckRequest(BaseModel):
    url: str


class PrecheckResponse(BaseModel):
    reachable: bool
    video_count: int


@router.post("/analyze/precheck", response_model=PrecheckResponse)
async def post_precheck(req: PrecheckRequest) -> PrecheckResponse:
    try:
        result = await precheck(req.url)
    except DataSourceError as e:
        log.warning("precheck failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PRECHECK_FAILED", "message": str(e)[:200]},
        )
    return PrecheckResponse(reachable=result["reachable"], video_count=result["video_count"])


# ---- /ai/hot_board ---------------------------------------------------------

class HotItemResponse(BaseModel):
    title: str
    hot_index: int
    video_count: int


@router.get("/hot_board", response_model=list[HotItemResponse])
async def get_hot_board() -> list[HotItemResponse]:
    try:
        items = await hot_board()
    except DataSourceError as e:
        log.warning("hot_board failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "HOT_BOARD_FAILED", "message": str(e)[:200]},
        )
    return [
        HotItemResponse(title=i.title, hot_index=i.hot_index, video_count=i.video_count)
        for i in items
    ]


# ---- /ai/analyze/video/text ------------------------------------------------

class VideoTextRequest(BaseModel):
    task_id: int
    transcript: str


@router.post("/analyze/video/text")
async def post_video_text(req: VideoTextRequest) -> dict[str, Any]:
    """同步结构化：transcript UGC 过审 → LLM 结构化 → 输出过审 → 写 done+result → 返回。

    命中安全返回 {blocked: true}（不写 result，Java 决策退/不退）。
    """
    result = await structure_video(task_id=req.task_id, transcript=req.transcript)
    return result


# ---- /ai/analyze/video/link ------------------------------------------------

class VideoLinkRequest(BaseModel):
    task_id: int
    url: str


@router.post("/analyze/video/link", status_code=status.HTTP_202_ACCEPTED)
async def post_video_link(req: VideoLinkRequest, background_tasks: BackgroundTasks) -> dict[str, int]:
    """202：先写 running+updated_at，再 BackgroundTasks 跑 transcribe→结构化→done/failed。

    立即返回 {task_id}；后台进度/结果直写 analyze_task（无流式）。
    """
    # 202-before-background：endpoint 在调度前显式写 running，Java 轮询看到 running 而非
    # stale-queued。updated_at=now() 由 update_task 内部保证。
    await video_graph.update_task(req.task_id, status="running", progress=0)
    background_tasks.add_task(analyze_video_link, req.task_id, req.url)
    return {"task_id": req.task_id}


# ---- /ai/analyze/account ---------------------------------------------------

class AccountRequest(BaseModel):
    task_id: int
    url: str


@router.post("/analyze/account", status_code=status.HTTP_202_ACCEPTED)
async def post_account(req: AccountRequest, background_tasks: BackgroundTasks) -> dict[str, int]:
    """202：先写 running+updated_at，再 BackgroundTasks 跑 TOP20→逐条→三层→done/partial/failed。"""
    await account_graph.update_task(req.task_id, status="running", progress=0)
    background_tasks.add_task(analyze_account, req.task_id, req.url)
    return {"task_id": req.task_id}
