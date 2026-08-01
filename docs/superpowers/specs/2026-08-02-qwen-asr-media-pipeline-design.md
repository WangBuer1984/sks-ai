# 长音频转写对齐 clever-hans（TikHub 下载 → wav/切片 → qwen3-asr-flash）

日期：2026-08-02  
状态：已批准（2026-08-02；含复盘补丁 + 结构审查补丁 + 代码事实校对补丁）  
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

**B. 拆账号逐条（已有 VideoMeta）**

```text
VideoMeta
  → MediaRef(platform=douyin, download_url, headers=抖音默认 UA+Referer, title=VideoMeta.title)
  → transcribe(MediaRef)  # 禁止裸 str 进拆账号热路径；download.py 保持平台无感
```

兼容：`transcribe(str)` 仍保留作测试/内部 seam，语义为「已是直链、裸下载」——**不**猜平台、**不**自动补 headers。**业务拆账号必须走 MediaRef**。

**`transcribe` 内部（A/B 汇合后）**

```text
HTTP 下载到临时目录（decode_key 非空则解码）
  → ffmpeg → WAV 16kHz mono
  → 按 duration 判定：
       duration ≤ 300s：单次 qwen3-asr-flash
       duration > 300s 或未知：切片（segment≈270s, overlap=3s）→ 分段识别 → 文本 overlap 去重拼接
  → wav 体积守卫：16k mono PCM ≈256kbps，300s≈9.2MB；若 duration≤300s 但 wav>10MB
       → 视为异常 DataSourceError（不做「压码率」；wav 码率固定，压码率对 wav 无意义）
  → finally 清理本轮临时文件；见 §5.5 陈旧临时文件 GC
  → 既有 LLM 结构化 / 账号归纳（不变）
```

参数对齐 clever-hans：`segment_duration=270`，`overlap=3`；单次调用遵守官方 `qwen3-asr-flash` **≤5 分钟且 ≤10MB**。

## 5. 模块设计（均在 `sks-ai`）

### 5.1 新增

| 路径 | 职责 |
|---|---|
| `app/datasource/media/download.py` | **纯下载**：给定最终 URL + headers → 临时文件；超时/非 2xx → `DataSourceError`。**不**调用 TikHub，**不**做高清 fallback |
| `app/datasource/media/audio.py` | `convert_to_wav` / `get_audio_duration` / `slice_audio`；依赖本机 ffmpeg/ffprobe |
| `app/datasource/media/qwen_asr.py` | `qwen3-asr-flash`（DashScope `MultiModalConversation` + `file://{wav_path}`）；`asr_options` / system 消息对齐 clever-hans：`{"language":"zh","enable_lid":True}`；system 拼「视频标题 / 作者」作 context；重试 ≤3；仍失败 → `DataSourceError` |
| `app/datasource/media/merge.py`（或放 `transcribe.py` 内私有） | 分段文本拼接：对齐 clever-hans `_merge_transcript_parts` / `_find_overlap_text`（字符串最长公共前后缀，非时间戳） |
| `app/datasource/media/__init__.py` | 包导出（按需） |

实现可参考 `clever-hans/backend/app/core/media/` + `pipeline.py` 拼接函数，但：

- 下载源是 TikHub 直链，不是 yt-dlp
- 失败抛 `DataSourceError`，不返回空 transcript
- 配置：`from app.config import settings`（`app/config.py` 内 `Settings` 单例实例，不是独立包 `app.config.settings`）

**模块边界（LOAD-BEARING）**

- **高清播放 fallback / play_addr 为空**：只发生在 `tikhub.resolve_media`（或抖音 `video_meta` 增强）阶段；产出的 `MediaRef.download_url` 已是最终可下地址。
- **下载失败后的一次 TikHub 高清重试**：若需要，由 `transcribe` 门面编排「通知 tikhub 换 URL → 再调 download」；**禁止** `download.py` 反向 import/调用 TikHub。

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
| `MediaRef` | 已 resolve / 已装配的媒体描述 | 用其字段见下；**业务热路径（单视频+拆账号）只走此类型** |
| `str` | **已是可下载直链**（测试/兼容 seam） | **禁止** `resolve_media`；**不**按 host 猜平台、**不**自动补抖音 headers（裸下载）；需要 headers/title/author 时调用方必须传 `MediaRef`。**拆账号业务不得依赖裸 str** |

