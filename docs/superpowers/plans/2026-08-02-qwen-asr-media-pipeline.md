# Qwen ASR Media Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 硬切长转写为 TikHub 下载 → wav/切片 → `qwen3-asr-flash`，修复单视频分享链直传，接入抖音双路径 +（spike 通过后）视频号单视频。

**Architecture:** 新增 `app/datasource/media/*`（download / audio / qwen_asr / merge）；`tikhub.py` 产出 `MediaRef`；重写 `transcribe.py` 门面编排；`video_analyze` / `account_analyze` 两处心跳同改入参。不改校准短 ASR、不删 `aliyun-python-sdk-core`（内容安全仍用）。

**Tech Stack:** FastAPI / httpx / ffmpeg / DashScope `MultiModalConversation` / pytest；参考 `../clever-hans/backend/app/core/media/` 与 `pipeline.py` 拼接函数。

**Spec:** `docs/superpowers/specs/2026-08-02-qwen-asr-media-pipeline-design.md`

## Global Constraints

- 业务热路径只传 `MediaRef`；`transcribe(str)` = 裸下载测试 seam，不猜平台、不补 headers。
- ASR 失败 → `DataSourceError`，禁止静默空串。
- `download.py` 不 import TikHub；高清 fallback 只在 resolve / 门面。
- 长转写 `_is_configured()` 查 `ALIYUN_ASR_KEY` **且** `ffmpeg`/`ffprobe` 在 PATH（`shutil.which`）；缺失 → `DataSourceError`。**不得**从 pyproject 移除 `aliyun-python-sdk-core`。
- `audio.py` 的 subprocess 失败必须翻译为 `DataSourceError`，禁止让 `FileNotFoundError` 冒泡成 skill 层泛型 Exception。
- 两处 `_transcribe_with_heartbeat`（video + account）必须同 PR 改签名；**只改入参/调用点，保留 blocked / 双 except / per-item continue，禁止整函数重写。**
- `VideoMeta.author` 从 aweme `author.nickname` 抽取并透传 MediaRef（路径 a）；禁止 author 字段恒 None。
- 视频号单视频以 spike 通过为前提；失败则降级可砍，不阻塞抖音 + Qwen 硬切。
- **Dockerfile 当前无 ffmpeg（必装）**——同时修复现有 `asr.py` webm→pcm（pydub）prod 路径。
- `app/config.py` 由 Task 2（`ASR_TMP_DIR`）与 Task 9（AppKey 注释）**增量编辑**，勿互相覆盖。
- 运行目录：`/Users/rick/work/sks-ai`；测试：`.venv/bin/python -m pytest …`

## File map

| 路径 | 动作 |
|---|---|
| `app/datasource/media/__init__.py` | Create |
| `app/datasource/media/types.py` | Create — `MediaRef` |
| `app/datasource/media/download.py` | Create |
| `app/datasource/media/audio.py` | Create |
| `app/datasource/media/merge.py` | Create |
| `app/datasource/media/qwen_asr.py` | Create |
| `app/datasource/media/semaphores.py` | Create — asr/download/convert 信号量 |
| `app/datasource/transcribe.py` | Rewrite facade |
| `app/datasource/tikhub.py` | Add `MediaRef` helpers / `resolve_media` / channels |
| `app/skills/video_analyze/graph.py` | resolve + heartbeat `MediaRef` |
| `app/skills/account_analyze/graph.py` | `video_meta_to_media_ref` + heartbeat |
| `app/config.py` | AppKey deprecated 注释；可选 `ASR_TMP_DIR` |
| `tests/test_transcribe.py` | Rewrite |
| `tests/test_media_*.py` | Create |
| `tests/test_tikhub.py` | Extend |
| `tests/test_video_analyze.py` / `test_account_analyze.py` | Heartbeat 入参小改 |
| `.env.example` / `Dockerfile` | ffmpeg + Key 注释 |
| `sks-agent/deploy/GO_LIVE_CHECKLIST.md` | 跨仓文档（另 commit） |

---

### Task 1: MediaRef + VideoMeta.author + `video_meta_to_media_ref`

**Files:**
- Create: `app/datasource/media/types.py`
- Create: `app/datasource/media/__init__.py`
- Modify: `app/datasource/tikhub.py` — `VideoMeta` 加 `author`；`_parse_video` 抽 `author.nickname`；`video_meta_to_media_ref`
- Test: `tests/test_media_ref.py`；必要时改 `tests/test_tikhub.py` 里构造 `VideoMeta(...)` 的调用（多一个字段）

