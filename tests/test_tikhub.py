"""TikHub 客户端测试：mock httpx（httpx.MockTransport），绝不发真实网络请求。

覆盖：account_top_videos 解析 N 条 VideoMeta、video_meta 单条、precheck 可访问性+条数、
hot_board 列表、base_url 必须 = https://api.tikhub.dev（主域名被墙，计划强约束）、
Authorization: Bearer 头注入、TikHub 业务/HTTP 失败 → DataSourceError。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings
from app.datasource import DataSourceError
from app.datasource.tikhub import (
    HotItem,
    VideoMeta,
    _base_url,
    account_top_videos,
    hot_board,
    precheck,
    video_meta,
)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _auth_header(request: httpx.Request) -> str:
    return request.headers.get("Authorization", "")


async def test_base_url_is_api_tikhub_dev():
    # 主域名 api.tikhub.io 被墙——计划强约束 base_url 必须是 api.tikhub.dev。
    assert _base_url() == "https://api.tikhub.dev"


async def test_base_url_default_constant_unaffected_by_empty_setting(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_BASE_URL", "")
    assert _base_url() == "https://api.tikhub.dev"


async def test_authorization_bearer_header_sent(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    captured = {}

    async def handler(request: httpx.Request):
        captured["auth"] = _auth_header(request)
        captured["url"] = str(request.url)
        if request.url.path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": {"sec_user_id": "SEC1"}})
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "aweme_list": [
                        {
                            "aweme_id": "1",
                            "desc": "t1",
                            "statistics": {"play_count": 10, "digg_count": 2},
                            "video": {"play_addr": {"url_list": ["https://dl/1.mp4"]}},
                        }
                    ],
                },
            },
        )

    client = _mock_client(handler)
    try:
        await account_top_videos("https://www.douyin.com/user/sec1", n=5, client=client)
    finally:
        await client.aclose()
    assert captured["auth"] == "Bearer tk-test-key"


async def test_account_top_videos_parses_n_video_meta(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    # get_sec_user_id → 返回 sec_user_id；fetch_user_post_videos → 返回 N 条
    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": {"sec_user_id": "SEC123"}})
        if path.endswith("fetch_user_post_videos"):
            assert "sec_user_id=SEC123" in str(request.url)
            count = int(request.url.params.get("count", "0"))
            assert count == 3
            items = [
                {
                    "aweme_id": str(i),
                    "desc": f"title-{i}",
                    "statistics": {"play_count": 100 * i, "digg_count": i},
                    "video": {"play_addr": {"url_list": [f"https://dl/{i}.mp4"]}},
                }
                for i in range(3)
            ]
            return httpx.Response(200, json={"code": 200, "data": {"aweme_list": items}})
        return httpx.Response(404)

    client = _mock_client(handler)
    try:
        videos = await account_top_videos("https://www.douyin.com/user/x", n=3, client=client)
    finally:
        await client.aclose()

    assert len(videos) == 3
    assert all(isinstance(v, VideoMeta) for v in videos)
    assert videos[0].title == "title-0"
    assert videos[0].play_count == 0
    assert videos[0].fav_count == 0
    assert videos[0].download_url == "https://dl/0.mp4"
    assert videos[2].play_count == 200


async def test_account_top_videos_caps_at_n(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": {"sec_user_id": "S"}})
        items = [
            {"aweme_id": str(i), "desc": f"t{i}", "statistics": {}, "video": {"play_addr": {"url_list": [f"u{i}"]}}}
            for i in range(10)
        ]
        return httpx.Response(200, json={"code": 200, "data": {"aweme_list": items}})

    client = _mock_client(handler)
    try:
        videos = await account_top_videos("https://x", n=20, client=client)
    finally:
        await client.aclose()
    # 只解析 API 返回的 10 条（不足 N 时按实际返回）
    assert len(videos) == 10


async def test_video_meta_returns_single(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        assert request.url.path.endswith("fetch_one_video_by_share_url")
        assert request.url.params.get("url") == "https://v.douyin.com/abc"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "aweme_detail": {
                        "aweme_id": "V1",
                        "desc": "a video",
                        "statistics": {"play_count": 99, "digg_count": 5},
                        "video": {"play_addr": {"url_list": ["https://dl/v1.mp4"]}},
                    }
                },
            },
        )

    client = _mock_client(handler)
    try:
        vm = await video_meta("https://v.douyin.com/abc", client=client)
    finally:
        await client.aclose()
    assert isinstance(vm, VideoMeta)
    assert vm.title == "a video"
    assert vm.play_count == 99
    assert vm.fav_count == 5
    assert vm.download_url == "https://dl/v1.mp4"


async def test_precheck_returns_reachable_and_count(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": {"sec_user_id": "SEC"}})
        if path.endswith("fetch_user_post_videos"):
            items = [{"aweme_id": str(i), "desc": "t", "statistics": {}, "video": {"play_addr": {"url_list": ["u"]}}} for i in range(4)]
            return httpx.Response(200, json={"code": 200, "data": {"aweme_list": items}})
        return httpx.Response(404)

    client = _mock_client(handler)
    try:
        result = await precheck("https://www.douyin.com/user/x", client=client)
    finally:
        await client.aclose()
    assert result["reachable"] is True
    assert result["video_count"] == 4


async def test_precheck_unreachable_when_sec_user_id_missing(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        # 主页 URL 无法解析 sec_user_id → 不可达
        return httpx.Response(200, json={"code": 200, "data": {}})

    client = _mock_client(handler)
    try:
        result = await precheck("https://www.douyin.com/user/bad", client=client)
    finally:
        await client.aclose()
    assert result["reachable"] is False
    assert result["video_count"] == 0


async def test_hot_board_returns_list(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        assert request.url.path.endswith("fetch_hot_total_list")
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "hot_list": [
                        {"hot_index": 1, "title": "trending", "video_count": 12},
                        {"hot_index": 2, "title": "second", "video_count": 5},
                    ]
                },
            },
        )

    client = _mock_client(handler)
    try:
        items = await hot_board(client=client)
    finally:
        await client.aclose()
    assert len(items) == 2
    assert all(isinstance(i, HotItem) for i in items)
    assert items[0].title == "trending"
    assert items[0].hot_index == 1


async def test_tikhub_http_error_raises_datasource_error(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        return httpx.Response(500, text="upstream boom")

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await video_meta("https://v.douyin.com/abc", client=client)
    finally:
        await client.aclose()


async def test_tikhub_business_code_non_200_raises_datasource_error(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        # HTTP 200 但业务码非 200
        return httpx.Response(200, json={"code": 404, "message": "not found", "data": {}})

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await video_meta("https://v.douyin.com/abc", client=client)
    finally:
        await client.aclose()


async def test_tikhub_unconfigured_key_raises_datasource_error(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "")
    with pytest.raises(DataSourceError):
        await video_meta("https://v.douyin.com/abc")


async def test_network_exception_raises_datasource_error(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        raise httpx.ConnectError("dns fail")

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await hot_board(client=client)
    finally:
        await client.aclose()