分享链必须由调用方 `resolve_media` → `MediaRef` 后再进门面；禁止把分享链当 `str` 传入。

**拼接算法**：移植 clever-hans `backend/app/core/pipeline.py` 的 `_merge_transcript_parts` + `_find_overlap_text`（tail/head 最长公共前后缀，上限约 50 字；无时间戳对齐）。实现期带与 clever-hans 同构的单测（见其 `tests/test_pipeline.py`）。

- 配置：读 `from app.config import settings`（见上）。长转写 `_is_configured()` **只查** `settings.ALIYUN_ASR_KEY`（不再查 ACCESS_KEY / AppKey）。短 ASR `asr.py` 的 `_is_configured()` 已是同一 Key，无需改语义。ffmpeg/ffprobe 缺失时首次转写报 `DataSourceError`（或启动探针）。

### 5.3 TikHub 扩展（`app/datasource/tikhub.py`）

| API | 用途 |
|---|---|
| 现有 `video_meta` / `account_top_videos` | 抖音单条 / TOP N，`download_url` |
| 新增 `channels_video_meta(...)` | 视频号：`POST/GET` TikHub `wechat_channels/v2/fetch_video_detail` → `media` + `decode_key` + 标题类字段 |
| 新增 `resolve_media(url) -> MediaRef` | URL 形态判定平台并分发；未知 → `DataSourceError` |
| 新增 `video_meta_to_media_ref(v, *, author=None) -> MediaRef` | **放 `tikhub.py`**：`VideoMeta` → `MediaRef`，写入抖音默认 headers、`title=v.title`、可选 `author`（拆账号传入账号名） |

`MediaRef` 字段：`platform`, `download_url`, `headers`（可选）, `decode_key`（可选）, `title`（可选）, `author`（可选，供 Qwen system context「作者: …」）, `raw_id`（可选）。

**抖音 headers / `play_addr` fallback 归属（LOAD-BEARING）**

- `play_addr` 仅是 `tikhub._parse_video` 内部中间字段，**不**出现在 `VideoMeta` / `MediaRef` 公共 API。
- 「`play_addr` 为空 → 调 TikHub 高清播放接口」**只发生在** `resolve_media` / `_parse_video`（或同文件辅助函数）内部；产出的 `MediaRef.download_url` 已是最终可下链接。
- `download.py` **不感知** `play_addr`、不感知 TikHub 高清接口、不 import tikhub。
- 默认下载 headers（`User-Agent` + `Referer: https://www.douyin.com/`）在 **装配 MediaRef 时写入**（`resolve_media` 或 `VideoMeta → MediaRef` 辅助函数）。
- 首次 download 403/失败后的「再取高清 URL 重试一次」若需要：由 **门面 `transcribe` 编排调用 tikhub**，再把新 URL 交给 download；仍失败 → `DataSourceError`。

**视频号 decode（必达门槛）**

- 实现编码前先做 **spike**：真实调用 TikHub `fetch_video_detail` ≥1 条，固化样例（`media` / `decode_key` 是否为空、解码算法）。
- 样例与解码方案写入实现笔记或本 spec 附录后再合入「视频号单视频」。
- 若 spike 无法在合理时间内（建议 ≤1 个工作日）闭环：将 **视频号单视频降为可砍**，本 milestone 必达收缩为抖音双路径（单视频+拆账号）；不阻塞硬切 Qwen。

**`VideoMeta` 与 `MediaRef`**

- 允许并存：`VideoMeta` 继续服务拆账号列表解析（字段：`title` / `play_count` / `fav_count` / `download_url`）。
- 拆账号逐条（**选定路径 b**）：`video_meta_to_media_ref(v, author=账号名) → MediaRef(...)` → `transcribe(MediaRef)`。  
  **不**走裸 `transcribe(v.download_url)`，以免丢防 403 headers 与 title/author context。  
  **不**把平台判定塞进 `download.py`。  
  `author`：拆账号场景几乎免费（主页/sec_user 解析可得）；单视频 `resolve_media` 若 TikHub 返回作者则填，否则可空。

