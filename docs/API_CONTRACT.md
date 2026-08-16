# API_CONTRACT — sks-ai `/ai/*` 端点 + 共享表契约

sks-ai 是服务提供方，本文件是跨仓契约真相。消费方 sks-server 的 `AiClient`（`src/main/java/com/sks/aiclient/AiClient.java`）内部 record 须与本文件逐字对齐。

本文件字段全部从 `app/api/*.py` 的 pydantic 模型抽取。**改端点入参/出参时同步改本文件**，否则跨仓漂移无人发现。

---

## 1. 鉴权与错误语义

Java 每个请求带两个头：

| 头 | 来源 | Python 行为 |
|---|---|---|
| `X-Service-Token` | deploy 仓 `.env` 的 `SERVICE_TOKEN`，两侧同值 | `app/api/deps.py::verify_service_token` 校验 |
| `X-Request-Id` | Java 生成的 UUID | **当前不读**——无中间件消费。缺失不报错 |

`verify_service_token` 挂在所有 `/ai/*` router 的 `dependencies` 上，`GET /health` 是唯一免鉴权端点。

**鉴权失败不是 401。** 实际状态码：

| 情况 | 状态码 | 响应体 |
|---|---|---|
| 缺 `X-Service-Token` 头 | **422** | `{"detail":[{"type":"missing","loc":["header","x-service-token"],...}]}` |
| token 与 `settings.SERVICE_TOKEN` 不匹配 | **403** | `{"detail":"invalid X-Service-Token"}` |
| Python 侧 `SERVICE_TOKEN` 为空（未注入 .env） | **503** | `{"detail":"SERVICE_TOKEN not configured"}` |
| 请求体 pydantic 校验失败 | **422** | FastAPI 标准 validation error |

> 缺头返回 422 而非 401，是因为 `x_service_token: str = Header(...)` 是必填参数，FastAPI 把它当校验错误收集。这条容易被写成 401——`AiClient` 的错误分支不要假设 401 存在。

**Java 侧统一翻译**：`AiClient.post` / `AiClient.get` 把所有非 2xx（403 / 422 / 5xx / 超时）一律翻译为 `BizException(ErrorCode.AI_FAILED)`（code 5001）。故 Python 的细分状态码只影响日志排查，不影响前端可见错误码。

**超时链**（设计 §5.3，内层短于外层）：Python 内 LLM 单次 120s × 最多 2（原始 + 1 重试）= **240s** → Java `sks.ai.read-timeout-seconds` **270s** → nginx `proxy_read_timeout` **300s**。外层不可先掐断仍在跑的 Python 调用，否则假 AI_FAILED → 误退款。

---

## 2. 通用约定

- **无 `ApiResponse` 包壳**。Python 返回自己的 pydantic JSON（如 `{"embedding":[...]}`），**不是** Java 的 `{code,message,data}`。
- **无流式**（硬不变量）。所有端点生成完整 → 内容安全审 → 一次性 JSON 返回。禁 SSE / 打字机。
- **`blocked` 语义**。UGC 或 LLM 产出命中内容安全时返回 `{"blocked": true}`，业务字段全部缺省。Python **不抛异常**，由 Java 决策退款/拒绝。
- **`response_model_exclude_unset=True`**。下列端点未设置的字段会**从 JSON 中省略**（不是 `null`）：`/ai/script_gen`、`/ai/rewrite_sentence`、`/ai/card_gen`、`/ai/interview/step`、`/ai/interview/result`、`/ai/attribution/single`、`/ai/attribution/weekly`。故 blocked 响应实际就是 `{"blocked":true}` 一个键。Java 侧 record 全部标了 `@JsonIgnoreProperties(ignoreUnknown = true)`，缺失字段落到 Java 默认值（对象 `null`、`boolean` `false`）。

---

## 3. 端点总表

