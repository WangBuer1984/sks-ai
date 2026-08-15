"""文案生成 LangGraph 测试：mock LLM / retrieve，绝不发真实网络/DB 请求。

覆盖：
- 正常生成 → 三段 + cited_card_ids
- 无 B 层卡命中时仍能生成（cited_card_ids 为空）
- rewrite_sentence 独立技能
- /ai/script_gen + /ai/rewrite_sentence 端点鉴权
创作链路不调阿里云内容安全。
"""

import pytest

from app.skills.script_gen.graph import generate_script
from app.skills.script_gen.rewrite import rewrite_sentence


async def _fake_chat_ok(*args, **kwargs):
    """返回合法三段结构（每段 sentences 数组），模拟 GLM 结构化输出。"""
    return {
        "hook": {"sentences": [{"idx": 0, "text": "你有没有想过为什么有人火了？"}]},
        "body": {"sentences": [{"idx": 0, "text": "第一，定位清晰。"}, {"idx": 1, "text": "第二，内容垂直。"}]},
        "cta": {"sentences": [{"idx": 0, "text": "关注我，下期见。"}]},
    }


class _FakeCard:
    """模拟 rag.retrieve.Card——只关心 id（用于 cited_card_ids 断言）。"""

    def __init__(self, id):
        self.id = id
        self.card_type = "topic"
        self.title = f"card-{id}"
        self.content = {"text": f"card content {id}"}


async def _fake_retrieve_two_cards(user_id, query, k=5, max_distance=0.25):
    return [_FakeCard(11), _FakeCard(22)]


async def _fake_retrieve_no_cards(user_id, query, k=5, max_distance=0.25):
    return []


@pytest.mark.asyncio
async def test_success_returns_three_sections_and_citations(monkeypatch):
    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_ok)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_b_cards", _fake_retrieve_two_cards)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert set(result) >= {"hook", "body", "cta", "cited_card_ids"}
    assert result["cited_card_ids"] == [11, 22]
    assert "blocked" not in result


@pytest.mark.asyncio
async def test_no_cards_retrieved_still_generates(monkeypatch):
    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_ok)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_b_cards", _fake_retrieve_no_cards)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert result["cited_card_ids"] == []
    assert "hook" in result and "body" in result and "cta" in result


@pytest.mark.asyncio
async def test_rewrite_sentence_returns_text(monkeypatch):
    async def _fake_chat(*args, **kwargs):
        return {"text": "换个说法的句子"}

    monkeypatch.setattr("app.skills.script_gen.rewrite.chat", _fake_chat)
    result = await rewrite_sentence(
        sentence="原句", section="body",
        full_script={"hook": {}, "body": {}, "cta": {}},
        profile={"tone": "幽默"},
    )
    assert result["text"] == "换个说法的句子"
    assert "blocked" not in result


def test_ai_script_gen_requires_token(token, monkeypatch):
    """无 X-Service-Token → 422（Header(...) 缺失）。"""
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake_generate(*args, **kwargs):
        return {"hook": {}, "body": {}, "cta": {}, "cited_card_ids": []}

    monkeypatch.setattr("app.api.script_gen.generate_script", _fake_generate)
    with TestClient(app) as c:
        r = c.post("/ai/script_gen", json={"user_id": 1, "topic": {"title": "x", "rationale": "y"}, "profile": {}, "platform": "douyin"})
    assert r.status_code == 422


def test_ai_script_gen_accepts_correct_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake_generate(*args, **kwargs):
        return {"hook": {}, "body": {}, "cta": {}, "cited_card_ids": [1, 2]}

    monkeypatch.setattr("app.api.script_gen.generate_script", _fake_generate)
    with TestClient(app) as c:
        r = c.post(
            "/ai/script_gen",
            json={"user_id": 1, "topic": {"title": "x", "rationale": "y"}, "profile": {}, "platform": "douyin"},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json()["cited_card_ids"] == [1, 2]


def test_ai_rewrite_sentence_requires_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake_rewrite(*args, **kwargs):
        return {"text": "ok"}

    monkeypatch.setattr("app.api.script_gen.rewrite_sentence", _fake_rewrite)
    with TestClient(app) as c:
        r = c.post("/ai/rewrite_sentence", json={"sentence": "x", "section": "body", "full_script": {}, "profile": {}})
    assert r.status_code == 422


def test_ai_rewrite_sentence_accepts_correct_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake_rewrite(*args, **kwargs):
        return {"text": "ok"}

    monkeypatch.setattr("app.api.script_gen.rewrite_sentence", _fake_rewrite)
    with TestClient(app) as c:
        r = c.post(
            "/ai/rewrite_sentence",
            json={"sentence": "x", "section": "body", "full_script": {}, "profile": {}},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json() == {"text": "ok"}
