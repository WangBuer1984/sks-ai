"""Task 1: /ai/analyze/video/metrics 双平台五码测试。

抖音走 video_meta；视频号走 channels_video_metrics；unknown → None。
端点 found/false 兜底。mock 注入，绝不发真实网络请求。
"""

import pytest
from app.datasource.tikhub import VideoMeta


@pytest.fixture(autouse=True)
def _stub_tikhub_configured(monkeypatch):
    """本模块全 mock 注入、绝不发真实网络请求——绕过 _is_configured 守卫，
    让 channels_video_metrics 等入口不因 CI 无 TIKHUB_API_KEY 而提前抛 DataSourceError。"""
    from app.datasource import tikhub
    monkeypatch.setattr(tikhub, "_is_configured", lambda: True)


@pytest.mark.asyncio
async def test_video_metrics_douyin(monkeypatch):
    from app.datasource import tikhub

    async def _fake_meta(url, *, client=None):
        return VideoMeta(title="t", play_count=100, fav_count=0, download_url="",
                         platform="douyin", like_count=10, comment_count=2,
                         share_count=3, collect_count=4)

    monkeypatch.setattr(tikhub, "video_meta", _fake_meta)
    m = await tikhub.video_metrics("https://v.douyin.com/abc")
    assert m is not None
    assert (m.play_count, m.like_count, m.comment_count, m.share_count, m.collect_count) == (100, 10, 2, 3, 4)


@pytest.mark.asyncio
async def test_video_metrics_wechat_channels(monkeypatch):
    from app.datasource import tikhub

    async def _fake_channels(url, *, client=None):
        return VideoMeta(title="c", play_count=200, fav_count=5, download_url="",
                         platform="wechat_channels", like_count=20, comment_count=4,
                         share_count=6, collect_count=5)

    monkeypatch.setattr(tikhub, "channels_video_metrics", _fake_channels)
    m = await tikhub.video_metrics("https://weixin.qq.com/sph/xxx")
    assert m is not None
    assert m.platform == "wechat_channels"
    assert m.play_count == 200


@pytest.mark.asyncio
async def test_video_metrics_unknown_returns_none(monkeypatch):
    from app.datasource import tikhub
    m = await tikhub.video_metrics("https://example.com/unknown")
    assert m is None


