# 视频号拆账号 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有拆账号契约上支持视频号：短号 `sph…` 或分享链 → finder username → TOP≤20（`max_pages=4`）→ 带 `decode_key` 的转写管线；前端双平台提示。

**Architecture:** `tikhub` 新增 `_account_entry_kind` 分发；视频号走 `fetch_channel_id_to_username` / `fetch_video_detail` → `fetch_user_videos`；扩展 `VideoMeta.decode_key/platform`；`video_meta_to_media_ref` 产出 channels `MediaRef`。`analyze_account` 锚点不动。`sks-web` 仅文案。`sks-server` 不改。

**Tech Stack:** Python 3.12 / httpx / pytest；前端 React `Analyze.tsx`；依赖已落地 Task 8（`decode_media` + WASM + node）。

**Spec:** `docs/superpowers/specs/2026-08-02-channels-account-analyze-design.md`

## Global Constraints

- 业务不可达 precheck → **一律** `{reachable: false, video_count: 0}`，**不抛** `DataSourceError`（传输失败 / 未配置 key 仍可抛）。
- `account_top_videos` 翻页 **`max_pages=4`**；precheck **只拉首页、不翻页**。
- 同条 `download_url` + `decode_key` 配对；禁止跨条目混用。
- **禁止**把分享链 path 段当 `channel_id`。
- 新增 `_account_entry_kind`；裸 `sph…` **不得**只靠 `_platform_of`。
- `analyze_account`：**只**依赖 `video_meta_to_media_ref` 升级；禁止重写循环锚点。
- 运行目录（后端）：`/Users/rick/work/sks-ai/.claude/worktrees/qwen-asr-media-pipeline`；测试：`.venv/bin/python -m pytest …`
- 前端：`/Users/rick/work/sks-web`（独立 commit）。
- Task 9（Dockerfile nodejs）不在本计划；缺 node 时单条 decode 已报 `DataSourceError`。

## File map

| 路径 | 动作 |
|------|------|
| `app/datasource/tikhub.py` | Modify — VideoMeta 字段；分类器；channels 列表/precheck；改写 douyin-only 门禁 |
| `tests/test_tikhub.py` | Modify — 分类器 / channels 列表 / precheck / 删门禁测 |
| `tests/test_account_analyze.py` | Modify — 断言 MediaRef.decode_key 透传 |
| `sks-web/src/pages/Analyze.tsx` | Modify — placeholder + 辅助文案 |
| `docs/spikes/…` 或 GO_LIVE | 不强制；Task 9 另计划 |

---

### Task 1: `VideoMeta` + `video_meta_to_media_ref` 支持 channels

**Files:**
- Modify: `app/datasource/tikhub.py` — `VideoMeta`、`video_meta_to_media_ref`
- Test: `tests/test_tikhub.py`

**Interfaces:**
- Produces: `VideoMeta(..., author="", decode_key: str | None = None, platform: str = "douyin")`
- Produces: `video_meta_to_media_ref(v) -> MediaRef` — 若 `v.platform=="wechat_channels"` 或 `v.decode_key` → channels headers + decode_key

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tikhub.py
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
```

- [ ] **Step 2: Run → FAIL**（无字段 / 仍走抖音头）

```bash
cd /Users/rick/work/sks-ai/.claude/worktrees/qwen-asr-media-pipeline
.venv/bin/python -m pytest tests/test_tikhub.py::test_video_meta_to_media_ref_channels_keeps_decode_key_pair -v
```

- [ ] **Step 3: 最小实现**

```python
@dataclass
class VideoMeta:
    title: str
    play_count: int
    fav_count: int
    download_url: str
    author: str = ""
    decode_key: str | None = None
    platform: str = "douyin"

def video_meta_to_media_ref(v: VideoMeta) -> MediaRef:
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
```

- [ ] **Step 4: Run → PASS**；全量 `test_tikhub` 中旧 `VideoMeta(...)` 仍兼容默认值

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: VideoMeta.decode_key/platform + channels MediaRef 装配"
```

