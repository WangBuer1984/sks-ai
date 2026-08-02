"""共享 pytest fixtures。"""

import pytest

from app.config import settings


@pytest.fixture
def token(monkeypatch):
    """设置 SERVICE_TOKEN=test-secret，返回该 token 供 /ai/* 端点鉴权。"""
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-secret")
    return "test-secret"


@pytest.fixture(autouse=True)
def _reset_download_shared_client():
    """download._shared_client 是进程级懒单例；测试间 reset 防跨 event loop stale client。

    httpx.AsyncClient 绑定创建它的 event loop。测试每个用例跑在独立 loop 上，
    若上一测遗留的 ``_shared_client`` 仍指向旧 loop，本测复用会撞
    "RuntimeError: ... attached to a different loop"。生产单 loop 不受影响。
    """
    from app.datasource.media import download

    download._shared_client = None
    download._shared_client_lock = None
    yield
    download._shared_client = None
    download._shared_client_lock = None