### 5.4 调用方

| 入口 | 改动 |
|---|---|
| `analyze_video_link(task_id, url)` | `ref = await resolve_media(url)` → `_transcribe_with_heartbeat(task_id, ref)` |
| `analyze_account` / `precheck` | **仅抖音**。视频号主页/未知平台 → `DataSourceError`（文案引导改用单视频粘链接） |
| `analyze_account` 逐条 | `ref = video_meta_to_media_ref(v, author=账号名)` → `_transcribe_with_heartbeat(task_id, ref)` |
| `structure_video` / 粘文案 | 不动 |
| `asr.py` / `/ai/asr` | 不动（仍用 `settings.ALIYUN_ASR_KEY`；本期不重命名） |

**心跳（两处独立实现，须同步改）**

- `_transcribe_with_heartbeat` **不在** `transcribe.py`，而是两份拷贝：  
  1. `app/skills/video_analyze/graph.py`  
  2. `app/skills/account_analyze/graph.py`  
- 二者均需同步调整入参：`download_url: str` → `media: MediaRef | str`（或统一 `MediaRef`），内部 `create_task(transcribe(media))`，60s touch `updated_at` 覆盖下载+转码+多段 ASR。
- 实现期 checklist：改完 video 必须同 PR 改 account，禁止只改一处。

**心跳 vs Java running-timeout（澄清，通常无需改 Java）**

- Java「5min running-timeout」语义是：**`updated_at` 停滞超过 5min** 才判 failed，不是墙钟总时长 5min。
- 心跳 60s touch 下，总耗时 15–20min 仍可存活。
- **本期默认不改** Java / sks-server timeout；实现计划列为核对项：「联调确认 heartbeat 覆盖新管线；若 touch 间隙断裂再与 sks-server 联动」。
- Python 单条硬上限（建议 15–20min）是独立熔断，超时 → `DataSourceError`，与 Java 策略正交。

**产品/前端联动（本期默认）**

- 后端先具备视频号单视频能力；`sks-web` Analyze 入口与错误文案是否展示视频号，可同期小改或紧随——实现计划单列任务，避免「后端有、前端永远抖音文案」的静默缺口。
- 视频号拆账号未做时，前端勿暴露「视频号拆账号」。

### 5.5 配置、依赖与运维

| 变量 | 变化 |
|---|---|
| `ALIYUN_ASR_KEY` | 长转写 + 短 ASR（`asr.py`）共用。**命名债**：实为 DashScope/百炼 API Key，**非**阿里云 ISI。本期**不重命名**（会同时震动 `asr.py` 的 `dashscope.api_key = settings.ALIYUN_ASR_KEY`）；`.env.example` / `app/config.py` 注释必须写明。可选后续改名 `DASHSCOPE_API_KEY` |
| `ALIYUN_ASR_APP_KEY` | 废弃：从长转写 `_is_configured`、`.env.example`、**`sks-agent/deploy/GO_LIVE_CHECKLIST.md`** 移除或标 deprecated |
| `ALIYUN_ACCESS_KEY_ID/SECRET` | **保留**：内容安全 `AcsClient` 仍用。从长转写 `_is_configured()` 检查项中**摘除**；不从 `.env.example` 删除 |
| `TIKHUB_API_KEY` | 不变 |
| `ASR_TMP_DIR`（可选） | 临时目录；默认系统 tempfile |
| 并发 | **MVP 必做** `asr` 信号量（初值 3 为起点，§8 联调压测后定稿）；download / convert 建议同步加（如 5 / 4） |

部署：`sks-ai` 镜像必须含 `ffmpeg` / `ffprobe`。

**依赖与 SDK 边界（已 grep 确认，2026-08-02）**

- `aliyun-python-sdk-core`：**不得从 `pyproject` 移除**。`app/safety/content_safety.py` 用同一包的 `AcsClient` + `CommonRequest` 打 `green-cip.*.aliyuncs.com`（TextModeration 2.0）；仓内**没有**独立 green SDK。长转写只是**停止**调用其 filetrans 路径，与内容安全在 SDK 上解耦、在依赖上共享。
- 误删该包会打挂 UGC 内容安全审核，违反仓内硬不变量。
- Dockerfile / ffmpeg：实现计划单列。