**Interfaces:**
- Produces: `MediaRef(platform, download_url, headers=None, decode_key=None, title=None, author=None, raw_id=None)`
- Produces: `VideoMeta(..., author: str = "")` — 从 aweme `author.nickname` / `author.nick_name` 抽取
- Produces: `DOUYIN_DOWNLOAD_HEADERS: dict[str, str]`
- Produces: `video_meta_to_media_ref(v: VideoMeta) -> MediaRef` — `author=v.author`（不再靠外部 nickname 变量）

**选定路径 (a)：** 扩展 `_parse_video`，禁止 author 恒 None。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_media_ref.py
from app.datasource.tikhub import VideoMeta, video_meta_to_media_ref, DOUYIN_DOWNLOAD_HEADERS, _parse_video

def test_parse_video_extracts_author_nickname():
    item = {
        "desc": "标题",
        "statistics": {"play_count": 1, "digg_count": 2},
        "video": {"play_addr": {"url_list": ["https://cdn.example/a.mp4"]}},
        "author": {"nickname": "张三"},
    }
    v = _parse_video(item)
    assert v.author == "张三"
    assert v.title == "标题"

def test_video_meta_to_media_ref_fills_headers_title_author():
    v = VideoMeta(
        title="你好", play_count=1, fav_count=2,
        download_url="https://cdn.example/a.mp4", author="张三",
    )
    ref = video_meta_to_media_ref(v)
    assert ref.platform == "douyin"
    assert ref.author == "张三"
    assert ref.title == "你好"
    assert ref.headers["Referer"] == "https://www.douyin.com/"
    assert "User-Agent" in ref.headers
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_media_ref.py -v
```

- [ ] **Step 3: 实现**

```python
# types.py — MediaRef 含 author: str | None = None
```

```python
@dataclass
class VideoMeta:
    title: str
    play_count: int
    fav_count: int
    download_url: str
    author: str = ""  # NEW — aweme author.nickname

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
    return MediaRef(
        platform="douyin",
        download_url=v.download_url,
        headers=dict(DOUYIN_DOWNLOAD_HEADERS),
        title=v.title or None,
        author=v.author or None,
    )
```

`DOUYIN_DOWNLOAD_HEADERS` 同前（UA + Referer）。

- [ ] **Step 4: 修现有测试里 `VideoMeta(...)` 构造（若位置参数被打乱，改用关键字）— PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: MediaRef + VideoMeta.author + video_meta_to_media_ref"
```

---

### Task 2: `download.py` + 陈旧临时文件 GC 入口 helper

**Files:**
- Create: `app/datasource/media/download.py`
- Modify: `app/config.py` — **仅**增加可选 `ASR_TMP_DIR: str = ""`（AppKey 注释留给 Task 9，勿在此大改 config）
- Test: `tests/test_media_download.py`

**Interfaces:**
- Produces: `async def download_url(url: str, *, headers: dict[str, str] | None = None) -> Path`
- Produces: `def gc_stale_tmp(*, max_age_hours: float = 2.0) -> int` — 删 `ASR_TMP_DIR` / 系统 tempfile 下 `sks_asr_*` 陈旧文件

- [ ] **Step 1: 写失败测试（httpx MockTransport）**

```python
async def test_download_url_writes_file(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ASR_TMP_DIR", str(tmp_path))
    # MockTransport 返回 b"fake-bytes" → Path.exists() and read_bytes()
```

