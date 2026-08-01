# 长音频转写对齐 clever-hans（TikHub 下载 → wav/切片 → qwen3-asr-flash）

日期：2026-08-02  
状态：已批准（2026-08-02；复盘补丁同日合入）  
范围：`sks-ai` 拆视频（粘链接）/ 拆账号转写管线；参考 `clever-hans` 媒体+ASR，取链仍用 TikHub（不用 yt-dlp）

## 1. 背景与问题

当前 P3 长转写（`app/datasource/transcribe.py`）走阿里云 ISI **录音文件识别**（filetrans）：

- 要求公网 `file_link` + `ALIYUN_ASR_APP_KEY`（NLS 项目 AppKey）
- 不下载到本地；把 URL 交给阿里云拉取
- 单视频链接路径 `analyze_video_link` 甚至把**分享链**直接当 `file_link`，未先经 TikHub resolve

校准短 ASR（`asr.py` / `paraformer-realtime-v2`）已可用，但与长视频场景不同，不能复用扛拆账号。

`clever-hans` 已验证更贴合的链路：本地下载 → ffmpeg wav →（>5min 切片）→ DashScope **`qwen3-asr-flash`**（`file://` 本地文件），密钥为百炼/DashScope Key。

## 2. 目标与非目标

### 目标

1. **硬切换**：长转写全面改为  
   `TikHub 解析 → HTTP 下载（视频号按需 decode）→ wav 16k mono →（切片）→ qwen3-asr-flash → 文本`  
   删除业务对 ISI filetrans / `ALIYUN_ASR_APP_KEY` 的依赖。
2. **平台**：设计覆盖抖音 + 微信视频号；实现必达与可砍见 §6。
3. **编排入口不变语义**：analyze 仍经 `transcribe` 门面拿全文案；失败仍 `DataSourceError` → 任务 `failed` → 既有退款。
4. **单视频修复**：分享链必须先 resolve 成可下载媒体，再进入转写。

### 非目标

- 不用 yt-dlp；不引入双解析器。
- 不改校准短 ASR（继续 `paraformer-realtime-v2` + `ALIYUN_ASR_KEY`）。
- 不改粘文案路径（无转写）。
- 不保留 filetrans 双轨回退。
- ASR 失败不静默空串（与 clever-hans 降级策略不同，避免假成功）。

## 3. 约束（已确认）

| 项 | 选择 |
|---|---|
| 架构 | 媒体管线拆模块 + 薄 `transcribe` 门面（方案 1） |
| 旧 filetrans | 硬切换，去掉 AppKey 依赖 |
| 平台设计 | 抖音 + 视频号 |
| 实现分期建议 | 必达：抖音单视频 + 抖音拆账号 + 视频号单视频；视频号拆账号可砍（接口不稳时） |
| 短 ASR | 保持 `asr.py`，与长转写共用 DashScope Key 即可 |

## 4. 目标管线

两条入口，勿混用：

**A. 分享链 / 需解析的 URL（单视频）**

```text
用户分享链
  → resolve_media(url) → MediaRef{ platform, download_url, headers?, decode_key?, title? }
  → transcribe(MediaRef) → …见下
```

**B. 已是媒体直链（拆账号逐条）**

```text
download_url: str
  → transcribe(str)  # 只下载+转写，禁止再 resolve_media
```

**`transcribe` 内部（A/B 汇合后）**

```text
HTTP 下载到临时目录（decode_key 非空则解码）
  → ffmpeg → WAV 16kHz mono
  → 若单文件 >10MB：再压码率或强制切片，仍超则 DataSourceError
  → duration ≤ 300s 且 ≤10MB：一次 qwen3-asr-flash
     duration > 300s 或未知或体积仍大：切片（segment≈270s, overlap=3s）
       → 分段识别 → overlap 文本去重拼接
  → finally 清理临时文件
  → 既有 LLM 结构化 / 账号归纳（不变）
```

参数对齐 clever-hans：`segment_duration=270`，`overlap=3`；单次调用遵守官方 `qwen3-asr-flash` **≤5 分钟且 ≤10MB**（时长与体积双约束）。

## 5. 模块设计（均在 `sks-ai`）

### 5.1 新增

| 路径 | 职责 |
|---|---|
| `app/datasource/media/download.py` | 下载 URL → 临时文件；可选 headers；超时/非 2xx → `DataSourceError` |
| `app/datasource/media/audio.py` | `convert_to_wav` / `get_audio_duration` / `slice_audio`；依赖本机 ffmpeg/ffprobe |
| `app/datasource/media/qwen_asr.py` | `qwen3-asr-flash`（DashScope `MultiModalConversation` + `file://`）；可选 context（title）；重试 ≤3；仍失败 → `DataSourceError` |
| `app/datasource/media/__init__.py` | 包导出（按需） |