| 方法 | 路径 | 鉴权 | 用途 | Java 调用方 |
|---|---|---|---|---|
| GET | `/health` | 免 | 健康检查 | compose healthcheck |
| POST | `/ai/embed` | 需 | 文本 → 1024 维向量 | `AiClient.embed` |
| POST | `/ai/safety/check` | 需 | UGC 内容安全审 | `AiClient.safetyCheck` |
| POST | `/ai/script_gen` | 需 | 生成稿件（扣费） | `AiClient.scriptGen` |
| POST | `/ai/rewrite_sentence` | 需 | 单句重写（免费） | `AiClient.rewriteSentence` |
| POST | `/ai/card_gen` | 需 | 补卡：抽卡 + 缺口 + 冲突 | `AiClient.cardGen` |
| POST | `/ai/interview/step` | 需 | 定位访谈推进一轮（免费） | `AiClient.interviewStep` |
| GET | `/ai/interview/result` | 需 | 只读取访谈产出 | `AiClient.interviewResult` |
| POST | `/ai/interview/sample-opening` | 需 | 试试效果：产无档案/有档案两版开场钩子 | `AiClient.sampleOpening` |
| POST | `/ai/asr` | 需 | 短音频转文字（multipart） | `AiClient.asr` |
| POST | `/ai/analyze/precheck` | 需 | 拆账号预检（免费，不扣费） | `AiClient.precheck` |
| GET | `/ai/hot_board` | 需 | 平台热点榜 | `AiClient.hotBoard` |
| GET | `/ai/analyze/video/metrics` | 需 | 单视频互动五码（抖音+视频号） | `AiClient.fetchVideoMetrics` |
| POST | `/ai/analyze/video/text` | 需 | 拆视频（同步，文案直传） | `AiClient.analyzeVideoText` |
| POST | `/ai/analyze/video/link` | 需 | 拆视频（异步 202，链接） | `AiClient.analyzeVideoLink` |
| POST | `/ai/analyze/account` | 需 | 拆账号（异步 202） | `AiClient.analyzeAccount` |
| POST | `/ai/attribution/single` | 需 | flop 归因（免费） | `AiClient.attributionSingle` |
| POST | `/ai/attribution/weekly` | 需 | 周归因聚合 | `AiClient.attributionWeekly` |

---

## 4. 端点详情

### GET /health

免鉴权。出参 `{"status": "UP"}`。

`init_pool` 或 checkpointer setup 失败**不影响** `/health` 为 UP（见 `app/main.py` lifespan）——两者失败只 log，对应端点按需失败。故 `/health` UP **不等于** DB 可用。

### POST /ai/embed

```jsonc
// 入参 EmbedRequest
{ "text": "string" }
// 出参 EmbedResponse
{ "embedding": [/* float × 1024 */] }
```

长度恒为 1024（智谱 embedding-3，`app/rag/embedding.py` 有运行期 assert）。与 `kb_card.embedding vector(1024)` 列绑定，换模型需全库重算 + 改列定义。

### POST /ai/safety/check

```jsonc
// 入参 SafetyRequest        // 出参 SafetyResponse
{ "text": "string" }         { "safe": true }
```

`safe=false` 时由 Java 抛 `CONTENT_BLOCKED`（5002）。

### POST /ai/script_gen

```jsonc
// 入参 ScriptGenRequest
{
  "user_id": 1,                                   // int，必填
  "topic": { "title": "string", "rationale": "" },// TopicRequest，rationale 默认 ""
  "profile": {},                                  // dict，默认 {}
  "platform": "douyin",                           // Literal["douyin","channels"]，默认 douyin；其余值 422
  "duration": "45",                               // '45'|'90'|'180' 秒，默认 '45'
  "generation_group_id": 42,                      // int|null，默认 null；Java 编排标识，Python 不做组去重/计费
  "framework": "钩子-冲突-反转-收尾",                 // str|null，默认 null；进入写稿 prompt
  "cited_content_ids": [7, 8]                     // list[int]|null，默认 null；非空则跳过检索、按 id 加载（懒生成复用快照）
}
// 出参 ScriptGenResponse（exclude_unset）
{
  "hook": { "sentences": [{ "idx": 0, "text": "..." }] },  // dict|null
  "body": { "sentences": [...] },
  "cta":  { "sentences": [...] },
  "cited_content_ids": [7, 8],                    // list[int]，整篇内容参考（新）
  "cited_card_ids": [1, 2],                       // list[int]，旧 B 卡引用，兼容周期内保留
  "blocked": false
}
// blocked 时：{ "blocked": true }
```

