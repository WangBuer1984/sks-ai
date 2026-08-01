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
- `_is_configured()` 长转写只查 `ALIYUN_ASR_KEY`；**不得**从 pyproject 移除 `aliyun-python-sdk-core`。
- 两处 `_transcribe_with_heartbeat`（video + account）必须同 PR 改签名。
- 视频号单视频以 spike 通过为前提；失败则降级可砍，不阻塞抖音 + Qwen 硬切。
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

### Task 1: MediaRef + 抖音默认 headers + `video_meta_to_media_ref`

**Files:**
- Create: `app/datasource/media/types.py`
- Create: `app/datasource/media/__init__.py`
- Modify: `app/datasource/tikhub.py`
- Test: `tests/test_tikhub.py`（追加）或 `tests/test_media_ref.py`

**Interfaces:**
- Produces: `MediaRef(platform, download_url, headers=None, decode_key=None, title=None, author=None, raw_id=None)`
- Produces: `DOUYIN_DOWNLOAD_HEADERS: dict[str, str]`
- Produces: `video_meta_to_media_ref(v: VideoMeta, *, author: str | None = None) -> MediaRef`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_media_ref.py
from app.datasource.media.types import MediaRef
from app.datasource.tikhub import VideoMeta, video_meta_to_media_ref, DOUYIN_DOWNLOAD_HEADERS

def test_video_meta_to_media_ref_fills_douyin_headers_and_title():
    v = VideoMeta(title="你好", play_count=1, fav_count=2, download_url="https://cdn.example/a.mp4")
    ref = video_meta_to_media_ref(v, author="张三")
    assert ref.platform == "douyin"
    assert ref.download_url == v.download_url
    assert ref.title == "你好"
    assert ref.author == "张三"
    assert ref.headers["Referer"] == "https://www.douyin.com/"
    assert "User-Agent" in ref.headers
    assert DOUYIN_DOWNLOAD_HEADERS["Referer"] == "https://www.douyin.com/"
```

- [ ] **Step 2: Run — expect FAIL（符号未定义）**

```bash
.venv/bin/python -m pytest tests/test_media_ref.py -v
```

- [ ] **Step 3: 实现**

```python
# app/datasource/media/types.py
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class MediaRef:
    platform: str  # "douyin" | "wechat_channels"
    download_url: str
    headers: dict[str, str] = field(default_factory=dict)
    decode_key: str | None = None
    title: str | None = None
    author: str | None = None
    raw_id: str | None = None
```

```python
# app/datasource/media/__init__.py
from app.datasource.media.types import MediaRef
__all__ = ["MediaRef"]
```

在 `tikhub.py` 增加：

```python
from app.datasource.media.types import MediaRef

DOUYIN_DOWNLOAD_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}

def video_meta_to_media_ref(v: VideoMeta, *, author: str | None = None) -> MediaRef:
    return MediaRef(
        platform="douyin",
        download_url=v.download_url,
        headers=dict(DOUYIN_DOWNLOAD_HEADERS),
        title=v.title or None,
        author=author,
    )
```

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git add app/datasource/media/types.py app/datasource/media/__init__.py \
  app/datasource/tikhub.py tests/test_media_ref.py
git commit -m "feat: MediaRef + video_meta_to_media_ref（抖音默认 headers）"
```

---

### Task 2: `download.py` + 陈旧临时文件 GC 入口 helper

**Files:**
- Create: `app/datasource/media/download.py`
- Modify: `app/config.py` — 增加可选 `ASR_TMP_DIR: str = ""`
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

- [ ] **Step 2: audio 单测** — 若 CI 无 ffmpeg，用 `monkeypatch` mock `asyncio.to_thread` / subprocess；本地有 ffmpeg 可加 integration 标 `@pytest.mark.skipif(not shutil.which("ffmpeg"))`。

`convert_to_wav` / `slice_audio` 参考 clever-hans `audio.py`（`ffmpeg -ar 16000 -ac 1 -vn`；切片 `-ss/-t`）。

- [ ] **Step 3: PASS + Commit**

```bash
git commit -m "feat: media audio convert/slice + clever-hans merge"
```

---

### Task 4: `qwen_asr.py`（失败抛 DataSourceError，不对齐 clever-hans 空串降级）

