"""TikHub 数据 API 客户端（拆账号/拆视频取数 + 抖音热榜）。

基址强约束：``https://api.tikhub.dev``（主域名 api.tikhub.io 被墙，国内必须用 .dev；
计划 §Task 3.1 明确要求 verbatim）。``TIKHUB_BASE_URL`` 配置项默认即此值，仅给联调期
切环境留口子——默认值不可改。

鉴权：``Authorization: Bearer <TIKHUB_API_KEY>``（TikHub 标准 Bearer 头）。

接口路径（抖音）：
  - get_sec_user_id:           GET /api/v1/douyin/web/get_sec_user_id?url=
  - fetch_user_post_videos:    GET /api/v1/douyin/app/v3/fetch_user_post_videos?sec_user_id=&count=&max_cursor=
  - fetch_one_video_by_share:  GET /api/v1/douyin/web/fetch_one_video_by_share_url?share_url=
  - fetch_hot_total_list:      GET /api/v1/douyin/billboard/fetch_hot_total_list?page=&page_size=
                               （page/page_size 必填，缺则 422）
  - fetch_video_statistics:    GET /api/v1/douyin/app/v3/fetch_video_statistics?aweme_ids=
                               （每次最多 2 个；补播放/点赞/评论/分享/收藏）

接口路径（视频号，单视频）：
  - fetch_video_detail:        POST /api/v1/wechat_channels/v2/fetch_video_detail
                               body ``{share_url, raw:false}`` → media.full_url + decode_key

响应形状：TikHub 统一包络 ``{code, request_id, message, data: {...}}``。抖音：
``desc``/``create_time``/``text_extra``/``statistics.{play,digg,comment,share,collect}_count``；
列表常缺播放 → 补调 statistics。视频号：``title``/``create_time``/
``read/like/fav/forward/comment_count``。

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
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.datasource import DataSourceError
from app.datasource.media import MediaRef

log = logging.getLogger(__name__)

# 分享链 fetch_video_detail 进程内短缓存：precheck 与 account_top_videos /
# channels_video_meta 常对同一 URL 各打一次，TTL 内复用响应减 TikHub 双倍账单。
_CHANNELS_DETAIL_TTL_SEC = 300.0
_CHANNELS_DETAIL_CACHE_MAX = 64
_channels_detail_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()

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
_PATH_VIDEO_STATISTICS = "/api/v1/douyin/app/v3/fetch_video_statistics"

# 抖音热点总榜分页：TikHub 把 page/page_size/type 列为必填 query（缺则 422）。
# type=snapshot=按时刻取当前热榜（range=按时间范围，今日热点不需要）。
# page_size 取 50 覆盖整张总榜；过大可能被上游拒，联调期如需再调。
_HOT_BOARD_PAGE = 1
_HOT_BOARD_PAGE_SIZE = 50
_HOT_BOARD_TYPE = "snapshot"
_PATH_CHANNELS_VIDEO_DETAIL = "/api/v1/wechat_channels/v2/fetch_video_detail"
_PATH_CHANNELS_USER_VIDEOS = "/api/v1/wechat_channels/v2/fetch_user_videos"

# 视频号账号视频列表翻页硬上限：即使 up_continue 恒真，最多拉 4 页（避免账号视频数
# 极大时的取数放大；Task 3.3 按返回 N 计费，上限保护扣费边界）。
_CHANNELS_MAX_PAGES = 4

# 视频号 CDN 下载头（防盗链弱约束；完整鉴权已在 full_url = url+url_token）。
CHANNELS_DOWNLOAD_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# TikHub HTTP 超时（元数据 API，非媒体 CDN 下载——下载见 media/download.py）。
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# === TikHub 瞬时错误重试（GET 默认重试；POST 默认不重试防翻页放大账单） ===
# GET 端点瞬时网络抖动/5xx 重试安全。视频号翻页 POST（fetch_user_videos）上游可能
# 已记账，5xx 再打会放大账单 → ``_post_json`` 默认 retry=False。单次非翻页 POST
# （fetch_video_detail 一次性 detail）可显式传 retry=True，瞬时重试不构成放大。
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
    """单个视频元数据。供 Task 3.2 拆视频/拆账号消费。

    互动字段语义（对齐 TikHub）：
    - ``like_count`` = digg（点赞）/ 视频号 like_count
    - ``collect_count`` / ``fav_count`` = 收藏（抖音 collect；视频号 fav）。``fav_count``
      保留给历史 DB 列名，值恒等于 ``collect_count``。
    - ``comment_count`` / ``share_count`` / ``play_count`` 各自独立。
    - ``duration_sec`` = 时长（秒）；无则 None。抖音 ``video.duration`` 为毫秒，视频号
      ``media.duration`` 多为秒，解析时归一。
    """
    title: str
    play_count: int | None
    fav_count: int  # = collect_count（收藏），非 digg
    download_url: str
    # 备选直链（抖音 play_addr.url_list 多 CDN 节点）；首个即 download_url。
    download_urls: list[str] = field(default_factory=list)
    author: str = ""  # aweme author.nickname / author.nick_name
    decode_key: str | None = None  # 视频号 CDN 解码键；抖音无需解码，留 None
    platform: str = "douyin"  # "douyin" / "wechat_channels"；驱动 media_ref 装配分支
    description: str = ""
    tags: list[str] = field(default_factory=list)
    published_at: int | None = None  # unix 秒；无则 None
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    collect_count: int = 0
    duration_sec: int | None = None  # 时长秒；上游无则 None
    aweme_id: str | None = None  # 抖音作品 id，供 statistics 补播放


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


def _body_says_retry(resp: httpx.Response) -> bool:
    """TikHub 瞬时故障 400 的判定：body（message/detail 等）大小写不敏感含 'retry'。

    TikHub 把上游瞬时故障误标成 HTTP 400 + body 含 "Please retry."（如
    ``{"detail":{"message":"Request failed. Please retry. ..."}}``）。属上游不规范行为：
    按字面「4xx 不重试」会把可恢复错误打成 precheck/拆账号失败。

    判定直接对 ``resp.text`` 做 lowercase 子串匹配——覆盖 detail 为对象
    （{message: "Please retry."}）或列表（422 验证错误，不含 retry）等各种嵌套形状，
    无需脆弱的 JSON 路径假设。非通用 4xx 重试：仅此窄口触发，401/403/404、无 retry
    字样的 400 仍一次抛。
    """
    return "retry" in (resp.text or "").lower()


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    retry: bool = True,
) -> dict:
    """TikHub JSON 请求（GET/POST）。

    ``retry=True``（默认，供 GET）：瞬时传输错误与 HTTP 5xx 最多
    ``_RETRY_ATTEMPTS`` 次。``retry=False``（POST 计费）：单次尝试，失败立即
    ``DataSourceError``。4xx / 业务 code!=200 从不重试——**例外**：TikHub 把瞬时
    上游故障误标成 400 + body 含 "retry"（上游不规范，非通用 4xx 重试），此窄口
    与 5xx 同一套 backoff 重试；401/403/404、无 retry 字样的 400 仍一次抛。
    """
    url = f"{_base_url()}{path}"
    attempts = _RETRY_ATTEMPTS if retry else 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
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
            if attempt < attempts - 1:
                log.warning(
                    "tikhub transient transport error (%s) attempt %d/%d, retrying: %s",
                    path, attempt + 1, attempts, e,
                )
                await _sleep(_RETRY_BACKOFFS[attempt])
                continue
            raise DataSourceError(
                f"tikhub transport failed ({path}) after {attempts} attempts: {e}"
            ) from e
        except httpx.HTTPError as e:
            # 其他 httpx 错误（非瞬时）——不重试
            raise DataSourceError(f"tikhub transport failed ({path}): {e}") from e
        # 5xx 视为上游瞬时错误，可重试（仅 retry=True）
        if 500 <= resp.status_code < 600:
            if attempt < attempts - 1:
                log.warning(
                    "tikhub http %d (%s) attempt %d/%d, retrying",
                    resp.status_code, path, attempt + 1, attempts,
                )
                last_exc = DataSourceError(
                    f"tikhub http {resp.status_code} ({path}): {resp.text[:200]}"
                )
                await _sleep(_RETRY_BACKOFFS[attempt])
                continue
            raise DataSourceError(f"tikhub http {resp.status_code} ({path}): {resp.text[:200]}")
        # TikHub 瞬时故障误标 400 + body 含 "retry"（上游不规范；非通用 4xx 重试）——
        # 窄口重试，次数/backoff 与 5xx 同一套。仅 status==400 且 _body_says_retry 触发。
        if resp.status_code == 400 and _body_says_retry(resp):
            if attempt < attempts - 1:
                log.warning(
                    "tikhub http 400 retryable (%s) attempt %d/%d, retrying: %s",
                    path, attempt + 1, attempts, resp.text[:200],
                )
                last_exc = DataSourceError(f"tikhub http 400 ({path}): {resp.text[:200]}")
                await _sleep(_RETRY_BACKOFFS[attempt])
                continue
            raise DataSourceError(f"tikhub http 400 ({path}): {resp.text[:200]}")
        if resp.status_code < 200 or resp.status_code >= 300:
            # 其余 4xx —— 不重试
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
    """GET TikHub JSON（幂等读，瞬时错误可重试）。"""
    return await _request_json(client, "GET", path, params=params, retry=True)


async def _post_json(
    client: httpx.AsyncClient, path: str, json_body: dict, *, retry: bool = False
) -> dict:
    """POST TikHub JSON。

    默认 ``retry=False``（防翻页放大账单——``fetch_user_videos`` 等翻页 POST 上游
    可能已记账，5xx 再打会重复计费）。单次非翻页 POST（如 ``fetch_video_detail``
    一次性 detail 查询，precheck channels_share 路径）可传 ``retry=True``：瞬时
    5xx 重试 3 次不构成放大，避免单次瞬时失败即 502（Java 不预扣、用户重试）。
    """
    return await _request_json(
        client, "POST", path, json_body=json_body, retry=retry
    )


def _safe_int(value: object, default: int = 0) -> int:
    """TikHub 计数字段 → int。容忍 ``None`` / ``"1,234"`` / ``"1.2k"``；失败 → default。"""
    if value is None or value is False:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip().replace(",", "").replace(" ", "")
    if not s:
        return default
    try:
        if len(s) > 1 and s[-1] in "kKmM":
            mult = 1_000 if s[-1] in "kK" else 1_000_000
            return int(float(s[:-1]) * mult)
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _title_from_desc(desc: str) -> str:
    """描述首句作标题（≤80 字）；空则空串。"""
    text = (desc or "").strip()
    if not text:
        return ""
    first = re.split(r"[\n。！？]", text, maxsplit=1)[0].strip() or text
    return first[:80]


def _extract_hashtags(*texts: object) -> list[str]:
    """从 text_extra / cha_list / 正文 #话题 抽标签，去重保序。"""
    seen: list[str] = []
    for blob in texts:
        if isinstance(blob, list):
            for it in blob:
                if not isinstance(it, dict):
                    continue
                name = (
                    it.get("hashtag_name")
                    or it.get("cha_name")
                    or it.get("tag_name")
                    or it.get("name")
                )
                if isinstance(name, str) and name.strip():
                    t = name.strip().lstrip("#")
                    if t and t not in seen:
                        seen.append(t)
        elif isinstance(blob, str):
            for m in re.finditer(r"#([^\s#]+)", blob):
                t = m.group(1).strip()
                if t and t not in seen:
                    seen.append(t)
    return seen


