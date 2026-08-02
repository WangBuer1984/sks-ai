# 视频号拆账号设计（2026-08-02）

**Status:** Approved for implementation（review patch 2026-08-02）  
**Parent:** [qwen-asr-media-pipeline-design](./2026-08-02-qwen-asr-media-pipeline-design.md)（Task 8 单视频已落地；本 spec 解除「拆账号仅抖音」）  
**Approach:** 方案 1 — 现有 `account` 契约上平台分发；列表直出可转写媒体  
**Prerequisite:** Task 8（`3d0d23a`）已补审查通过（见 §0）

## 0. Prerequisite — Task 8 补审查（P0）

实现本规范前，对 `3d0d23a` 补审查结论（质量批准，带供应链记录）：

| 项 | 结论 |
|----|------|
| `decode_media` 注入 | 填 Task 5 seam；测试可 monkeypatch `None`；`decode_key` 空则不调用 |
| 缺 `node` / 缺 WASM / CLI 非零 / 非 ftyp | 一律 `DataSourceError`（`channels_decode.py`）— 账号路径单条 `continue`、全量可 `failed` |
| WASM 来源 | 微信客户端 `wasm_video_decode` 经社区再分发；glue 改编自 [RongleCat/n8n-nodes-wechat-channels](https://github.com/RongleCat/n8n-nodes-wechat-channels)；算法见 [Evil0ctal/…](https://github.com/Evil0ctal/WeChat-Channels-Video-File-Decryption) |
| 完整性 | `wasm_video_decode.wasm` SHA-256 = `dca796bacec37d8522c7983b3945e5d579bd74164e3b21f0ebc773be6dfc8b6e`（3785516 bytes）；写入 `app/datasource/media/wechat_wasm/README.md` |
| License | 微信二进制无明确开源许可；属逆向再分发灰区——产品接受为「平台对接必要依赖」；不得再对外单独提供 decrypt SaaS |

未再阻塞：可在本规范上实现账号路径。

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

**Java 可不改**（§10）：不可达一律 200 + `{reachable:false, video_count:0}`（见 §5.2）；`AnalyzeService.startAccount` 在扣费前判断 `!reachable || videoCount<=0` 即拒绝。  
（注：precheck **传输层**失败抛错时 Java 也不扣费——扣费在 precheck 成功之后；但业务不可达不得用抛错代替 `reachable:false`，以免与抖音文案/路径分叉。）

## 5. Backend resolution（tikhub）

### 5.1 共享入口分类器（新函数，非仅 `_platform_of`）

`_platform_of` 只看 host，对裸 `sph…` 会判 `unknown`。新增例如 `_account_entry_kind(raw: str) -> Literal["douyin","channels_id","channels_share","unknown"]`：

识别顺序（对 trim 后字符串；无 netloc 时 host 检测必须安全 no-match，禁止因 `urlparse` 崩）：

1. **抖音 host**（`douyin.com` / `iesdouyin.com`）→ `douyin`
2. **视频号短号** `^sph[A-Za-z0-9_-]+$`（整串，无 scheme）→ `channels_id`
3. **视频号分享链** host 以 `weixin.qq.com` 结尾且 path 含 `/sph/` → `channels_share`  
   （**禁止**把 path 段当成 `channel_id`）
4. 其它 → `unknown`

`account_top_videos` / `precheck` **共用**该分类器 + 下游解析。

### 5.2 解析 → username

| kind | 动作 |
|------|------|
| `douyin` | 现有 `get_sec_user_id` + posts |
| `channels_id` | `POST …/fetch_channel_id_to_username` → `username`；未命中（username null）→ 见 precheck/列表失败语义 |
| `channels_share` | `fetch_video_detail(share_url)` → `data.username` |
| `unknown` | `DataSourceError("unsupported …")`（配置错误类；precheck 可映射为 unreachable，见下） |

### 5.3 `account_top_videos`（视频号）

1. 解析出 `username`
2. `POST …/fetch_user_videos`，`raw=false`
3. **翻页硬上限 `max_pages=4`**，或凑满 **N=20**，或 `up_continue` 为假 → 停止；按已取条数返回（可 `< N`）
4. 每条 `videos[i].media`：`full_url`（或 `url+url_token`）+ **同条** `decode_key`  
   **禁止跨条目混用 decode_key / URL**（与 Task 8 单视频不变式相同）
5. 单请求 timeout ≥ 30s；翻页总时长另受 `max_pages` 约束

### 5.4 precheck（视频号）— P1 / P2 钉死

与抖音对齐：

- **只拉首页一页** `fetch_user_videos`（`last_buffer` 空），**不翻页**
- `video_count = len(videos)`（本页条数；非全站 total）
- 业务不可达（短号未命中 / 分享链无 username / 首页 0 条 / unknown 入口）→ **一律**  
  `{reachable: false, video_count: 0}`，**不抛** `DataSourceError`  
  （传输失败 / 未配置 key 仍可抛，与现抖音 precheck 一致）
- 成功 → `{reachable: true, video_count}`

## 6. Data model

扩展 `VideoMeta`（向后兼容默认值）：

```text
decode_key: str | None = None
platform: str = "douyin"   # "douyin" | "wechat_channels"
```

视频号列表映射：

- `title` ← 归一 shortTitle / title（复用 `_channels_title`）
- `play_count` ← `read_count`（**阅读量代理**，非真实播放量；跨平台与抖音 `play_count` 同列比较需谨慎；缺省 0）
- `fav_count` ← **先** `fav_count`，缺省再用 `like_count`，再缺省 0
- `download_url` ← `media.full_url`（同条响应）
- `author` ← 列表项或页级 `nickname`
- `decode_key` / `platform="wechat_channels"`（与同条 `download_url` 配对）

`video_meta_to_media_ref(v)`：

- `platform=="wechat_channels"` 或非空 `decode_key` → `MediaRef` + `CHANNELS_DOWNLOAD_HEADERS` + `decode_key`
- 抖音 → 现有 `DOUYIN_DOWNLOAD_HEADERS`（不变）

`analyze_account`：**仅**保证循环内仍 `ref = video_meta_to_media_ref(v)` → heartbeat；禁止重写函数体其余锚点（per-item except / check / progress / partial）。

## 7. Frontend

`sks-web/src/pages/Analyze.tsx`（拆账号 mode）：

- placeholder：「抖音：账号主页链接。视频号：sph 开头的视频号 ID，或该号任意一条分享链接。」
- 输入框下增加一行辅助说明（同义、可更短）
- 不改 API client 形状；校验保持非空 trim

拆视频·粘链接文案可顺带区分抖音/视频号（可选，非本 spec 必达）。

## 8. Failure & ops

- 全量 scrape 失败 → `failed` + 全额退（现有）
- 单条 download/decode/ASR 失败 → `continue` → 可能 `partial`
- 视频号 decode：Task 8 `node` + vendored WASM；缺 node → `DataSourceError`
- Dockerfile / GO_LIVE（Task 9）：`ffmpeg` + `nodejs`

## 9. Tests

- `_account_entry_kind`：裸 sph / 分享链 / 抖音 / 垃圾串；裸串不得因 urlparse 崩
- 短号 → username mock；分享链 → detail mock；`fetch_user_videos` 含 `decode_key`；翻页在 `max_pages` 截断
- precheck：未命中 → `{reachable:false, video_count:0}`（不抛）；首页有条 → reachable
- 删除「channels → douyin only」门禁测试；改为 channels 可走通 mock
- account_analyze：`VideoMeta(decode_key=…)` → `transcribe` 收到配对 `MediaRef`
- 前端：placeholder 文案（有测则断言，无则 checklist）

## 10. Rollout

- `sks-ai` worktree + `sks-web` 文案
- **`sks-server` 不改**（P1 钉死 `reachable:false`）

## Review patch log（2026-08-02）

| ID | 裁决 |
|----|------|
| P0 | Task 8 补审查通过 + WASM SHA-256 入 README |
| P1 | 业务不可达 → `{reachable:false, video_count:0}`，不抛 |
| P2a | `account_top_videos` `max_pages=4` |
| P2b | precheck 只拉首页、不翻页、`video_count=len(page)` |
| P3 | play_count 代理说明；fav 优先序；新 `_account_entry_kind`；同条 decode 配对 |