**Files:**
- Create: `app/datasource/media/qwen_asr.py`
- Create: `app/datasource/media/semaphores.py`
- Test: `tests/test_media_qwen_asr.py`

**Interfaces:**
- Produces: `MODEL_NAME = "qwen3-asr-flash"`
- Produces: `async def recognize_wav(wav_path: Path | str, *, title: str | None = None, author: str | None = None) -> str`
- Produces: `get_asr_semaphore() -> asyncio.Semaphore`（默认 3）

- [ ] **Step 1: 写失败测试**

```python
async def test_recognize_wav_success(monkeypatch):
    # monkeypatch MultiModalConversation.call 返回 status_code=200 + text
    text = await recognize_wav("/tmp/x.wav", title="题", author="作者")
    assert text == "识别结果"

async def test_recognize_wav_all_retries_raise(monkeypatch):
    # 三次失败 → DataSourceError，不是 ""
```

- [ ] **Step 2: 实现** — 对齐 clever-hans 调用形态，但第 3 次失败 `raise DataSourceError(...)`：

```python
dashscope.api_key = settings.ALIYUN_ASR_KEY
messages = [{"role": "user", "content": [{"audio": f"file://{wav_path}"}]}]
# system: "这是一段短视频音频。\n视频标题: …\n作者: …"
response = MultiModalConversation.call(
    model=MODEL_NAME,
    messages=messages,
    stream=False,
    incremental_output=False,
    result_format="message",
    asr_options={"language": "zh", "enable_lid": True},
)
```

空文本（200 但无字）→ `DataSourceError("qwen asr empty text")`。

- [ ] **Step 3: PASS + Commit**

```bash
git commit -m "feat: qwen3-asr-flash recognize_wav（失败抛 DataSourceError）"
```

---

### Task 5: 重写 `transcribe.py` 门面 + 重写 `test_transcribe.py`

**Files:**
- Rewrite: `app/datasource/transcribe.py`
- Rewrite: `tests/test_transcribe.py`
- Modify: `app/config.py` — `ALIYUN_ASR_KEY` 注释标明 DashScope；`ALIYUN_ASR_APP_KEY` 标 deprecated（可留字段以免旧 .env 炸）

**Interfaces:**
- Produces: `async def transcribe(media: MediaRef | str) -> str`
- Produces: `def _is_configured() -> bool` — 仅 `bool(settings.ALIYUN_ASR_KEY)`
- Seams（供测试 monkeypatch）：`download_url` / `convert_to_wav` / `get_audio_duration` / `slice_audio` / `recognize_wav` / `merge_transcript_parts` / `gc_stale_tmp`

- [ ] **Step 1: 重写测试（不再 mock POP）**

```python
async def test_transcribe_media_ref_short_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "sk-test")
    wav = tmp_path / "a.wav"; wav.write_bytes(b"x")
    # mock download→path, convert→wav, duration=10.0, recognize→"你好"
    ref = MediaRef(platform="douyin", download_url="https://x/a.mp4", headers={...})
    assert await tr.transcribe(ref) == "你好"

async def test_transcribe_str_is_bare_download_no_headers_guess(monkeypatch, tmp_path):
    # 捕获 download_url 调用：headers 必须是 None 或 {}
    ...

async def test_transcribe_slices_when_duration_over_300(monkeypatch, tmp_path):
    # duration=400 → slice 返回 2 段 → recognize 两次 → merge

async def test_transcribe_wav_over_10mb_short_duration_errors(monkeypatch, tmp_path):
    # duration=10, wav.stat().st_size = 11*1024*1024 → DataSourceError

async def test_transcribe_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "")
    with pytest.raises(DataSourceError, match="ALIYUN_ASR_KEY"):
        await tr.transcribe("https://x")
```

- [ ] **Step 2: 实现门面伪码**

