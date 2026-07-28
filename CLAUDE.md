# CLAUDE.md — sks-ai 仓

本仓为 Python AI 服务（FastAPI + LangGraph），不暴露公网，只接受带 `X-Service-Token` 的内网请求。

## 硬不变量（实现时不得违背）

- **无流式输出 + 先审后返**：所有展示给用户的 LLM 自然语言产出（稿件、卡片、访谈、拆解、归因）= 生成完整 → 内容安全审通过 → 一次性 JSON 返回。禁 SSE/打字机。
- **GLM 单厂商 + 型号只在 llm/ 配置层**：LLM 走智谱 GLM（OpenAI 兼容），向量用 embedding-3 固定 1024 维，绑 pgvector `vector(1024)` 列；业务代码不硬编码型号。创作类 GLM-4.7(thinking off)/轻量抽取 GLM-4.5-Air/深度归纳归因 GLM-4.7(thinking on)。
- **不做数据库迁移**（checkpointer 例外，sks-ai 自己 setup）；共享表 `kb_card`/`analyze_task` 由 sks-server Flyway 建，本仓只读写。
- **UGC 过内容安全审**。
- **Python 不暴露公网只信 X-Service-Token**；不做鉴权，信内网 + 共享 token。

## 本仓构建/测试命令

- `uv sync`（装依赖，含 dev）
- `uv run pytest tests/test_xxx.py -v`（单文件）
- `uv run uvicorn app.main:app --reload --port 8000`（本地跑）
- 镜像构建：`docker build -t sks-ai .`（Dockerfile `uv sync --no-dev --frozen`，严格按 uv.lock）

## 契约

- `/ai/*` HTTP 端点 + 共享表契约见 `docs/API_CONTRACT.md`（本仓是服务提供方，契约归本仓拥有）。
