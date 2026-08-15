"""阿里云内容安全 check() 测试：mock AcsClient，绝不发真实网络请求。"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.safety import content_safety
from app.safety.content_safety import _chunks, check


def _patch_ak(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_ID", "ak")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_SECRET", "sk")


def _safe_body() -> dict:
    return {"Code": 200, "Data": {"reason": "", "descriptions": "", "labels": ""}}


def _hit_body() -> dict:
    return {"Code": 200, "Data": {"reason": "涉政", "labels": "political"}}


@pytest.mark.asyncio
async def test_check_returns_true_for_clean(monkeypatch):
    _patch_ak(monkeypatch)
    monkeypatch.setattr(content_safety, "_moderate_sync", lambda text: _safe_body())
    assert await check("正常文本") is True


@pytest.mark.asyncio
async def test_check_returns_false_for_blocked(monkeypatch):
    _patch_ak(monkeypatch)
    monkeypatch.setattr(content_safety, "_moderate_sync", lambda text: _hit_body())
    assert await check("违禁文本") is False


@pytest.mark.asyncio
async def test_check_returns_false_on_api_error_code(monkeypatch):
    _patch_ak(monkeypatch)
    monkeypatch.setattr(
        content_safety, "_moderate_sync",
        lambda text: {"Code": 500, "Message": "Internal"},
    )
    assert await check("x") is False


@pytest.mark.asyncio
async def test_check_empty_text_is_safe_without_api(monkeypatch):
    # 无 AK 也不应 fail-closed 拦空串（避免 blank content 400）。
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_ID", "")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_SECRET", "")
    assert await check("   ") is True


@pytest.mark.asyncio
async def test_check_chunks_long_text_and_all_must_pass(monkeypatch):
    """>600 字须分片；任一片命中 → False；片长均 ≤600。"""
    _patch_ak(monkeypatch)
    seen: list[str] = []

    def _mod(text: str) -> dict:
        seen.append(text)
        assert len(text) <= content_safety._MAX_CONTENT_CHARS
        # 第二片命中
        if len(seen) == 2:
            return _hit_body()
        return _safe_body()

    monkeypatch.setattr(content_safety, "_moderate_sync", _mod)
    long = "甲" * 600 + "乙" * 50  # 650 字 → 2 片
    assert await check(long) is False
    assert len(seen) == 2
    assert seen[0] == "甲" * 600
    assert seen[1] == "乙" * 50


@pytest.mark.asyncio
async def test_check_chunks_long_text_all_safe(monkeypatch):
    _patch_ak(monkeypatch)
    seen: list[str] = []

    def _mod(text: str) -> dict:
        seen.append(text)
        return _safe_body()

    monkeypatch.setattr(content_safety, "_moderate_sync", _mod)
    long = "字" * 1201  # → 3 片
    assert await check(long) is True
    assert len(seen) == 3
    assert all(len(p) <= 600 for p in seen)


@pytest.mark.asyncio
async def test_check_sends_content_via_service_parameters(monkeypatch):
    """回归：ServiceParameters JSON 携带 content（非旧版 tasks/scenes）。"""
    _patch_ak(monkeypatch)
    captured: dict = {}

    class _FakeAcs:
        def __init__(self, *a, **k):
            pass

        def do_action_with_exception(self, req):
            captured["service"] = req.get_query_params().get("Service")
            captured["params"] = req.get_query_params().get("ServiceParameters")
            return json.dumps(_safe_body()).encode()

    monkeypatch.setattr(content_safety, "AcsClient", _FakeAcs)
    assert await check("hello world") is True
    assert captured["service"] == "comment_detection"
    assert json.loads(captured["params"]) == {"content": "hello world"}


@pytest.mark.asyncio
async def test_check_exception_fail_closed(monkeypatch):
    _patch_ak(monkeypatch)

    def _boom(_text):
        raise RuntimeError("network down")

    monkeypatch.setattr(content_safety, "_moderate_sync", _boom)
    assert await check("x") is False


def test_chunks_helpers():
    assert _chunks("") == []
    assert _chunks("  ") == []
    assert _chunks("abc") == ["abc"]
    assert _chunks("a" * 601) == ["a" * 600, "a"]