```python
async def transcribe(media: MediaRef | str) -> str:
    if not _is_configured():
        raise DataSourceError("ALIYUN_ASR_KEY not configured")
    gc_stale_tmp()
    ref = media if isinstance(media, MediaRef) else MediaRef(
        platform="unknown", download_url=media, headers={}
    )
    temps: list[Path] = []
    try:
        async with download_sem:
            src = await download_url(ref.download_url, headers=ref.headers or None)
        temps.append(src)
        if ref.decode_key:
            src = decode_channels_media(src, ref.decode_key)  # Task 7；无则 stub raise
            temps.append(src)
        async with convert_sem:
            wav = await convert_to_wav(src)
        temps.append(wav)
        duration = get_audio_duration(wav)
        size = wav.stat().st_size
        if duration > 0 and duration <= 300 and size > 10 * 1024 * 1024:
            raise DataSourceError("wav exceeds 10MB within 300s — unexpected")
        ctx = {"title": ref.title, "author": ref.author}
        if duration > 0 and duration <= 300:
            async with asr_sem:
                text = await recognize_wav(wav, title=ref.title, author=ref.author)
        else:
            segs = await slice_audio(wav)
            temps.extend(segs)
            parts = []
            for seg in segs:
                async with asr_sem:
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

单条墙钟上限：可用 `asyncio.wait_for(transcribe_inner(...), timeout=1200)` 或在门面最外层 20min。

删除全部 filetrans / `AcsClient` / `_submit_task` / `_get_task_result` 代码与模块 docstring 中的 ISI 描述。

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

- [ ] **Step 1: `resolve_media` 测试**

- 抖音分享链 host（`v.douyin.com` / `douyin.com` / `iesdouyin.com`）→ 调现有 `video_meta` 逻辑 → `video_meta_to_media_ref`。
- 未知 URL → `DataSourceError`。
- `play_addr`/`download_url` 为空时：在 `_parse_video` / resolve 内尝试高清播放接口（若本期不加接口，至少 download_url 空 → `DataSourceError("empty download_url")`）。高清 fallback 实现可作为本 Task 子步骤；路径常量加到 tikhub 顶部。

- [ ] **Step 2: 改 `video_analyze/graph.py`**

```python
from app.datasource.tikhub import resolve_media

async def _transcribe_with_heartbeat(task_id: int, media: MediaRef | str) -> str:
    task = asyncio.create_task(transcribe(media))
    # …原 60s 循环不变

async def analyze_video_link(task_id: int, url: str) -> None:
    await update_task(task_id, status="running", progress=0)
    try:
        ref = await resolve_media(url)
        transcript = await _transcribe_with_heartbeat(task_id, ref)
        ...
```

- [ ] **Step 3: 更新 `test_video_analyze`** — mock `resolve_media` + `transcribe`（或只 mock 模块级 `transcribe` 且让 resolve 也 mock）。确认不再把原始分享链直接 `transcribe(url)`。

- [ ] **Step 4: PASS + Commit**

```bash
git commit -m "feat: resolve_media + 单视频链接先解析再转写"
```

---

### Task 7: 拆账号接线 `MediaRef` + 同步 heartbeat

**Files:**
- Modify: `app/skills/account_analyze/graph.py`
- Test: `tests/test_account_analyze.py`

- [ ] **Step 1: 改 heartbeat 签名与逐条调用**

```python
from app.datasource.tikhub import video_meta_to_media_ref

async def _transcribe_with_heartbeat(task_id: int, media: MediaRef | str) -> str:
    task = asyncio.create_task(transcribe(media))
    ...

