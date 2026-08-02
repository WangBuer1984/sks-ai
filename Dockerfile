FROM python:3.12-slim

# 长转写：ffmpeg/ffprobe（Qwen 管线 + 短 ASR pydub webm→pcm）
# 视频号：nodejs（WASM decrypt_cli）
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && command -v ffmpeg \
    && command -v ffprobe \
    && (command -v node || command -v nodejs)

# uv 单二进制
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# 依赖层：仅锁文件 → 代码改动不使依赖层失效
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

COPY app ./app
RUN uv sync --no-dev --frozen

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
