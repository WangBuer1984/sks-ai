"""拆视频 skill 测试：mock transcribe/llm/db seam，绝不发真实网络/DB 请求。

覆盖：
- /ai/analyze/video/text 同步：LLM 结构化 → analyze_task(done+result) → 返回结构（无内容安全）
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
    """LLM 结构化 → 写 analyze_task(done, progress=100, result) → 返回结构（无内容安全）。"""
    calls: list[dict] = []

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"task_id": task_id, "status": status, "progress": progress,
                       "result": result, "error": error})

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


# ---- video/link 202 异步路径 -----------------------------------------------

@pytest.mark.asyncio
async def test_video_link_done_transcribe_then_structure_then_done(monkeypatch):
    """link：resolve_media→transcribe(MediaRef)→结构化→写 done+result。progress 单条 0→100。"""
    import app.skills.video_analyze.graph as vg
    from app.datasource.media import MediaRef

    calls: list[dict] = []
    captured: dict = {}

    async def _resolve(url):
        captured["share_url"] = url
        return MediaRef(
            platform="douyin",
            download_url="https://cdn.example/a.mp4",
            headers={"Referer": "https://www.douyin.com/"},
            title="t",
            author="a",
        )

    async def _transcribe(media, *, on_progress=None):
        # 原始分享链不得传入 transcribe——必须收到 resolve_media 产出的 MediaRef。
        captured["transcribe_arg"] = media
        assert isinstance(media, MediaRef)
        assert media.download_url.startswith("https://cdn")
        if on_progress is not None:
            await on_progress(0.5)
            await on_progress(1.0)
        return "转写文本"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    monkeypatch.setattr(vg, "resolve_media", _resolve)
    monkeypatch.setattr(vg, "transcribe", _transcribe)
    monkeypatch.setattr(vg, "chat", _fake_chat_structure)
    monkeypatch.setattr(vg, "update_task", _update_task)
    # 心跳打到假 pool（不报错即可）
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    await vg.analyze_video_link(task_id=7, url="https://v.douyin.com/abc")

    # resolve_media 收到的是原始分享链，transcribe 收到的是 MediaRef（直链 cdn）
    assert captured["share_url"] == "https://v.douyin.com/abc"
    assert isinstance(captured["transcribe_arg"], MediaRef)
    assert captured["transcribe_arg"].download_url == "https://cdn.example/a.mp4"

    statuses = [c["status"] for c in calls]
    # running（bg 启动）→ done
    assert "running" in statuses
    assert statuses[-1] == "done"
    # 阶段进度：启动 5 → resolve≥20 → 转写推进 → 结构化前≥90 → done 100
    progress_seq = [c["progress"] for c in calls if c["progress"] is not None]
    assert progress_seq[0] == 5
    assert any(p >= 20 for p in progress_seq)
    assert any(p >= 90 for p in progress_seq)
    assert progress_seq[-1] == 100
    # 单调不减（允许同值重复写）
    assert progress_seq == sorted(progress_seq)
    done = [c for c in calls if c["status"] == "done"][0]
    assert done["progress"] == 100
    assert done["result"]["structure"]


@pytest.mark.asyncio
async def test_video_link_failed_on_transcribe_datasource_error(monkeypatch):
    """transcribe 抛 DataSourceError → status=failed+error。"""
    import app.skills.video_analyze.graph as vg
    from app.datasource.media import MediaRef

    calls: list[dict] = []

    async def _resolve(url):
        return MediaRef(
            platform="douyin",
            download_url="https://cdn.example/a.mp4",
            headers={"Referer": "https://www.douyin.com/"},
        )

    async def _transcribe(media, *, on_progress=None):
        raise DataSourceError("asr boom")

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    monkeypatch.setattr(vg, "resolve_media", _resolve)
    monkeypatch.setattr(vg, "transcribe", _transcribe)
    monkeypatch.setattr(vg, "update_task", _update_task)
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    await vg.analyze_video_link(task_id=7, url="https://v/x")

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

    async def _resolve(url):
        from app.datasource.media import MediaRef
        return MediaRef(
            platform="douyin",
            download_url="https://cdn.example/a.mp4",
            headers={"Referer": "https://www.douyin.com/"},
        )

    async def _transcribe(url, *, on_progress=None):
        order.append("transcribe")
        return "t"

    async def _chat(*a, **kw):
        order.append("chat")
        return {"structure": "s", "why_hot": "w", "framework": "f", "diff_hint": "d"}

    monkeypatch.setattr(vg, "resolve_media", _resolve)
    monkeypatch.setattr(vg, "transcribe", _transcribe)
    monkeypatch.setattr(vg, "chat", _chat)
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

    async def _resolve(url):
        from app.datasource.media import MediaRef
        return MediaRef(
            platform="douyin",
            download_url="https://cdn.example/a.mp4",
            headers={"Referer": "https://www.douyin.com/"},
        )

    async def _slow_transcribe(url, *, on_progress=None):
        await asyncio.sleep(0.05)
        return "慢转写结果"

    monkeypatch.setattr(vg, "resolve_media", _resolve)
    monkeypatch.setattr(vg, "transcribe", _slow_transcribe)
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


# ---- 链接流 transcript 落 result --------------------------------------------

@pytest.mark.asyncio
async def test_video_link_result_includes_transcript(monkeypatch):
    """link 终态 result 含 transcript 全文（详情展示用），且与转写产物逐字一致。"""
    import app.skills.video_analyze.graph as vg

    calls: list[dict] = []
    full_text = "开场就问你家师傅怕不怕检查，第一处看阴阳角……评论区扣「验收」领清单。"

    async def _resolve(url):
        from app.datasource.media import MediaRef
        return MediaRef(platform="douyin", download_url="https://cdn.example/a.mp4")

    async def _transcribe(media, *, on_progress=None):
        return full_text

    async def _chat(skill, messages, json_schema=None):
        return {"structure": "钩子→正文→CTA", "why_hot": "切中焦虑",
                "framework": "问题-方案", "diff_hint": "可复用"}

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    monkeypatch.setattr(vg, "resolve_media", _resolve)
    monkeypatch.setattr(vg, "transcribe", _transcribe)
    monkeypatch.setattr(vg, "chat", _chat)
    monkeypatch.setattr(vg, "update_task", _update_task)
    # 心跳走假 pool：run_with_heartbeat 用的是自己模块里的 _heartbeat，patch vg.heartbeat 无效
    # （同 test_video_link_done_transcribe_then_structure_then_done 的做法）。
    _patch_pool(monkeypatch, _FakePool())

    await vg.analyze_video_link(task_id=1, url="https://v.douyin.com/abc")

    done = [c for c in calls if c["status"] == "done"][0]
    assert done["result"]["transcript"] == full_text
    assert done["result"]["structure"] == "钩子→正文→CTA"


def test_transcript_not_requested_from_llm():
    """transcript 不进 LLM schema——否则等于要求模型复述全文（成本与截断风险）。"""
    import app.skills.video_analyze.graph as vg

    assert "transcript" not in vg.VIDEO_STRUCTURE_SCHEMA["properties"]
    assert "transcript" not in vg.VIDEO_STRUCTURE_SCHEMA["required"]
    assert "transcript" not in vg._STRUCT_FIELDS


@pytest.mark.asyncio
async def test_video_text_result_stays_four_fields(monkeypatch):
    """同步流（粘文案）result 形状不变：不塞 transcript（原文本就在用户手里/input 里）。"""
    import app.skills.video_analyze.graph as vg

    calls: list[dict] = []

    async def _chat(skill, messages, json_schema=None):
        return {"structure": "s", "why_hot": "w", "framework": "f", "diff_hint": "d"}

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "result": result})

    monkeypatch.setattr(vg, "chat", _chat)
    monkeypatch.setattr(vg, "update_task", _update_task)

    await vg.structure_video(task_id=1, transcript="用户粘贴的原文")

    result = [c for c in calls if c["status"] == "done"][0]["result"]
    assert set(result.keys()) == {"structure", "why_hot", "framework", "diff_hint"}