Java `ScriptGenResult(blocked, hook, body, cta, citedCardIds, citedContentIds)`，`hook/body/cta` 用 `JsonNode` 承载后 `toString()` 写 JSONB 列。**Java 不在 AiClient 抛 blocked**——`ScriptService.generate` 先置 failed + 退款再抛 `CONTENT_BLOCKED`（§4.1 refund 必须在任何异常抛出前完成）。

#### D18–D21：AI 仍是**无状态单平台生成**

一轮生成包含抖音版与视频号版两个独立平台版本（spec `2026-08-15-kb-content-library-design` D21），但**这件事完全在 Java 侧编排**：Java 对同一 `generation_group_id` 各调本端点一次，`platform` 不同。Python 这边不变的部分比变的多，写清以免两侧各自加逻辑：

- **Python 不知道「组」的存在**：`generation_group_id` 不做去重、不缓存、不影响输出。两个版本各自独立生成，正文不是「同一篇换皮」。Java 用它把两次调用绑成一轮。
- **`framework` 进入写稿 prompt**：拆解页「用这个框架仿写」带入的爆款框架；为 `null` 时用默认口播结构。
- **`cited_content_ids` 非空时跳过检索**：懒生成视频号版时 Java 传入首版引用快照，Python 按 id + `user_id` 加载整篇，保证同组两个版本参考同一批内容。
- **计费与幂等全在 Java**：一轮扣 1 条，幂等键是 `generation_group_id`；视频号版懒生成（首次切页签才调）**不另扣费**。Python 不参与、也不需要知道扣没扣。
- **`platform` 是 `Literal["douyin", "channels"]`**：退役值（`kuaishou` / `xiaohongshu`）直接 422，脏平台进不了 prompt。Java 侧在扣额度前已经拒过一次（4005），这里是第二道。**注意与 `app.datasource` 那套 `douyin` / `wechat_channels` 不是同一命名空间**——那是抓数平台标识，别互相赋值。
- `framework` 是拆解页「用这个框架仿写」带入的爆款框架；为 `null` 时用默认口播结构。
- `profile` 用定位档案的**七个规范键**（`persona` / `targetAudience` / `differentiation` / `conversionPath` / `tone` / `redlines` / `contentPillars`，见 sks-server `docs/REST_CONTRACT.md`）。历史上访谈 summarize 产出的是中文键，映射到这七个键的责任在 Java 侧写档案时完成。
- **进 prompt 前只投影这七个键**（`app/skills/profile_fields.py::render_profile`，`script_gen` 与 `rewrite_sentence` 共用同一函数）：旧中文键映射过来，其余键（FAQ、`_interview_turns`、任何将来多塞的字段）**丢弃**而非透传。整块 dump 档案等于让「档案里多一个字段」悄悄改掉写稿风格，且没有一处会报错。改写与生成必须用同一投影，否则用户点一次「换个说法」就会拿到风格突变的句子。
- **FAQ 不进 `script_gen`**：高频问答是选题来源（用户点「生成选题」建 `source=faq` 的选题），不是写稿素材；直接注入会让每篇稿子都莫名带上问答腔。
- `cited_content_ids` 取代 `cited_card_ids`：检索粒度是**整篇内容**（`kb_content`，不切片），Java 据此写 `content_reference` 并在右栏渲染「本稿参考了你的这些内容」。检索命中同一 `generation_group_id` 的两个平台版本时**最多取一个**（优先与当前 `platform` 一致者），避免近重复内容占满 top-k。
- 知识库为空 / 无命中时照常生成（`cited_content_ids` 为空数组），**不报错**——前端如实显示「本稿只基于你的定位档案」。
- 检索从 `kb_content` 整篇召回 2–3 篇：overfetch 后按 `generation_group_id` 去重，优先当前 `platform`，接近时爆款优先。SQL 必须带 `user_id`。

### POST /ai/rewrite_sentence

```jsonc
// 入参 RewriteSentenceRequest
{ "sentence": "string", "section": "string", "full_script": {}, "profile": {} }
// 出参 RewriteSentenceResponse（exclude_unset）
{ "text": "新句", "blocked": false }
```

与 script_gen 不同：Java **在 `AiClient.rewriteSentence` 内**对 `blocked=true` 直接抛 `CONTENT_BLOCKED`——单句重写不扣额度、无需 refund 编排。

