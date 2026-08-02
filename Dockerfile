FROM python:3.12-slim

# 长转写：ffmpeg/ffprobe（Qwen 管线 + 短 ASR pydub webm→pcm）
# 视频号：nodejs（WASM decrypt_cli）
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv 单二进制
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --no-dev --frozen

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
