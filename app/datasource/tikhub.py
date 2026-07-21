"""TikHub 数据 API 客户端（拆账号/拆视频取数 + 抖音热榜）。

基址强约束：``https://api.tikhub.dev``（主域名 api.tikhub.io 被墙，国内必须用 .dev；
计划 §Task 3.1 明确要求 verbatim）。``TIKHUB_BASE_URL`` 配置项默认即此值，仅给联调期
切环境留口子——默认值不可改。

鉴权：``Authorization: Bearer <TIKHUB_API_KEY>``（TikHub 标准 Bearer 头）。

接口路径（抖音）：
  - get_sec_user_id:           GET /api/v1/douyin/web/get_sec_user_id?url=
  - fetch_user_post_videos:    GET /api/v1/douyin/app/v3/fetch_user_post_videos?sec_user_id=&count=&max_cursor=
  - fetch_one_video_by_share:  GET /api/v1/douyin/web/fetch_one_video_by_share_url?url=
  - fetch_hot_total_list:      GET /api/v1/douyin/billboard/fetch_hot_total_list

响应形状：TikHub 统一包络 ``{code, request_id, message, data: {...}}``；抖音 aweme 字段
``desc``（标题）/``statistics.play_count``/``statistics.digg_count``（收藏）/``video.play_addr.url_list``
（下载直链）。**aweme_list / aweme_detail / hot_list 的精确嵌套层级需联调期用真实 key
核对**——此处解析按 TikHub 官方文档的常见形状，结构正确但未用真实 key 验证。

失败语义：HTTP 非 2xx、网络异常、业务 code != 200 → 统一抛 ``DataSourceError``，
Task 3.2/3.3 捕获后翻译（拆账号全量失败 → 全额退款 + 引导视频粘贴，PRD §11.3）。

模块级 seam：每个 public async 函数接受可选 ``client: httpx.AsyncClient | None``，
测试注入 ``httpx.MockTransport`` 客户端，绝不发真实网络请求（见 tests/test_tikhub.py）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.config import settings
from app.datasource import DataSourceError

log = logging.getLogger(__name__)

# === 端点路径常量（联调期如 TikHub 改路径，仅改这里） ===
_PATH_GET_SEC_USER_ID = "/api/v1/douyin/web/get_sec_user_id"
_PATH_USER_POST_VIDEOS = "/api/v1/douyin/app/v3/fetch_user_post_videos"
_PATH_ONE_VIDEO_BY_SHARE = "/api/v1/douyin/web/fetch_one_video_by_share_url"
_PATH_HOT_TOTAL_LIST = "/api/v1/douyin/billboard/fetch_hot_total_list"

# 阿里云录音文件识别用 OSS/public URL 直传，TikHub 下载直链多为短时签名 URL——
# 联调期需确认阿里云侧能否拉到该直链；如不能，需在 transcribe 内先下载再传 file_link。
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# === TikHub GET 瞬时错误重试策略（仅作用于幂等的 _get_json，不含 transcribe._submit_task） ===
# Brief Step 3「超时重试」：TikHub 四个 GET 端点均为幂等读，瞬时网络抖动/5xx 重试安全。
# transcribe._submit_task（阿里云 SubmitTask）非幂等——严禁重试（会创建重复转写任务）。
_RETRY_ATTEMPTS = 3  # 总尝试次数（首次 + 2 次重试）
_RETRY_BACKOFFS = (0.5, 1.5)  # 每次重试前 sleep 秒数（attempt 0→0.5s, attempt 1→1.5s）
# 仅瞬时传输错误 + 5xx 可重试；4xx（bad URL/auth）与业务 code!=200（确定性失败）不重试。
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
)


async def _sleep(seconds: float) -> None:
    """可被测试 monkeypatch 的 sleep seam（避免真实 backoff 拖慢测试）。"""
    await asyncio.sleep(seconds)


@dataclass
class VideoMeta:
    """单个视频元数据。供 Task 3.2 拆视频/拆账号消费。"""
    title: str
    play_count: int
    fav_count: int
    download_url: str


@dataclass
class HotItem:
    """热榜单条。供 Task 1.7 HotTopicJob（经 Task 3.3 路由）消费。"""
    title: str
    hot_index: int
    video_count: int


def _base_url() -> str:
    """实际使用的 base_url。settings 为空则回落到计划强约束的默认值。"""
    return (settings.TIKHUB_BASE_URL or "https://api.tikhub.dev").rstrip("/")


def _is_configured() -> bool:
    return bool(getattr(settings, "TIKHUB_API_KEY", ""))


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.TIKHUB_API_KEY}"}


async def _get_json(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    """GET TikHub JSON，带瞬时错误有界重试。

    重试仅覆盖瞬时传输错误（ConnectError/ReadTimeout/ConnectTimeout/
    RemoteProtocolError）与 HTTP 5xx。4xx 客户端错误与 TikHub 业务 code!=200
    属确定性失败，不重试。重试上限 ``_RETRY_ATTEMPTS`` 次，backoff 见
    ``_RETRY_BACKOFFS``。耗尽后抛 DataSourceError（与历史行为一致）。
    """
    url = f"{_base_url()}{path}"
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = await client.get(url, params=params, headers=_headers(), timeout=_TIMEOUT)
        except _RETRYABLE_TRANSPORT_ERRORS as e:
            last_exc = e
            if attempt < _RETRY_ATTEMPTS - 1:
                log.warning(
                    "tikhub transient transport error (%s) attempt %d/%d, retrying: %s",
                    path, attempt + 1, _RETRY_ATTEMPTS, e,
                )
                await _sleep(_RETRY_BACKOFFS[attempt])
                continue
            raise DataSourceError(
                f"tikhub transport failed ({path}) after {_RETRY_ATTEMPTS} attempts: {e}"
            ) from e
        except httpx.HTTPError as e:
            # 其他 httpx 错误（非瞬时）——不重试
            raise DataSourceError(f"tikhub transport failed ({path}): {e}") from e
        # 5xx 视为上游瞬时错误，可重试
        if 500 <= resp.status_code < 600:
            if attempt < _RETRY_ATTEMPTS - 1:
                log.warning(
                    "tikhub http %d (%s) attempt %d/%d, retrying",
                    resp.status_code, path, attempt + 1, _RETRY_ATTEMPTS,
                )
                last_exc = DataSourceError(
                    f"tikhub http {resp.status_code} ({path}): {resp.text[:200]}"
                )
                await _sleep(_RETRY_BACKOFFS[attempt])
                continue
            raise DataSourceError(f"tikhub http {resp.status_code} ({path}): {resp.text[:200]}")
        if resp.status_code < 200 or resp.status_code >= 300:
            # 4xx —— 不重试
            raise DataSourceError(f"tikhub http {resp.status_code} ({path}): {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError as e:
            raise DataSourceError(f"tikhub bad json ({path}): {e}") from e
        code = body.get("code")
        if code != 200:
            # 业务码失败 —— 确定性，不重试
            raise DataSourceError(f"tikhub business code={code} ({path}): {body.get('message', '')}")
        return body
    # 逻辑上不可达（重试循环必然在 continue 或 raise 处分支），防御性兜底
    raise DataSourceError(
        f"tikhub retry loop exited unexpectedly ({path}): {last_exc}"
    )


def _parse_video(item: dict) -> VideoMeta:
    stats = item.get("statistics") or {}
    play_addr = (item.get("video") or {}).get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    return VideoMeta(
        title=str(item.get("desc") or ""),
        play_count=int(stats.get("play_count") or 0),
        fav_count=int(stats.get("digg_count") or 0),
        download_url=str(url_list[0]) if url_list else "",
    )


async def _resolve_sec_user_id(client: httpx.AsyncClient, url: str) -> str | None:
    """主页 URL → sec_user_id。解析失败返回 None（precheck 据此判不可达）。"""
    body = await _get_json(client, _PATH_GET_SEC_USER_ID, {"url": url})
    data = body.get("data") or {}
    val = data.get("sec_user_id")
    return val if isinstance(val, str) and val else None


async def account_top_videos(url: str, n: int = 20, *, client: httpx.AsyncClient | None = None) -> list[VideoMeta]:
    """取账号主页 Top N 视频（按 TikHub 返回顺序，默认 20 条）。

    未配置 TIKHUB_API_KEY → DataSourceError（懒初始化失败，per-request，不阻断 import）。
    解析失败 / 网络 / 业务码非 200 → DataSourceError。
    """
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        sec_uid = await _resolve_sec_user_id(client, url)
        if not sec_uid:
            raise DataSourceError(f"cannot resolve sec_user_id from url: {url}")
        body = await _get_json(
            client,
            _PATH_USER_POST_VIDEOS,
            {"sec_user_id": sec_uid, "count": n, "max_cursor": 0, "sort_type": 0},
        )
        data = body.get("data") or {}
        items = data.get("aweme_list") or data.get("list") or []
        videos = [_parse_video(it) for it in items[:n]]
        return videos
    finally:
        if own:
            await client.aclose()


async def video_meta(url: str, *, client: httpx.AsyncClient | None = None) -> VideoMeta:
    """单视频元数据（按分享/短链 URL）。"""
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        body = await _get_json(client, _PATH_ONE_VIDEO_BY_SHARE, {"url": url})
        data = body.get("data") or {}
        item = data.get("aweme_detail") or data.get("item") or data
        if not isinstance(item, dict) or not item:
            raise DataSourceError(f"video_meta: empty item for url {url}")
        return _parse_video(item)
    finally:
        if own:
            await client.aclose()


async def precheck(url: str, *, client: httpx.AsyncClient | None = None) -> dict:
    """轻量可达性 + 视频条数预检（Java 预扣额度门槛，Task 3.3）。

    解析 sec_user_id 成功即 reachable=True；再拉一次首页视频列表（count=20，
    与 ``account_top_videos`` 默认 ``n=20`` 对齐），按返回条数记 video_count。

    video_count 是**首页视频数（≤20），非精确总数**；TikHub 为分页接口，如需精确
    总数需翻页聚合。Task 3.3 按此估算扣费 ``max(1, min(10, floor(N/2)))``——
    此处给一个真实首页规模使公式非退化（账号 ≥2 条视频即 ≥1 档 graduated）。

    返回 ``{"reachable": bool, "video_count": int}``。
    """
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        sec_uid = await _resolve_sec_user_id(client, url)
        if not sec_uid:
            return {"reachable": False, "video_count": 0}
        body = await _get_json(
            client,
            _PATH_USER_POST_VIDEOS,
            {"sec_user_id": sec_uid, "count": 20, "max_cursor": 0, "sort_type": 0},
        )
        data = body.get("data") or {}
        items = data.get("aweme_list") or data.get("list") or []
        return {"reachable": True, "video_count": len(items)}
    finally:
        if own:
            await client.aclose()


async def hot_board(*, client: httpx.AsyncClient | None = None) -> list[HotItem]:
    """抖音热榜（fetch_hot_total_list）。供 Task 1.7 HotTopicJob 经 Task 3.3 路由消费。"""
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        body = await _get_json(client, _PATH_HOT_TOTAL_LIST)
        data = body.get("data") or {}
        raw = data.get("hot_list") or data.get("list") or data.get("billboard_list") or []
        items: list[HotItem] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            items.append(
                HotItem(
                    title=str(r.get("title") or r.get("word") or ""),
                    hot_index=int(r.get("hot_index") or r.get("rank") or 0),
                    video_count=int(r.get("video_count") or 0),
                )
            )
        return items
    finally:
        if own:
            await client.aclose()