---

### Task 2: `_account_entry_kind` 分类器

**Files:**
- Modify: `app/datasource/tikhub.py`
- Test: `tests/test_tikhub.py`

**Interfaces:**
- Produces: `_account_entry_kind(raw: str) -> Literal["douyin","channels_id","channels_share","unknown"]`

- [ ] **Step 1: 写失败测试**

```python
def test_account_entry_kind_classifies_inputs():
    assert _account_entry_kind("https://v.douyin.com/abc/") == "douyin"
    assert _account_entry_kind("sphi9BjV8GK0Zsl") == "channels_id"
    assert _account_entry_kind("https://weixin.qq.com/sph/ADk6xBh2hq") == "channels_share"
    assert _account_entry_kind("  sphABC_123  ") == "channels_id"
    assert _account_entry_kind("not-a-url") == "unknown"
    assert _account_entry_kind("https://example.com/x") == "unknown"
    # 裸串不得崩
    assert _account_entry_kind("sph") == "unknown"  # 过短 / 不匹配完整 pattern 则 unknown；按 ^sph[A-Za-z0-9_-]+$，「sph」仅前缀不够 → unknown
```

（若采用 `^sph[A-Za-z0-9_-]+$`，`sph` 单独不匹配 → `unknown`。示例短号须含 `sph` + 后续字符。）

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: 实现**

```python
import re
from typing import Literal

_SPH_ID_RE = re.compile(r"^sph[A-Za-z0-9_-]+$")

def _account_entry_kind(raw: str) -> Literal["douyin", "channels_id", "channels_share", "unknown"]:
    s = (raw or "").strip()
    if not s:
        return "unknown"
    if _SPH_ID_RE.fullmatch(s):
        return "channels_id"
    host = (urlparse(s).hostname or "").lower()
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return "douyin"
    path = urlparse(s).path or ""
    if host.endswith("weixin.qq.com") and "/sph/" in path:
        return "channels_share"
    return "unknown"
```

注意：短号检测在 host 之前（裸串无 host）。

- [ ] **Step 4: PASS + Commit**

```bash
git commit -m "feat: _account_entry_kind 拆账号入口分类（含裸 sph）"
```

---

### Task 3: 视频号 username 解析 + `fetch_user_videos` 列表（含翻页上限）

**Files:**
- Modify: `app/datasource/tikhub.py` — 路径常量、`_resolve_channels_username`、`_parse_channels_video`、`_channels_top_videos`；改写 `account_top_videos` 去掉 douyin-only、按 kind 分发
- Test: `tests/test_tikhub.py`

**锚点（LOAD-BEARING）：** 抖音分支体不变——保留 `_resolve_sec_user_id` + `fetch_user_post_videos` + `_parse_video` 循环原文；**仅**删除 `douyin-only` gate，外包一层 `kind = _account_entry_kind(url)` 分发（`douyin` → 原主体；channels → 新路径）。**禁止**重写/「顺手重构」抖音取号逻辑。

**Interfaces:**
- Consumes: `_account_entry_kind`、`_post_json`、`_channels_title`、`CHANNELS_DOWNLOAD_HEADERS`
- Produces: `account_top_videos` 对 channels 返回 `list[VideoMeta]`（`platform=wechat_channels`，配对 `decode_key`）
- Constants: `_PATH_CHANNELS_ID_TO_USER = "/api/v1/wechat_channels/v2/fetch_channel_id_to_username"`；`_PATH_CHANNELS_USER_VIDEOS = "/api/v1/wechat_channels/v2/fetch_user_videos"`；`_CHANNELS_MAX_PAGES = 4`

- [ ] **Step 1: 写失败测试（MockTransport）**