def _unix_published(value: object) -> int | None:
    """create_time → unix 秒；非法 / 0 → None。"""
    if value is None or value is False:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    # 毫秒兜底
    if n > 10_000_000_000:
        n = n // 1000
    return n if n > 0 else None


def _duration_sec(value: object, *, unit: str = "auto") -> int | None:
    """时长 → 秒。``unit``: ``ms`` / ``s`` / ``auto``（≥1000 当毫秒）。0/非法 → None。"""
    if value is None or value is False:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if unit == "ms" or (unit == "auto" and n >= 1000):
        n = n / 1000.0
    sec = int(round(n))
    return sec if sec > 0 else None


# 转写只要 16k 单声道语音，但 ``play_addr`` 给的是平台默认画质档（实测 929s 视频 37.3MB）。
# 同一个 aweme 里还躺着两个更省的源，按字节数从小到大优先，拼成一个有序列表交给
# ``download_urls`` 逐个试——任一级缺失或下载失败都自然降级，无需额外分支：
#   1. ``video.bit_rate_audio``：纯音频轨，约 play_addr 的 14%。实测确认是视频原声而非
#      BGM（转写与整片逐字一致），但覆盖率只有 ~40%。
#   2. ``video.bit_rate`` 的最低画质档：约 48%，覆盖 ~100%。
#   3. ``play_addr``：兜底，即改造前的行为。
# 依据与实测数据：docs/spikes/douyin-audio-only-download.md
#
# 档位不取到底（360p 等）而是设下限，避免极低码率连带压坏音轨、拖累 ASR 准确率。
_ASR_MIN_GEAR_HEIGHT = 540
# data_size 缺失时的排序兜底：当「未知大小」而非「0 字节」，不让它假装最小档胜出。
_UNKNOWN_SIZE = 1 << 62


