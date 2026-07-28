# sks-ai

Python AI 服务（FastAPI + LangGraph + 智谱 GLM）。内网服务，不暴露公网。

## 本地跑

```bash
uv sync
source .env  # DATABASE_URL / ZHIPU_API_KEY / TIKHUB_API_KEY / SERVICE_TOKEN / ALIYUN_* / ALIYUN_ASR_*
uv run uvicorn app.main:app --reload --port 8000
```
`.env` 仅本地调试参考；运行时由 deploy 仓 compose `env_file` 注入。

## 镜像构建

```bash
docker build -t ghcr.io/wangbuer1984/sks-ai:dev .
```
Dockerfile `uv sync --no-dev --frozen` 严格按 uv.lock 装。CI（`.github/workflows/ci.yml`）在 git tag `v*` 时 build+push 到 GHCR。镜像只保证 `linux/amd64`。

## 健康检查

`GET /health` → `{"status":"UP"}`（覆盖 asyncpg 池懒重试；checkpointer 无懒重试，依赖 compose depends_on 保证 pg 先起）。
