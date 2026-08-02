# 视频号拆账号设计（2026-08-02）

**Status:** Draft for review  
**Parent:** [qwen-asr-media-pipeline-design](./2026-08-02-qwen-asr-media-pipeline-design.md)（Task 8 单视频已落地；本 spec 解除「拆账号仅抖音」）  
**Approach:** 方案 1 — 现有 `account` 契约上平台分发；列表直出可转写媒体

## 1. Goal

拆账号支持**视频号**：用户粘贴视频号 ID（`sph…`）或该号任意一条分享链；后端解析出 finder `username`，拉 TOP N 作品并走现有转写/结构化/归纳管线。抖音主页链接路径不变。

## 2. Non-goals

- 不要求用户输入 `v2_…@finder`
- 不新开 Java / Python 独立 channels 账号路由
- 不做视频号合集、直播回放、号内搜索
- 不改扣费公式（仍固定 10 条）与 progress / partial / failed 语义

## 3. User-facing input（B3）

| 平台 | 用户粘贴 |
|------|----------|
| 抖音 | 账号主页链接（现有） |
| 视频号 | **短号** `sph…`（如 `sphi9BjV8GK0Zsl`），或 **分享链** `https://weixin.qq.com/sph/...` |

前端（`sks-web` `/analyze` 拆账号）：placeholder + 辅助文案双平台提示；**无**平台下拉；仍 `POST /analyze/account {url}`。

## 4. API contract

不变：

- Java：`precheck(url)` → 固定扣 10 → `POST /ai/analyze/account {task_id, url}`
- Python：`precheck` / `account_top_videos` / `analyze_account`

`url` 字段语义扩展为「账号入口字符串」（链接或短号）。

## 5. Backend resolution（tikhub）

识别顺序（`account_top_videos` / `precheck` 共用）：

1. **抖音 host**（`douyin.com` / `iesdouyin.com`）→ 现有 `get_sec_user_id` + `fetch_user_post_videos`
2. **视频号短号** `^sph[A-Za-z0-9_-]+$` → `POST …/fetch_channel_id_to_username` → `username`
3. **视频号分享链** host ∈ `weixin.qq.com` 且 path 含 `/sph/` → `fetch_video_detail(share_url)` → `data.username`  
   （**禁止**把 path 段当成 `channel_id`：分享链 token ≠ `sph…` 短号）
4. 其它 → `DataSourceError("unsupported …")`

拿到 `username` 后：

- `POST …/fetch_user_videos`，`raw=false`，`last_buffer` 翻页直至凑满 **N=20** 或无下一页
- 每条 `videos[i].media`：`full_url`（或 `url+url_token`）+ `decode_key`
- 超时：channels 接口建议 client timeout ≥ 30s（TikHub 文档）

### precheck

- 解析 username 成功且首页（或已拉取）`video_count > 0` → `{reachable: true, video_count}`
- 短号未命中 / 分享链无效 / 无作品 → `{reachable: false, video_count: 0}` 或抛 `DataSourceError`（与现抖音「不可达」对齐，保证 Java **不扣费**）

## 6. Data model

扩展 `VideoMeta`（向后兼容默认值）：

```text
decode_key: str | None = None
platform: str = "douyin"   # "douyin" | "wechat_channels"
```

视频号列表映射：

- `title` ← 归一 shortTitle / title
- `play_count` ← `read_count`（微信无公开播放量时用阅读类计数；无则 0）
- `fav_count` ← `fav_count` 或 `like_count`
- `download_url` ← `media.full_url`
- `author` ← 列表/账号 `nickname`
- `decode_key` / `platform="wechat_channels"`

`video_meta_to_media_ref(v)`：

- `wechat_channels` 或非空 `decode_key` → `MediaRef` + `CHANNELS_DOWNLOAD_HEADERS` + `decode_key`
- 抖音 → 现有 `DOUYIN_DOWNLOAD_HEADERS`（不变）

`analyze_account`：**仅**保证循环内仍 `ref = video_meta_to_media_ref(v)` → heartbeat；禁止重写函数体其余锚点（per-item except / check / progress / partial）。

## 7. Frontend

`sks-web/src/pages/Analyze.tsx`（拆账号 mode）：

- placeholder 示例：「抖音：账号主页链接。视频号：sph 开头的视频号 ID，或该号任意一条分享链接。」
- 输入框下增加一行辅助说明（同义、可更短）
- 不改 API client 形状；校验保持非空 trim

拆视频·粘链接文案可顺带区分抖音/视频号（可选，非本 spec 必达）。

## 8. Failure & ops

- 全量 scrape 失败 → `failed` + 全额退（现有）
- 单条 download/decode/ASR 失败 → `continue` → 可能 `partial`
- 视频号 decode 依赖 Task 8：`node` + vendored WASM；缺 node → 单条/全量按现有 `DataSourceError` 处理
- Dockerfile / GO_LIVE：Task 9 须装 `ffmpeg` + `nodejs`（本功能依赖）

## 9. Tests

- tikhub：短号 → username mock；分享链 → detail mock；`fetch_user_videos` 解析含 `decode_key`；翻页凑 N；未知输入报错
- 删除/改写「channels host → douyin only」门禁测试
- account_analyze：fake `VideoMeta(decode_key=…)` → `transcribe` 收到带 `decode_key` 的 `MediaRef`
- 前端：若有组件测则断言 placeholder；无则手工 checklist

## 10. Rollout

实现仓：`sks-ai` worktree（媒体管线分支）+ `sks-web` 文案 PR；`sks-server` 无契约变更则可不改。