```python
async def test_download_http_error_raises_datasource_error(...):
    # 404 → DataSourceError
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: 实现要点**

- 用 `httpx.AsyncClient` GET，`follow_redirects=True`，timeout 60s。
- 写入 `tempfile.mkstemp(prefix="sks_asr_dl_", dir=settings.ASR_TMP_DIR or None)`。
- **禁止** import `tikhub`。
- 非 2xx / 传输错误 → `DataSourceError`。

- [ ] **Step 4: PASS + Commit**

```bash
git commit -m "feat: media download_url + ASR_TMP_DIR / 陈旧 GC helper"
```

---

### Task 3: `audio.py` + `merge.py`

**Files:**
- Create: `app/datasource/media/audio.py`
- Create: `app/datasource/media/merge.py`
- Test: `tests/test_media_audio_merge.py`

**Interfaces:**
- Produces: `async def convert_to_wav(src: Path) -> Path` — 16k mono
- Produces: `def get_audio_duration(wav: Path | str) -> float` — ffprobe；失败返回 `0.0`
- Produces: `async def slice_audio(wav: Path, segment_duration: int = 270, overlap: int = 3) -> list[Path]`
- Produces: `def merge_transcript_parts(parts: list[str], overlap: int = 3) -> str`
- Produces: `def find_overlap_text(tail: str, head: str) -> str`

- [ ] **Step 1: merge 单测（无 ffmpeg，纯文本）— 对齐 clever-hans**

```python
from app.datasource.media.merge import merge_transcript_parts, find_overlap_text

def test_find_overlap_text():
    assert find_overlap_text("大家来到我的频道", "来到我的频道今天") == "来到我的频道"
    assert find_overlap_text("大家好", "欢迎来到") == ""

def test_merge_transcript_parts_overlap():
    parts = ["前面文字来到我的频道", "来到我的频道后面继续"]
    assert "来到我的频道" in merge_transcript_parts(parts, overlap=3)
    # 不应重复整段 overlap
```

实现直接移植 clever-hans `pipeline.py` 的 `_merge_transcript_parts` / `_find_overlap_text`（`max_len = min(..., 50)`）。

- [ ] **Step 2: audio 单测** — 若 CI 无 ffmpeg，用 `monkeypatch` mock `asyncio.to_thread` / subprocess；本地有 ffmpeg 可加 `@pytest.mark.skipif(not shutil.which("ffmpeg"))`。

`convert_to_wav` / `slice_audio` 参考 clever-hans `audio.py`（`ffmpeg -ar 16000 -ac 1 -vn`；切片 `-ss/-t`）。

**失败语义（必做）：** `FileNotFoundError`（无 ffmpeg）/ 非 0 退出 / timeout → 一律 `raise DataSourceError("ffmpeg …")`，禁止裸 subprocess 异常冒泡。

**切片体积注：** `segment_duration=270` → 16k mono PCM ≈ 8.24MB &lt; 10MB，单段天然满足 qwen 体积上限，无需再给每段加 10MB 守卫。

- [ ] **Step 3: PASS + Commit**

```bash
git commit -m "feat: media audio convert/slice + clever-hans merge"
```

---

### Task 4: `qwen_asr.py` + 三路信号量

**Files:**
- Create: `app/datasource/media/qwen_asr.py`
- Create: `app/datasource/media/semaphores.py`
- Test: `tests/test_media_qwen_asr.py`

**Interfaces:**
- Produces: `MODEL_NAME = "qwen3-asr-flash"`
- Produces: `async def recognize_wav(wav_path: Path | str, *, title: str | None = None, author: str | None = None) -> str`
- Produces: `get_asr_semaphore() -> asyncio.Semaphore`（默认 3）
- Produces: `get_download_semaphore() -> asyncio.Semaphore`（默认 5）
- Produces: `get_convert_semaphore() -> asyncio.Semaphore`（默认 4）

- [ ] **Step 1: 写失败测试**

```python
async def test_recognize_wav_success(monkeypatch):
    text = await recognize_wav("/tmp/x.wav", title="题", author="作者")
    assert text == "识别结果"

async def test_recognize_wav_transient_retries_then_raises(monkeypatch):
    # MultiModalConversation.call 连续抛 RuntimeError / 非 200 → 共 3 次后 DataSourceError
    # 不是返回 ""

async def test_recognize_wav_empty_text_no_retry(monkeypatch):
    # status_code=200 但无文本 → 立即 DataSourceError("qwen asr empty text")，call 次数 == 1
```

- [ ] **Step 2: 实现重试策略（写死）**

| 情况 | 行为 |
|---|---|
| 非 200 / 网络 / `RuntimeError` 等瞬态 | 最多 3 次尝试，耗尽 → `DataSourceError` |
| 200 但解析后空文本 | **不重试**，立即 `DataSourceError("qwen asr empty text")` |

调用形态对齐 clever-hans（`file://` + `asr_options={language:zh, enable_lid:True}` + system 标题/作者）。**禁止** clever-hans 式空串降级。