**临时文件与 hard-kill**

- 正常/异常路径：`transcribe` 的 `finally` 清理本轮文件。
- 进程 OOM / 被 kill：`finally` **不执行**。缓解（MVP 必做其一或组合）：  
  1. 使用系统 `tempfile` + 进程退出尽力清理；  
  2. `transcribe` 入口对 `ASR_TMP_DIR`（或约定前缀）做 **陈旧文件 GC**（如 mtime > 2h 删除）；  
  3. 可选：运维侧磁盘监控告警。  
- 不得仅依赖「finally 即可防占满」。

**成本与超时（设计级）**

- 拆账号最坏：20 ×（下载 + 多段 Qwen），耗时与费用高于原 filetrans；联调后粗算是否覆盖定价。
- 单条 Python 硬上限 15–20min；整号靠心跳续 `updated_at`（见上）。

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

- **必重写**：`tests/test_transcribe.py`——现状全程 mock `_submit_task` / `_get_task_result` POP seam；切 Qwen 后改为 mock `download` / `convert_to_wav` / `slice_audio` / `qwen_asr` / merge。  
- **skill 层测试改动小**：`test_video_analyze.py` / `test_account_analyze.py` mock 的是 skill 模块级 `transcribe` 别名（经 `_transcribe_with_heartbeat`），不是 POP seam。只要符号仍被 import，通常只需：心跳包裹层入参改为 `MediaRef` 时同步 mock 入参；返回值语义不变则断言可不动。  
- 单测另补：`resolve_media`、`video_meta_to_media_ref` 带默认 headers、分享链误传契约、merge 对齐 clever-hans。  
- 联调：  
  1. 真实抖音链 +（spike 通过后）真实视频号链各 ≥1；无 AppKey 可跑通。  
  2. **DashScope 并发压测**：标定 `asr` 信号量。  
  3. 心跳下单条 >5min 墙钟不被 Java 误判 failed。

## 9. 文档与清单同步

- `sks-ai/docs/API_CONTRACT.md`：**无需**改写「转写实现/filetrans」描述（契约本就不暴露实现细节）。仅核对：`POST /ai/asr` 的 503/`ASR_NOT_CONFIGURED` 仍对应 `ALIYUN_ASR_KEY`，**不要**引入 AppKey 文案。  
- `sks-ai/.env.example`、`sks-agent/.env.example`：废弃 AppKey；`ALIYUN_ASR_KEY` 注释写明 DashScope/百炼；保留 ACCESS_KEY 给内容安全；注明 ffmpeg。  
- **`sks-agent/deploy/GO_LIVE_CHECKLIST.md`**（已存在于 **sks-agent** 仓，非 sks-ai）：更新 AppKey / 长转写 / ffmpeg 条目。勿写成 `sks-ai/deploy/...`。  
- Java running-timeout：**默认不改**；实现计划与 sks-server 核对项。  
- 本设计实现后由 writing-plans 产出实现计划（另文）。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| TikHub 直链 403 | 拆账号走 MediaRef+默认 headers（路径 b）；高清 fallback 仅 resolve/_parse_video 或门面，不在 download.py |
| 视频号 decode 细节不清 | spike 门槛；失败则单视频可砍 |
| Qwen ≤5min / ≤10MB | duration 切片为主；≤300s 却 >10MB wav 当异常 |
| 临时磁盘占满 | finally + 入口陈旧 GC；不假设 hard-kill 能 finally |
| DashScope QPS | asr 信号量 + §8 压测定值 |
| 拆账号成本/耗时上升 | 联调粗算；单条 Python 超时；心跳续命 |
| `str` 误传分享链 | 契约单测 |
| `ALIYUN_ASR_KEY` 命名债 | 注释写明；重命名可选后续 |

## 11. 决议摘要

