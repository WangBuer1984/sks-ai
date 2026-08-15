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
async def test_double_encoded_section_is_normalized(monkeypatch):
    """GLM 偶尔把某段双重编码成 JSON 字符串——归一为 dict，不炸 ScriptGenResponse。

    回归线上 500：cta 返回 ``'{"sentences":[…]}'``（str）→ ScriptGenResponse(cta: dict) 校验失败。
    """
    async def _fake_chat_double(*args, **kwargs):
        return {
            "hook": {"sentences": [{"idx": 0, "text": "钩子句"}]},
            "body": '{"sentences": [{"idx": 0, "text": "正文句"}]}',  # 双重编码
            "cta": '{"sentences": [{"idx": 0, "text": "结尾句"}]}',  # 双重编码
        }

    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_double)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_b_cards", _fake_retrieve_no_cards)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert isinstance(result["hook"], dict)
    assert isinstance(result["body"], dict)
    assert isinstance(result["cta"], dict)
    assert result["body"]["sentences"][0]["text"] == "正文句"
    assert result["cta"]["sentences"][0]["text"] == "结尾句"


@pytest.mark.asyncio
async def test_malformed_section_string_degrades_to_empty(monkeypatch):
    """非 JSON 字符串段 → 空 dict，不阻断生成（不 500）。"""
    async def _fake_chat_bad(*args, **kwargs):
        return {
            "hook": "不是合法JSON",
            "body": {"sentences": [{"idx": 0, "text": "正常段"}]},
            "cta": "",
        }

    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_bad)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_b_cards", _fake_retrieve_no_cards)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert result["hook"] == {}, "非 JSON 字符串应降级为空 dict"
    assert result["cta"] == {}, "空字符串应降级为空 dict"
    assert result["body"]["sentences"][0]["text"] == "正常段"


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