```python
async def test_account_top_videos_channels_id_paginates_until_n_or_max_pages(monkeypatch):
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")
    pages_hit = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("fetch_channel_id_to_username"):
            return httpx.Response(200, json={"code": 200, "data": {
                "channel_id": "sphi9BjV8GK0Zsl",
                "username": "v2_abc@finder",
                "nickname": "人民日报",
            }})
        if path.endswith("fetch_user_videos"):
            pages_hit["n"] += 1
            body = json.loads(request.content.decode())
            assert body["raw"] is False
            assert body["username"] == "v2_abc@finder"
            # 每页 6 条；第 4 页仍 up_continue，但 max_pages=4 应停 → 最多 24，再切到 n=20
            vids = [{
                "id": f"id-{pages_hit['n']}-{i}",
                "title": f"t{i}",
                "nickname": "人民日报",
                "read_count": 10,
                "fav_count": 1,
                "like_count": 9,
                "media": {
                    "full_url": f"http://cdn/{pages_hit['n']}-{i}.mp4",
                    "decode_key": f"k-{pages_hit['n']}-{i}",
                },
            } for i in range(6)]
            return httpx.Response(200, json={"code": 200, "data": {
                "username": "v2_abc@finder",
                "nickname": "人民日报",
                "videos": vids,
                "up_continue": True,
                "last_buffer": f"buf{pages_hit['n']}",
            }})
        return httpx.Response(404, json={"code": 404})

    client = _mock_client(handler)
    videos = await account_top_videos("sphi9BjV8GK0Zsl", n=20, client=client)
    assert len(videos) == 20
    assert pages_hit["n"] <= 4
    assert videos[0].platform == "wechat_channels"
    assert videos[0].decode_key == "k-1-0"
    assert videos[0].download_url.endswith("1-0.mp4")
    assert videos[0].play_count == 10  # read_count 代理
    assert videos[0].fav_count == 1    # fav 优先于 like
    await client.aclose()


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
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: 实现要点**

```python
_PATH_CHANNELS_ID_TO_USER = "/api/v1/wechat_channels/v2/fetch_channel_id_to_username"
_PATH_CHANNELS_USER_VIDEOS = "/api/v1/wechat_channels/v2/fetch_user_videos"
_CHANNELS_MAX_PAGES = 4

async def _resolve_channels_username(client, kind: str, raw: str) -> str | None:
    if kind == "channels_id":
        body = await _post_json(client, _PATH_CHANNELS_ID_TO_USER, {"channel_id": raw.strip(), "raw": False})
        u = (body.get("data") or {}).get("username")
        return u if isinstance(u, str) and u.endswith("@finder") else None
    if kind == "channels_share":
        body = await _post_json(client, _PATH_CHANNELS_VIDEO_DETAIL, {"share_url": raw.strip(), "raw": False})
        u = (body.get("data") or {}).get("username")
        return u if isinstance(u, str) and u else None
    return None

def _parse_channels_video(item: dict, *, fallback_author: str = "") -> VideoMeta | None:
    media = item.get("media") or {}
    if not isinstance(media, dict):
        return None
    full = str(media.get("full_url") or "").strip() or (
        str(media.get("url") or "") + str(media.get("url_token") or "")
    ).strip()
    if not full:
        return None
    dk = media.get("decode_key") or media.get("decodeKey")
    fav = item.get("fav_count")
    if fav is None:
        fav = item.get("like_count") or 0
    return VideoMeta(
        title=_channels_title(item.get("title")),
        play_count=int(item.get("read_count") or 0),
        fav_count=int(fav or 0),
        download_url=full,
        author=str(item.get("nickname") or fallback_author or ""),
        decode_key=str(dk).strip() if dk not in (None, "") else None,
        platform="wechat_channels",
    )

