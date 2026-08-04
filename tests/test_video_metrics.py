"""Task 1: /ai/analyze/video/metrics 双平台五码测试。

抖音走 video_meta；视频号走 channels_video_metrics；unknown → None。
端点 found/false 兜底。mock 注入，绝不发真实网络请求。
"""

import pytest
from app.datasource.tikhub import VideoMeta


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
