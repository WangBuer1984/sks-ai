# 长音频转写对齐 clever-hans（TikHub 下载 → wav/切片 → qwen3-asr-flash）

日期：2026-08-02  
状态：待审  
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

```text
用户 URL（分享链 / 主页）
  → 平台判定（抖音 | 视频号 | 未知）
  → TikHub resolve → MediaRef{ download_url, headers?, decode_key?, title? }
  → HTTP 下载到临时目录（decode_key 非空则解码）
  → ffmpeg → WAV 16kHz mono
  → duration ≤ 300s：一次 qwen3-asr-flash
     duration > 300s 或未知：切片（segment≈270s, overlap=3s）→ 分段识别 → overlap 文本去重拼接
  → finally 清理临时文件
  → 既有 LLM 结构化 / 账号归纳（不变）
```

参数对齐 clever-hans：`segment_duration=270`，`overlap=3`，单次上限参考官方 `qwen3-asr-flash` **≤5 分钟 / 10MB**。

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

- `str` 兼容期：若传入仍是 URL，视为「已是 download_url」（拆账号逐条）；单视频应由调用方先 resolve。
- 配置：`_is_configured()` 改为检查 `ALIYUN_ASR_KEY`（及可选探测 ffmpeg，联调期至少文档要求）。

### 5.3 TikHub 扩展（`app/datasource/tikhub.py`）

| API | 用途 |
|---|---|
| 现有 `video_meta` / `account_top_videos` | 抖音单条 / TOP N，`download_url` |
| 新增 `channels_video_meta(...)` | 视频号：`POST/GET` TikHub `wechat_channels/v2/fetch_video_detail` → `media` + `decode_key` + 标题类字段 |
| 新增 `resolve_media(url) -> MediaRef` | URL 形态判定平台并分发；未知 → `DataSourceError` |

`MediaRef` 建议字段：`platform`, `download_url`, `headers`（可选）, `decode_key`（可选）, `title`（可选）, `raw_id`（可选）。

抖音下载：GET 直链，带常见 `User-Agent` + `Referer: https://www.douyin.com/`（防 403）。  
视频号：下载后若存在 `decode_key`，按 TikHub/社区约定做解码（实现期对照 TikHub 返回样例；解码失败明确 `DataSourceError`）。

高清播放 URL：若 `play_addr` 为空或下载失败，允许 fallback 到 TikHub 高清播放接口（抖音），再重试一次下载。

### 5.4 调用方

| 入口 | 改动 |
|---|---|
| `analyze_video_link(task_id, url)` | `ref = await resolve_media(url)` → `transcribe(ref)`（带心跳） |
| `analyze_account` 逐条 | 继续对抖音 `VideoMeta.download_url` 调 `transcribe`；若做视频号账号则用对应列表 → `MediaRef` |
| `structure_video` / 粘文案 | 不动 |
| `asr.py` / `/ai/asr` | 不动 |

心跳：`_transcribe_with_heartbeat` 覆盖「下载 + 转码 + 多段 ASR」全程（仍可能 > Java running-timeout 5min）。

### 5.5 配置与运维

| 变量 | 变化 |
|---|---|
| `ALIYUN_ASR_KEY` | 长转写 + 短 ASR 共用（必填于长转写路径） |
| `ALIYUN_ASR_APP_KEY` | 废弃：从必填检查、`.env.example`、GO_LIVE checklist 移除或标 deprecated |
| `TIKHUB_API_KEY` | 不变 |
| `ASR_TMP_DIR`（可选） | 临时目录；默认系统 tempfile |
| 并发（可选） | download / convert / asr 信号量；初值保守（如 5 / 4 / 3），可随后调 |

部署：`sks-ai` 镜像必须含 `ffmpeg` / `ffprobe`。

内容安全 AK/SK（`ALIYUN_ACCESS_KEY_ID/SECRET`）与长转写解耦，不再因 ASR 需要 AppKey。

## 6. 里程碑范围（建议）

### 必达（同一 milestone 合入）

1. 媒体管线 + Qwen 门面，filetrans 删除  
2. 抖音：单视频（resolve + 转写）+ 拆账号（逐条 download_url）  
3. 视频号：**单视频**（resolve + 下载/解码 + 转写）  
4. 测试与文档/契约更新  

### 可砍（不堵必达）

- **视频号拆账号**：若 TikHub 用户作品列表不稳定或字段不足，本里程碑只做单条；账号拆紧随下一小迭代。产品文案可暂仅暴露视频号「粘链接」。

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
| 视频号 decode 细节不清 | 联调样例驱动；单视频优先；账号可砍 |
| Qwen 5min/10MB 限制 | 切片 + overlap 拼接（clever-hans 已验证思路） |
| 临时磁盘占满 | `finally` 清理；可选 `ASR_TMP_DIR` 监控 |
| 与短 ASR 共 Key 被限流 | 分信号量；观测后再调并发 |

## 11. 决议摘要

- 对齐 clever-hans **转写形态**，不对齐 yt-dlp。  
- 长转写 = Qwen；短校准 = Paraformer；同一 DashScope Key。  
- 硬切 filetrans。  
- 必达三件套 + 视频号账号可砍。