def _clean_urls(raw: object) -> list[str]:
    """``url_list`` → 去空去重的直链列表；非 list 输入返回空。"""
    out: list[str] = []
    for u in raw if isinstance(raw, list) else []:
        if isinstance(u, str):
            s = u.strip()
            if s and s not in out:
                out.append(s)
    return out


def _gear_height(gear_name: object) -> int | None:
    """``gear_name`` → 纵向分辨率：``540_2_1``→540、``additional_1080_1_1``→1080。

    取首个 >=100 的数字段，跳过 ``_1_1`` 这类档内序号。解析不出返回 None（该档跳过）。
    """
    for part in str(gear_name or "").split("_"):
        if part.isdigit() and int(part) >= 100:
            return int(part)
    return None


def _audio_track_urls(video: dict) -> list[str]:
    """``video.bit_rate_audio`` 的纯音频直链；没有则空列表。

    形状不稳：实测该字段既出现过 dict 也出现过 list，缺失时是空 ``{}``（与
    ``_resolve_sec_user_id`` 的 str/dict 分歧同类），两种都吃。且内层 ``url_list``
    是 ``{main_url, backup_url}`` 的 dict——与 ``play_addr.url_list`` 的 list 形状不同。
    """
    node = video.get("bit_rate_audio")
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return []
    meta = node.get("audio_meta")
    if not isinstance(meta, dict):
        return []
    urls = meta.get("url_list")
    if not isinstance(urls, dict):
        return []
    out: list[str] = []
    for key in ("main_url", "backup_url"):
        u = urls.get(key)
        if isinstance(u, str) and u.strip() and u.strip() not in out:
            out.append(u.strip())
    return out