def test_video_metrics_endpoint_passes_through(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.datasource import tikhub
    from app.api import analyze
    monkeypatch.setattr(analyze.settings, "SERVICE_TOKEN", "test-secret")

    async def _vm(url):
        return VideoMeta(title="t", play_count=7, fav_count=0, download_url="",
                         platform="douyin", like_count=1, comment_count=0,
                         share_count=0, collect_count=0)
    monkeypatch.setattr(analyze, "video_metrics", _vm)
    with TestClient(app) as c:
        r = c.get("/ai/analyze/video/metrics",
                  params={"url": "https://v.douyin.com/x"},
                  headers={"X-Service-Token": "test-secret"})
    assert r.status_code == 200
    j = r.json()
    assert j["found"] is True
    assert j["play_count"] == 7


def test_video_metrics_endpoint_not_found(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import analyze
    monkeypatch.setattr(analyze.settings, "SERVICE_TOKEN", "test-secret")
    async def _vm(url): return None
    monkeypatch.setattr(analyze, "video_metrics", _vm)
    with TestClient(app) as c:
        r = c.get("/ai/analyze/video/metrics",
                  params={"url": "https://bad"},
                  headers={"X-Service-Token": "test-secret"})
    assert r.status_code == 200
    assert r.json()["found"] is False


@pytest.mark.asyncio
async def test_channels_video_metrics_parses_real_detail_fixture(monkeypatch):
    """真实视频号 detail 响应结构 fixture（来自 smoke https://weixin.qq.com/sph/ADk6xBh2hq）。

    锁住 detail `data` 顶层取数点（read_count/like_count/fav_count/forward_count/
    comment_count + media.url），防 TikHub 改字段名或嵌套子层时 5 码静默返 0。
    read_count=0 是视频号 detail API 限制（不返真实播放量），非 parser bug。
    """
    from app.datasource import tikhub

    detail = {
        "code": 200,
        "data": {
            "id": 14919266588327413890,
            "nickname": "前进的胖掌柜",
            "title": [{"shortTitle": "职能部门正在杀死公司"}],
            "create_time": 1778515170,
            "read_count": 0,  # 视频号 detail 不返真实播放量
            "like_count": 13702,
            "fav_count": 12202,
            "forward_count": 20510,
            "comment_count": 681,
            "media": {
                "url": "http://wxapp.tc.qq.com/251/20302/stodownload?enc=xxx",
                "decode_key": "abc",
                "duration": 570,
            },
        },
    }

    async def _fake_fetch(client, share_url):
        return detail

    monkeypatch.setattr(tikhub, "_fetch_channels_video_detail", _fake_fetch)
    m = await tikhub.channels_video_metrics("https://weixin.qq.com/sph/ADk6xBh2hq")
    assert m is not None
    assert m.platform == "wechat_channels"
    assert m.play_count is None  # read_count=0 → 不可用信号（非真 0）
    assert m.like_count == 13702
    assert m.comment_count == 681
    assert m.share_count == 20510  # forward_count → share
    assert m.collect_count == 12202  # fav_count → collect
    assert m.fav_count == 12202
    assert m.download_url  # media.url 非空 → 不返 None


@pytest.mark.asyncio
async def test_channels_video_metrics_no_media_returns_none(monkeypatch):
    """detail 无 media（或 media 无 url）→ None（_parse_channels_video 跳过）。"""
    from app.datasource import tikhub

    async def _fake_fetch(client, share_url):
        return {"code": 200, "data": {"read_count": 1, "like_count": 2}}

    monkeypatch.setattr(tikhub, "_fetch_channels_video_detail", _fake_fetch)
    m = await tikhub.channels_video_metrics("https://weixin.qq.com/sph/x")
    assert m is None


@pytest.mark.asyncio
async def test_channels_video_metrics_positive_read_kept(monkeypatch):
    """罕见 read_count>0 → 保留真值（不强制 None）。"""
    from app.datasource import tikhub

    detail = {
        "code": 200,
        "data": {
            "nickname": "x",
            "title": [{"shortTitle": "t"}],
            "read_count": 1234,
            "like_count": 10,
            "fav_count": 1,
            "forward_count": 2,
            "comment_count": 3,
            "media": {"url": "http://example.com/v", "decode_key": "k", "duration": 10},
        },
    }

    async def _fake_fetch(client, share_url):
        return detail

    monkeypatch.setattr(tikhub, "_fetch_channels_video_detail", _fake_fetch)
    m = await tikhub.channels_video_metrics("https://weixin.qq.com/sph/x")
    assert m is not None
    assert m.play_count == 1234


def test_video_metrics_endpoint_channels_play_null(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import analyze
    from app.datasource.tikhub import VideoMeta

    monkeypatch.setattr(analyze.settings, "SERVICE_TOKEN", "test-secret")

    async def _vm(url):
        return VideoMeta(
            title="c",
            play_count=None,
            fav_count=5,
            download_url="http://x",
            platform="wechat_channels",
            like_count=20,
            comment_count=4,
            share_count=6,
            collect_count=5,
        )

    monkeypatch.setattr(analyze, "video_metrics", _vm)
    with TestClient(app) as c:
        r = c.get(
            "/ai/analyze/video/metrics",
            params={"url": "https://weixin.qq.com/sph/x"},
            headers={"X-Service-Token": "test-secret"},
        )
    assert r.status_code == 200
    j = r.json()
    assert j["found"] is True
    assert j["play_count"] is None
    assert j["like_count"] == 20