### POST /ai/card_gen

```jsonc
// 入参 CardGenRequest
{ "user_id": 1, "raw_text": "string", "target_layer": "B" }
// 出参 CardGenResponse（exclude_unset）
{
  "cards": [ { "card_type": "string", "title": "string", "content": {} } ],
  "gaps": ["string"],
  "conflicts": [ { "card_id": 1, "card_index": 0, "reason": "string" } ],
  "blocked": false
}
```

`card_index` 指向本次返回的 `cards` 数组下标，让 Java confirm 流程把「新卡」映射到要覆盖的「现有卡 `card_id`」。冲突检测读 `kb_card`（见 §5）。

### POST /ai/interview/step

```jsonc
// 入参 InterviewStepRequest
{ "user_id": 1, "session_id": "string", "user_reply": null, "materials": null }
// 出参 InterviewStepResponse（exclude_unset）
{ "stage": "string", "question": "string", "profile_draft": {}, "done": false, "blocked": false }
```

首轮带 `materials` + `user_reply=null`；后续轮反之。LangGraph checkpoint 的 `thread_id` 由 Python 内部构造为 `f"{user_id}:{session_id}"`。

`stage=summarize`（`done=true`）时 `profile_draft` 是 summarize 的原样产出，形状见下节。Java 把它当 `JsonNode` 整块透传给前端（`ProfileController.InterviewStepView.profileDraft`），**不重排、不裁剪**——前端据此渲染七字段草稿与 FAQ 候选勾选框。

#### summarize 产出形状（D19/D20）

```jsonc
{
  "profile": {
    "persona": "string",          // 人设
    "targetAudience": "string",   // 目标人群
    "differentiation": "string",  // 差异化
    "conversionPath": "string",   // 转化路径
    "tone": "string",             // 口吻
    "redlines": ["string"],       // 红线（清单，可空数组）
    "contentPillars": ["string"]  // 内容支柱（清单，可空数组）
  },
  "faq_candidates": [             // 高频问答候选，最多 5 条；可空数组
    { "question": "string", "answer": "string" }   // answer 可缺省
  ]
}
```

- **七个规范键是跨仓共享的键名**（与 sks-server `ProfileFields` / `docs/REST_CONTRACT.md` 同名同序）。历史上 summarize 产的是中文键（`人设`/`人群`/…）且带 `a_cards`，**那批 checkpoint 仍在库里**：读侧兼容映射在 Java `com.sks.profile.ProfileContent`（写档案时）与本仓 `app/skills/profile_fields.py`（进 prompt 时），**产出侧只写规范键**。
- **形状是固定的：七字段与 `faq_candidates` 全部 `required`**。「可以为空」由**空数组**表达（`redlines: []`、`faq_candidates: []`），不是把键变成可省略——一份缺键的响应若也算合法，Java 侧只会安静地少存字段，事后分不清是「用户没有红线」还是「模型忘了输出」。
- **`faq_candidates` 只回给用户勾选，AI 不写共享库**：prompt 要求「只提取用户真的说过的问题，不要按行业常识编造」。落库发生在用户确认之后——前端把勾中的几条放进 `POST /api/profile/confirm` 的 `faqs`，由 Java 与档案同事务写 `profile_faq`。
- **`a_cards` 已从 schema 移除**（A/B/C 卡片概念退场，D1/D5/D19）：`confirm` 不再建 A 卡。

### GET /ai/interview/result

Query 参数 `thread_id`（必填）。Java 侧须自行拼 `"userId:sessionId"`，与上面的构造规则对齐。

```jsonc
// 出参 InterviewResultResponse（exclude_unset）
{ "profile": {}, "a_cards": [ {...} ], "found": true }
// 无 checkpoint 时：{ "found": false }
```

`profile` 是 summarize 产出**原样透传**（`fetch_result` 不做任何加工）：新 checkpoint 是上节的七字段，旧 checkpoint 是中文键 + `a_cards`。两种都可能被 `confirm` 读到，**规范化的责任在 Java 写档案时**（`ProfileContent.canonical`）——Python 这边有意不映射，免得两处规则各自演化。

> `a_cards` 是 **DEPRECATED 的读侧残留**：新 schema 不再产它，仅旧 checkpoint 里有；Java `confirm` 已不读（D19 起不建 A 卡）。字段保留只为不破坏既有反序列化，兼容周期后随 `kb_card` 一起去掉。