- 对齐 clever-hans **转写形态**，不对齐 yt-dlp。  
- 长转写 = Qwen；短校准 = Paraformer；同一 `ALIYUN_ASR_KEY`（DashScope/百炼，命名保留）。  
- 硬切 filetrans；**`aliyun-python-sdk-core` 因内容安全保留**——ASR 与内容安全在调用路径上解耦、在依赖包上共享。  
- `ALIYUN_ACCESS_KEY_ID/SECRET` 留给内容安全；长转写 `_is_configured` 只查 DashScope Key。  
- 必达：抖音单视频 + 抖音拆账号 +（spike 通过后的）视频号单视频；视频号账号可砍；spike 失败则视频号单视频亦可砍。  
- 业务热路径只走 `MediaRef`（含默认 headers）；分享链必须先 `resolve_media`。  
- 两处 `_transcribe_with_heartbeat` 同 PR 改签名。

## 12. 复盘补丁记录（2026-08-02）

自审后合入：入参契约、管线分 A/B 入口、10MB 约束、账号仅抖音、视频号 decode spike 门槛、asr 信号量必做、成本/超时、前端联动、依赖/ffmpeg 说明。

## 13. 第一轮结构审查补丁（2026-08-02）

采纳审查意见后锁定：

| ID | 决议 |
|---|---|
| P1-1 | 高清 fallback / play_addr 空 → **仅** tikhub resolve（或门面编排一次重试）；`download.py` 纯下载 |
| P1-2 | 初版曾倾向 str 门面补 headers；§14 改为拆账号必走 MediaRef（路径 b） |
| P1-3 | 保留名 `ALIYUN_ASR_KEY`，注释标明 DashScope/百炼；重命名可选后续 |
| P2-4 | 拼接对齐 clever-hans `_merge_transcript_parts` / `_find_overlap_text` + 同构单测 |
| P2-5 | 去掉「压码率」；按 duration 切片；≤300s 且 wav>10MB → 异常 |
| P2-6 | Java 5min = updated_at 停滞；心跳 60s 续命；**默认不改 Java**；Python 单条硬上限独立 |
| P3-7 | §8 增加 DashScope 并发压测联调项 |
| P3-8 | finally + 入口陈旧临时文件 GC；承认 hard-kill 无 finally |
| P3-9 | **已确认**：`aliyun-python-sdk-core` 因 `content_safety.py` **必须保留** |

## 14. 代码事实校对补丁（2026-08-02，第二轮）

针对「与代码现状冲突」审查，再锁定：

| ID | 决议 |
|---|---|
| P0-1 | 措辞改为：**不得**移除 `aliyun-python-sdk-core`；长转写只停用 filetrans；§11 写明 SDK 解耦边界 |
| P0-2 | GO_LIVE 路径订正为已存在的 **`sks-agent/deploy/GO_LIVE_CHECKLIST.md`** |
| P0-3 | `API_CONTRACT.md` 无需改实现描述；只核对短 ASR 503 ↔ `ALIYUN_ASR_KEY` |
| P0-4 | 配置写法改为 `from app.config import settings`（`app/config.py` Settings 实例） |
| P1-1 | **选定 (b)**：拆账号 `VideoMeta → MediaRef(+headers+title)`，不走裸 str |
| P1-2 | `play_addr` 仅 resolve/_parse_video 内部；download 不感知 |
| P1-3 | 点明 video/account **两处** heartbeat 同 PR 改 |
| P1-4 | 命名债 + 不重命名原因（`asr.py`）写清 |
| P3-1 | 细化：重写 `test_transcribe.py`；video/account 测试改动面小 |
| P3-3 | ACCESS_KEY 保留给内容安全；仅从长转写 `_is_configured` 摘除 |

## 15. 外部引用验证 + P3 润色（2026-08-02）

外部断言已核对通过：Java `updated_at` 停滞 5min、clever-hans merge（50 字 overlap 上限 / 200 搜索窗）、`qwen3-asr-flash` + `file://` + `MultiModalConversation`。

| ID | 决议 |
|---|---|
| P3-a | `MediaRef.author?`；`video_meta_to_media_ref(..., author=)` 填账号名 |
| P3-b | `qwen_asr`：`asr_options={language:zh, enable_lid:True}` + system 标题/作者，对齐 clever-hans |
| P3-c | `video_meta_to_media_ref` **明确放 `tikhub.py`** |
| P3-d | `str` seam = 裸下载，不猜平台、不补 headers |
