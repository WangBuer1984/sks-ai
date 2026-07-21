"""补卡 card_gen skill 测试：mock LLM / safety / DB，绝不发真实网络/DB 请求。

覆盖：
- UGC 安全：raw_text 命中 → {blocked:true}，不调 chat 抽卡
- 正常抽取：返回 cards + gaps（缺分类）+ conflicts（空）
- 冲突检测：同 card_type + 标题重叠 → conflicts 含 card_id + card_index
- 无冲突：同 card_type 但标题不同 → conflicts 为空
- /ai/card_gen 端点鉴权 + blocked 响应

语义选择（已文档化于 graph.py docstring）：
- gap = 完整分类表减去本次抽取到的 card_type（本段 raw_text 覆盖度，非用户整库缺口）——
  无需 DB、最简可测。
- conflict = 与用户「现有卡」同 card_type 且标题重叠（大小写不敏感子串），返回
  {card_id, card_index, reason}——跨层统一（A/C 层无 embedding，标题重叠是唯一统一口径）。
"""

import pytest

from app.skills.card_gen.graph import generate_cards


# ---- fakes -----------------------------------------------------------------

async def _unsafe(t):
    return False  # check 是 async def，桩须为协程


async def _safe(t):
    return True


async def _fake_chat_two_cards(*args, **kwargs):
    """返回两张卡（产品 + 受众），缺风格/场景/卖点（B 层完整集 5 类）。"""
    return {
        "cards": [
            {"card_type": "产品", "title": "美白精华", "content": {"price": "99"}},
            {"card_type": "受众", "title": "25-35岁女性", "content": {"age": "25-35"}},
        ]
    }


async def _fake_chat_one_card(*args, **kwargs):
    return {
        "cards": [
            {"card_type": "产品", "title": "老款精华", "content": {"price": "99"}},
        ]
    }


async def _fake_existing_cards_conflict(user_id, layer):
    """现有卡：一张同 card_type + 标题重叠（与 _fake_chat_one_card 的「老款精华」重叠）。"""
    return [
        {"id": 42, "card_type": "产品", "title": "老款精华液"},
    ]


async def _fake_existing_cards_none(user_id, layer):
    return []


async def _fake_existing_cards_diff_title(user_id, layer):
    """同 card_type 但标题完全不重叠。"""
    return [{"id": 7, "card_type": "产品", "title": "完全不同的另一款"}]


# ---- UGC 安全 ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_unsafe_raw_text_returns_blocked_and_skips_extraction(monkeypatch):
    """raw_text 命中安全 → {blocked:True}，且不调 chat（chat 未被调）。"""
    called = {"n": 0}

    async def _chat(*args, **kwargs):
        called["n"] += 1
        return {"cards": []}

    monkeypatch.setattr("app.skills.card_gen.graph.check", _unsafe)
    monkeypatch.setattr("app.skills.card_gen.graph.chat", _chat)
    monkeypatch.setattr("app.skills.card_gen.graph.fetch_existing_cards", _fake_existing_cards_none)
    result = await generate_cards(user_id=1, raw_text="违规内容", target_layer="B")
    assert result == {"blocked": True}
    assert called["n"] == 0  # 安全未过 → 不抽卡


# ---- 正常抽取 + 缺口 --------------------------------------------------------

@pytest.mark.asyncio
async def test_success_returns_cards_and_gaps(monkeypatch):
    """抽到产品/受众两类 → gaps = B 层完整集 {产品,受众,风格,场景,卖点} - {产品,受众}。"""
    monkeypatch.setattr("app.skills.card_gen.graph.check", _safe)
    monkeypatch.setattr("app.skills.card_gen.graph.chat", _fake_chat_two_cards)
    monkeypatch.setattr("app.skills.card_gen.graph.fetch_existing_cards", _fake_existing_cards_none)
    result = await generate_cards(user_id=1, raw_text="我的美白精华卖给25-35岁女性", target_layer="B")
    assert not result.get("blocked")  # 成功时不带 blocked 键
    assert len(result["cards"]) == 2
    # 缺风格/场景/卖点三类
    assert set(result["gaps"]) == {"风格", "场景", "卖点"}
    assert result["conflicts"] == []  # 无现有卡


# ---- 冲突检测 ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_conflict_detected_when_same_card_type_and_title_overlap(monkeypatch):
    """新卡「老款精华」与现有卡「老款精华液」同 card_type + 标题重叠 → 冲突。"""
    monkeypatch.setattr("app.skills.card_gen.graph.check", _safe)
    monkeypatch.setattr("app.skills.card_gen.graph.chat", _fake_chat_one_card)
    monkeypatch.setattr("app.skills.card_gen.graph.fetch_existing_cards", _fake_existing_cards_conflict)
    result = await generate_cards(user_id=1, raw_text="老款精华", target_layer="B")
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["card_id"] == 42
    assert conflict["card_index"] == 0  # 对应 cards[0]
    assert "产品" in conflict["reason"]


@pytest.mark.asyncio
async def test_no_conflict_when_title_does_not_overlap(monkeypatch):
    """同 card_type 但标题完全不重叠 → 无冲突。"""
    monkeypatch.setattr("app.skills.card_gen.graph.check", _safe)
    monkeypatch.setattr("app.skills.card_gen.graph.chat", _fake_chat_one_card)
    monkeypatch.setattr("app.skills.card_gen.graph.fetch_existing_cards", _fake_existing_cards_diff_title)
    result = await generate_cards(user_id=1, raw_text="老款精华", target_layer="B")
    assert result["conflicts"] == []


# ---- 端点鉴权 ---------------------------------------------------------------

def test_ai_card_gen_requires_token(token, monkeypatch):
    """无 X-Service-Token → 422（Header(...) 缺失）。"""
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake_generate(*args, **kwargs):
        return {"cards": [], "gaps": [], "conflicts": []}

    monkeypatch.setattr("app.api.card_gen.generate_cards", _fake_generate)
    with TestClient(app) as c:
        r = c.post("/ai/card_gen", json={"user_id": 1, "raw_text": "x", "target_layer": "B"})
    assert r.status_code == 422


def test_ai_card_gen_accepts_correct_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake_generate(*args, **kwargs):
        return {"cards": [{"card_type": "产品", "title": "t", "content": {}}], "gaps": ["风格"], "conflicts": []}

    monkeypatch.setattr("app.api.card_gen.generate_cards", _fake_generate)
    with TestClient(app) as c:
        r = c.post(
            "/ai/card_gen",
            json={"user_id": 1, "raw_text": "x", "target_layer": "B"},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["gaps"] == ["风格"]
    assert body["conflicts"] == []


def test_ai_card_gen_blocked_response(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake_generate(*args, **kwargs):
        return {"blocked": True}

    monkeypatch.setattr("app.api.card_gen.generate_cards", _fake_generate)
    with TestClient(app) as c:
        r = c.post(
            "/ai/card_gen",
            json={"user_id": 1, "raw_text": "违规", "target_layer": "B"},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json() == {"blocked": True}
