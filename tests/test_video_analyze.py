"""拆视频 skill 测试：mock transcribe/llm/safety/db seam，绝不发真实网络/DB 请求。

覆盖：
- /ai/analyze/video/text 同步：UGC 安全 → LLM 结构化 → 输出过审 → analyze_task(done+result) → 返回结构
- /ai/analyze/video/text UGC 命中安全 → {blocked:true}，不调 chat、不写 result
- /ai/analyze/video/text LLM 输出命中安全 → {blocked:true}，不写 result
- /ai/analyze/video/link 202：endpoint 先写 status=running+updated_at，再 BackgroundTasks 跑 transcribe→结构化→done
- /ai/analyze/video/link 转写 DataSourceError → status=failed+error
- 202 路径在 background task 启动前已写 running（调用顺序断言）
- token 守卫（missing/wrong）
- progress 语义：单条 0→100
- 每次 UPDATE 显式 SET updated_at=now()（用真实 update_task + 假 pool 断言 SQL）
- 心跳：长转写期间 updated_at 被周期性 touch
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.datasource import DataSourceError


# ---- fakes -----------------------------------------------------------------

async def _safe(_t):
    return True


async def _unsafe(_t):
    return False


async def _fake_chat_structure(*args, **kwargs):
    """模拟 GLM 结构化输出（4 字段文本）。"""
    return {
        "structure": "开场钩子→痛点→方案→CTA",
        "why_hot": "切中受众焦虑，节奏紧凑",
        "framework": "问题-方案-引导",
        "diff_hint": "本账号可复用开场节奏",
    }


class _FakePool:
    """记录 execute SQL，用于断言 updated_at=now()。"""

    def __init__(self):
        self.execs: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.execs.append((query, args))

    async def fetch(self, query, *args):
        return []


# ---- video/text 同步路径 ----------------------------------------------------

@pytest.mark.asyncio
async def test_video_text_done_writes_result_and_returns_structure(monkeypatch):
    """UGC 安全 + LLM 输出安全 → 写 analyze_task(done, progress=100, result) → 返回结构。"""
    calls: list[dict] = []

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"task_id": task_id, "status": status, "progress": progress,
                       "result": result, "error": error})

    monkeypatch.setattr("app.skills.video_analyze.graph.check", _safe)
    monkeypatch.setattr("app.skills.video_analyze.graph.chat", _fake_chat_structure)
    monkeypatch.setattr("app.skills.video_analyze.graph.update_task", _update_task)

    from app.skills.video_analyze.graph import structure_video
    res = await structure_video(task_id=42, transcript="一段文案")

    assert res["structure"] == "开场钩子→痛点→方案→CTA"
    assert res["why_hot"]
    # 写 done + progress=100 + result + updated_at（update_task 内部保证）
    dones = [c for c in calls if c["status"] == "done"]
    assert len(dones) == 1
    assert dones[0]["progress"] == 100
    assert dones[0]["result"]["structure"] == res["structure"]
    assert dones[0]["task_id"] == 42


@pytest.mark.asyncio
async def test_video_text_blocked_ugc_returns_blocked_and_skips_llm(monkeypatch):
    """transcript 命中安全 → {blocked:true}，不调 chat、不写 result。"""
    chat_calls = {"n": 0}
    update_calls: list[dict] = []

    async def _chat(*a, **kw):
        chat_calls["n"] += 1
        return {"structure": "x"}

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        update_calls.append({"status": status, "result": result})

    monkeypatch.setattr("app.skills.video_analyze.graph.check", _unsafe)
    monkeypatch.setattr("app.skills.video_analyze.graph.chat", _chat)
    monkeypatch.setattr("app.skills.video_analyze.graph.update_task", _update_task)

    from app.skills.video_analyze.graph import structure_video
    res = await structure_video(task_id=1, transcript="违规")

    assert res == {"blocked": True}
    assert chat_calls["n"] == 0
    # 不写 done/result
    assert all(c["status"] != "done" for c in update_calls)


@pytest.mark.asyncio
async def test_video_text_blocked_llm_output_returns_blocked(monkeypatch):
    """LLM 输出命中安全 → {blocked:true}，不写 result。"""
    update_calls: list[dict] = []

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        update_calls.append({"status": status, "result": result})

    # check 第一次（UGC）安全，第二次（LLM 输出）不安全
    seq = iter([True, False])

    async def _check(_t):
        return next(seq)

    monkeypatch.setattr("app.skills.video_analyze.graph.check", _check)
    monkeypatch.setattr("app.skills.video_analyze.graph.chat", _fake_chat_structure)
    monkeypatch.setattr("app.skills.video_analyze.graph.update_task", _update_task)

    from app.skills.video_analyze.graph import structure_video
    res = await structure_video(task_id=1, transcript="一段文案")

    assert res == {"blocked": True}
    assert all(c["status"] != "done" for c in update_calls)


# ---- video/link 202 异步路径 -----------------------------------------------

@pytest.mark.asyncio
async def test_video_link_done_transcribe_then_structure_then_done(monkeypatch):
    """link：transcribe→结构化→写 done+result。progress 单条 0→100。"""
    calls: list[dict] = []

    async def _transcribe(url):
        return "转写文本"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    monkeypatch.setattr("app.skills.video_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.video_analyze.graph.check", _safe)
    monkeypatch.setattr("app.skills.video_analyze.graph.chat", _fake_chat_structure)
    monkeypatch.setattr("app.skills.video_analyze.graph.update_task", _update_task)
    # 心跳打到假 pool（不报错即可）
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.video_analyze.graph import analyze_video_link
    await analyze_video_link(task_id=7, url="https://v.douyin.com/abc")

    statuses = [c["status"] for c in calls]
    # running（bg 启动）→ done
    assert "running" in statuses
    assert statuses[-1] == "done"
    done = [c for c in calls if c["status"] == "done"][0]
    assert done["progress"] == 100
    assert done["result"]["structure"]


@pytest.mark.asyncio
async def test_video_link_failed_on_transcribe_datasource_error(monkeypatch):
    """transcribe 抛 DataSourceError → status=failed+error。"""
    calls: list[dict] = []

    async def _transcribe(url):
        raise DataSourceError("asr boom")

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    monkeypatch.setattr("app.skills.video_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.video_analyze.graph.update_task", _update_task)
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.video_analyze.graph import analyze_video_link
    await analyze_video_link(task_id=7, url="https://v/x")

    failed = [c for c in calls if c["status"] == "failed"]
    assert len(failed) == 1
    assert "asr boom" in failed[0]["error"]


@pytest.mark.asyncio
async def test_video_link_sets_running_before_background(monkeypatch):
    """202 路径：endpoint 在 background task 启动前已写 running。"""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.skills.video_analyze.graph as vg

    order: list[str] = []

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        order.append(f"update:{status}")

    async def _transcribe(url):
        order.append("transcribe")
        return "t"

    async def _chat(*a, **kw):
        order.append("chat")
        return {"structure": "s", "why_hot": "w", "framework": "f", "diff_hint": "d"}

    monkeypatch.setattr(vg, "transcribe", _transcribe)
    monkeypatch.setattr(vg, "chat", _chat)
    monkeypatch.setattr(vg, "check", _safe)
    monkeypatch.setattr(vg, "update_task", _update_task)
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    with TestClient(app) as c:
        r = c.post("/ai/analyze/video/link",
                   json={"task_id": 5, "url": "https://v"},
                   headers={"X-Service-Token": "test-secret"})
    assert r.status_code == 202
    assert r.json() == {"task_id": 5}
    # endpoint 写 running 在前，background 的 transcribe/chat 在后
    assert order[0] == "update:running"
    assert order.index("update:running") < order.index("transcribe")


# ---- SQL 不变量：每次 UPDATE 显式 SET updated_at=now() ----------------------

@pytest.mark.asyncio
async def test_every_update_task_sql_includes_updated_at_now(monkeypatch):
    """用真实 update_task + 假 pool：所有 UPDATE SQL 必须含 updated_at = now()。"""
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.analyze_store import update_task
    await update_task(1, status="running", progress=0)
    await update_task(1, status="done", progress=100, result={"x": 1})
    await update_task(1, status="failed", error="boom")
    await update_task(1, progress=50)  # 仅 progress

    assert len(pool.execs) == 4
    for query, _args in pool.execs:
        assert "updated_at = now()" in query, f"UPDATE 缺 updated_at=now(): {query}"


# ---- 心跳：长转写期间 updated_at 被周期性 touch ----------------------------

@pytest.mark.asyncio
async def test_heartbeat_touches_updated_at_during_long_transcribe(monkeypatch):
    """transcribe 长耗时（多个心跳间隔）→ heartbeat 多次写 updated_at=now()。"""
    import app.skills.video_analyze.graph as vg

    monkeypatch.setattr(vg, "HEARTBEAT_INTERVAL", 0.01)

    async def _slow_transcribe(url):
        await asyncio.sleep(0.05)
        return "慢转写结果"

    monkeypatch.setattr(vg, "transcribe", _slow_transcribe)
    monkeypatch.setattr(vg, "check", _safe)
    monkeypatch.setattr(vg, "chat", _fake_chat_structure)

    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    # update_task 也走假 pool（记录 running/done 的 SQL）
    # 不 monkeypatch update_task → 用真实实现 → 走假 pool

    await vg.analyze_video_link(task_id=9, url="https://v")

    # 心跳 SQL = "UPDATE analyze_task SET updated_at = now() WHERE id = $1"
    hb = [q for q, _ in pool.execs if q.startswith("UPDATE analyze_task SET updated_at = now()")]
    assert len(hb) >= 2, f"心跳应被多次 touch，实际 {len(hb)} 次"


# ---- 端点鉴权 ---------------------------------------------------------------

def test_video_text_requires_token(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    async def _noop(*a, **kw):
        return {"blocked": True}

    monkeypatch.setattr("app.api.analyze.structure_video", _noop)
    with TestClient(app) as c:
        r = c.post("/ai/analyze/video/text", json={"task_id": 1, "transcript": "x"})
    assert r.status_code == 422  # Header(...) 缺失


def test_video_text_wrong_token_rejected(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    async def _noop(*a, **kw):
        return {"blocked": True}

    monkeypatch.setattr("app.api.analyze.structure_video", _noop)
    with TestClient(app) as c:
        r = c.post("/ai/analyze/video/text",
                   json={"task_id": 1, "transcript": "x"},
                   headers={"X-Service-Token": "wrong"})
    assert r.status_code == 403


# ---- precheck / hot_board 端点 ---------------------------------------------

def test_precheck_passes_through(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    async def _precheck(url):
        return {"reachable": True, "video_count": 8}

    monkeypatch.setattr("app.api.analyze.precheck", _precheck)
    with TestClient(app) as c:
        r = c.post("/ai/analyze/precheck",
                   json={"url": "https://u"},
                   headers={"X-Service-Token": "test-secret"})
    assert r.status_code == 200
    assert r.json() == {"reachable": True, "video_count": 8}


def test_hot_board_passes_through(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    async def _hb():
        from app.datasource.tikhub import HotItem
        return [HotItem(title="t", hot_index=1, video_count=3)]

    monkeypatch.setattr("app.api.analyze.hot_board", _hb)
    with TestClient(app) as c:
        r = c.get("/ai/hot_board", headers={"X-Service-Token": "test-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body[0]["title"] == "t"


# ---- helper -----------------------------------------------------------------

def _patch_pool(monkeypatch, pool):
    async def _get_pool():
        return pool
    monkeypatch.setattr("app.skills.analyze_store.get_pool", _get_pool)