`found=false` 由 `ProfileService.confirm` 翻译为 `PARAM_INVALID`（4005）。

### POST /ai/interview/sample-opening

```jsonc
// 入参 SampleOpeningRequest
{ "user_id": 1, "thread_id": "1:sess", "topic": null }
// topic 省略时默认「报价为什么差一倍」。Java 侧须自行拼 thread_id = "userId:sessionId"。

// 出参 SampleOpeningResponse（exclude_unset）
{ "found": true, "topic": "报价为什么差一倍", "without": "…", "with": "…" }
// 无 checkpoint / 无 profile：{ "found": false }
```

### POST /ai/asr

`multipart/form-data`，字段名 **`audio`**（Java 侧用 `ByteArrayResource` 覆盖 `getFilename()` 返回 `audio.wav`）。

出参 `{ "text": "转写文本" }`。专有错误：

| 情况 | 状态码 | body |
|---|---|---|
| 空音频 | 400 | `{"detail":{"error":"EMPTY_AUDIO"}}` |
| > 60 MB | 413 | `{"detail":{"error":"AUDIO_TOO_LARGE"}}` |
| `ALIYUN_ASR_KEY` 未配置 | 503 | `{"detail":{"error":"ASR_NOT_CONFIGURED"}}` |
| 阿里云识别失败 | 502 | `{"detail":{"error":"ASR_FAILED"}}` |
| 转写命中内容安全（用户录音必须过审） | 422 | `{"detail":{"error":"CONTENT_BLOCKED"}}` |

502/503/其他非 2xx 被 Java 翻译为 `AI_FAILED`（提示改用文字）；422 `CONTENT_BLOCKED` → `CONTENT_BLOCKED`。**不阻断访谈**。解析账号/解析视频**不过**内容安全。

### POST /ai/analyze/precheck

```jsonc
{ "url": "string" }              // 入参 PrecheckRequest
{ "reachable": true, "video_count": 12 }   // 出参 PrecheckResponse
```

`video_count` 是 TikHub 首页**估算**（≤20），非精确总数——拆账号扣费公式 `max(1, min(10, floor(N/2)))` 依赖此估算，是设计 §4.3 接受的契约。

入参 url 由 sks-server 归一化后传入，通常已是有效 URL；_platform_of 是平台判定的最终权威。

上游 `DataSourceError` → **502** `{"detail":{"error":"PRECHECK_FAILED","message":"..."}}`（message 截断 200 字符）。

### GET /ai/hot_board

无入参。出参是**数组**（不是对象）：

```jsonc
[ { "title": "string", "hot_index": 100, "video_count": 20 } ]
```

上游 `DataSourceError` → **502** `{"detail":{"error":"HOT_BOARD_FAILED","message":"..."}}`。

Java `HotItem` 把 `hot_index`/`video_count` 声明为可空 `Integer`，Python 侧则是必填 `int`。目前 Java 只用 `title` 打分入库，两字段留给后续排序/配额。

### GET /ai/analyze/video/metrics

Query `url`（视频分享链）。

入参 url 由 sks-server 归一化后传入，通常已是有效 URL；_platform_of 是平台判定的最终权威。

```jsonc
{ "found": true, "play_count": 1234, "like_count": 56, "comment_count": 7, "share_count": 8, "collect_count": 9 }
// play_count: int | null（抖音真值含 0；视频号 read_count<=0 视为不可用 → null）
// 非视频/不可达/未知平台：{ "found": false, "play_count": null, ... }
```

抖音走 `video_meta`；视频号走 `channels_video_metrics`（detail 解析）；未知平台 → `found=false`。上游 `DataSourceError` → **502** `{"detail":{"error":"VIDEO_METRICS_FAILED","message":"..."}}`（message 截断 200 字符）。

### POST /ai/analyze/video/text（同步）

```jsonc
{ "task_id": 1, "transcript": "string" }   // 入参 VideoTextRequest
```

> **本端点是唯一没有声明 `response_model` 的**——出参是 `structure_video()` 的原始 dict。形状由 `app/skills/video_analyze/graph.py` 的 `VIDEO_STRUCTURE_SCHEMA` 决定，改那里等于改契约。