# 在 analyze_account 循环内（原 v.download_url 处）：
author = ...  # 若已有昵称变量则传入；否则 None
ref = video_meta_to_media_ref(v, author=author)
transcript = await _transcribe_with_heartbeat(task_id, ref)
```

- [ ] **Step 2: `precheck` / 账号入口平台门禁** — 若 URL 像视频号，`account_top_videos` / `precheck` 抛 `DataSourceError("account analyze supports douyin only; use video link for channels")`。可用简单 host 检测函数 `_platform_of(url) -> str` 与 `resolve_media` 共用。

- [ ] **Step 3: 测试小改 — PASS + Commit**

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

- [ ] **Step 2a: Spike 成功** — 实现 `channels_video_meta` + decode；`resolve_media` 识别 `weixin.qq.com/sph` / `channels.weixin.qq.com`；单测 mock 响应；commit。

- [ ] **Step 2b: Spike 失败** — 在 design/plan 勾选「视频号单视频可砍」；`resolve_media` 对视频号 URL 返回明确 `DataSourceError("wechat channels not enabled yet")`；**不阻塞** Task 5–7 合入。

```bash
git commit -m "feat: 视频号单视频 resolve+decode" 
# 或
git commit -m "chore: 视频号 spike 未过，单视频入口明确报错（可砍）"
```

---

### Task 9: 配置 / Docker / 清单文档

**Files:**
- Modify: `app/config.py`, `sks-ai/.env.example`, `sks-ai/Dockerfile`
- Modify: `sks-agent/.env.example`, `sks-agent/deploy/GO_LIVE_CHECKLIST.md`（**另仓 commit**）
- `sks-ai/docs/API_CONTRACT.md` — **仅核对** `/ai/asr` 503 仍对应 `ALIYUN_ASR_KEY`，不写 filetrans

- [ ] **Step 1: `config.py`**

```python
# 一句话识别 + 长转写（qwen3-asr-flash）共用——此值为 DashScope/百炼 API Key，非阿里云 ISI。
ALIYUN_ASR_KEY: str = ""
ASR_TMP_DIR: str = ""  # 可选；空则用系统 tempfile
# ALIYUN_ASR_APP_KEY: deprecated — 长转写已改 Qwen，勿再依赖；字段可留空兼容旧 .env
ALIYUN_ASR_APP_KEY: str = ""
```

- [ ] **Step 2: Dockerfile** — 确保安装 `ffmpeg`（`apt-get install -y ffmpeg` 或等价）。

- [ ] **Step 3: GO_LIVE** — 删除/改写 `ALIYUN_ASR_APP_KEY` 与 filetrans 检查项；增加：`ALIYUN_ASR_KEY`（DashScope）、镜像含 ffmpeg、长转写 Qwen 管线联调。

- [ ] **Step 4: Commit（sks-ai + sks-agent 分别）**

```bash
# sks-ai
git commit -m "chore: ASR 配置注释/ffmpeg/废弃 AppKey 文档"

# sks-agent
git commit -m "chore: GO_LIVE 长转写改为 Qwen + ffmpeg"
```

---

### Task 10: 全量回归 + 联调核对清单

- [ ] **Step 1: 单元测试**

```bash
cd /Users/rick/work/sks-ai
.venv/bin/python -m pytest tests/test_media_ref.py tests/test_media_download.py \
  tests/test_media_audio_merge.py tests/test_media_qwen_asr.py tests/test_transcribe.py \
  tests/test_tikhub.py tests/test_video_analyze.py tests/test_account_analyze.py -q
```

Expected: all PASS

- [ ] **Step 2: 联调 checklist（人工）**

1. 无 `ALIYUN_ASR_APP_KEY`，仅 `ALIYUN_ASR_KEY` + `TIKHUB_API_KEY`，抖音单视频粘链接成功。
2. 抖音拆账号 ≥1 条转写成功（观察 headers 无 403）。
3. 单条墙钟 >5min 时 Java 不误判 failed（心跳续命）。
4. （可选）DashScope 并发压测，调整 `ASR_CONCURRENCY`（若做成 settings，默认 3）。
5. 视频号：spike 路径或明确错误文案。

- [ ] **Step 3: 最终 commit（若有压测参数调整）**

---

## Spec coverage self-check

| Spec 要求 | Task |
|---|---|
| MediaRef + headers/author | 1, 7 |
| download 纯下载 + GC | 2 |
| wav/slice/merge | 3 |
| qwen3-asr-flash + asr_options | 4 |
| 硬切 filetrans 门面 | 5 |
| 单视频 resolve | 6 |
| 拆账号 MediaRef + 双 heartbeat | 6–7 |
| 视频号 spike/可砍 | 8 |
| 不删 aliyun-sdk；ACCESS_KEY 留给安全 | 5, 9 |
| GO_LIVE / .env / ffmpeg | 9 |
| API_CONTRACT 不改实现描述 | 9 |
| 失败不空串；str 裸下载 | 4, 5 |
| Java timeout 默认不改 | 6 心跳保留 60s |

## Placeholder scan

无 TBD；视频号 decode 算法以 Task 8 spike 产出为准（允许 2b 可砍分支）。

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-02-qwen-asr-media-pipeline.md`.

**Two execution options:**

1. **Subagent-Driven（推荐）** — 每 Task 新开子代理，Task 间审查  
2. **Inline Execution** — 本会话按 executing-plans 连续做并设检查点  

Which approach?
