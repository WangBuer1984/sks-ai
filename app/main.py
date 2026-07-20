"""FastAPI 入口：随口说 Python AI 服务。

P0 仅提供 /health 健康检查端点；后续 task 在此挂载各 skill 路由。
"""

from fastapi import FastAPI

app = FastAPI(title="sks-ai", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "UP"}