```jsonc
// 成功
{ "structure": "string", "why_hot": "string", "framework": "string", "diff_hint": "string" }
// 命中安全
{ "blocked": true }
```

四个字段在 schema 里都是 `required`。成功时 Python 会自行写 `analyze_task(status='done', progress=100, result)`，Java 仍按 §4.1 占位模式幂等 backfill（防 Python 写后 Java 读 HTTP 失败的中间态）。

### POST /ai/analyze/video/link（异步 202）

```jsonc
{ "task_id": 1, "url": "string" }   // 入参 VideoLinkRequest
{ "task_id": 1 }                    // 出参，HTTP 202
```

**202-before-background 不变量**：endpoint 在 `add_task` 之前先 `update_task(status='running', progress=0)`，保证 Java 轮询看到 `running` 而非 stale `queued`（§4.3 超时判定靠 `updated_at`）。

`BackgroundTasks` 是进程内执行，**Python 重启不续跑**——靠 Java 轮询的超时/停滞判定兜底退款。

`video/link` 终态 `analyze_task.result` 为**五字段**：`{structure, why_hot, framework, diff_hint, transcript}`——`transcript` 是 ASR 转写全文，结构化之后注入（**不在** LLM schema 里）。前端拆视频结果页据此展示文案全文。`video/text`（粘文案）result 仍是四字段：原文是用户输入，已在 `analyze_task.input` 与前端本地状态里。

### POST /ai/analyze/account（异步 202）

入参出参同 video/link（`AccountRequest {task_id, url}` → `{task_id}`，HTTP 202）。后台跑 TOP20 → 逐条 → 三层，进度按 `floor(done*100/total)` 直写 `analyze_task`，终态 `done`/`partial`/`failed`。

入参 url 由 sks-server 归一化后传入，通常已是有效 URL；_platform_of 是平台判定的最终权威。

### POST /ai/attribution/single

```jsonc
{ "script": "string", "play_count": 100, "baseline": 300.0 }   // 入参
{ "diagnosis": "string", "suggestions": ["string"], "blocked": false }   // 出参（exclude_unset）
```

`script` 是扁平化的稿件纯文本（hook/body/cta 句子拼接），`baseline` 是 Java 侧算好的近 30 天均值。归因 **FREE 不扣费**，且**不改复盘态**（no AI judges state）。

### POST /ai/attribution/weekly

```jsonc
// 入参 AttributionWeeklyRequest
{ "user_id": 1, "scripts": [ { "script": "", "play_count": 0, "review_state": "unknown", "baseline": null } ] }
// 出参 AttributionWeeklyResponse（exclude_unset）
{ "summary": "string", "wins": ["string"], "gaps": ["string"], "next_focus": "string", "blocked": false }
```

`scripts` 的元素类型是 `dict[str, Any]`（**不是** 严格模型），故 Java 透传的额外字段（`title` 等）会被放行；skill prompt 只取 `WeeklyScriptItem` 文档化的四个字段。

---

## 5. 共享表契约

下面三张表全部由 **sks-server 的 Flyway 建**：`kb_card` 与 `analyze_task` 来自 `V1__core_schema.sql`，
`kb_content` 来自 `V10__kb_content_library.sql`。sks-ai **不做迁移**——唯一例外是 LangGraph checkpointer
的私有表，由 `app/main.py::_init_checkpointer` 自行 `saver.setup()`。

### `kb_content` — sks-ai **只读**（D18–D21 新增，取代 `kb_card` 的检索角色）

由 sks-server 的 Flyway `V10__kb_content_library.sql` 建。**内容底仓**：一行 = 一篇内容（用户手建/粘贴的 Markdown，或点「采用当前平台版」后入库的平台生成稿）。

```sql
id                  BIGSERIAL PRIMARY KEY
user_id             BIGINT NOT NULL REFERENCES app_user(id)
title               VARCHAR(200) NOT NULL
body                TEXT NOT NULL             -- Markdown 正文（不是 JSONB）
source              VARCHAR(20) NOT NULL      -- manual/platform_generated（CHECK 钉死）
platform            VARCHAR(20)               -- douyin/channels；手建未登记时 NULL（CHECK 钉死）
generation_group_id BIGINT                    -- 同一轮生成的两个平台版本共享；手建为 NULL
script_id           BIGINT                    -- 平台生成稿的来源稿；手建为 NULL
embedding           vector(1024)              -- 整篇一个向量（不切片）；算失败留 NULL，暂不参与检索
deleted             BOOLEAN NOT NULL DEFAULT false
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
```

