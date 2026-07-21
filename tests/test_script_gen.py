"""文案生成 LangGraph 测试：mock LLM / safety / retrieve，绝不发真实网络/DB 请求。

覆盖 brief 两条核心用例（verbatim）+ 边界：
- 命中安全且重写仍命中 → blocked
- 正常生成 → 三段 + cited_card_ids
- 无 B 层卡命中时仍能生成（cited_card_ids 为空）
- rewrite_sentence 独立技能
- /ai/script_gen + /ai/rewrite_sentence 端点鉴权
"""

import pytest

from app.skills.script_gen.graph import generate_script
from app.skills.script_gen.rewrite import rewrite_sentence


# ---- brief verbatim fakes --------------------------------------------------

async def _unsafe(t):
    return False  # check 是 async def，桩必须也是协程，普通 lambda 会让 await 处 TypeError


async def _safe(t):
    return True


async def _fake_chat_ok(*args, **kwargs):
    """返回合法三段结构（每段 sentences 数组），模拟 GLM 结构化输出。"""
    return {
        "hook": {"sentences": [{"idx": 0, "text": "你有没有想过为什么有人火了？"}]},
        "body": {"sentences": [{"idx": 0, "text": "第一，定位清晰。"}, {"idx": 1, "text": "第二，内容垂直。"}]},
        "cta": {"sentences": [{"idx": 0, "text": "关注我，下期见。"}]},
    }


async def _fake_chat_returns_bad(*args, **kwargs):
    """返回包含违禁词的三段（check 已被 mock 为 unsafe，内容仅占位）。"""
    return {
        "hook": {"sentences": [{"idx": 0, "text": "违禁开场白"}]},
        "body": {"sentences": [{"idx": 0, "text": "违禁正文"}]},
        "cta": {"sentences": [{"idx": 0, "text": "违禁结尾"}]},
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


# ---- brief case 1: 命中安全且重写仍命中 → blocked ----------------------------

@pytest.mark.asyncio
async def test_blocked_content_returns_blocked_flag(monkeypatch):
    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_returns_bad)
    monkeypatch.setattr("app.skills.script_gen.graph.check", _unsafe)  # 一直命中
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                    profile={}, platform="douyin")
    assert result["blocked"] is True


# ---- brief case 2: 正常生成 → 三段 + cited_card_ids --------------------------

@pytest.mark.asyncio
async def test_success_returns_three_sections_and_citations(monkeypatch):
    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_ok)
    monkeypatch.setattr("app.skills.script_gen.graph.check", _safe)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_b_cards", _fake_retrieve_two_cards)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert set(result) >= {"hook", "body", "cta", "cited_card_ids"}
    assert result["cited_card_ids"] == [11, 22]


# ---- 边界: 无 B 层卡命中 → 仍生成，cited_card_ids 为空 -----------------------

@pytest.mark.asyncio
async def test_no_cards_retrieved_still_generates(monkeypatch):
    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_ok)
    monkeypatch.setattr("app.skills.script_gen.graph.check", _safe)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_b_cards", _fake_retrieve_no_cards)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert result["cited_card_ids"] == []
    assert "hook" in result and "body" in result and "cta" in result


# ---- 边界: 首次 unsafe → 重写后 safe → 返回重写后的稿 -----------------------

@pytest.mark.asyncio
async def test_first_unsafe_rewrite_then_safe_returns_script(monkeypatch):
    """安全首次命中 → 重写一次 → 安全通过 → 返回重写后的稿（非 blocked）。"""
    call_count = {"n": 0}

    async def _chat_then_clean(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 首次生成（含违禁词）
            return {
                "hook": {"sentences": [{"idx": 0, "text": "违禁"}]},
                "body": {"sentences": [{"idx": 0, "text": "违禁正文"}]},
                "cta": {"sentences": [{"idx": 0, "text": "违禁结尾"}]},
            }
        # 重写后干净
        return {
            "hook": {"sentences": [{"idx": 0, "text": "干净开场"}]},
            "body": {"sentences": [{"idx": 0, "text": "干净正文"}]},
            "cta": {"sentences": [{"idx": 0, "text": "干净结尾"}]},
        }

    check_count = {"n": 0}

    async def _check_first_unsafe_then_safe(text):
        check_count["n"] += 1
        return check_count["n"] > 1  # 首次 unsafe, 第二次 safe

    monkeypatch.setattr("app.skills.script_gen.graph.chat", _chat_then_clean)
    monkeypatch.setattr("app.skills.script_gen.graph.check", _check_first_unsafe_then_safe)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_b_cards", _fake_retrieve_two_cards)

    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert "blocked" not in result or result.get("blocked") is False
    assert result["hook"]["sentences"][0]["text"] == "干净开场"
    assert call_count["n"] == 2  # 生成 + 重写 = 2 次 chat
    assert check_count["n"] == 2  # 2 次 check


# ---- rewrite_sentence 独立技能 ---------------------------------------------

@pytest.mark.asyncio
async def test_rewrite_sentence_returns_text(monkeypatch):
    async def _fake_chat(*args, **kwargs):
        return {"text": "换个说法的句子"}

    monkeypatch.setattr("app.skills.script_gen.rewrite.chat", _fake_chat)
    monkeypatch.setattr("app.skills.script_gen.rewrite.check", _safe)
    result = await rewrite_sentence(
        sentence="原句", section="body",
        full_script={"hook": {}, "body": {}, "cta": {}},
        profile={"tone": "幽默"},
    )
    assert result["text"] == "换个说法的句子"


@pytest.mark.asyncio
async def test_rewrite_sentence_blocked_on_unsafe(monkeypatch):
    async def _fake_chat(*args, **kwargs):
        return {"text": "违禁重写"}

    monkeypatch.setattr("app.skills.script_gen.rewrite.chat", _fake_chat)
    monkeypatch.setattr("app.skills.script_gen.rewrite.check", _unsafe)
    result = await rewrite_sentence(
        sentence="原句", section="body",
        full_script={}, profile={},
    )
    assert result["blocked"] is True


# ---- 端点鉴权 ---------------------------------------------------------------

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


def test_ai_script_gen_blocked_response(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake_generate(*args, **kwargs):
        return {"blocked": True}

    monkeypatch.setattr("app.api.script_gen.generate_script", _fake_generate)
    with TestClient(app) as c:
        r = c.post(
            "/ai/script_gen",
            json={"user_id": 1, "topic": {"title": "x", "rationale": "y"}, "profile": {}, "platform": "douyin"},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json() == {"blocked": True}


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
