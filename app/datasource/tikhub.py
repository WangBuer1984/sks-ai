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

接口路径（视频号，单视频）：
  - fetch_video_detail:        POST /api/v1/wechat_channels/v2/fetch_video_detail
                               body ``{share_url, raw:false}`` → media.full_url + decode_key

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

import ast
import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.datasource import DataSourceError
from app.datasource.media import MediaRef

log = logging.getLogger(__name__)

# === 抖音下载请求头 ===
# 抖音 CDN 直链下载需带浏览器 UA + Referer: https://www.douyin.com/，否则 403。
# ``video_meta_to_media_ref`` 通过 ``dict(DOUYIN_DOWNLOAD_HEADERS)`` 注入新鲜字典——
# 严禁直接传本模块字典给多个 ref（共享可变 dict，一处 mutate 全局生效）。
DOUYIN_DOWNLOAD_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}

# === 端点路径常量（联调期如 TikHub 改路径，仅改这里） ===
_PATH_GET_SEC_USER_ID = "/api/v1/douyin/web/get_sec_user_id"
_PATH_USER_POST_VIDEOS = "/api/v1/douyin/app/v3/fetch_user_post_videos"
_PATH_ONE_VIDEO_BY_SHARE = "/api/v1/douyin/web/fetch_one_video_by_share_url"
_PATH_HOT_TOTAL_LIST = "/api/v1/douyin/billboard/fetch_hot_total_list"
_PATH_CHANNELS_VIDEO_DETAIL = "/api/v1/wechat_channels/v2/fetch_video_detail"

# 视频号 CDN 下载头（防盗链弱约束；完整鉴权已在 full_url = url+url_token）。
CHANNELS_DOWNLOAD_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

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
    author: str = ""  # aweme author.nickname / author.nick_name
    decode_key: str | None = None  # 视频号 CDN 解码键；抖音无需解码，留 None
    platform: str = "douyin"  # "douyin" / "wechat_channels"；驱动 media_ref 装配分支


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


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """TikHub JSON 请求（GET/POST），带瞬时错误有界重试。

    重试仅覆盖瞬时传输错误（ConnectError/ReadTimeout/ConnectTimeout/
    RemoteProtocolError）与 HTTP 5xx。4xx 客户端错误与 TikHub 业务 code!=200
    属确定性失败，不重试。重试上限 ``_RETRY_ATTEMPTS`` 次，backoff 见
    ``_RETRY_BACKOFFS``。耗尽后抛 DataSourceError（与历史行为一致）。
    """
    url = f"{_base_url()}{path}"
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=_headers(),
                timeout=_TIMEOUT,
            )
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


