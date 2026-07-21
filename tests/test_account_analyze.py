"""拆账号 skill 测试：mock tikhub/transcribe/llm/safety/db seam，绝不发真实网络/DB 请求。

覆盖：
- 跑完后 analyze_task.status='done'、benchmark_video 有 N 行、result 含 账号画像/规律归纳/迁移建议 三层
- 某条转写抛错 → status='partial' 且 progress 反映已完成比例
- 全量 scrape DataSourceError → status='failed'
- 全部条目失败 → status='failed'
- progress 语义 = floor(done/total×100) 整数，分段更新
- 心跳：长转写期间 updated_at 被周期性 touch
- /ai/analyze/account 端点鉴权
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.datasource import DataSourceError
from app.datasource.tikhub import VideoMeta


# ---- fakes -----------------------------------------------------------------

async def _safe(_t):
    return True


async def _unsafe(_t):
    return False


def _videos(n: int) -> list[VideoMeta]:
    return [VideoMeta(title=f"t{i}", play_count=100 * i, fav_count=i,
                       download_url=f"https://dl/{i}.mp4") for i in range(n)]


async def _fake_chat_item(*args, **kwargs):
    """account_analyze_item 结构化输出。"""
    return {
        "structure": "钩子→正文→CTA",
        "why_hot": "切中焦虑",
        "framework": "问题-方案",
        "diff_hint": "可复用",
    }


async def _fake_chat_summary(*args, **kwargs):
    """account_analyze_summary 三层归纳。"""
    return {
        "account_profile": "职场成长博主",
        "patterns": "每周三更，开场强钩子",
        "migration_advice": "先复用开场节奏，再差异化选题",
    }


class _FakePool:
    def __init__(self):
        self.execs: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.execs.append((query, args))

    async def fetch(self, query, *args):
        return []


def _patch_pool(monkeypatch, pool):
    async def _get_pool():
        return pool
    monkeypatch.setattr("app.skills.analyze_store.get_pool", _get_pool)


# ---- done 路径 --------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_done_status_benchmark_rows_three_layers(monkeypatch):
    """3 条视频全成 → status='done'、benchmark_video 3 行、result 三层 + videos 摘要。"""
    calls: list[dict] = []
    bench: list[dict] = []

    async def _top(url, n=20):
        return _videos(3)

    async def _transcribe(url):
        return f"转写-{url}"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    async def _insert_bench(task_id, title, play_count, fav_count, transcript, structure):
        bench.append({"title": title, "play_count": play_count})

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.account_analyze.graph.check", _safe)
    # chat 按 skill 名分流
    async def _chat(skill, messages, json_schema=None):
        if skill == "account_analyze_item":
            return await _fake_chat_item()
        if skill == "account_analyze_summary":
            return await _fake_chat_summary()
        raise AssertionError(f"unexpected skill {skill}")
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _chat)
    monkeypatch.setattr("app.skills.account_analyze.graph.update_task", _update_task)
    monkeypatch.setattr("app.skills.account_analyze.graph.insert_benchmark_video", _insert_bench)
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.account_analyze.graph import analyze_account
    await analyze_account(task_id=1, url="https://u")

    assert len(bench) == 3
    dones = [c for c in calls if c["status"] == "done"]
    assert len(dones) == 1
    result = dones[0]["result"]
    assert set(["account_profile", "patterns", "migration_advice"]).issubset(result.keys())
    assert result["account_profile"] == "职场成长博主"
    assert len(result["videos"]) == 3
    # progress 序列：0（启动）→ 33 → 66 → 100
    progress_seq = [c["progress"] for c in calls if c["progress"] is not None]
    assert progress_seq[0] == 0
    assert progress_seq[-1] == 100
    assert progress_seq == sorted(progress_seq)  # 单调不减


@pytest.mark.asyncio
async def test_account_partial_progress_reflects_finished_ratio(monkeypatch):
    """3 条视频第 2 条转写失败 → status='partial'，progress=66（floor(2/3*100)）。"""
    calls: list[dict] = []
    bench: list[dict] = []

    async def _top(url, n=20):
        return _videos(3)

    async def _transcribe(url):
        if "dl/1" in url:
            raise DataSourceError("asr fail on item 1")
        return f"转写-{url}"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    async def _insert_bench(task_id, title, play_count, fav_count, transcript, structure):
        bench.append({"title": title})

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.account_analyze.graph.check", _safe)
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _fake_chat_summary)
    monkeypatch.setattr("app.skills.account_analyze.graph.update_task", _update_task)
    monkeypatch.setattr("app.skills.account_analyze.graph.insert_benchmark_video", _insert_bench)
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.account_analyze.graph import analyze_account
    await analyze_account(task_id=1, url="https://u")

    # 2 条成功 → 2 行 benchmark
    assert len(bench) == 2
    finals = [c for c in calls if c["status"] in ("partial", "done", "failed")]
    assert len(finals) == 1
    assert finals[0]["status"] == "partial"
    assert finals[0]["progress"] == 66  # floor(2/3 * 100)
    assert finals[0]["error"]  # 有 error 文本
    # partial 仍写三层 result（在成功条目上归纳）
    assert finals[0]["result"]
    assert "account_profile" in finals[0]["result"]


@pytest.mark.asyncio
async def test_account_failed_on_full_scrape_datasource_error(monkeypatch):
    """account_top_videos 抛 DataSourceError → status='failed'，无 benchmark 行。"""
    calls: list[dict] = []
    bench: list[dict] = []

    async def _top(url, n=20):
        raise DataSourceError("tikhub scrape fail")

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    async def _insert_bench(*a, **kw):
        bench.append({})

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.update_task", _update_task)
    monkeypatch.setattr("app.skills.account_analyze.graph.insert_benchmark_video", _insert_bench)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", lambda url: _ret("t"))
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _fake_chat_summary)
    monkeypatch.setattr("app.skills.account_analyze.graph.check", _safe)
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.account_analyze.graph import analyze_account
    await analyze_account(task_id=1, url="https://u")

    assert len(bench) == 0
    failed = [c for c in calls if c["status"] == "failed"]
    assert len(failed) == 1
    assert "tikhub scrape fail" in failed[0]["error"]


@pytest.mark.asyncio
async def test_account_failed_when_all_items_fail(monkeypatch):
    """所有条目转写失败 → status='failed'。"""
    calls: list[dict] = []
    bench: list[dict] = []

    async def _top(url, n=20):
        return _videos(2)

    async def _transcribe(url):
        raise DataSourceError("asr fail")

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    async def _insert_bench(*a, **kw):
        bench.append({})

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _fake_chat_summary)
    monkeypatch.setattr("app.skills.account_analyze.graph.check", _safe)
    monkeypatch.setattr("app.skills.account_analyze.graph.update_task", _update_task)
    monkeypatch.setattr("app.skills.account_analyze.graph.insert_benchmark_video", _insert_bench)
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.account_analyze.graph import analyze_account
    await analyze_account(task_id=1, url="https://u")

    assert len(bench) == 0
    failed = [c for c in calls if c["status"] == "failed"]
    assert len(failed) == 1


# ---- 心跳 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_heartbeat_during_long_transcribe(monkeypatch):
    """单条长转写 → heartbeat 多次 touch updated_at=now()。"""
    import app.skills.account_analyze.graph as ag
    monkeypatch.setattr(ag, "HEARTBEAT_INTERVAL", 0.01)

    async def _top(url, n=20):
        return _videos(1)

    async def _slow_transcribe(url):
        await asyncio.sleep(0.05)
        return "慢转写"

    monkeypatch.setattr(ag, "account_top_videos", _top)
    monkeypatch.setattr(ag, "transcribe", _slow_transcribe)
    monkeypatch.setattr(ag, "check", _safe)
    monkeypatch.setattr(ag, "chat", _fake_chat_summary)
    # update_task 用真实实现 → 走假 pool
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)
    # insert_benchmark_video 用真实实现 → 走假 pool

    await ag.analyze_account(task_id=1, url="https://u")

    hb = [q for q, _ in pool.execs
          if q.startswith("UPDATE analyze_task SET updated_at = now() WHERE")]
    assert len(hb) >= 2, f"心跳应多次 touch，实际 {len(hb)} 次"


# ---- 端点鉴权 + 202 --------------------------------------------------------

def test_account_endpoint_requires_token(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr("app.api.analyze.analyze_account", _noop)
    with TestClient(app) as c:
        r = c.post("/ai/analyze/account", json={"task_id": 1, "url": "https://u"})
    assert r.status_code == 422


def test_account_endpoint_returns_202_and_sets_running_before_background(monkeypatch):
    """endpoint 先写 running，再 BackgroundTasks，返回 202 {task_id}。"""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.skills.account_analyze.graph as ag

    order: list[str] = []

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        order.append(f"update:{status}")

    async def _analyze(task_id, url):
        order.append("bg:analyze")

    monkeypatch.setattr(ag, "update_task", _update_task)
    monkeypatch.setattr("app.api.analyze.analyze_account", _analyze)
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    with TestClient(app) as c:
        r = c.post("/ai/analyze/account",
                   json={"task_id": 11, "url": "https://u"},
                   headers={"X-Service-Token": "test-secret"})
    assert r.status_code == 202
    assert r.json() == {"task_id": 11}
    assert order[0] == "update:running"
    assert order.index("update:running") < order.index("bg:analyze")


# ---- helper -----------------------------------------------------------------

async def _ret(v):
    return v
