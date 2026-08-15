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
from app.datasource.media import MediaRef
from app.datasource.tikhub import (
    CHANNELS_DOWNLOAD_HEADERS,
    DOUYIN_DOWNLOAD_HEADERS,
    HotItem,
    VideoMeta,
    _account_entry_kind,
    _base_url,
    _channels_title,
    _clear_channels_detail_cache,
    _parse_channels_video,
    _platform_of,
    account_top_videos,
    channels_video_meta,
    hot_board,
    precheck,
    resolve_media,
    video_meta,
    video_meta_to_media_ref,
)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _auth_header(request: httpx.Request) -> str:
    return request.headers.get("Authorization", "")


@pytest.fixture(autouse=True)
def _clear_channels_detail_cache_between_tests():
    """分享链 detail 短缓存不得跨测串味。"""
    _clear_channels_detail_cache()
    yield
    _clear_channels_detail_cache()


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
            return httpx.Response(200, json={"code": 200, "data": "SEC1"})
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
            return httpx.Response(200, json={"code": 200, "data": "SEC123"})
        if path.endswith("fetch_user_post_videos"):
            assert "sec_user_id=SEC123" in str(request.url)
            count = int(request.url.params.get("count", "0"))
            assert count == 3
            items = [
                {
                    "aweme_id": str(i),
                    "desc": f"title-{i}",
                    "statistics": {
                        "play_count": 100 * i,
                        "digg_count": i,
                        "collect_count": 10 * i,
                        "comment_count": 2 * i,
                        "share_count": i,
                    },
                    "video": {"play_addr": {"url_list": [f"https://dl/{i}.mp4"]}},
                }
                for i in range(3)
            ]
            return httpx.Response(200, json={"code": 200, "data": {"aweme_list": items}})
        if path.endswith("fetch_video_statistics"):
            # 无补丁时保持列表字段；返回空 data 跳过覆盖
            return httpx.Response(200, json={"code": 200, "data": []})
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
    assert videos[0].like_count == 0
    assert videos[0].fav_count == 0  # collect
    assert videos[0].download_url == "https://dl/0.mp4"
    assert videos[2].play_count == 200
    assert videos[2].like_count == 2
    assert videos[2].collect_count == 20
    assert videos[2].comment_count == 4
    assert videos[2].share_count == 2
    assert videos[2].fav_count == 20