`semaphores.py`：三个懒单例 getter（模块级 `None`，首次创建）。

- [ ] **Step 3: PASS + Commit**

```bash
git commit -m "feat: qwen3-asr-flash + download/convert/asr 信号量"
```

---

### Task 5: 重写 `transcribe.py` 门面 + 重写 `test_transcribe.py`

**Files:**
- Rewrite: `app/datasource/transcribe.py`
- Rewrite: `tests/test_transcribe.py`
- `app/config.py` — **本 Task 可不改**；DashScope/AppKey 注释归 Task 9（避免与 Task 2 冲突）

**Interfaces:**
- Produces: `async def transcribe(media: MediaRef | str) -> str`
- Produces: `def _is_configured() -> bool` — `ALIYUN_ASR_KEY` **且** `shutil.which("ffmpeg")` **且** `shutil.which("ffprobe")`
- Produces: 模块级 `decode_media: Callable[[Path, str], Path] | None = None` — 可插拔；Task 8a 注入，默认 None
- Seams：`download_url` / `convert_to_wav` / `get_audio_duration` / `slice_audio` / `recognize_wav` / `merge_transcript_parts` / `gc_stale_tmp` / `get_*_semaphore`

- [ ] **Step 1: 重写测试（不再 mock POP）**

```python
async def test_transcribe_media_ref_short_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "sk-test")
    monkeypatch.setattr(tr, "_ffmpeg_available", lambda: True)  # 或 which mock
    # mock download→path, convert→wav, duration=10.0, recognize→"你好"
    ref = MediaRef(platform="douyin", download_url="https://x/a.mp4",
                   headers={"Referer": "https://www.douyin.com/"}, author="张三")
    assert await tr.transcribe(ref) == "你好"

async def test_transcribe_str_is_bare_download_no_headers_guess(monkeypatch, tmp_path):
    # 捕获 download_url：headers 必须是 None 或 {}

async def test_transcribe_slices_when_duration_over_300(monkeypatch, tmp_path): ...

async def test_transcribe_wav_over_10mb_short_duration_errors(monkeypatch, tmp_path): ...

async def test_transcribe_not_configured_missing_key(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "")
    with pytest.raises(DataSourceError, match="ALIYUN_ASR_KEY"):
        await tr.transcribe("https://x")

async def test_transcribe_not_configured_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "sk-test")
    monkeypatch.setattr(tr, "_ffmpeg_available", lambda: False)
    with pytest.raises(DataSourceError, match="ffmpeg"):
        await tr.transcribe("https://x")

async def test_transcribe_decode_key_without_decoder_errors(monkeypatch, tmp_path):
    # decode_key set, decode_media is None → DataSourceError("channels decode not enabled")
```

- [ ] **Step 2: 实现门面**

```python
import shutil
from app.datasource.media.semaphores import (
    get_asr_semaphore, get_download_semaphore, get_convert_semaphore,
)

decode_media = None  # Task 8a: 赋值为 channels decode 函数

def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

def _is_configured() -> bool:
    return bool(settings.ALIYUN_ASR_KEY) and _ffmpeg_available()

async def transcribe(media: MediaRef | str) -> str:
    if not settings.ALIYUN_ASR_KEY:
        raise DataSourceError("ALIYUN_ASR_KEY not configured")
    if not _ffmpeg_available():
        raise DataSourceError("ffmpeg/ffprobe not found on PATH")
    gc_stale_tmp()
    ref = media if isinstance(media, MediaRef) else MediaRef(
        platform="unknown", download_url=media, headers={}
    )
    temps: list[Path] = []
    try:
        async with get_download_semaphore():
            src = await download_url(ref.download_url, headers=ref.headers or None)
        temps.append(src)
        if ref.decode_key:
            if decode_media is None:
                raise DataSourceError("channels decode not enabled")
            src = decode_media(src, ref.decode_key)
            temps.append(src)
        async with get_convert_semaphore():
            wav = await convert_to_wav(src)
        temps.append(wav)
        duration = get_audio_duration(wav)
        size = wav.stat().st_size
        # 整段守卫；切片单段 270s≈8.24MB 天然 <10MB，无需逐段再检
        if duration > 0 and duration <= 300 and size > 10 * 1024 * 1024:
            raise DataSourceError("wav exceeds 10MB within 300s — unexpected")
        if duration > 0 and duration <= 300:
            async with get_asr_semaphore():
                text = await recognize_wav(wav, title=ref.title, author=ref.author)
        else:
            async with get_convert_semaphore():
                segs = await slice_audio(wav)
            temps.extend(segs)
            parts = []
            for seg in segs:
                async with get_asr_semaphore():
                    parts.append(await recognize_wav(seg, title=ref.title, author=ref.author))
            text = merge_transcript_parts(parts)
        if not text.strip():
            raise DataSourceError("asr produced empty transcript")
        return text
    finally:
        for p in temps:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
```