def _low_gear_urls(video: dict) -> list[str]:
    """``video.bit_rate`` 中不低于 ``_ASR_MIN_GEAR_HEIGHT`` 的最小档直链。"""
    gears = video.get("bit_rate")
    if not isinstance(gears, list):
        return []
    best_size = _UNKNOWN_SIZE
    best_urls: list[str] = []
    for g in gears:
        if not isinstance(g, dict):
            continue
        height = _gear_height(g.get("gear_name"))
        if height is None or height < _ASR_MIN_GEAR_HEIGHT:
            continue
        addr = g.get("play_addr")
        if not isinstance(addr, dict):
            continue
        urls = _clean_urls(addr.get("url_list"))
        if not urls:
            continue
        size = _safe_int(addr.get("data_size")) or _UNKNOWN_SIZE
        if not best_urls or size < best_size:
            best_size, best_urls = size, urls
    return best_urls


def _asr_source_urls(video: dict) -> list[str]:
    """按「字节数从小到大」拼出该视频的候选下载源（纯音频轨 → 低画质档 → play_addr）。"""
    play_addr = video.get("play_addr")
    default_urls = _clean_urls(
        (play_addr or {}).get("url_list") if isinstance(play_addr, dict) else None
    )
    ordered: list[str] = []
    for group in (_audio_track_urls(video), _low_gear_urls(video), default_urls):
        for u in group:
            if u not in ordered:
                ordered.append(u)
    return ordered


def _parse_video(item: dict) -> VideoMeta:
    stats = item.get("statistics") or {}
    if not isinstance(stats, dict):
        stats = {}
    video = item.get("video") or {}
    if not isinstance(video, dict):
        video = {}
    author_obj = item.get("author") or {}
    author = str(author_obj.get("nickname") or author_obj.get("nick_name") or "")
    # 候选源有序列表：纯音频轨 → 低画质档 → play_addr 多 CDN 节点（见 _asr_source_urls）。
    # 下载层逐个尝试，慢流/失败时换下一个，避免单点卡死耗满转写墙钟。
    download_urls = _asr_source_urls(video)
    download_url = download_urls[0] if download_urls else ""
    desc = str(item.get("desc") or "")
    like = _safe_int(stats.get("digg_count"))
    collect = _safe_int(stats.get("collect_count"))
    aweme_id = item.get("aweme_id") or item.get("awemeId") or item.get("id")
    aweme_id_s = str(aweme_id).strip() if aweme_id not in (None, "") else None
    tags = _extract_hashtags(
        item.get("text_extra"),
        item.get("cha_list"),
        desc,
    )
    # 抖音文档：video.duration 毫秒；偶发顶层 duration
    duration = _duration_sec(video.get("duration"), unit="ms")
    if duration is None:
        duration = _duration_sec(item.get("duration"), unit="auto")
    return VideoMeta(
        title=_title_from_desc(desc) or desc[:80],
        play_count=_safe_int(stats.get("play_count")),
        fav_count=collect,  # DB 列 fav = 收藏
        download_url=download_url,
        download_urls=download_urls,
        author=author,
        description=desc,
        tags=tags,
        published_at=_unix_published(item.get("create_time")),
        like_count=like,
        comment_count=_safe_int(stats.get("comment_count")),
        share_count=_safe_int(stats.get("share_count")),
        collect_count=collect,
        duration_sec=duration,
        aweme_id=aweme_id_s,
        platform="douyin",
    )