# account_top_videos:
#   kind = _account_entry_kind(url)
#   if kind == "unknown": raise DataSourceError(...)
#   if kind == "douyin":
#       <EXISTING BODY VERBATIM — _resolve_sec_user_id + fetch_user_post_videos + _parse_video>
#       # 禁止改动上述抖音主体
#   else:
#       username = await _resolve...; if not username: raise DataSourceError("channels username unresolved")
#       loop pages 0..max_pages-1 with last_buffer; append until n
```

- [ ] **Step 4: 删除/改写** `test_account_top_videos_rejects_channels_host_before_http`（不再 douyin-only）

- [ ] **Step 5: PASS + Commit**

```bash
git commit -m "feat: 视频号 account_top_videos（短号/分享链→TOP N，max_pages=4）"
```

---

### Task 4: precheck 视频号（单页、reachable:false）

**Files:**
- Modify: `app/datasource/tikhub.py` — `precheck`
- Test: `tests/test_tikhub.py`

**Interfaces:**
- Produces: channels 业务失败 → `{reachable: False, video_count: 0}`（不抛）

- [ ] **Step 1: 写失败测试**

```python
async def test_precheck_channels_id_miss_returns_unreachable(monkeypatch):
    """username=None 必须短路，不得再打 fetch_user_videos。"""
    monkeypatch.setattr(settings, "TIKHUB_API_KEY", "tk")
    calls = {"id": 0, "videos": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fetch_channel_id_to_username"):
            calls["id"] += 1
            return httpx.Response(200, json={"code": 200, "data": {
                "channel_id": "sphNope123456", "username": None, "error": "not found",
            }})
        if request.url.path.endswith("fetch_user_videos"):
            calls["videos"] += 1
            return httpx.Response(200, json={"code": 200, "data": {"videos": [{"media": {}}]}})
        return httpx.Response(500, text=f"unexpected {request.url.path}")

    client = _mock_client(handler)
    r = await precheck("sphNope123456", client=client)  # 满足 sph regex
    assert r == {"reachable": False, "video_count": 0}
    assert calls["id"] == 1
    assert calls["videos"] == 0  # miss 短路：禁止再拉作品列表
    await client.aclose()

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
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: 实现** — `precheck` 去掉 douyin-only；`unknown` / username 空 / 0 条 → unreachable dict；channels 只请求 videos 一页。

- [ ] **Step 4: 删除** `test_precheck_rejects_channels_host_before_http`

- [ ] **Step 5: PASS + Commit**

```bash
git commit -m "feat: 视频号 precheck 单页 + reachable:false 对齐抖音"
```

---

### Task 5: account_analyze 断言 decode_key 透传

**Files:**
- Modify: `tests/test_account_analyze.py`（实现侧通常 **零改** `graph.py`）
- Test: 同上

**Interfaces:**
- Consumes: Task 1 `video_meta_to_media_ref`

- [ ] **Step 1: 写完整测试**（对齐现有 `test_account_analyze.py` 的 monkeypatch 风格）

```python
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

    async def _insert_bench(task_id, title, play_count, fav_count, transcript, structure):
        return None

    async def _chat(skill, messages, json_schema=None):
        if skill == "account_analyze_item":
            return await _fake_chat_item()
        if skill == "account_analyze_summary":
            return await _fake_chat_summary()
        raise AssertionError(skill)

    monkeypatch.setattr("app.skills.account_analyze.graph.account_top_videos", _top)
    monkeypatch.setattr("app.skills.account_analyze.graph.transcribe", _transcribe)
    monkeypatch.setattr("app.skills.account_analyze.graph.check", _safe)
    monkeypatch.setattr("app.skills.account_analyze.graph.chat", _chat)
    monkeypatch.setattr("app.skills.account_analyze.graph.update_task", _update_task)
    monkeypatch.setattr("app.skills.account_analyze.graph.insert_benchmark_video", _insert_bench)

    from app.skills.account_analyze.graph import analyze_account
    await analyze_account(task_id=1, url="sphi9BjV8GK0Zsl")
    assert isinstance(captured["media"], MediaRef)
    assert captured["media"].decode_key == "dk-pair-1"
```

（复用文件顶层已有 `_safe` / `_fake_chat_item` / `_fake_chat_summary`；与 `test_account_done_status_…` 同 monkeypatch 风格。）

- [ ] **Step 2: Run**

```bash
.venv/bin/python -m pytest tests/test_account_analyze.py::test_analyze_account_passes_channels_decode_key_to_transcribe -v
```

Expected: PASS（Task 1 完成后）。若 FAIL 仅修 `video_meta_to_media_ref`，**禁止**重写 `analyze_account` 循环。

- [ ] **Step 3: Commit**

```bash
git commit -m "test: 拆账号透传视频号 MediaRef.decode_key"
```

---

### Task 6: sks-web 拆账号文案

**Files:**
- Modify: `/Users/rick/work/sks-web/src/pages/Analyze.tsx`
- Test: 无单测则手工 checklist

**对照现状（执行前必读组件）：** `Mode = 'videoText' | 'videoLink' | 'account'`；placeholder 约 L124–129；提交按钮下已有 `mode !== 'videoText'` 的异步说明（`text-[11.5px] text-paper-muted`，约 L217–221）——**保留该段**，另加 `mode === 'account'` 辅助行（可紧挨其前/后）。

- [ ] **Step 1: 改 placeholder + 辅助行**

```tsx
const placeholder =
  mode === 'videoText'
    ? '粘贴视频完整文案…'
    : mode === 'videoLink'
      ? '粘贴单条视频链接（抖音或视频号分享链）…'
      : '抖音：账号主页链接。视频号：sph 开头的视频号 ID，或该号任意一条分享链接。';

// 在提交按钮与「异步任务…」说明之间（或并列）增加：
{mode === 'account' && (
  <p className="mt-2 text-[11.5px] text-paper-muted">
    抖音请粘贴主页链接；视频号请粘贴视频号 ID（以 sph 开头）或该账号下任意一条分享链接（weixin.qq.com/sph/…）。
  </p>
)}
```

- [ ] **Step 2: 本地目视 `/analyze` 拆账号 tab**

- [ ] **Step 3: Commit（sks-web 仓）**

```bash
cd /Users/rick/work/sks-web
git add src/pages/Analyze.tsx
git commit -m "fix: 拆账号支持视频号短号/分享链提示文案"
```

---

### Task 7: 回归

- [ ] **Step 1: 后端全量**

```bash
cd /Users/rick/work/sks-ai/.claude/worktrees/qwen-asr-media-pipeline
.venv/bin/python -m pytest tests/ -q
```

Expected: 既有 3 个环境失败可保留；本计划相关全部 PASS；无新增失败。

- [ ] **Step 2: 手工联调 checklist（有 key 时）**

1. precheck `sphi9BjV8GK0Zsl`（或真实短号）→ reachable  
2. precheck 垃圾短号 → unreachable、Java 不扣费  
3. precheck / account 分享链 `https://weixin.qq.com/sph/...` → 可拉列表  
4. 单条 decode 需 node 在 PATH  

- [ ] **Step 3: 若有文档漂移，一行备注进 spec Status「Implemented」**（可选 commit）

---

## Spec coverage checklist

| Spec | Task |
|------|------|
| §0 Task 8 依赖 | 前提（已审） |
| §5.1 `_account_entry_kind` | 2 |
| §5.2 username 解析 | 3 |
| §5.3 TOP N + max_pages + 配对 decode | 3 |
| §5.4 precheck 单页 + reachable:false | 4 |
| §6 VideoMeta / media_ref | 1, 5 |
| §7 前端 | 6 |
| §9 测试 / 删门禁 | 3, 4, 7 |
| §10 不改 Java | 遵守 |

## Placeholder scan

无 TBD /「类似 Task N」省略实现。

## Plan review patch（2026-08-02）

| ID | 处理 |
|----|------|
| P2-1 | Task 3 增加 LOAD-BEARING：抖音分支体 verbatim，仅删 gate + dispatch 包裹 |
| P2-2 | Task 4 miss 测加 `calls["videos"]==0` 证明短路 |
| P3 | Task 5 补全可运行测试草图；Task 6 钉死对照 Analyze.tsx 行号/勿删异步说明 |
| P3 | 最终 whole-branch review 须覆盖 `3d0d23a`（Task 8） |