async def _get_json(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    """GET TikHub JSON（``_request_json`` 薄包装，保持既有调用点）。"""
    return await _request_json(client, "GET", path, params=params)


async def _post_json(client: httpx.AsyncClient, path: str, json_body: dict) -> dict:
    """POST TikHub JSON（视频号 ``fetch_video_detail`` 等）。"""
    return await _request_json(client, "POST", path, json_body=json_body)


def _parse_video(item: dict) -> VideoMeta:
    stats = item.get("statistics") or {}
    play_addr = (item.get("video") or {}).get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    author_obj = item.get("author") or {}
    author = str(author_obj.get("nickname") or author_obj.get("nick_name") or "")
    return VideoMeta(
        title=str(item.get("desc") or ""),
        play_count=int(stats.get("play_count") or 0),
        fav_count=int(stats.get("digg_count") or 0),
        download_url=str(url_list[0]) if url_list else "",
        author=author,
    )


def video_meta_to_media_ref(v: VideoMeta) -> MediaRef:
    """VideoMeta → MediaRef（按 platform/decode_key 装配下载头 + 解码键）。

    ``v.platform=="wechat_channels"`` 或带 ``decode_key`` → 视频号分支：注入
    ``CHANNELS_DOWNLOAD_HEADERS`` 并透传 ``decode_key``；否则抖音分支，注入
    ``DOUYIN_DOWNLOAD_HEADERS``。``headers=dict(...)`` 均复制新鲜字典，避免多个
    ref 共享模块级可变 dict。``title``/``author`` 空串归一为 ``None``（下游空值更稳）。
    """
    if v.platform == "wechat_channels" or v.decode_key:
        return MediaRef(
            platform="wechat_channels",
            download_url=v.download_url,
            headers=dict(CHANNELS_DOWNLOAD_HEADERS),
            decode_key=v.decode_key,
            title=v.title or None,
            author=v.author or None,
        )
    return MediaRef(
        platform="douyin",
        download_url=v.download_url,
        headers=dict(DOUYIN_DOWNLOAD_HEADERS),
        title=v.title or None,
        author=v.author or None,
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
    # 平台门禁：拆账号仅支持抖音主页；视频号 host 走拆视频链路。
    if _platform_of(url) != "douyin":
        raise DataSourceError("account analyze supports douyin only; use video link for channels")
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
    # 平台门禁：拆账号仅支持抖音主页；视频号 host 走拆视频链路。
    if _platform_of(url) != "douyin":
        raise DataSourceError("account analyze supports douyin only; use video link for channels")
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


def _platform_of(url: str) -> str:
    """URL host → 平台标识（"douyin" / "wechat_channels" / "unknown"）。

    供 Task 6 ``resolve_media`` 与 Task 7 账号分析平台门复用——单一 host 归类源，
    避免 resolve_media 与 Task 7 各写一份正则导致口径漂移。
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "unknown"
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return "douyin"
    if host.endswith("weixin.qq.com"):
        return "wechat_channels"
    return "unknown"


def _channels_title(raw: object) -> str:
    """视频号 ``title`` 字段归一：精简响应偶发返回 shortTitle 列表的 Python/JSON 串。"""
    if raw is None:
        return ""
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return str(raw[0].get("shortTitle") or raw[0].get("title") or "")
    if not isinstance(raw, str):
        return str(raw)
    text = raw.strip()
    if not text:
        return ""
    # raw=false 样例：``"[{'shortTitle': '职能部门正在杀死公司', ...}]"``
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return str(parsed[0].get("shortTitle") or parsed[0].get("title") or "")
    except (ValueError, SyntaxError, MemoryError):
        pass
    m = re.search(r"shortTitle['\"]?\s*[:=]\s*['\"]([^'\"]+)", text)
    if m:
        return m.group(1)
    return text


async def channels_video_meta(url: str, *, client: httpx.AsyncClient | None = None) -> MediaRef:
    """视频号分享短链 → ``MediaRef``（``full_url`` + ``decode_key`` + nickname/title）。

    ``POST /api/v1/wechat_channels/v2/fetch_video_detail``，``raw=false`` 精简结构。
    ``full_url`` 优先；否则 ``url + url_token``。``decode_key`` 与 URL 必须同一次响应配对。
    """
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    if _platform_of(url) != "wechat_channels":
        raise DataSourceError(f"channels_video_meta: not a channels url {url}")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        body = await _post_json(
            client,
            _PATH_CHANNELS_VIDEO_DETAIL,
            {"share_url": url, "raw": False},
        )
        data = body.get("data") or {}
        if not isinstance(data, dict) or not data:
            raise DataSourceError(f"channels_video_meta: empty data for url {url}")
        media = data.get("media") or {}
        if not isinstance(media, dict):
            raise DataSourceError("channels_video_meta: media missing")
        full_url = str(media.get("full_url") or "").strip()
        if not full_url:
            base = str(media.get("url") or "")
            token = str(media.get("url_token") or media.get("urlToken") or "")
            full_url = (base + token).strip()
        if not full_url:
            raise DataSourceError("channels_video_meta: empty download_url")
        decode_key_raw = media.get("decode_key") or media.get("decodeKey")
        decode_key = str(decode_key_raw).strip() if decode_key_raw not in (None, "") else None
        raw_id = data.get("id")
        return MediaRef(
            platform="wechat_channels",
            download_url=full_url,
            headers=dict(CHANNELS_DOWNLOAD_HEADERS),
            decode_key=decode_key,
            title=_channels_title(data.get("title")) or None,
            author=str(data.get("nickname") or "") or None,
            raw_id=str(raw_id) if raw_id not in (None, "") else None,
        )
    finally:
        if own:
            await client.aclose()


async def resolve_media(url: str, *, client: httpx.AsyncClient | None = None) -> MediaRef:
    """分享/短链 URL → ``MediaRef``（含下载直链 + 平台必带头 + author/title）。

    抖音 host → 调现有 ``video_meta(url, client=client)`` → ``video_meta_to_media_ref``；
    视频号 host → ``channels_video_meta``（``full_url`` + ``decode_key``）；
    未知平台 → ``DataSourceError("unsupported url ...")``；
    ``video_meta`` 解析成功但 ``download_url`` 为空 → ``DataSourceError("empty download_url")``
    （高清 fallback 接口可选，本任务不实现——此即地板）。

    本函数是 ``analyze_video_link`` 与 ``transcribe`` 之间的解析 seam：把原始分享链
    解析成 ``MediaRef`` 后，**原始分享链不再向下游传递**——``transcribe`` 只见直链 + 头。
    """
    platform = _platform_of(url)
    if platform == "wechat_channels":
        return await channels_video_meta(url, client=client)
    if platform != "douyin":
        raise DataSourceError(f"resolve_media: unsupported url {url}")
    vm = await video_meta(url, client=client)
    if not vm.download_url:
        raise DataSourceError("resolve_media: empty download_url")
    return video_meta_to_media_ref(vm)