单条墙钟上限：外层 `asyncio.wait_for(..., timeout=1200)`（20min）。

删除全部 filetrans / `AcsClient` / `_submit_task` / `_get_task_result`。

- [ ] **Step 3: PASS + Commit**

```bash
git commit -m "feat: transcribe 门面硬切 Qwen 管线（删除 filetrans）"
```

---

### Task 6: `resolve_media`（抖音）+ 接线 `analyze_video_link`

**Files:**
- Modify: `app/datasource/tikhub.py` — `resolve_media` / 可选高清 fallback
- Modify: `app/skills/video_analyze/graph.py`
- Test: `tests/test_tikhub.py`, `tests/test_video_analyze.py`

**Interfaces:**
- Produces: `async def resolve_media(url: str, *, client=None) -> MediaRef`
- Heartbeat: `async def _transcribe_with_heartbeat(task_id: int, media: MediaRef | str) -> str`

**锚点（LOAD-BEARING）：** 仅插入 `resolve_media` + 改 heartbeat 入参 `url→media`；**保留**现有 `await _structure_transcript(transcript)`、`result is None → failed(blocked)`、`except DataSourceError → failed`、`except Exception → failed`、`done` 路径。**禁止**用 `...` 整段重写 `analyze_video_link`。注意：链接路径用的是 **`_structure_transcript`**，不是同步入口 `structure_video`。

- [ ] **Step 1: `resolve_media` 测试**

- 抖音 host → `video_meta` → `video_meta_to_media_ref`（含 author）。
- 未知 URL → `DataSourceError`。
- `download_url` 空 → `DataSourceError("empty download_url")`（高清 fallback 可选子步骤）。

- [ ] **Step 2: 最小 diff 改 `video_analyze/graph.py`**

```python
from app.datasource.tikhub import resolve_media
from app.datasource.media.types import MediaRef

# 模块级别名供测试 monkeypatch：
# resolve_media = resolve_media  （或 from tikhub import resolve_media as resolve_media）

async def _transcribe_with_heartbeat(task_id: int, media: MediaRef | str) -> str:
    task = asyncio.create_task(transcribe(media))
    # 原 60s shield 循环一字不改

async def analyze_video_link(task_id: int, url: str) -> None:
    await update_task(task_id, status="running", progress=0)
    try:
        ref = await resolve_media(url)                    # NEW
        transcript = await _transcribe_with_heartbeat(task_id, ref)  # was url
        result = await _structure_transcript(transcript)  # KEEP
    except DataSourceError as e:                         # KEEP
        ...
    # 其余 blocked / Exception / done 全部 KEEP
```

- [ ] **Step 3: 测试 mock 写死**

```python
async def fake_resolve(url: str, **kwargs):
    return MediaRef(
        platform="douyin",
        download_url="https://cdn.example/a.mp4",
        headers={"Referer": "https://www.douyin.com/"},
        title="t", author="a",
    )

monkeypatch.setattr(vg, "resolve_media", fake_resolve)  # 必须，否则真打 TikHub
captured = {}
async def fake_transcribe(media):
    captured["media"] = media
    return "转写文本"
monkeypatch.setattr(vg, "transcribe", fake_transcribe)
# 跑 analyze_video_link 后：
assert isinstance(captured["media"], MediaRef)
assert captured["media"].download_url.startswith("https://cdn")
# 不得把原始分享链传入 transcribe
```

- [ ] **Step 4: PASS + Commit**

```bash
git commit -m "feat: resolve_media + 单视频链接先解析再转写"
```

---

### Task 7: 拆账号接线 `MediaRef` + 同步 heartbeat

**Files:**
- Modify: `app/skills/account_analyze/graph.py`
- Test: `tests/test_account_analyze.py`