实现可参考 `clever-hans/backend/app/core/media/`，但：

- 下载源是 TikHub 直链，不是 yt-dlp
- 失败抛 `DataSourceError`，不返回空 transcript
- 配置走 `app.config.settings`

### 5.2 重写门面

`app/datasource/transcribe.py`：

- 删除 filetrans POP（`AcsClient` / `SubmitTask` / `GetTaskResult`）
- 对外保留测试可 mock 的 seam，建议形态：

```python
async def transcribe(media: MediaRef | str) -> str:
    """下载 → wav →（切片）Qwen ASR → 全文。失败 DataSourceError。"""
```

**入参契约（LOAD-BEARING）**

| 类型 | 含义 | 行为 |
|---|---|---|
| `MediaRef` | 已 resolve 的媒体描述 | 用其 `download_url` / headers / decode_key / title |
| `str` | **已是可下载直链**（拆账号逐条） | **只下载+转写，禁止调用 `resolve_media`** |

分享链必须由调用方 `resolve_media` → `MediaRef` 后再进门面；禁止把分享链当 `str` 传入。

- 配置：`_is_configured()` 改为检查 `ALIYUN_ASR_KEY`；ffmpeg/ffprobe 缺失时在首次转写报 `DataSourceError`（或启动探针，实现期二选一，文档写明）。

### 5.3 TikHub 扩展（`app/datasource/tikhub.py`）

| API | 用途 |
|---|---|
| 现有 `video_meta` / `account_top_videos` | 抖音单条 / TOP N，`download_url` |
| 新增 `channels_video_meta(...)` | 视频号：`POST/GET` TikHub `wechat_channels/v2/fetch_video_detail` → `media` + `decode_key` + 标题类字段 |
| 新增 `resolve_media(url) -> MediaRef` | URL 形态判定平台并分发；未知 → `DataSourceError` |

`MediaRef` 建议字段：`platform`, `download_url`, `headers`（可选）, `decode_key`（可选）, `title`（可选）, `raw_id`（可选）。

抖音下载：GET 直链，带常见 `User-Agent` + `Referer: https://www.douyin.com/`（防 403）。  
高清播放 URL：若 `play_addr` 为空或下载失败，允许 fallback 到 TikHub 高清播放接口（抖音），再重试一次下载。

**视频号 decode（必达门槛）**

- 实现编码前先做 **spike**：真实调用 TikHub `fetch_video_detail` ≥1 条，固化样例（`media` / `decode_key` 是否为空、解码算法）。
- 样例与解码方案写入实现笔记或本 spec 附录后再合入「视频号单视频」。
- 若 spike 无法在合理时间内（建议 ≤1 个工作日）闭环：将 **视频号单视频降为可砍**，本 milestone 必达收缩为抖音双路径（单视频+拆账号）；不阻塞硬切 Qwen。

`VideoMeta` 可继续服务拆账号；单视频 resolve 产出 `MediaRef`（可含 `title` 供 Qwen context）。不必强行把 `VideoMeta` 改造成 `MediaRef`，允许并存。

### 5.4 调用方

| 入口 | 改动 |
|---|---|
| `analyze_video_link(task_id, url)` | `ref = await resolve_media(url)` → `transcribe(ref)`（带心跳） |
| `analyze_account` / `precheck` | **仅抖音**。视频号主页/未知平台 → `DataSourceError`（文案引导改用单视频粘链接） |
| `analyze_account` 逐条 | 对抖音 `VideoMeta.download_url` 调 `transcribe(str)` |
| `structure_video` / 粘文案 | 不动 |
| `asr.py` / `/ai/asr` | 不动 |

心跳：`_transcribe_with_heartbeat` 覆盖「下载 + 转码 + 多段 ASR」全程（仍可能 > Java running-timeout 5min）。

**产品/前端联动（本期默认）**

- 后端先具备视频号单视频能力；`sks-web` Analyze 入口与错误文案是否展示视频号，可同期小改或紧随——实现计划单列任务，避免「后端有、前端永远抖音文案」的静默缺口。
- 视频号拆账号未做时，前端勿暴露「视频号拆账号」。

### 5.5 配置、依赖与运维

| 变量 | 变化 |
|---|---|
| `ALIYUN_ASR_KEY` | 长转写 + 短 ASR 共用（必填于长转写路径） |
| `ALIYUN_ASR_APP_KEY` | 废弃：从必填检查、`.env.example`、GO_LIVE checklist 移除或标 deprecated |
| `TIKHUB_API_KEY` | 不变 |
| `ASR_TMP_DIR`（可选） | 临时目录；默认系统 tempfile |
| 并发 | **MVP 必做** `asr` 信号量（建议初值 3）；download / convert 建议同步加（如 5 / 4），防拆账号 20 路打爆 DashScope |

