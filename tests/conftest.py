"""共享 pytest fixtures。"""

import pytest

from app.config import settings


@pytest.fixture
def token(monkeypatch):
    """设置 SERVICE_TOKEN=test-secret，返回该 token 供 /ai/* 端点鉴权。"""
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")
    return "test-secret"
