"""/ai/* 鉴权 + 端点挂载测试：用 httpx MockTransport mock 外部调用。"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")
    return "test-secret"


def _client(token_val):
    return TestClient(app, headers={"X-Service-Token": token_val})


def test_health_is_public():
    """CLAUDE.md: /health 是唯一免 token 端点。"""
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "UP"}


def test_ai_embed_requires_token(token):
    with TestClient(app) as c:  # 不带 token
        r = c.post("/ai/embed", json={"text": "x"})
    assert r.status_code == 422  # Header(...) 缺失 → 422


def test_ai_embed_rejects_wrong_token(token):
    with TestClient(app) as c:
        r = c.post("/ai/embed", json={"text": "x"}, headers={"X-Service-Token": "wrong"})
    assert r.status_code == 403


def test_ai_embed_accepts_correct_token(token, monkeypatch):
    async def fake_embed(text, *, client=None):
        return [0.0] * 1024

    monkeypatch.setattr("app.api.embed.embed", fake_embed)
    with TestClient(app) as c:
        r = c.post("/ai/embed", json={"text": "x"}, headers={"X-Service-Token": token})
    assert r.status_code == 200
    assert len(r.json()["embedding"]) == 1024


def test_ai_safety_check_accepts_correct_token(token, monkeypatch):
    async def fake_check(text, *, client=None):
        return True

    monkeypatch.setattr("app.api.safety.safety_check", fake_check)
    with TestClient(app) as c:
        r = c.post("/ai/safety/check", json={"text": "x"}, headers={"X-Service-Token": token})
    assert r.status_code == 200
    assert r.json()["safe"] is True


def test_ai_safety_check_rejects_wrong_token(token):
    with TestClient(app) as c:
        r = c.post("/ai/safety/check", json={"text": "x"}, headers={"X-Service-Token": "nope"})
    assert r.status_code == 403
