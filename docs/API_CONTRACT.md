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
| POST | `/ai/asr` | 需 | 短音频转文字（multipart） | `AiClient.asr` |
| POST | `/ai/analyze/precheck` | 需 | 拆账号预检（免费，不扣费） | `AiClient.precheck` |
| GET | `/ai/hot_board` | 需 | 平台热点榜 | `AiClient.hotBoard` |
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
  "platform": "douyin"                            // 默认 "douyin"
}
// 出参 ScriptGenResponse（exclude_unset）
{
  "hook": { "sentences": [{ "idx": 0, "text": "..." }] },  // dict|null
  "body": { "sentences": [...] },
  "cta":  { "sentences": [...] },
  "cited_card_ids": [1, 2],                       // list[int]
  "blocked": false
}
// blocked 时：{ "blocked": true }
```

Java `ScriptGenResult(blocked, hook, body, cta, citedCardIds)`，`hook/body/cta` 用 `JsonNode` 承载后 `toString()` 写 JSONB 列。**Java 不在 AiClient 抛 blocked**——`ScriptService.generate` 先置 failed + 退款再抛 `CONTENT_BLOCKED`（§4.1 refund 必须在任何异常抛出前完成）。

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

### GET /ai/interview/result

Query 参数 `thread_id`（必填）。Java 侧须自行拼 `"userId:sessionId"`，与上面的构造规则对齐。

```jsonc
// 出参 InterviewResultResponse（exclude_unset）
{ "profile": {}, "a_cards": [ {...} ], "found": true }
// 无 checkpoint 时：{ "found": false }
```

> **弱契约提醒**：Python 侧 `a_cards` 声明为 `list[dict[str, Any]]`，形状不受 pydantic 约束；Java 侧按 `CardGenCard {card_type, title, content}` 反序列化。两边靠约定而非类型对齐，改 `summarize` 产出形状时必须同步 Java。

`found=false` 由 `ProfileService.confirm` 翻译为 `PARAM_INVALID`（4005）。

### POST /ai/asr

`multipart/form-data`，字段名 **`audio`**（Java 侧用 `ByteArrayResource` 覆盖 `getFilename()` 返回 `audio.wav`）。

出参 `{ "text": "转写文本" }`。专有错误：

| 情况 | 状态码 | body |
|---|---|---|
| 空音频 | 400 | `{"detail":{"error":"EMPTY_AUDIO"}}` |
| > 60 MB | 413 | `{"detail":{"error":"AUDIO_TOO_LARGE"}}` |
| `ALIYUN_ASR_KEY` 未配置 | 503 | `{"detail":{"error":"ASR_NOT_CONFIGURED"}}` |
| 阿里云识别失败 | 502 | `{"detail":{"error":"ASR_FAILED"}}` |

全部被 Java 翻译为 `AI_FAILED`，前端提示改用文字输入，**不阻断访谈**。

### POST /ai/analyze/precheck

```jsonc
{ "url": "string" }              // 入参 PrecheckRequest
{ "reachable": true, "video_count": 12 }   // 出参 PrecheckResponse
```

`video_count` 是 TikHub 首页**估算**（≤20），非精确总数——拆账号扣费公式 `max(1, min(10, floor(N/2)))` 依赖此估算，是设计 §4.3 接受的契约。

上游 `DataSourceError` → **502** `{"detail":{"error":"PRECHECK_FAILED","message":"..."}}`（message 截断 200 字符）。

### GET /ai/hot_board

无入参。出参是**数组**（不是对象）：

```jsonc
[ { "title": "string", "hot_index": 100, "video_count": 20 } ]
```

上游 `DataSourceError` → **502** `{"detail":{"error":"HOT_BOARD_FAILED","message":"..."}}`。

Java `HotItem` 把 `hot_index`/`video_count` 声明为可空 `Integer`，Python 侧则是必填 `int`。目前 Java 只用 `title` 打分入库，两字段留给后续排序/配额。

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

### POST /ai/analyze/account（异步 202）

入参出参同 video/link（`AccountRequest {task_id, url}` → `{task_id}`，HTTP 202）。后台跑 TOP20 → 逐条 → 三层，进度按 `floor(done*100/total)` 直写 `analyze_task`，终态 `done`/`partial`/`failed`。

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

两张表由 **sks-server 的 Flyway 建**（`src/main/resources/db/migration/V1__core_schema.sql`）。sks-ai **不做迁移**——唯一例外是 LangGraph checkpointer 的私有表，由 `app/main.py::_init_checkpointer` 自行 `saver.setup()`。

### `kb_card` — sks-ai **只读**

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