def _apply_statistics_blob(meta: VideoMeta, stats: dict) -> None:
    """用 statistics 接口字段覆盖 meta（有值才盖）。"""
    if not isinstance(stats, dict):
        return
    play = stats.get("play_count")
    if play is not None:
        meta.play_count = _safe_int(play, meta.play_count)
    digg = stats.get("digg_count")
    if digg is not None:
        meta.like_count = _safe_int(digg, meta.like_count)
    comment = stats.get("comment_count")
    if comment is not None:
        meta.comment_count = _safe_int(comment, meta.comment_count)
    share = stats.get("share_count")
    if share is not None:
        meta.share_count = _safe_int(share, meta.share_count)
    collect = stats.get("collect_count")
    if collect is not None:
        c = _safe_int(collect, meta.collect_count)
        meta.collect_count = c
        meta.fav_count = c


async def _enrich_douyin_statistics(
    client: httpx.AsyncClient, metas: list[VideoMeta]
) -> list[VideoMeta]:
    """按 aweme_id 批量补调 fetch_video_statistics（每次最多 2 个）。失败单批跳过不抛。"""
    id_to_meta: dict[str, VideoMeta] = {
        m.aweme_id: m for m in metas if m.aweme_id and m.platform == "douyin"
    }
    ids = list(id_to_meta.keys())
    for i in range(0, len(ids), 2):
        batch = ids[i : i + 2]
        try:
            body = await _get_json(
                client, _PATH_VIDEO_STATISTICS, {"aweme_ids": ",".join(batch)}
            )
        except DataSourceError as e:
            log.warning("fetch_video_statistics failed for %s: %s", batch, e)
            continue
        data = body.get("data")
        # 响应可能是 list / dict(aweme_id→stats) / {statistics_list:[]}
        blobs: list[tuple[str | None, dict]] = []
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict):
                    aid = str(it.get("aweme_id") or it.get("id") or "") or None
                    stats = it.get("statistics") if isinstance(it.get("statistics"), dict) else it
                    blobs.append((aid, stats if isinstance(stats, dict) else {}))
        elif isinstance(data, dict):
            if any(k in data for k in ("play_count", "digg_count", "statistics")):
                # 单条扁平
                aid = str(data.get("aweme_id") or data.get("id") or "") or None
                stats = data.get("statistics") if isinstance(data.get("statistics"), dict) else data
                blobs.append((aid, stats if isinstance(stats, dict) else {}))
            else:
                for k, v in data.items():
                    if isinstance(v, dict):
                        stats = v.get("statistics") if isinstance(v.get("statistics"), dict) else v
                        blobs.append((str(k), stats if isinstance(stats, dict) else {}))
        # 按 id 对齐；对不上则按 batch 顺序回填
        unmatched = list(batch)
        for aid, stats in blobs:
            target_id = aid if aid in id_to_meta else (unmatched.pop(0) if unmatched else None)
            if target_id and target_id in id_to_meta:
                _apply_statistics_blob(id_to_meta[target_id], stats)
                if target_id in unmatched:
                    unmatched.remove(target_id)
    return metas


def video_meta_to_media_ref(v: VideoMeta) -> MediaRef:
    """VideoMeta → MediaRef（按 platform/decode_key 装配下载头 + 解码键）。

    ``v.platform=="wechat_channels"`` 或带 ``decode_key`` → 视频号分支：注入
    ``CHANNELS_DOWNLOAD_HEADERS`` 并透传 ``decode_key``（可为 None——未加密或
    TikHub 缺字段时跳过 decode，由 ffmpeg 实际成败判定）；否则抖音分支。
    ``headers=dict(...)`` 均复制新鲜字典，避免多个 ref 共享模块级可变 dict。
    ``title``/``author`` 空串归一为 ``None``（下游空值更稳）。
    ``raw_id`` 透传 ``aweme_id``——``transcribe`` 的转写缓存以它为键（CDN 直链带时效
    签名，按 URL 缓存必然全 miss）。视频号侧未解出稳定 id，为 None → 不参与缓存。
    """
    if v.platform == "wechat_channels" or v.decode_key:
        return MediaRef(
            platform="wechat_channels",
            download_url=v.download_url,
            download_urls=v.download_urls,
            headers=dict(CHANNELS_DOWNLOAD_HEADERS),
            decode_key=v.decode_key,
            title=v.title or None,
            author=v.author or None,
            raw_id=v.aweme_id or None,
        )
    return MediaRef(
        platform="douyin",
        download_url=v.download_url,
        download_urls=v.download_urls,
        headers=dict(DOUYIN_DOWNLOAD_HEADERS),
        title=v.title or None,
        author=v.author or None,
        raw_id=v.aweme_id or None,
    )