部署：`sks-ai` 镜像必须含 `ffmpeg` / `ffprobe`。

依赖：长转写不再需要 `aliyun-python-sdk-core`（若无其他模块使用则可从 `pyproject` 移除）；内容安全若仍用 AK/SK + Green SDK，与 ASR 解耦保留。Dockerfile / 依赖变更在实现计划中单列。

**成本与超时（设计级）**

- 拆账号最坏：20 ×（下载 + 多段 Qwen），耗时与费用高于原 filetrans 直传；定价侧需在联调后粗算是否仍覆盖（记录在实现/运维笔记即可）。
- 单条转写建议硬上限（如 15–20min，含切片），超时 → `DataSourceError`；整号任务继续靠心跳 + 既有 Java running 策略，避免无限挂起。

## 6. 里程碑范围（建议）

### 必达（同一 milestone 合入）

1. 媒体管线 + Qwen 门面，filetrans 删除；asr 信号量  
2. 抖音：单视频（resolve + 转写）+ 拆账号（逐条 download_url）；账号/precheck 仅抖音  
3. 视频号：**单视频**（resolve + 下载/解码 + 转写）——以 §5.3 spike 通过为前提  
4. 测试与文档/契约更新  

### 可砍（不堵必达）

- **视频号拆账号**：列表不稳或字段不足则不做；产品勿暴露入口。  
- **视频号单视频**：spike 失败时降级可砍，本 milestone 仍交付抖音双路径 + Qwen 硬切。

## 7. 失败与钱路

| 失败点 | 行为 |
|---|---|
| TikHub 不可达 / 业务码失败 | `DataSourceError` → task `failed` |
| 下载/解码失败 | 同上 |
| ffmpeg 失败 | 同上 |
| Qwen 3 次仍失败 | 同上（**不**写空 transcript 当成功） |
| LLM 结构化 blocked | 保持现有 failed + 安全文案 |

Java 侧退款 / poller 行为不改，仍依赖 Python 写 `failed`。

## 8. 测试计划

- 单测（mock 网络与 DashScope）：download、slice merge、qwen 重试、`transcribe` 编排清临时文件、`resolve_media` 抖音/视频号/未知。  
- 重写 `tests/test_transcribe.py`（去掉 POP mock）。  
- 更新 `test_video_analyze` / `test_account_analyze` 中对 `transcribe` 的 monkeypatch 签名（若改为 `MediaRef`）。  
- 联调：真实抖音链 + 真实视频号链各 ≥1；确认无 `ALIYUN_ASR_APP_KEY` 可跑通。

## 9. 文档与清单同步

- `sks-ai/docs/API_CONTRACT.md`：若有 filetrans/AppKey 描述，改为 Qwen 管线。  
- `sks-ai/.env.example`、`sks-agent/.env.example`、`deploy/GO_LIVE_CHECKLIST.md`：AppKey 降级/删除；注明 ffmpeg。  
- 本设计实现后由 writing-plans 产出实现计划（另文）。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| TikHub 直链 403 | UA/Referer；抖音高清 URL fallback |
| 视频号 decode 细节不清 | spike 门槛；失败则单视频可砍 |
| Qwen ≤5min / ≤10MB | 时长切片 + 体积校验/再切；仍超则报错 |
| 临时磁盘占满 | `finally` 清理；可选 `ASR_TMP_DIR` 监控 |
| 与短 ASR 共 Key 被限流 | **asr 信号量 MVP 必做**；观测后再调 |
| 拆账号成本/耗时上升 | 联调粗算；单条超时上限 |
| `str` 误传分享链 | 契约单测锁定：分享链必须 `MediaRef` |

## 11. 决议摘要

- 对齐 clever-hans **转写形态**，不对齐 yt-dlp。  
- 长转写 = Qwen；短校准 = Paraformer；同一 DashScope Key。  
- 硬切 filetrans。  
- 必达：抖音单视频 + 抖音拆账号 +（spike 通过后的）视频号单视频；视频号账号可砍；spike 失败则视频号单视频亦可砍。  
- `transcribe(str)` = 直链 only；分享链必须先 `resolve_media`。

## 12. 复盘补丁记录（2026-08-02）

自审后合入：入参契约、管线分 A/B 入口、10MB 约束、账号仅抖音、视频号 decode spike 门槛、asr 信号量必做、成本/超时、前端联动、依赖/ffmpeg 说明。
