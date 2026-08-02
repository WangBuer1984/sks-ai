"""Qwen3-ASR-Flash（DashScope 云端 ASR）调用层。

**与 clever-hans 的关键分歧（无静默空串降级）**：clever-hans 在失败 / 空文本时
返回 ``""``，让上游静默吞掉。本仓库改为统一抛 ``DataSourceError``：
  - 瞬态（非 200 / 网络 / ``RuntimeError``）：最多 3 次尝试，耗尽 → ``DataSourceError``。
  - 200 但解析后空文本：**不重试**，立即 ``DataSourceError("qwen asr empty text")``。
空文本被视为数据源故障而非可降级空串，避免后续 pipeline 在无声文本上误判成功。

接口（对齐 brief）：``async def recognize_wav(wav_path, *, title=None, author=None) -> str``。
**速率限制（asr 信号量）由 facade（Task 5 ``transcribe.py``）独占持有**——本模块
**不**在内部 acquire 信号量，避免双重 acquire / 死锁。

调用形态对齐 clever-hans：
  - ``MultiModalConversation.call`` + ``asr_options={"language":"zh","enable_lid":True}``。
  - ``file://{abs_wav_path}``（``Path.resolve()`` 取绝对路径，相对路径会被 DashScope 静默误读）。
  - 可选 system 消息传 title/author 辅助专名识别（仅当值非空时插入对应行）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import dashscope
from dashscope import MultiModalConversation

from app.config import settings
from app.datasource import DataSourceError

log = logging.getLogger(__name__)

MODEL_NAME = "qwen3-asr-flash"
# 瞬态重试上限（共 3 次尝试）。空文本不在此计数——空被视为不可重试的数据源故障。
_MAX_ATTEMPTS = 3


def _build_messages(wav_path_abs: str, title: Optional[str], author: Optional[str]) -> list[dict]:
    """构建 DashScope messages：user 携带音频 file:// URL，system 可选携带标题/作者。

    system 消息仅当 title 或 author 非空时插入；空值对应的行被省略（不留静默空行）。
    """
    messages = [
        {
            "role": "user",
            "content": [{"audio": f"file://{wav_path_abs}"}],
        },
    ]

    system_parts: list[str] = []
    if title:
        system_parts.append(f"视频标题: {title}")
    if author:
        system_parts.append(f"作者: {author}")
    if system_parts:
        system_text = "这是一段短视频音频。\n" + "\n".join(system_parts)
        messages.append(
            {
                "role": "system",
                "content": [{"text": system_text}],
            }
        )
    return messages


def _call_dashscope_sync(
    wav_path_abs: str, title: Optional[str], author: Optional[str]
) -> str:
    """同步调用 DashScope API（由 ``recognize_wav`` 经 ``asyncio.to_thread`` 调用）。

    返回 200 时解析出的文本字符串（**可能为空 ``""``**——本函数不决定重试 vs 空，
    空判定由 async 层在重试循环之外执行）。非 200 抛 ``RuntimeError``（瞬态）。
    """
    dashscope.api_key = settings.ALIYUN_ASR_KEY
    messages = _build_messages(wav_path_abs, title, author)

    response = MultiModalConversation.call(
        model=MODEL_NAME,
        messages=messages,
        stream=False,
        incremental_output=False,
        result_format="message",
        asr_options={"language": "zh", "enable_lid": True},
    )

    if response.status_code == 200:
        # 解析逻辑对齐 clever-hans（qwen_asr_backend.py:96-118）：三级 fallback。
        output = response.output
        if hasattr(output, "choices") and output.choices:
            choice = output.choices[0]
            if hasattr(choice, "message") and choice.message:
                content = choice.message.get("content", [])
                for item in content:
                    if isinstance(item, dict) and item.get("text"):
                        return item["text"].strip()
                # fallback: content 可能是字符串列表
                text = "".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )
                return text.strip() if text else ""
        # fallback: 尝试直接取 output.text
        if hasattr(output, "text"):
            return output.text.strip() if output.text else ""
        return ""
    else:
        error_msg = response.message if hasattr(response, "message") else "unknown"
        raise RuntimeError(f"DashScope ASR error: {response.status_code} {error_msg}")


async def recognize_wav(
    wav_path: Path | str,
    *,
    title: Optional[str] = None,
    author: Optional[str] = None,
) -> str:
    """调用 DashScope qwen3-asr-flash 识别 wav，返回文本字符串。

    失败语义（与 clever-hans 分歧——无空串降级）：
      - ``ALIYUN_ASR_KEY`` 未配置 → 立即 ``DataSourceError``（防御性二次校验）。
      - 瞬态（非 200 / ``RuntimeError``）：最多 3 次尝试，耗尽 → ``DataSourceError``。
      - 200 但空文本：**不重试**，立即 ``DataSourceError("qwen asr empty text")``。

    本函数 **不** acquire ``asr_sem``——速率限制由 facade（Task 5）独占持有。
    """
    # 防御性 key 校验（facade Task 5 也会校验；此处为 defense-in-depth）。
    if not settings.ALIYUN_ASR_KEY:
        raise DataSourceError("ALIYUN_ASR_KEY not configured")

    # file:// 必须用绝对路径——相对路径会被 DashScope 静默误读。
    wav_path_abs = str(Path(wav_path).resolve())

    text: Optional[str] = None
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            text = await asyncio.to_thread(
                _call_dashscope_sync, wav_path_abs, title, author
            )
            # 成功（含空串）即跳出重试循环；空判定在循环之外执行。
            break
        except RuntimeError as exc:
            last_exc = exc
            log.warning("Qwen ASR attempt %d/%d failed: %s", attempt + 1, _MAX_ATTEMPTS, exc)
            continue
    else:
        # 全部 3 次均抛 RuntimeError（瞬态耗尽）。
        raise DataSourceError(f"qwen asr failed after 3 retries: {last_exc}")

    # 空文本检查——位于重试循环之外，不由异常触发，故空 200 仅 1 次调用且不重试。
    if not text or not text.strip():
        raise DataSourceError("qwen asr empty text")

    return text