async def _resolve_sec_user_id(client: httpx.AsyncClient, url: str) -> str | None:
    """主页 URL → sec_user_id。解析失败返回 None（precheck 据此判不可达）。

    TikHub ``get_sec_user_id`` 的 ``data`` 直接是 sec_user_id 字符串（如
    ``MS4wLjABAAAA...``），不是 ``{sec_user_id: ...}`` 字典——早期实现误当 dict 解析，
    对真响应 ``str.get`` 抛 AttributeError。此处两种形状都兼容（dict 形状留给旧测试 mock
    与上游可能的形状回退）。
    """
    body = await _get_json(client, _PATH_GET_SEC_USER_ID, {"url": url})
    data = body.get("data")
    if isinstance(data, str):
        return data if data.strip() else None
    if isinstance(data, dict):
        val = data.get("sec_user_id")
        return val if isinstance(val, str) and val else None
    return None


async def account_top_videos(url: str, n: int = 20, *, client: httpx.AsyncClient | None = None) -> list[VideoMeta]:
    """取账号主页 Top N 视频（按 TikHub 返回顺序，默认 20 条）。

    按 ``_account_entry_kind(url)`` 分发：douyin 短链 → ``_resolve_sec_user_id`` +
    ``fetch_user_post_videos``；视频号分享链 → ``_resolve_channels_username``
    + ``fetch_user_videos`` 翻页（硬上限 ``_CHANNELS_MAX_PAGES``）。未知入口 → DataSourceError。

    未配置 TIKHUB_API_KEY → DataSourceError（懒初始化失败，per-request，不阻断 import）。
    解析失败 / 网络 / 业务码非 200 → DataSourceError。
    """
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    kind = _account_entry_kind(url)
    if kind == "unknown":
        raise DataSourceError(f"account_top_videos: unsupported url {url}")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        if kind == "douyin":
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
            videos = [_parse_video(it) for it in items[:n] if isinstance(it, dict)]
            return await _enrich_douyin_statistics(client, videos)
        # channels_share → 视频号账号视频列表
        username = await _resolve_channels_username(client, kind, url)
        if not username:
            raise DataSourceError(f"account_top_videos: cannot resolve channels username from {url}")
        videos: list[VideoMeta] = []
        last_buffer: str | None = None
        for _page in range(_CHANNELS_MAX_PAGES):
            req_body = {"username": username, "raw": False}
            if last_buffer:
                req_body["last_buffer"] = last_buffer
            body = await _post_json(client, _PATH_CHANNELS_USER_VIDEOS, req_body)
            data = body.get("data") or {}
            fallback_author = str(data.get("nickname") or "") or ""
            raw_items = data.get("videos") or []
            page_videos = [
                v for v in (_parse_channels_video(it, fallback_author=fallback_author) for it in raw_items)
                if v is not None
            ]
            videos.extend(page_videos)
            if len(videos) >= n:
                break
            if not data.get("up_continue"):
                break
            last_buffer = data.get("last_buffer")
            if not last_buffer:
                break
        return videos[:n]
    finally:
        if own:
            await client.aclose()


def _clear_channels_detail_cache() -> None:
    """测试 seam：清空分享链 detail 缓存。"""
    _channels_detail_cache.clear()


def _channels_detail_cache_get(share_url: str) -> dict | None:
    now = time.monotonic()
    item = _channels_detail_cache.get(share_url)
    if item is None:
        return None
    expires, body = item
    if expires <= now:
        _channels_detail_cache.pop(share_url, None)
        return None
    _channels_detail_cache.move_to_end(share_url)
    return body


def _channels_detail_cache_put(share_url: str, body: dict) -> None:
    _channels_detail_cache[share_url] = (
        time.monotonic() + _CHANNELS_DETAIL_TTL_SEC,
        body,
    )
    _channels_detail_cache.move_to_end(share_url)
    while len(_channels_detail_cache) > _CHANNELS_DETAIL_CACHE_MAX:
        _channels_detail_cache.popitem(last=False)


async def _fetch_channels_video_detail(
    client: httpx.AsyncClient, share_url: str
) -> dict:
    """``fetch_video_detail`` + 短 TTL 缓存（按 strip 后的 share_url）。"""
    key = share_url.strip()
    hit = _channels_detail_cache_get(key)
    if hit is not None:
        log.debug("channels video_detail cache hit url=%s", key[:64])
        return hit
    body = await _post_json(
        client,
        _PATH_CHANNELS_VIDEO_DETAIL,
        {"share_url": key, "raw": False},
        retry=True,
    )
    _channels_detail_cache_put(key, body)
    return body


