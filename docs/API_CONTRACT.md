# API_CONTRACT — sks-ai /ai/* 端点 + 共享表契约

sks-ai 是服务提供方，本文件为跨仓契约真相。sks-server 的 `AiClient` record 须与本文件字段一致。

## 鉴权
所有 /ai/* 请求须带 `X-Service-Token`（与 deploy 仓 .env `SERVICE_TOKEN` 一致）+ `X-Request-Id`（Java 生成）。无则 401。

## 端点

| 方法 | 路径 | 用途 | 入参 | 出参 |
|---|---|---|---|---|
| GET | /health | 健康检查 | - | `{"status":"UP"}` |
| POST | /ai/analyze/precheck | 拆视频/拆账号预检（不扣费） | ... | ... |
| ... | ... | ... | ... | ... |

> 实现期补全：端点入参/出参 pydantic model 字段逐个从 `app/api/*` 抽取填入；sks-server `AiClient` record 字段须与本表逐字对齐。

## 共享表（sks-server Flyway 建，本仓读写）

- `kb_card`（layer A/B/C + card_type + embedding vector(1024)）
- `analyze_task`（async 任务进度/结果，Python 直接写此表，Java @Scheduled 轮询）

字段契约见拆分 spec §3 数据模型 + tech-design §3。
