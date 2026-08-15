"""Qwen3-ASR-Flash（DashScope 云端 ASR）调用层。

**与 clever-hans 的关键分歧（无静默空串降级）**：clever-hans 在失败 / 空文本时
返回 ``""``，让上游静默吞掉。本仓库改为统一抛 ``DataSourceError``：
  - 永久（欠费 / 鉴权 / 模型不存在，见 ``PermanentAsrError``）：**不重试**，立即抛。
  - 瞬态（其余非 200 / 网络 / ``RuntimeError``）：最多 3 次尝试，耗尽 → ``DataSourceError``。
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
import time
from pathlib import Path

import dashscope
from dashscope import MultiModalConversation

from app.config import settings
from app.datasource import DataSourceError

log = logging.getLogger(__name__)

MODEL_NAME = "qwen3-asr-flash"
# 瞬态重试上限（共 3 次尝试）。空文本不在此计数——空被视为不可重试的数据源故障。
_MAX_ATTEMPTS = 3
# 重试间隔（秒）：attempt 失败后、下一次前 sleep，避免限流连打。
_RETRY_BACKOFFS = (0.5, 1.5)

# 永久错误标记（小写子串匹配）：账号欠费/鉴权失败/模型不存在——重试只是等量放大耗时，
# 且把根因埋进第 3 条日志。线上实测：欠费时每条视频白烧 ~8-10s×3 次，10 条 ~100s。
# 与 tikhub ``_get_json`` 同口径：4xx 默认不可重试，只在确认是瞬态时才重试；此处反过来
# ——默认重试（保住 DashScope 偶发瞬态 400），只在命中确定性标记时才放弃。
_PERMANENT_MARKERS = (
    "access denied",
    "overdue",
    "arrear",
    "invalid api",
    "incorrect api",
    "unauthorized",
    "permission denied",
    "model not exist",
    "model not found",
    # 请求本身超限——同一个文件重投必然再失败。实测 spike：359s 整段 wav 不切片直喂
    # → 400 ``Multimodal file size is too large``，被当瞬态重试 3 次白烧 45s。
    # 生产链路有 slice_audio 兜着，但切片阈值调错/上游改限额时会直接撞上。
    "file size is too large",
    "file is too large",
)
# 天然永久的状态码：鉴权/授权失败重试无意义。400 需配合上面的标记判断（DashScope
# 把瞬态上游故障也标 400）。
_PERMANENT_STATUS = (401, 403)


class PermanentAsrError(RuntimeError):
    """不可重试的 DashScope 错误（欠费/鉴权/模型不存在）。

    ``recognize_wav`` 见此异常立即翻译为 ``DataSourceError``，不走重试循环。
    """


def _is_permanent(status_code: object, message: str) -> bool:
    """状态码 + 错误文案 → 是否永久错误（不可重试）。"""
    if status_code in _PERMANENT_STATUS:
        return True
    low = (message or "").lower()
    return any(m in low for m in _PERMANENT_MARKERS)


def _build_messages(wav_path_abs: str, title: str | None, author: str | None) -> list[dict]:
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
    wav_path_abs: str, title: str | None, author: str | None
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
        detail = f"DashScope ASR error: {response.status_code} {error_msg}"
        if _is_permanent(response.status_code, str(error_msg)):
            raise PermanentAsrError(detail)
        raise RuntimeError(detail)


async def recognize_wav(
    wav_path: Path | str,
    *,
    title: str | None = None,
    author: str | None = None,
) -> str:
    """调用 DashScope qwen3-asr-flash 识别 wav，返回文本字符串。

    失败语义（与 clever-hans 分歧——无空串降级）：
      - ``ALIYUN_ASR_KEY`` 未配置 → 立即 ``DataSourceError``（防御性二次校验）。
      - 永久（``PermanentAsrError``：欠费/鉴权/模型不存在）：**不重试**，立即
        ``DataSourceError("qwen asr permanent error: …")``。
      - 瞬态（其余非 200 / 网络等）：最多 3 次尝试，耗尽 → ``DataSourceError``。
        捕获宽 ``Exception``（含 dashscope/httpx 非 RuntimeError）；``BaseException`` 不重试。
      - 200 但空文本：**不重试**，立即 ``DataSourceError("qwen asr empty text")``。

    本函数 **不** acquire ``asr_sem``——速率限制由 facade（Task 5）独占持有。
    """
    # 防御性 key 校验（facade Task 5 也会校验；此处为 defense-in-depth）。
    if not settings.ALIYUN_ASR_KEY:
        raise DataSourceError("ALIYUN_ASR_KEY not configured")

    # file:// 必须用绝对路径——相对路径会被 DashScope 静默误读。
    wav_path_abs = str(Path(wav_path).resolve())

    log.info("recognize_wav start: wav=%s", Path(wav_path).name)
    t0 = time.monotonic()
    text: str | None = None
    last_exc: BaseException | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            text = await asyncio.to_thread(
                _call_dashscope_sync, wav_path_abs, title, author
            )
            # 成功（含空串）即跳出重试循环；空判定在循环之外执行。
            break
        except PermanentAsrError as exc:
            # 欠费/鉴权/模型不存在：重试不会变好，立即失败并把根因放在首条日志。
            log.warning("Qwen ASR permanent error, not retrying: %s", exc)
            raise DataSourceError(f"qwen asr permanent error: {exc}") from exc
        except Exception as exc:
            last_exc = exc
            log.warning("Qwen ASR attempt %d/%d failed: %s", attempt + 1, _MAX_ATTEMPTS, exc)
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_BACKOFFS[attempt])
            continue
    else:
        # 全部 3 次均抛（瞬态耗尽）→ 统一 DataSourceError，禁止裸异常冒泡。
        raise DataSourceError(f"qwen asr failed after 3 retries: {last_exc}") from last_exc

    # 空文本检查——位于重试循环之外，不由异常触发，故空 200 仅 1 次调用且不重试。
    if not text or not text.strip():
        raise DataSourceError("qwen asr empty text")

    log.info(
        "recognize_wav done: wav=%s text_len=%d elapsed=%.2fs",
        Path(wav_path).name, len(text), time.monotonic() - t0,
    )
    return text