async def _resolve_channels_username(client: httpx.AsyncClient, kind: str, raw: str) -> str | None:
    """视频号账号入口 → username。

    channels_share（分享链）→ ``_fetch_channels_video_detail``（带缓存），从单视频
    响应取 ``data.username``。其余 kind → None（不应被调用，
    ``account_top_videos`` 已 dispatch）。
    """
    if kind == "channels_share":
        body = await _fetch_channels_video_detail(client, raw)
        u = (body.get("data") or {}).get("username")
        return u if isinstance(u, str) and u else None
    return None


def _parse_channels_video(item: dict, *, fallback_author: str = "") -> VideoMeta | None:
    """视频号 ``fetch_user_videos`` 单条 → ``VideoMeta``。

    互动：read=播放 / like=点赞 / fav=收藏 / forward=分享 / comment=评论。
    无 media / 无 full_url → None（跳过该条）。
    """
    media = item.get("media") or {}
    if not isinstance(media, dict):
        return None
    full = str(media.get("full_url") or "").strip() or (
        str(media.get("url") or "") + str(media.get("url_token") or "")
    ).strip()
    if not full:
        return None
    dk = media.get("decode_key") or media.get("decodeKey")
    decode_key = str(dk).strip() if dk not in (None, "") else None
    title = _channels_title(item.get("title"))
    desc = str(item.get("desc") or item.get("description") or title or "")
    collect = _safe_int(item.get("fav_count"))
    like = _safe_int(item.get("like_count"))
    tags = _extract_hashtags(title, desc)
    # 视频号：media.duration 多为秒（spike 样例 570s）；≥1000 按毫秒兜底
    duration = _duration_sec(media.get("duration"), unit="auto")
    if duration is None:
        duration = _duration_sec(item.get("duration"), unit="auto")
    _read = _safe_int(item.get("read_count"))
    play_count = _read if _read > 0 else None
    return VideoMeta(
        title=title or _title_from_desc(desc),
        play_count=play_count,
        fav_count=collect,
        download_url=full,
        download_urls=[full],
        author=str(item.get("nickname") or fallback_author or ""),
        decode_key=decode_key,
        platform="wechat_channels",
        description=desc,
        tags=tags,
        published_at=_unix_published(item.get("create_time")),
        like_count=like,
        comment_count=_safe_int(item.get("comment_count")),
        share_count=_safe_int(item.get("forward_count")),
        collect_count=collect,
        duration_sec=duration,
    )


async def video_meta(url: str, *, client: httpx.AsyncClient | None = None) -> VideoMeta:
    """单视频元数据（按分享/短链 URL）。"""
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        # TikHub 该端点 query 参数名为 share_url（非 url）——发错名 → 422 missing share_url。
        body = await _get_json(client, _PATH_ONE_VIDEO_BY_SHARE, {"share_url": url})
        data = body.get("data") or {}
        item = data.get("aweme_detail") or data.get("item") or data
        if not isinstance(item, dict) or not item:
            raise DataSourceError(f"video_meta: empty item for url {url}")
        metas = await _enrich_douyin_statistics(client, [_parse_video(item)])
        return metas[0]
    finally:
        if own:
            await client.aclose()


async def channels_video_metrics(
    url: str, *, client: httpx.AsyncClient | None = None
) -> VideoMeta | None:
    """视频号单视频互动指标（按分享链）。

    复用 ``_fetch_channels_video_detail`` 拿 detail 响应，再交 ``_parse_channels_video``
    取数（read→play / like→like / fav→collect / forward→share / comment→comment）。
    detail 响应 ``data`` 字段结构与 ``fetch_user_videos`` 单条同源（同 ``read_count``/
    ``like_count``/``fav_count``/``comment_count``/``forward_count`` 顶层取数点）；
    若实际 detail 把指标嵌在子层，需在此调整取数路径（见核对风险注）。
    无 data / 无 media+full_url → None（``_parse_channels_video`` 返回 None 即如此）。
    """
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        body = await _fetch_channels_video_detail(client, url)
        item = body.get("data") or body
        if not isinstance(item, dict) or not item:
            return None
        return _parse_channels_video(item)
    finally:
        if own:
            await client.aclose()


async def video_metrics(url: str, *, client: httpx.AsyncClient | None = None) -> VideoMeta | None:
    """单视频互动五码（双平台分发）。

    抖音 host → ``video_meta``；视频号 host → ``channels_video_metrics``；
    未知平台 → None（非视频/不可达，端点翻译 found=false）。上游 DataSourceError 透传给端点。
    """
    plat = _platform_of(url)
    if plat == "douyin":
        return await video_meta(url, client=client)
    if plat == "wechat_channels":
        return await channels_video_metrics(url, client=client)
    return None