**锚点（LOAD-BEARING）：** 仅改 heartbeat 签名 + 循环内 `v.download_url` → `video_meta_to_media_ref(v)`；**保留** per-item `except DataSourceError: continue`、`except Exception: continue`、`if not await check: continue`、progress/partial/failed 逻辑。**禁止**重写 `analyze_account` 函数体。

- [ ] **Step 1: 最小 diff**

```python
from app.datasource.tikhub import video_meta_to_media_ref
from app.datasource.media.types import MediaRef

async def _transcribe_with_heartbeat(task_id: int, media: MediaRef | str) -> str:
    task = asyncio.create_task(transcribe(media))
    # 原 60s 循环 KEEP

# 循环内原：
#   transcript = await _transcribe_with_heartbeat(task_id, v.download_url)
# 改为：
ref = video_meta_to_media_ref(v)  # author 已在 VideoMeta（Task 1）
transcript = await _transcribe_with_heartbeat(task_id, ref)
# 其后 except / check / insert_benchmark 全部 KEEP
```

- [ ] **Step 2: 平台门禁** — `precheck` / `account_top_videos`：视频号 host → `DataSourceError("account analyze supports douyin only; use video link for channels")`。与 `resolve_media` 共用 `_platform_of(url)`。

- [ ] **Step 3: 测试** — mock `transcribe` 时断言入参为 `MediaRef` 且 `author`/`headers` 来自 fixture VideoMeta。PASS + Commit

```bash
git commit -m "feat: 拆账号 VideoMeta→MediaRef + heartbeat 签名同步"
```

---

### Task 8: 视频号 spike +（可选）`channels_video_meta` / decode

**Files:**
- Modify: `app/datasource/tikhub.py`
- Possibly: `app/datasource/media/channels_decode.py`
- Test: mock 样例；联调笔记写入 `docs/superpowers/specs/` 附录或 `docs/spikes/2026-08-02-wechat-channels-decode.md`

- [ ] **Step 1: Spike（人工/脚本，≤1 工作日）**

用真实 `TIKHUB_API_KEY` 调 `POST /api/v1/wechat_channels/v2/fetch_video_detail`，保存脱敏样例：`media` URL、`decode_key` 是否为空、解码算法。

- [ ] **Step 2a: Spike 成功** — 实现 `channels_video_meta` + decode 函数；在 `transcribe` 模块赋值 `decode_media = channels_decode_fn`（见 Task 5 可插拔 seam）；`resolve_media` 识别视频号 host；单测 mock；commit。

- [ ] **Step 2b: Spike 失败** — 勾选可砍；`resolve_media` 对视频号 → `DataSourceError("wechat channels not enabled yet")`；**保持** `decode_media = None`（门面遇 `decode_key` 已报 `channels decode not enabled`）。**不阻塞** Task 5–7。

```bash
git commit -m "feat: 视频号单视频 resolve+decode"
# 或
git commit -m "chore: 视频号 spike 未过，单视频入口明确报错（可砍）"
```

---

### Task 9: 配置 / Docker / 清单文档

**Files:**
- Modify: `app/config.py`（**增量**：DashScope/AppKey 注释；勿删 Task 2 的 `ASR_TMP_DIR`）
- Modify: `sks-ai/.env.example`, `sks-ai/Dockerfile`
- Modify: `sks-agent/.env.example`, `sks-agent/deploy/GO_LIVE_CHECKLIST.md`（**另仓 commit**）
- `sks-ai/docs/API_CONTRACT.md` — **仅核对** `/ai/asr` 503 ↔ `ALIYUN_ASR_KEY`

- [ ] **Step 1: `config.py` 注释**

```python
# 一句话识别 + 长转写（qwen3-asr-flash）共用——DashScope/百炼 API Key，非阿里云 ISI。
ALIYUN_ASR_KEY: str = ""
# ASR_TMP_DIR 已由 Task 2 加入——此处勿覆盖
# ALIYUN_ASR_APP_KEY: deprecated — 长转写已改 Qwen；字段保留兼容旧 .env
ALIYUN_ASR_APP_KEY: str = ""
```

- [ ] **Step 2: Dockerfile（必做，当前镜像无 ffmpeg）**