async def test_account_top_videos_caps_at_n(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": "S"})
        if path.endswith("fetch_video_statistics"):
            return httpx.Response(200, json={"code": 200, "data": []})
        items = [
            {"aweme_id": str(i), "desc": f"t{i}", "statistics": {}, "video": {"play_addr": {"url_list": [f"u{i}"]}}}
            for i in range(10)
        ]
        return httpx.Response(200, json={"code": 200, "data": {"aweme_list": items}})

    client = _mock_client(handler)
    try:
        videos = await account_top_videos("https://www.douyin.com/user/x", n=20, client=client)
    finally:
        await client.aclose()
    # 只解析 API 返回的 10 条（不足 N 时按实际返回）
    assert len(videos) == 10


async def test_video_meta_returns_single(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("fetch_video_statistics"):
            return httpx.Response(200, json={"code": 200, "data": []})
        assert path.endswith("fetch_one_video_by_share_url")
        assert request.url.params.get("url") == "https://v.douyin.com/abc"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "aweme_detail": {
                        "aweme_id": "V1",
                        "desc": "a video",
                        "statistics": {
                            "play_count": 99,
                            "digg_count": 5,
                            "collect_count": 3,
                            "comment_count": 1,
                            "share_count": 2,
                        },
                        "video": {
                            "duration": 45000,
                            "play_addr": {"url_list": ["https://dl/v1.mp4"]},
                        },
                        "create_time": 1711305600,
                        "text_extra": [{"hashtag_name": "验收"}],
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
    assert vm.like_count == 5
    assert vm.collect_count == 3
    assert vm.fav_count == 3
    assert vm.comment_count == 1
    assert vm.share_count == 2
    assert vm.tags == ["验收"]
    assert vm.published_at == 1711305600
    assert vm.duration_sec == 45
    assert vm.download_url == "https://dl/v1.mp4"


async def test_enrich_douyin_statistics_merges_five_metrics(monkeypatch):
    """列表缺播放时，fetch_video_statistics 补齐；digg→like、collect→收藏。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    stats_calls: list[str] = []

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": "SEC"})
        if path.endswith("fetch_user_post_videos"):
            items = [
                {
                    "aweme_id": "A1",
                    "desc": "口播 #话题A",
                    "statistics": {"digg_count": 1, "collect_count": 2},
                    "video": {"play_addr": {"url_list": ["https://dl/a1.mp4"]}},
                    "create_time": 1710000000,
                },
                {
                    "aweme_id": "A2",
                    "desc": "第二条",
                    "statistics": {},
                    "video": {"play_addr": {"url_list": ["https://dl/a2.mp4"]}},
                },
            ]
            return httpx.Response(200, json={"code": 200, "data": {"aweme_list": items}})
        if path.endswith("fetch_video_statistics"):
            ids = request.url.params.get("aweme_ids", "")
            stats_calls.append(ids)
            assert ids == "A1,A2"
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [
                        {
                            "aweme_id": "A1",
                            "play_count": 10000,
                            "digg_count": 88,
                            "comment_count": 7,
                            "share_count": 3,
                            "collect_count": 12,
                        },
                        {
                            "aweme_id": "A2",
                            "play_count": 500,
                            "digg_count": 9,
                            "comment_count": 1,
                            "share_count": 0,
                            "collect_count": 4,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    client = _mock_client(handler)
    try:
        videos = await account_top_videos("https://www.douyin.com/user/x", n=2, client=client)
    finally:
        await client.aclose()

    assert len(stats_calls) == 1
    assert len(videos) == 2
    v0, v1 = videos[0], videos[1]
    assert v0.play_count == 10000
    assert v0.like_count == 88  # digg，不是收藏
    assert v0.comment_count == 7
    assert v0.share_count == 3
    assert v0.collect_count == 12
    assert v0.fav_count == 12
    assert "话题A" in v0.tags
    assert v0.published_at == 1710000000
    assert v1.play_count == 500
    assert v1.like_count == 9
    assert v1.collect_count == 4


async def test_precheck_returns_reachable_and_count(monkeypatch):
    # 修复盲点：mock 必须尊重 count 查询参数（返回 min(count, pool) 条），
    # 否则 count=1 的 bug 会被 4-条-固定-response 掩盖。
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    POOL = 4  # 账号仅有 4 条视频

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": "SEC"})
        if path.endswith("fetch_user_post_videos"):
            count = int(request.url.params.get("count", "0"))
            assert count > 1, "precheck must request a meaningful first-page count, not 1"
            n = min(count, POOL)
            items = [
                {"aweme_id": str(i), "desc": "t", "statistics": {}, "video": {"play_addr": {"url_list": ["u"]}}}
                for i in range(n)
            ]
            return httpx.Response(200, json={"code": 200, "data": {"aweme_list": items}})
        return httpx.Response(404)

    client = _mock_client(handler)
    try:
        result = await precheck("https://www.douyin.com/user/x", client=client)
    finally:
        await client.aclose()
    assert result["reachable"] is True
    assert result["video_count"] == 4


async def test_precheck_returns_meaningful_first_page_count(monkeypatch):
    # Q2 RED→GREEN 证明：20 条视频的账号，precheck 必须返回反映首页的 video_count
    # （≤20 且非退化的 {0,1}），以使 Task 3.3 的 max(1,min(10,floor(N/2))) 公式非退化。
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    POOL = 50  # 账号有 50 条视频（首页最多 20 条）

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": "SEC"})
        if path.endswith("fetch_user_post_videos"):
            count = int(request.url.params.get("count", "0"))
            n = min(count, POOL)
            items = [
                {"aweme_id": str(i), "desc": "t", "statistics": {}, "video": {"play_addr": {"url_list": ["u"]}}}
                for i in range(n)
            ]
            return httpx.Response(200, json={"code": 200, "data": {"aweme_list": items}})
        return httpx.Response(404)

    client = _mock_client(handler)
    try:
        result = await precheck("https://www.douyin.com/user/x", client=client)
    finally:
        await client.aclose()
    assert result["reachable"] is True
    # 反映真实首页条数（capped at 20），而非退化的 {0,1}
    assert result["video_count"] == 20, f"expected meaningful first-page count (20), got {result['video_count']}"


async def test_precheck_unreachable_when_sec_user_id_missing(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        # 主页 URL 无法解析 sec_user_id → TikHub 返回空 data → 不可达
        return httpx.Response(200, json={"code": 200, "data": ""})

    client = _mock_client(handler)
    try:
        result = await precheck("https://www.douyin.com/user/bad", client=client)
    finally:
        await client.aclose()
    assert result["reachable"] is False
    assert result["video_count"] == 0


async def test_precheck_channels_share_home_count_no_pagination(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")
    calls = {"videos": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_video_detail"):
            return httpx.Response(200, json={"code": 200, "data": {"username": "v2_x@finder"}})
        if request.url.path.endswith("fetch_user_videos"):
            calls["videos"] += 1
            body = json.loads(request.content.decode())
            assert not body.get("last_buffer")
            return httpx.Response(200, json={"code": 200, "data": {
                "videos": [{"media": {"full_url": "http://a", "decode_key": "1"}, "read_count": 1}] * 3,
                "up_continue": True,
                "last_buffer": "MORE",
            }})
        return httpx.Response(500)
    client = _mock_client(handler)
    r = await precheck("https://weixin.qq.com/sph/abc", client=client)
    assert r["reachable"] is True
    assert r["video_count"] == 3
    assert calls["videos"] == 1  # 不翻页
    await client.aclose()


async def test_precheck_empty_home_page_unreachable(monkeypatch):
    """首页 0 条 → reachable:false（抖音与视频号同口径）。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")

    def douyin_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("get_sec_user_id"):
            return httpx.Response(200, json={"code": 200, "data": "u1"})
        if request.url.path.endswith("fetch_user_post_videos"):
            return httpx.Response(200, json={"code": 200, "data": {"aweme_list": []}})
        return httpx.Response(500, text=request.url.path)

    client = _mock_client(douyin_handler)
    r = await precheck("https://www.douyin.com/user/x", client=client)
    assert r == {"reachable": False, "video_count": 0}
    await client.aclose()

    def channels_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_video_detail"):
            return httpx.Response(200, json={"code": 200, "data": {"username": "v2_x@finder"}})
        if request.url.path.endswith("fetch_user_videos"):
            return httpx.Response(200, json={"code": 200, "data": {"videos": []}})
        return httpx.Response(500, text=request.url.path)

    client = _mock_client(channels_handler)
    r2 = await precheck("https://weixin.qq.com/sph/abc", client=client)
    assert r2 == {"reachable": False, "video_count": 0}
    await client.aclose()


async def test_precheck_unknown_host_returns_unreachable_no_http_no_raise(monkeypatch):
    """unknown 入口（裸 channels host 缺 /sph/、not-a-url、裸 sph 短号）→ unreachable dict，不抛、不打 HTTP。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"unexpected {request.url.path}")

    client = _mock_client(handler)
    r = await precheck("https://channels.weixin.qq.com/no-sph-here", client=client)
    assert r == {"reachable": False, "video_count": 0}
    # 裸 sph 短号走 unknown（不发 HTTP），也 unreachable
    r2 = await precheck("sphi9BjV8GK0Zsl", client=client)
    assert r2 == {"reachable": False, "video_count": 0}
    await client.aclose()


async def test_hot_board_returns_list(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    async def handler(request: httpx.Request):
        assert request.url.path.endswith("fetch_hot_total_list")
        # page / page_size / type 必填（缺则 TikHub 422）——回归防回退
        assert request.url.params["page"] == "1"
        assert request.url.params["page_size"] == "50"
        assert request.url.params["type"] == "snapshot"
        # 真实响应形状：body.data.data.objs，字段 sentence/hot_score/video_count/rank
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "code": 200,
                    "data": {
                        "page": 1,
                        "objs": [
                            {"rank": 1, "sentence": "trending", "hot_score": 11914614, "video_count": 12},
                            {"rank": 2, "sentence": "second", "hot_score": 500, "video_count": 5},
                        ],
                        "last_update_time": 1736126146,
                    },
                    "extra": {},
                    "message": "ok",
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
    assert items[0].hot_index == 11914614  # hot_score，热度值
    assert items[0].video_count == 12
    assert items[1].title == "second"
    assert items[1].hot_index == 500


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


async def test_get_json_retries_transient_connect_error_then_succeeds(monkeypatch):
    # Q1 RED→GREEN：首次 ConnectError（瞬时 DNS 抖动）→ 重试 → 成功。
    # 当前实现无重试，首次失败即抛 DataSourceError（RED）。
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    # 把 backoff sleep 压成 0，避免测试真实等待
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    async def handler(request: httpx.Request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient dns blip")
        return httpx.Response(200, json={"code": 200, "data": {"hot_list": []}})

    client = _mock_client(handler)
    try:
        items = await hot_board(client=client)
    finally:
        await client.aclose()
    assert items == []
    assert calls["n"] == 2, f"expected 2 attempts (1 fail + 1 retry-success), got {calls['n']}"


async def test_get_json_retries_5xx_then_succeeds(monkeypatch):
    # Q1：5xx 同属可重试瞬时错误——首次 502 → 重试 → 成功。
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    async def handler(request: httpx.Request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"code": 200, "data": {"hot_list": []}})

    client = _mock_client(handler)
    try:
        items = await hot_board(client=client)
    finally:
        await client.aclose()
    assert items == []
    assert calls["n"] == 2


async def test_get_json_does_not_retry_on_4xx(monkeypatch):
    # Q1 不可重试类：4xx 客户端错误（bad URL/auth）→ 立即抛，仅 1 次尝试。
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    async def handler(request: httpx.Request):
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await hot_board(client=client)
    finally:
        await client.aclose()
    assert calls["n"] == 1, "4xx must not be retried"


async def test_get_json_retries_400_with_retry_then_succeeds(monkeypatch):
    # TikHub 瞬时故障误标 400 + body 含 "retry"（上游不规范）→ 窄口重试一次后成功。
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    async def handler(request: httpx.Request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                400,
                text='{"detail":{"code":400,"message":"Request failed. Please retry. Check the docs."}}',
            )
        return httpx.Response(200, json={"code": 200, "data": {"hot_list": []}})

    client = _mock_client(handler)
    try:
        items = await hot_board(client=client)
    finally:
        await client.aclose()
    assert items == []
    assert calls["n"] == 2, "400+retry must be retried once (then succeed)"


async def test_get_json_does_not_retry_on_400_without_retry(monkeypatch):
    # 普通 400（body 不含 retry，如参数校验）→ 不重试，仅 1 次尝试（保留 4xx 不变量）。
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    async def handler(request: httpx.Request):
        calls["n"] += 1
        # 422 风格的参数校验错误，body 不含 "retry"
        return httpx.Response(400, text='{"detail":[{"msg":"Field required","loc":["query","x"]}]}')

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await hot_board(client=client)
    finally:
        await client.aclose()
    assert calls["n"] == 1, "plain 400 (no retry hint) must not be retried"


async def test_get_json_does_not_retry_on_business_code_failure(monkeypatch):
    # Q1 不可重试类：TikHub 业务 code != 200（确定性失败）→ 立即抛，仅 1 次尝试。
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    async def handler(request: httpx.Request):
        calls["n"] += 1
        return httpx.Response(200, json={"code": 404, "message": "not found", "data": {}})

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await hot_board(client=client)
    finally:
        await client.aclose()
    assert calls["n"] == 1, "business-code failure must not be retried"


async def test_post_json_channels_detail_retries_5xx_then_succeeds(monkeypatch):
    """单次 detail（fetch_video_detail）属非翻页 POST，可 retry=True：前两次 500 第三次 200 → 成功，call_count==3。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_video_detail"):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(500, text="upstream boom")
            return httpx.Response(200, json={"code": 200, "data": {"username": "u"}})
        return httpx.Response(500, text="unexpected path")

    client = _mock_client(handler)
    try:
        body = await tikhub_mod._fetch_channels_video_detail(
            client, "https://weixin.qq.com/sph/abc"
        )
    finally:
        await client.aclose()
    assert body["data"]["username"] == "u"
    assert calls["n"] == 3, f"detail (retry=True) should retry 3x on 5xx, got {calls['n']}"


async def test_post_json_channels_detail_exhausts_retries_then_raises(monkeypatch):
    """retry=True 3 次全 500 → DataSourceError（而非单次即失败）。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_video_detail"):
            calls["n"] += 1
            return httpx.Response(500, text="upstream boom")
        return httpx.Response(500, text="unexpected path")

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await tikhub_mod._fetch_channels_video_detail(
                client, "https://weixin.qq.com/sph/abc"
            )
    finally:
        await client.aclose()
    assert calls["n"] == 3, f"detail retry=True should attempt 3x, got {calls['n']}"


async def test_post_json_pagination_user_videos_does_not_retry_5xx(monkeypatch):
    """fetch_user_videos 翻页 POST 保持 retry=False：单次 500 即 DataSourceError，call_count==1（防翻页放大账单）。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(tikhub_mod, "_sleep", _no_sleep)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_video_detail"):
            return httpx.Response(200, json={"code": 200, "data": {"username": "u"}})
        if request.url.path.endswith("fetch_user_videos"):
            calls["n"] += 1
            return httpx.Response(500, text="upstream boom")
        return httpx.Response(500, text="unexpected path")

    client = _mock_client(handler)
    try:
        with pytest.raises(DataSourceError):
            await precheck("https://weixin.qq.com/sph/abc", client=client)
    finally:
        await client.aclose()
    assert calls["n"] == 1, f"pagination POST must not retry, got {calls['n']}"


# ---- resolve_media：分享/短链 → MediaRef ----------------------------------

async def test_platform_of_detects_douyin_hosts():
    # 抖音分享短链 + 主域 + iesdouyin 变体均识别为 douyin。
    assert _platform_of("https://v.douyin.com/abc/") == "douyin"
    assert _platform_of("https://www.douyin.com/video/123") == "douyin"
    assert _platform_of("https://www.iesdouyin.com/share/x") == "douyin"
    # 微信视频号 host（sph 短链 + channels 子域）
    assert _platform_of("https://channels.weixin.qq.com/x") == "wechat_channels"
    assert _platform_of("https://weixin.qq.com/sph/ADk6xBh2hq") == "wechat_channels"
    # 未知 host
    assert _platform_of("https://example.com/v") == "unknown"
    assert _platform_of("not-a-url") == "unknown"


def test_channels_title_parses_short_title_repr():
    raw = "[{'shortTitle': '职能部门正在杀死公司', 'pbRequestMsgInfo': None}]"
    assert _channels_title(raw) == "职能部门正在杀死公司"
    assert _channels_title([{"shortTitle": "直接列表"}]) == "直接列表"
    assert _channels_title("普通标题") == "普通标题"


async def test_resolve_media_douyin_calls_video_meta_and_returns_media_ref(monkeypatch):
    """抖音 host → 调 video_meta → video_meta_to_media_ref（含 author + 新鲜头）。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    captured: dict = {}

    async def _fake_video_meta(url, *, client=None):
        captured["url"] = url
        captured["client"] = client
        return VideoMeta(
            title="a video",
            play_count=10,
            fav_count=2,
            download_url="https://dl/v1.mp4",
            author="作者甲",
        )

    monkeypatch.setattr(tikhub_mod, "video_meta", _fake_video_meta)

    ref = await resolve_media("https://v.douyin.com/abc")

    assert isinstance(ref, MediaRef)
    assert ref.platform == "douyin"
    assert ref.download_url == "https://dl/v1.mp4"
    # 抖音下载头注入（新鲜 dict，非模块级共享对象）
    assert ref.headers == DOUYIN_DOWNLOAD_HEADERS
    assert ref.headers is not DOUYIN_DOWNLOAD_HEADERS
    assert ref.headers["Referer"] == "https://www.douyin.com/"
    assert ref.author == "作者甲"
    assert ref.title == "a video"
    assert captured["url"] == "https://v.douyin.com/abc"


async def test_resolve_media_unknown_host_raises_datasource_error(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    called = {"n": 0}

    async def _fail_video_meta(url, *, client=None):
        called["n"] += 1
        return VideoMeta("", 0, 0, "")

    monkeypatch.setattr(tikhub_mod, "video_meta", _fail_video_meta)

    with pytest.raises(DataSourceError, match="unsupported"):
        await resolve_media("https://example.com/v")
    # 未知平台不得调 video_meta（短路在平台门）
    assert called["n"] == 0


async def test_resolve_media_empty_download_url_raises_datasource_error(monkeypatch):
    """video_meta 解析成功但 download_url 空 → DataSourceError（高清 fallback 地板）。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _empty_video_meta(url, *, client=None):
        return VideoMeta(title="t", play_count=0, fav_count=0, download_url="", author="")

    monkeypatch.setattr(tikhub_mod, "video_meta", _empty_video_meta)

    with pytest.raises(DataSourceError, match="empty download_url"):
        await resolve_media("https://v.douyin.com/abc")


async def test_channels_video_meta_parses_full_url_and_decode_key(monkeypatch):
    """raw=false 精简结构 → MediaRef(full_url, decode_key, author/title)。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/wechat_channels/v2/fetch_video_detail")
        assert _auth_header(request).startswith("Bearer ")
        body = json.loads(request.content.decode())
        assert body["share_url"].startswith("https://weixin.qq.com/sph/")
        assert body["raw"] is False
        return httpx.Response(
            200,
            json={
                "code": 200,
                "message": "ok",
                "data": {
                    "id": "14919266588327413890",
                    "nickname": "前进的胖掌柜",
                    "title": "[{'shortTitle': '职能部门正在杀死公司', 'pbRequestMsgInfo': None}]",
                    "media": {
                        "url": "http://cdn.example/v",
                        "url_token": "?t=1",
                        "full_url": "http://cdn.example/v?t=1",
                        "decode_key": "910035402",
                        "file_size": 100,
                    },
                },
            },
        )

    client = _mock_client(handler)
    ref = await channels_video_meta(
        "https://weixin.qq.com/sph/ADk6xBh2hq", client=client
    )
    assert isinstance(ref, MediaRef)
    assert ref.platform == "wechat_channels"
    assert ref.download_url == "http://cdn.example/v?t=1"
    assert ref.decode_key == "910035402"
    assert ref.author == "前进的胖掌柜"
    assert ref.title == "职能部门正在杀死公司"
    assert ref.raw_id == "14919266588327413890"
    assert ref.headers == CHANNELS_DOWNLOAD_HEADERS
    assert ref.headers is not CHANNELS_DOWNLOAD_HEADERS
    await client.aclose()


async def test_resolve_media_channels_dispatches_to_channels_video_meta(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk-test-key")
    import app.datasource.tikhub as tikhub_mod

    async def _fake_channels(url, *, client=None):
        return MediaRef(
            platform="wechat_channels",
            download_url="http://cdn/x",
            decode_key="k1",
            title="t",
            author="a",
        )

    monkeypatch.setattr(tikhub_mod, "channels_video_meta", _fake_channels)
    ref = await resolve_media("https://weixin.qq.com/sph/abc")
    assert ref.platform == "wechat_channels"
    assert ref.decode_key == "k1"


async def test_channels_video_meta_allows_missing_decode_key(monkeypatch):
    """无 decode_key → MediaRef.decode_key=None（未加密/缺字段，跳过 WASM）。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")
    _clear_channels_detail_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "data": {
            "media": {"full_url": "http://cdn/v.mp4"},
        }})

    client = _mock_client(handler)
    ref = await channels_video_meta("https://weixin.qq.com/sph/abc", client=client)
    assert ref.platform == "wechat_channels"
    assert ref.download_url == "http://cdn/v.mp4"
    assert ref.decode_key is None
    await client.aclose()


def test_parse_channels_video_allows_missing_decode_key():
    v = _parse_channels_video({
        "title": "t", "read_count": 1, "nickname": "a",
        "media": {"full_url": "http://cdn/v.mp4"},
    })
    assert v is not None
    assert v.platform == "wechat_channels"
    assert v.decode_key is None


def test_video_meta_to_media_ref_channels_allows_missing_decode_key():
    v = VideoMeta(
        title="t", play_count=1, fav_count=0,
        download_url="http://cdn/a.mp4",
        author="a",
        decode_key=None,
        platform="wechat_channels",
    )
    ref = video_meta_to_media_ref(v)
    assert ref.platform == "wechat_channels"
    assert ref.decode_key is None


async def test_channels_detail_cached_across_precheck_and_account(monkeypatch):
    """#9：同一分享链 precheck → account_top_videos 只打一次 fetch_video_detail。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")
    _clear_channels_detail_cache()
    calls = {"detail": 0, "videos": 0}
    url = "https://weixin.qq.com/sph/cache-me"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_video_detail"):
            calls["detail"] += 1
            return httpx.Response(200, json={"code": 200, "data": {
                "username": "v2_cache@finder",
                "nickname": "n",
                "media": {"full_url": "http://cdn/x", "decode_key": "k"},
            }})
        if request.url.path.endswith("fetch_user_videos"):
            calls["videos"] += 1
            return httpx.Response(200, json={"code": 200, "data": {
                "videos": [{
                    "title": "t", "read_count": 1, "nickname": "n",
                    "media": {"full_url": "http://cdn/v.mp4", "decode_key": "k1"},
                }],
                "up_continue": False,
            }})
        return httpx.Response(500, text=request.url.path)

    client = _mock_client(handler)
    r = await precheck(url, client=client)
    assert r == {"reachable": True, "video_count": 1}
    videos = await account_top_videos(url, n=5, client=client)
    assert len(videos) == 1
    assert calls["detail"] == 1
    assert calls["videos"] == 2  # precheck 首页 + account 再拉一页
    await client.aclose()


# ---- Task 7 平台门禁：account_top_videos / precheck 抖音 only --------------

async def test_account_top_videos_channels_share_uses_video_detail_username(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_video_detail"):
            return httpx.Response(200, json={"code": 200, "data": {
                "username": "v2_from_share@finder",
                "nickname": "前进的胖掌柜",
                "media": {"full_url": "http://x", "decode_key": "1"},
            }})
        if request.url.path.endswith("fetch_user_videos"):
            body = json.loads(request.content.decode())
            assert body["username"] == "v2_from_share@finder"
            return httpx.Response(200, json={"code": 200, "data": {
                "videos": [{
                    "title": "[{'shortTitle': '职能部门正在杀死公司'}]",
                    "read_count": 3, "fav_count": 0, "like_count": 2,
                    "nickname": "前进的胖掌柜",
                    "media": {"full_url": "http://cdn/v.mp4", "decode_key": "dk1"},
                }],
                "up_continue": False,
            }})
        return httpx.Response(500, text="no")
    client = _mock_client(handler)
    videos = await account_top_videos("https://weixin.qq.com/sph/ADk6xBh2hq", n=20, client=client)
    assert len(videos) == 1
    assert videos[0].title == "职能部门正在杀死公司"
    assert videos[0].decode_key == "dk1"
    await client.aclose()


async def test_account_top_videos_channels_share_paginates_until_n_or_max_pages(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")
    pages_hit = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_video_detail"):
            return httpx.Response(200, json={"code": 200, "data": {
                "username": "v2_share@finder", "nickname": "前进的胖掌柜",
            }})
        if request.url.path.endswith("fetch_user_videos"):
            pages_hit["n"] += 1
            body = json.loads(request.content.decode())
            assert body["username"] == "v2_share@finder"
            assert body["raw"] is False
            # 首页不带 last_buffer；后续页带上一页返回的 last_buffer
            if pages_hit["n"] == 1:
                assert "last_buffer" not in body
            else:
                assert body["last_buffer"] == f"buf{pages_hit['n'] - 1}"
            vids = [{
                "title": f"t{i}",
                "nickname": "前进的胖掌柜",
                "read_count": 10, "fav_count": 1, "like_count": 9,
                "media": {"full_url": f"http://cdn/{pages_hit['n']}-{i}.mp4", "decode_key": f"k-{pages_hit['n']}-{i}"},
            } for i in range(6)]
            return httpx.Response(200, json={"code": 200, "data": {
                "username": "v2_share@finder", "nickname": "前进的胖掌柜",
                "videos": vids, "up_continue": True, "last_buffer": f"buf{pages_hit['n']}",
            }})
        return httpx.Response(500, text="no")
    client = _mock_client(handler)
    videos = await account_top_videos("https://weixin.qq.com/sph/ADk6xBh2hq", n=20, client=client)
    assert len(videos) == 20
    assert pages_hit["n"] == 4           # 6×4=24 ≥ 20，4 页截断（= max_pages 硬上限）
    assert videos[0].platform == "wechat_channels"
    assert videos[0].decode_key == "k-1-0"
    assert videos[0].download_url.endswith("1-0.mp4")
    assert videos[0].play_count == 10  # read_count 代理
    assert videos[0].fav_count == 1    # 收藏
    assert videos[0].like_count == 9   # 点赞独立
    await client.aclose()


# ---- video_meta_to_media_ref：channels 装配 decode_key ----------------------

def test_video_meta_to_media_ref_channels_keeps_decode_key_pair():
    v = VideoMeta(
        title="t", play_count=1, fav_count=2,
        download_url="http://cdn/a.mp4?tok=1",
        author="胖掌柜",
        decode_key="910035402",
        platform="wechat_channels",
    )
    ref = video_meta_to_media_ref(v)
    assert ref.platform == "wechat_channels"
    assert ref.download_url == v.download_url
    assert ref.decode_key == "910035402"
    assert ref.headers == CHANNELS_DOWNLOAD_HEADERS
    assert ref.headers is not CHANNELS_DOWNLOAD_HEADERS
    assert ref.author == "胖掌柜"


# ---- _account_entry_kind：拆账号入口分类 ---------------------------------

def test_account_entry_kind_classifies_inputs():
    assert _account_entry_kind("https://v.douyin.com/abc/") == "douyin"
    assert _account_entry_kind("https://weixin.qq.com/sph/ADk6xBh2hq") == "channels_share"
    assert _account_entry_kind("not-a-url") == "unknown"
    assert _account_entry_kind("https://example.com/x") == "unknown"
    # 裸 sph 短号不再识别为 channels_id（TikHub fetch_channel_id_to_username 对任意 sph
    # 派生 @finder username，无法识别 bogus）→ 走 unknown，不发 HTTP。
    assert _account_entry_kind("sphi9BjV8GK0Zsl") == "unknown"
    assert _account_entry_kind("  sphABC_123  ") == "unknown"
    # 裸串不得崩
    assert _account_entry_kind("sph") == "unknown"