async def precheck(url: str, *, client: httpx.AsyncClient | None = None) -> dict:
    """轻量可达性 + 视频条数预检（Java 预扣额度门槛，Task 3.3）。

    按 ``_account_entry_kind(url)`` 分发：douyin → ``_resolve_sec_user_id`` +
    ``fetch_user_post_videos``（count=20，与 ``account_top_videos`` 默认 ``n=20``
    对齐）；视频号分享链 → ``_resolve_channels_username`` +
    ``fetch_user_videos`` **单页**（不翻页，仅取首页条数估规模）。未配置 /
    入口未知 / 解析 miss / 首页 0 条 → ``{"reachable": False, "video_count": 0}``（不抛）。

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
        kind = _account_entry_kind(url)
        if kind == "douyin":
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
            if not items:
                return {"reachable": False, "video_count": 0}
            return {"reachable": True, "video_count": len(items)}
        if kind == "channels_share":
            username = await _resolve_channels_username(client, kind, url)
            if not username:
                return {"reachable": False, "video_count": 0}
            body = await _post_json(
                client,
                _PATH_CHANNELS_USER_VIDEOS,
                {"username": username, "raw": False},
            )
            data = body.get("data") or {}
            videos = data.get("videos") or []
            if not videos:
                return {"reachable": False, "video_count": 0}
            return {"reachable": True, "video_count": len(videos)}
        return {"reachable": False, "video_count": 0}
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
        body = await _get_json(
            client,
            _PATH_HOT_TOTAL_LIST,
            {"page": _HOT_BOARD_PAGE, "page_size": _HOT_BOARD_PAGE_SIZE, "type": _HOT_BOARD_TYPE},
        )
        # TikHub 热榜响应嵌套两层 data：body.data.data.objs 才是热榜数组。
        # 外层 data 是 TikHub 统一包络 {code,data,extra,message}，内层 data 才含 {page,objs,last_update_time}。
        data = body.get("data") or {}
        inner = data.get("data") or {}
        raw = (
            inner.get("objs")
            or data.get("hot_list")
            or data.get("list")
            or data.get("billboard_list")
            or []
        )
        items: list[HotItem] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            items.append(
                HotItem(
                    # 真实字段是 sentence（热点词条）；title/word 留作其它端点形状的回退。
                    title=str(r.get("sentence") or r.get("title") or r.get("word") or ""),
                    # hot_score=热度值（如 11914614）；rank=位次（1..N），作 fallback。
                    hot_index=int(r.get("hot_score") or r.get("hot_index") or r.get("rank") or 0),
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


def _account_entry_kind(
    raw: str,
) -> Literal["douyin", "channels_share", "unknown"]:
    """拆账号入口分类：区分抖音短链 / 视频号分享页 / 未知。

    account_top_videos / precheck 据此分发到对应平台取数路径。与 ``_platform_of``
    不同：后者只做 host 归类，无法区分 channels_share 与其它 weixin.qq.com 入口
    （如裸 channels host）。两者共存，职责互补。

    顺序约束（load-bearing）：
      1. strip + 空串 → unknown；
      2. douyin host → douyin；
      3. weixin.qq.com host 且 path 含 ``/sph/`` → channels_share；
      4. 其余 → unknown（含裸 ``sph…`` 短号：``urlparse`` 无 host/scheme → unknown）。
    """
    s = (raw or "").strip()
    if not s:
        return "unknown"
    host = (urlparse(s).hostname or "").lower()
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return "douyin"
    path = urlparse(s).path or ""
    if host.endswith("weixin.qq.com") and "/sph/" in path:
        return "channels_share"
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
    """视频号分享短链 → ``MediaRef``（``full_url`` + 可选 ``decode_key`` + nickname/title）。

    ``POST …/fetch_video_detail``（经短缓存），``raw=false``。``full_url`` 优先；
    否则 ``url + url_token``。``decode_key`` 有则与 URL 同响应配对；缺省 None
    （未加密或字段缺失 → 跳过 WASM decode，直接转码）。
    """
    if not _is_configured():
        raise DataSourceError("TIKHUB_API_KEY not configured")
    if _platform_of(url) != "wechat_channels":
        raise DataSourceError(f"channels_video_meta: not a channels url {url}")
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        body = await _fetch_channels_video_detail(client, url)
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
        decode_key = (
            str(decode_key_raw).strip() if decode_key_raw not in (None, "") else None
        )
        raw_id = data.get("id")
        return MediaRef(
            platform="wechat_channels",
            download_url=full_url,
            download_urls=[full_url],
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
