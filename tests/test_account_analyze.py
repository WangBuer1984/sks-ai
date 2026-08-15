"""拆账号 skill 测试：mock tikhub/transcribe/llm/db seam，绝不发真实网络/DB 请求。

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
from app.datasource.media import MediaRef
from app.datasource.tikhub import VideoMeta


# ---- fakes -----------------------------------------------------------------

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

    async def _transcribe(media):
        # transcribe 收到的是 video_meta_to_media_ref 产出的 MediaRef（直链 + 抖音头），
        # 不再是裸 download_url 字符串。
        assert isinstance(media, MediaRef)
        return f"转写-{media.download_url}"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    async def _insert_bench(task_id, title, play_count, fav_count, transcript, structure, **kwargs):
        bench.append({"title": title, "play_count": play_count})

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
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

    async def _transcribe(media):
        assert isinstance(media, MediaRef)
        if "dl/1" in media.download_url:
            raise DataSourceError("asr fail on item 1")
        return f"转写-{media.download_url}"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    async def _insert_bench(task_id, title, play_count, fav_count, transcript, structure, **kwargs):
        bench.append({"title": title})

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
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
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", lambda media: _ret("t"))
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _fake_chat_summary)
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

    async def _transcribe(media):
        assert isinstance(media, MediaRef)
        raise DataSourceError("asr fail")

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    async def _insert_bench(*a, **kw):
        bench.append({})

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _fake_chat_summary)
    monkeypatch.setattr("app.skills.account_analyze.graph.update_task", _update_task)
    monkeypatch.setattr("app.skills.account_analyze.graph.insert_benchmark_video", _insert_bench)
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.account_analyze.graph import analyze_account
    await analyze_account(task_id=1, url="https://u")

    assert len(bench) == 0
    failed = [c for c in calls if c["status"] == "failed"]
    assert len(failed) == 1


# ---- 视频号 decode_key 透传 --------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_account_passes_channels_decode_key_to_transcribe(monkeypatch):
    """Task 1 升级 video_meta_to_media_ref 后，未改 graph 循环也应透传 decode_key。"""
    from app.datasource.media import MediaRef
    from app.datasource.tikhub import VideoMeta

    captured: dict = {}

    async def _top(url, n=20):
        return [VideoMeta(
            title="频道一条",
            play_count=3,
            fav_count=1,
            download_url="http://cdn/channels/a.mp4",
            author="前进的胖掌柜",
            decode_key="dk-pair-1",
            platform="wechat_channels",
        )]

    async def _transcribe(media):
        captured["media"] = media
        assert isinstance(media, MediaRef)
        assert media.platform == "wechat_channels"
        assert media.decode_key == "dk-pair-1"
        assert media.download_url == "http://cdn/channels/a.mp4"
        return "转写文案"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        return None

    async def _insert_bench(task_id, title, play_count, fav_count, transcript, structure, **kwargs):
        return None

    async def _chat(skill, messages, json_schema=None):
        if skill == "account_analyze_item":
            return await _fake_chat_item()
        if skill == "account_analyze_summary":
            return await _fake_chat_summary()
        raise AssertionError(skill)

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _chat)
    monkeypatch.setattr("app.skills.account_analyze.graph.update_task", _update_task)
    monkeypatch.setattr("app.skills.account_analyze.graph.insert_benchmark_video", _insert_bench)

    from app.skills.account_analyze.graph import analyze_account
    await analyze_account(task_id=1, url="sphi9BjV8GK0Zsl")
    assert isinstance(captured["media"], MediaRef)
    assert captured["media"].decode_key == "dk-pair-1"


# ---- 心跳 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_heartbeat_during_long_transcribe(monkeypatch):
    """单条长转写 → heartbeat 多次 touch updated_at=now()。"""
    import app.skills.account_analyze.graph as ag
    monkeypatch.setattr(ag, "HEARTBEAT_INTERVAL", 0.01)

    async def _top(url, n=20):
        return _videos(1)

    async def _slow_transcribe(media):
        assert isinstance(media, MediaRef)
        await asyncio.sleep(0.05)
        return "慢转写"

    monkeypatch.setattr(ag, "account_top_videos", _top)
    monkeypatch.setattr(ag, "transcribe", _slow_transcribe)
    monkeypatch.setattr(ag, "chat", _fake_chat_summary)
    # update_task 用真实实现 → 走假 pool
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)
    # insert_benchmark_video 用真实实现 → 走假 pool

    await ag.analyze_account(task_id=1, url="https://u")

    hb = [q for q, _ in pool.execs
          if q.startswith("UPDATE analyze_task SET updated_at = now() WHERE")]
    assert len(hb) >= 2, f"心跳应多次 touch，实际 {len(hb)} 次"


# ---- 进度递增时机（回归锁）---------------------------------------------------

@pytest.mark.asyncio
async def test_progress_increments_during_transcribe_not_after(monkeypatch):
    """回归：进度必须随每条转写完成递增，不能等 gather 全完才动。

    原先 insert+done+++progress 放在 ``asyncio.gather`` 之后的串行回填循环，导致
    整段并发慢转写期间 progress 恒 0（线上 12min 0%、视频号同路径循环复现）。修复后
    insert+进度迁入 ``_process_item``，该条转写+结构化+写行全成即递增。

    用 gate 阻塞第 3 条 transcribe：前两条全成时 progress 应已到 66；原 bug 此刻为 0
    （gather 未完 → 回填循环未跑）。
    """
    import app.skills.account_analyze.graph as ag
    monkeypatch.setattr(ag, "HEARTBEAT_INTERVAL", 0.01)

    calls: list[dict] = []
    gate = asyncio.Event()

    async def _top(url, n=20):
        return _videos(3)

    async def _transcribe(media):
        idx = int(media.download_url.rsplit("/", 1)[-1].split(".")[0])
        if idx == 2:
            await gate.wait()  # 第 3 条阻塞，直到断言放行
        await asyncio.sleep(0.01)
        return f"转写-{media.download_url}"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        calls.append({"status": status, "progress": progress, "result": result, "error": error})

    async def _insert_bench(*a, **kw):
        return None

    monkeypatch.setattr(ag, "account_top_videos", _top)
    monkeypatch.setattr(ag, "transcribe", _transcribe)
    monkeypatch.setattr(ag, "chat", _fake_chat_summary)
    monkeypatch.setattr(ag, "update_task", _update_task)
    monkeypatch.setattr(ag, "insert_benchmark_video", _insert_bench)
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    task = asyncio.create_task(ag.analyze_account(task_id=1, url="https://u"))
    try:
        await asyncio.sleep(0.15)  # 足够 0、1 两条全成（transcribe 0.01s + insert + progress）
        max_prog = max(
            (c["progress"] for c in calls if c["progress"] is not None), default=0
        )
        assert max_prog >= 66, (
            f"第 3 条阻塞期间 progress 应≥66（前 2 条已全成递增），实际 {max_prog}；"
            "若为 0 说明进度仍在 gather 之后才更新（回归 bug）"
        )
    finally:
        gate.set()  # 放第 3 条收尾
    await task


# ---- 端点鉴权 + 202（ASGITransport，避开 TestClient×asyncpg loop flaky） ------

async def _noop_lifespan_deps():
    """跳过真实 DB pool / checkpointer（httpx 0.28 ASGITransport 无 lifespan= 参数）。"""
    return None


def _patch_asgi_lifespan_deps(monkeypatch):
    """AsyncClient+ASGITransport 若触发 lifespan，避免连真实 Postgres。"""
    monkeypatch.setattr("app.main.init_pool", _noop_lifespan_deps)
    monkeypatch.setattr("app.main.close_pool", _noop_lifespan_deps)
    monkeypatch.setattr("app.main._init_checkpointer", _noop_lifespan_deps)


async def test_account_endpoint_requires_token(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    _patch_asgi_lifespan_deps(monkeypatch)
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr("app.api.analyze.analyze_account", _noop)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/ai/analyze/account", json={"task_id": 1, "url": "https://u"})
    assert r.status_code == 422


async def test_account_endpoint_wrong_token_rejected(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    _patch_asgi_lifespan_deps(monkeypatch)
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/ai/analyze/account",
            json={"task_id": 1, "url": "https://u"},
            headers={"X-Service-Token": "wrong"},
        )
    assert r.status_code == 403


async def test_account_endpoint_returns_202_and_sets_running_before_background(monkeypatch):
    """endpoint 先写 running，再 BackgroundTasks，返回 202 {task_id}。"""
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    import app.skills.account_analyze.graph as ag

    _patch_asgi_lifespan_deps(monkeypatch)
    order: list[str] = []

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        order.append(f"update:{status}")

    async def _analyze(task_id, url):
        order.append("bg:analyze")

    monkeypatch.setattr(ag, "update_task", _update_task)
    monkeypatch.setattr("app.api.analyze.analyze_account", _analyze)
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/ai/analyze/account",
            json={"task_id": 11, "url": "https://u"},
            headers={"X-Service-Token": "test-secret"},
        )
    assert r.status_code == 202
    assert r.json() == {"task_id": 11}
    assert order[0] == "update:running"
    assert order.index("update:running") < order.index("bg:analyze")


# ---- author / video_url 落列 -------------------------------------------------

def test_video_url_built_for_douyin():
    """抖音：aweme_id → 作品链接（详情态用它预填拆视频输入框）。"""
    from app.skills.account_analyze.graph import _video_url

    v = VideoMeta(title="t", play_count=1, fav_count=1, download_url="https://dl/0.mp4",
                  platform="douyin", aweme_id="7412345678901234567")
    assert _video_url(v) == "https://www.douyin.com/video/7412345678901234567"


def test_video_url_none_without_aweme_id():
    """aweme_id 缺失 → None，不编造链接。"""
    from app.skills.account_analyze.graph import _video_url

    v = VideoMeta(title="t", play_count=1, fav_count=1, download_url="https://dl/0.mp4",
                  platform="douyin", aweme_id=None)
    assert _video_url(v) is None


def test_video_url_none_for_channels():
    """视频号无公开可构造链接 → None（即便 aweme_id 有值）。"""
    from app.skills.account_analyze.graph import _video_url

    v = VideoMeta(title="t", play_count=1, fav_count=1, download_url="https://dl/0.mp4",
                  platform="wechat_channels", aweme_id="export_id_xyz")
    assert _video_url(v) is None


@pytest.mark.asyncio
async def test_account_insert_passes_author_and_video_url(monkeypatch):
    """拆账号写行时带上 author 与 video_url（详情页展示作者 + 原视频外链）。"""
    captured: list[dict] = []

    async def _top(url, n=20):
        return [
            VideoMeta(title="抖音条", play_count=100, fav_count=1,
                      download_url="https://dl/0.mp4", author="装修避坑老张",
                      platform="douyin", aweme_id="7412345678901234567"),
            VideoMeta(title="视频号条", play_count=90, fav_count=1,
                      download_url="https://dl/1.mp4", author="老李说装修",
                      platform="wechat_channels", decode_key="k"),
        ]

    async def _transcribe(media):
        return "转写文案"

    async def _update_task(task_id, *, status=None, progress=None, result=None, error=None):
        pass

    async def _insert_bench(task_id, title, play_count, fav_count, transcript, structure,
                            **kwargs):
        captured.append({"title": title, "author": kwargs.get("author"),
                         "video_url": kwargs.get("video_url")})

    async def _chat(skill, messages, json_schema=None):
        if skill == "account_analyze_item":
            return await _fake_chat_item()
        return await _fake_chat_summary()

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _chat)
    monkeypatch.setattr("app.skills.account_analyze.graph.update_task", _update_task)
    monkeypatch.setattr("app.skills.account_analyze.graph.insert_benchmark_video", _insert_bench)
    _patch_pool(monkeypatch, _FakePool())

    from app.skills.account_analyze.graph import analyze_account
    await analyze_account(task_id=1, url="https://u")

    by_title = {c["title"]: c for c in captured}
    assert by_title["抖音条"]["author"] == "装修避坑老张"
    assert by_title["抖音条"]["video_url"] == "https://www.douyin.com/video/7412345678901234567"
    assert by_title["视频号条"]["author"] == "老李说装修"
    assert by_title["视频号条"]["video_url"] is None


@pytest.mark.asyncio
async def test_insert_benchmark_video_sql_has_author_and_video_url(monkeypatch):
    """真实 insert + 假 pool：新列进 INSERT 列表，值按位传（缺省 author='' / video_url=None）。"""
    pool = _FakePool()
    _patch_pool(monkeypatch, pool)

    from app.skills.analyze_store import insert_benchmark_video
    await insert_benchmark_video(
        1, "标题", 100, 20, "转写", {"structure": "s"},
        author="装修避坑老张", video_url="https://www.douyin.com/video/741",
    )
    await insert_benchmark_video(1, "无作者", 100, 20, "转写", {"structure": "s"})

    sql, args = pool.execs[0]
    assert "author" in sql and "video_url" in sql
    assert "$16" in sql, "两个新列要进 VALUES 占位（原 14 列 → 16 列）"
    # 按位断言（新列排在末尾，顺序即 INSERT 列顺序：… duration_sec, author, video_url）
    assert args[-2] == "装修避坑老张"
    assert args[-1] == "https://www.douyin.com/video/741"
    _, args2 = pool.execs[1]
    assert args2[-2] == ""      # author 缺省不写 None（列是 varchar，空串语义即「未知作者」）
    assert args2[-1] is None    # video_url 缺省 NULL → 前端不渲染外链


# ---- helper -----------------------------------------------------------------

async def _ret(v):
    return v