- **检索粒度是整篇**：口播稿几百字，一篇一个向量足够；引用按「篇」展示，用户看得懂。不做段落切片。
- **写全部在 sks-server**（保存 / 采用时调 `/ai/embed` 拿向量后自己落库）；sks-ai 只读，且**与 `kb_card` 同一条数据泄漏防线：SQL 必须带 `user_id = $1` 过滤，并且必须带 `deleted = false`**。
- 检索去重：同一 `generation_group_id` 的两个平台版本最多命中一个，优先与当前目标 `platform` 一致者。
- `embedding` 为 NULL 的行（回填的存量内容、算向量失败的内容）自然不会被向量检索命中——**不要**把 NULL 当错误处理。

### `kb_card` — sks-ai **只读**（**DEPRECATED**，保留一个兼容周期）

> A/B/C 三层卡片概念已退场（spec D1/D5）：新链路检索 `kb_content`。存量 B 卡**不迁移**（基本是测试数据）。
> 兼容周期内本表与 `app/rag/retrieve.py` 的 B 层召回保持可用，不再新增能力。

```sql
id          BIGSERIAL PRIMARY KEY
user_id     BIGINT NOT NULL REFERENCES app_user(id)
layer       CHAR(1) NOT NULL          -- A/B/C
card_type   VARCHAR(20) NOT NULL
title       VARCHAR(100) NOT NULL
content     JSONB NOT NULL
embedding   vector(1024)              -- A/C 层可为空，B 层必填
deleted     BOOLEAN NOT NULL DEFAULT false
created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
```

sks-ai 的两处读：`app/rag/retrieve.py`（B 层向量召回）、`app/skills/card_gen/graph.py`（冲突检测）。**写全部在 sks-server**（KB CRUD 时调 `/ai/embed` 拿向量后自己落库）。

> **数据泄漏防线**：sks-ai 读 `kb_card` 的 SQL **必须带 `user_id = $1` 过滤**，漏掉会导致用户 A 召回用户 B 的卡。`tests/test_retrieve.py` 用真实 pgvector 容器证明此不变量，CI 用 `SKS_REQUIRE_REAL_DB=1` 禁止该用例被 skip。

### `analyze_task` — sks-server INSERT，sks-ai UPDATE

```sql
id          BIGSERIAL PRIMARY KEY
user_id     BIGINT NOT NULL REFERENCES app_user(id)
task_type   VARCHAR(10) NOT NULL      -- account/video
status      VARCHAR(10) NOT NULL DEFAULT 'queued'  -- queued/running/partial/done/failed
progress    INT NOT NULL DEFAULT 0
charged     INT NOT NULL DEFAULT 0
input       JSONB
result      JSONB
error       VARCHAR(300)
updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
```

- **`progress` 语义钉死**：已完成条数 / 总条数 × 100（整数），**不是**阶段进度。按比例退款 `refundN = charged × (100 - progress) / 100` 的数学依赖此口径——改语义会直接改变退款金额。
- **每次 UPDATE 必须显式 `SET updated_at = now()`**：PG 无自动更新触发器，而 Java 的超时/停滞判定读 `updated_at`。`app/skills/analyze_store.py::update_task` 内部永远写这一列，长任务另有心跳单独刷它。
- `error` 由 Python 截断到 300 字符以匹配列宽。
- 行由 Java 创建（占位模式），Python 只 UPDATE，Java `AnalyzeTaskPoller` 轮询终态。

---

## 6. 改契约时的同步清单

改任一端点的入参/出参，须同步四处：

1. `app/api/*.py` 的 pydantic 模型（真相源）
2. 本文件对应小节
3. sks-server `AiClient` 的内部 record（注意 `@JsonProperty` 对齐 snake_case——RestClient 的 Jackson 默认不开 `SNAKE_CASE`）
4. sks-server 侧消费该结果的 service（blocked / 退款编排可能受影响）
