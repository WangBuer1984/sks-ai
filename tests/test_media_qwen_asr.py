"""qwen_asr.py 测试：mock DashScope ``MultiModalConversation.call``，绝不发真实请求。

覆盖（对应 task-4-brief + 预审补强 5 项）：
  1. 成功：200 带文本 → 返回文本；call kwargs 含 ``asr_options``（language:zh / enable_lid:True），
     且给定 title/author 时插入了 system 消息。
  2. 瞬态：``call`` 抛 ``RuntimeError`` 连续 3 次 → ``DataSourceError``（不返回 ""），call_count == 3。
  3. 空文本：200 但解析后为 "" → 立即 ``DataSourceError("qwen asr empty text")``，call_count == 1（不重试）。
  4. 缺 key：``ALIYUN_ASR_KEY=""`` → ``DataSourceError``，未调用 DashScope（call_count == 0）。
  5. 信号量懒单例：三个 getter 跨调用返回同一实例；初值 3/5/4。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.datasource import DataSourceError


def _fake_response_ok(text: str = "识别结果") -> SimpleNamespace:
    """构造 200 + output.choices[0].message.content=[{"text": text}] 响应。"""
    return SimpleNamespace(
        status_code=200,
        output=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message={"content": [{"text": text}]},
                )
            ],
            text=None,
        ),
        message="ok",
    )


def _fake_response_empty() -> SimpleNamespace:
    """构造 200 但解析后空文本（content 为空 list）。"""
    return SimpleNamespace(
        status_code=200,
        output=SimpleNamespace(
            choices=[
                SimpleNamespace(message={"content": []}),
            ],
            text=None,
        ),
        message="ok",
    )


# ---------------------------------------------------------------------------
# 1. 成功
# ---------------------------------------------------------------------------
async def test_recognize_wav_success(monkeypatch):
    from app.datasource.media import qwen_asr

    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "fake-key")

    captured: dict[str, Any] = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return _fake_response_ok("识别结果")

    monkeypatch.setattr(qwen_asr.MultiModalConversation, "call", fake_call)

    text = await qwen_asr.recognize_wav("/tmp/x.wav", title="题", author="作者")

    assert text == "识别结果"
    # asr_options 形态
    asr_options = captured.get("asr_options")
    assert asr_options == {"language": "zh", "enable_lid": True}
    # 给定 title/author 时应有 system 消息
    messages = captured.get("messages", [])
    roles = [m["role"] for m in messages]
    assert "system" in roles
    sys_msg = next(m for m in messages if m["role"] == "system")
    sys_text = "".join(
        item.get("text", "") if isinstance(item, dict) else str(item)
        for item in sys_msg["content"]
    )
    assert "题" in sys_text and "作者" in sys_text
    # file:// 用绝对路径
    from pathlib import Path

    user_content = messages[0]["content"]
    audio_url = user_content[0]["audio"]
    assert audio_url == f"file://{Path('/tmp/x.wav').resolve()}"


# ---------------------------------------------------------------------------
# 2. 瞬态重试 3 次后 DataSourceError
# ---------------------------------------------------------------------------
async def test_recognize_wav_transient_retries_then_raises(monkeypatch):
    from app.datasource.media import qwen_asr

    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "fake-key")
    monkeypatch.setattr(qwen_asr, "_RETRY_BACKOFFS", (0, 0))

    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        raise RuntimeError("DashScope ASR error: 500 boom")

    monkeypatch.setattr(qwen_asr.MultiModalConversation, "call", fake_call)

    with pytest.raises(DataSourceError):
        await qwen_asr.recognize_wav("/tmp/x.wav")

    assert call_count["n"] == 3


async def test_recognize_wav_retries_non_runtime_network_errors(monkeypatch):
    """dashscope/httpx 等非 RuntimeError 瞬态也须重试并最终 DataSourceError。"""
    from app.datasource.media import qwen_asr

    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "fake-key")
    monkeypatch.setattr(qwen_asr, "_RETRY_BACKOFFS", (0, 0))

    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        raise ConnectionError("connection reset")

    monkeypatch.setattr(qwen_asr.MultiModalConversation, "call", fake_call)

    with pytest.raises(DataSourceError, match="after 3 retries"):
        await qwen_asr.recognize_wav("/tmp/x.wav")

    assert call_count["n"] == 3


async def test_recognize_wav_permanent_400_not_retried(monkeypatch):
    """欠费/鉴权类 400 是永久错误：只调 1 次，不烧 3 次重试。

    线上实测（logs 15:16-16:49）：DashScope 欠费返回
    ``400 Access denied ... good standing``，旧实现无差别重试 3 次，每条视频白烧
    ~8-10s、10 条 ~100s，根因还被埋进第 3 条日志。
    """
    from app.datasource.media import qwen_asr

    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "fake-key")
    monkeypatch.setattr(qwen_asr, "_RETRY_BACKOFFS", (0, 0))

    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        return SimpleNamespace(
            status_code=400,
            output=None,
            message=(
                "Access denied, please make sure your account is in good standing."
            ),
        )

    monkeypatch.setattr(qwen_asr.MultiModalConversation, "call", fake_call)

    with pytest.raises(DataSourceError, match="permanent error"):
        await qwen_asr.recognize_wav("/tmp/x.wav")

    assert call_count["n"] == 1


async def test_recognize_wav_oversize_400_not_retried(monkeypatch):
    """体积超限 400 同样永久：同一个文件重投必然再失败。

    spike 实测（docs/spikes/douyin-audio-only-download.md §4）：359s 整段 wav 不切片
    直喂 → ``400 InternalError.Algo.InvalidParameter: Multimodal file size is too
    large``，旧标记表没覆盖，白烧 3 次共 45s。
    """
    from app.datasource.media import qwen_asr

    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "fake-key")
    monkeypatch.setattr(qwen_asr, "_RETRY_BACKOFFS", (0, 0))

    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        return SimpleNamespace(
            status_code=400,
            output=None,
            message=(
                "InternalError.Algo.InvalidParameter: Multimodal file size is too large"
            ),
        )

    monkeypatch.setattr(qwen_asr.MultiModalConversation, "call", fake_call)

    with pytest.raises(DataSourceError, match="permanent error"):
        await qwen_asr.recognize_wav("/tmp/x.wav")

    assert call_count["n"] == 1


async def test_recognize_wav_transient_400_still_retried(monkeypatch):
    """无永久标记的 400 仍按瞬态重试——DashScope 也会把上游抖动标成 400。"""
    from app.datasource.media import qwen_asr

    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "fake-key")
    monkeypatch.setattr(qwen_asr, "_RETRY_BACKOFFS", (0, 0))

    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        return SimpleNamespace(
            status_code=400, output=None, message="Request failed. Please retry."
        )

    monkeypatch.setattr(qwen_asr.MultiModalConversation, "call", fake_call)

    with pytest.raises(DataSourceError, match="after 3 retries"):
        await qwen_asr.recognize_wav("/tmp/x.wav")

    assert call_count["n"] == 3


# ---------------------------------------------------------------------------
# 3. 空文本：不重试，立即 DataSourceError("qwen asr empty text")
# ---------------------------------------------------------------------------
async def test_recognize_wav_empty_text_no_retry(monkeypatch):
    from app.datasource.media import qwen_asr

    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "fake-key")

    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        return _fake_response_empty()

    monkeypatch.setattr(qwen_asr.MultiModalConversation, "call", fake_call)

    with pytest.raises(DataSourceError, match="qwen asr empty text"):
        await qwen_asr.recognize_wav("/tmp/x.wav")

    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# 4. 缺 key：未调用 DashScope
# ---------------------------------------------------------------------------
async def test_recognize_wav_missing_key(monkeypatch):
    from app.datasource.media import qwen_asr

    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "")

    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        return _fake_response_ok()

    monkeypatch.setattr(qwen_asr.MultiModalConversation, "call", fake_call)

    with pytest.raises(DataSourceError):
        await qwen_asr.recognize_wav("/tmp/x.wav")

    assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# 5. 信号量懒单例
# ---------------------------------------------------------------------------
def test_semaphores_lazy_singleton():
    from app.datasource.media import semaphores

    asr1, asr2 = semaphores.get_asr_semaphore(), semaphores.get_asr_semaphore()
    dl1, dl2 = semaphores.get_download_semaphore(), semaphores.get_download_semaphore()
    cv1, cv2 = semaphores.get_convert_semaphore(), semaphores.get_convert_semaphore()
    dc1, dc2 = semaphores.get_decode_semaphore(), semaphores.get_decode_semaphore()

    assert asr1 is asr2
    assert dl1 is dl2
    assert cv1 is cv2
    assert dc1 is dc2
    # 不同信号量互不相同
    assert asr1 is not dl1
    assert asr1 is not cv1
    assert asr1 is not dc1

    # 初值（_value 是 asyncio.Semaphore 暴露的剩余配额，CPython 实现细节；
    # 此处断言以锁定期望值，若未来 CPython 改名则改为 identity-only 检查）。
    # asr=5：官方 qwen3-asr-flash 限流 RPM=100（无并发列），ASR 非瓶颈。
    # download=2：实测 CDN 聚合限速，并发越多越慢（见 docs/spikes/cdn-download-concurrency.md）。
    assert asr1._value == 5
    assert dl1._value == 2
    assert cv1._value == 4
    assert dc1._value == 2