现状：`FROM python:3.12-slim` + uv，**无** `apt-get`。必须增加，例如：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
```

（放在 `uv sync` 前或后均可，建议靠前。）

**连带修复：** 现有 `asr.py` webm→pcm（pydub）在 prod 也依赖 ffmpeg；GO_LIVE 已标「第一处会坏」。本步同时修短 ASR prod 路径。

- [ ] **Step 3: GO_LIVE** — 删/改 `ALIYUN_ASR_APP_KEY` 与 filetrans；加 `ALIYUN_ASR_KEY`（DashScope）、镜像含 ffmpeg、Qwen 长转写联调；保留/强化短 ASR webm 联检（ffmpeg 已装后应可勾）。

- [ ] **Step 4: Commit（sks-ai + sks-agent 分别）**

```bash
git commit -m "chore: Dockerfile 安装 ffmpeg；废弃 ASR AppKey 文档"
# sks-agent
git commit -m "chore: GO_LIVE 长转写改为 Qwen + ffmpeg"
```

---

### Task 10: 全量回归 + 联调核对清单

- [ ] **Step 1: 单元测试（全量，防 config 波及 content_safety 等）**

```bash
cd /Users/rick/work/sks-ai
.venv/bin/python -m pytest tests/ -q
```

Expected: all PASS

- [ ] **Step 2: 联调 checklist（人工）**

1. 无 `ALIYUN_ASR_APP_KEY`，仅 `ALIYUN_ASR_KEY` + `TIKHUB_API_KEY`，抖音单视频成功。
2. 抖音拆账号 ≥1 条成功；日志/抓包确认带 Referer；`author` 非空时进 Qwen context。
3. 单条墙钟 >5min 时 Java 不误判 failed（两处 heartbeat 60s）。
4. DashScope 并发压测，标定 asr 信号量。
5. 视频号：spike 路径或明确错误文案。
6. 校准短 ASR（webm）在新镜像上冒烟一次（ffmpeg 连带修复）。

- [ ] **Step 3: 最终 commit（若有压测参数调整）**

---

## Spec coverage self-check

| Spec 要求 | Task |
|---|---|
| MediaRef + headers/author（`_parse_video` 抽 nickname） | 1, 6, 7 |
| download 纯下载 + GC | 2 |
| wav/slice/merge；subprocess→DataSourceError | 3 |
| qwen3-asr-flash + asr_options；瞬态重试/空文本不重试 | 4 |
| 硬切 filetrans；ffmpeg 守卫；decode 可插拔 | 5 |
| 单视频 resolve；保留 `_structure_transcript`/blocked/except | 6 |
| 拆账号 MediaRef；双 heartbeat；保留 per-item continue | 6, 7 |
| 视频号 spike/可砍 | 8 |
| 不删 aliyun-sdk；ACCESS_KEY 留给安全 | 9 |
| Dockerfile ffmpeg（兼修 asr.py）；GO_LIVE / .env | 9 |
| API_CONTRACT 不改实现描述 | 9 |
| 失败不空串；str 裸下载 | 4, 5 |
| Java timeout 默认不改 | **6 与 7** 心跳均保 60s |

## Placeholder scan

无 TBD；视频号 decode 以 Task 8 spike 为准（2b 可砍）。`decode_media` 默认可插拔，不引用未定义符号。

## Plan review patch（2026-08-02）

采纳执行前审查：

| ID | 决议 |
|---|---|
| P1-1 | `_is_configured` + audio 均把 ffmpeg 缺失/失败 → `DataSourceError` |
| P1-2 | 路径 (a)：`VideoMeta.author` ← `author.nickname` |
| P1-3 | Task 6/7 锚点：禁止整函数重写；保留 blocked/except/continue |
| P2-1 | 瞬态最多 3 次；空文本立即失败不重试 |
| P2-2 | 测试写死 mock `resolve_media` + 断言 `MediaRef` |
| P2-3 | `decode_media` 可插拔 seam；注释 Task 8 |
| P2-4 | 三路 semaphore getter |
| P3-* | Dockerfile 必装 ffmpeg；全量 pytest；coverage 表 6+7；`_structure_transcript` 正名；config 增量编辑 |

---

## Execution handoff

Plan patched and saved to `docs/superpowers/plans/2026-08-02-qwen-asr-media-pipeline.md`.

**Two execution options:**

1. **Subagent-Driven（推荐）** — 每 Task 新开子代理，Task 间审查  
2. **Inline Execution** — 本会话连续做并设检查点  

Which approach?
