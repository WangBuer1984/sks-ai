"""归因 skill 测试：mock LLM / safety，绝不发真实网络/DB 请求。

覆盖 brief 两条核心用例（verbatim）+ 安全/鉴权边界：
- 单条归因返回 {diagnosis, suggestions}
- 周卡返回 {summary, wins, gaps, next_focus}
- LLM 输出命中安全 → {blocked: true}（不返回 unsafe 文本）
- safety.check 在 LLM 产出上被调用（用户可见文本过审——硬不变量）
- skill="attribution"（→ glm-4.7 thinking on，深度归纳档）
- /ai/attribution/single + /ai/attribution/weekly 鉴权（缺/错 token）
- 无 DB：skill 不查库，scripts 由 Java 在请求体内传入
"""

import pytest

from app.skills.attribution.graph import attribution_single, attribution_weekly


# ---- fakes -----------------------------------------------------------------

async def _safe(_t):
    return True


async def _unsafe(_t):
    return False


async def _fake_chat_single(*args, **kwargs):
    """模拟 GLM 结构化输出——单条归因。"""
    return {
        "diagnosis": "钩子不够强，前 3 秒未留住观众；正文信息密度低。",
        "suggestions": [
            "开场用反问句或冲突数字钩住注意力",
            "正文压缩到 3 个核心论点",
            "结尾 CTA 更直接，给出明确动作",
        ],
    }


async def _fake_chat_weekly(*args, **kwargs):
    """模拟 GLM 结构化输出——周卡。"""
    return {
        "summary": "本周发布 5 条，1 条爆款（3 倍均值），2 条 flop，整体表现低于均值。",
        "wins": ["爆款采用强反问钩子，留存率高", "周三发布时段表现最佳"],
        "gaps": ["2 条 flop 开场冗长", "正文缺少数据支撑"],
        "next_focus": "下周重点打磨开场 3 秒，并增加案例数据。",
    }


# ---- skill=attribution（深度归纳档，thinking on 由 MODEL_FOR 保证）---------

@pytest.mark.asyncio
async def test_single_passes_skill_attribution_to_chat(monkeypatch):
    """chat 须以 skill='attribution' 调用（→ MODEL_FOR['attribution'] glm-4.7 thinking on）。"""
    seen = {}

    async def _spy_chat(skill, messages, json_schema=None):
        seen["skill"] = skill
        seen["json_schema"] = json_schema
        return await _fake_chat_single()

    monkeypatch.setattr("app.skills.attribution.graph.chat", _spy_chat)
    monkeypatch.setattr("app.skills.attribution.graph.check", _safe)
    await attribution_single(script="一段口播文案", play_count=1200, baseline=500.0)
    assert seen["skill"] == "attribution"
    assert seen["json_schema"] is not None  # 结构化输出


@pytest.mark.asyncio
async def test_weekly_passes_skill_attribution_to_chat(monkeypatch):
    seen = {}

    async def _spy_chat(skill, messages, json_schema=None):
        seen["skill"] = skill
        seen["json_schema"] = json_schema
        return await _fake_chat_weekly()

    monkeypatch.setattr("app.skills.attribution.graph.chat", _spy_chat)
    monkeypatch.setattr("app.skills.attribution.graph.check", _safe)
    await attribution_weekly(user_id=42, scripts=[])
    assert seen["skill"] == "attribution"
    assert seen["json_schema"] is not None


# ---- brief case 1: 单条归因返回 diagnosis + suggestions ----------------------

@pytest.mark.asyncio
async def test_single_returns_diagnosis_and_suggestions(monkeypatch):
    monkeypatch.setattr("app.skills.attribution.graph.chat", _fake_chat_single)
    monkeypatch.setattr("app.skills.attribution.graph.check", _safe)
    result = await attribution_single(script="一段口播文案", play_count=1200, baseline=500.0)
    assert "diagnosis" in result and "suggestions" in result
    assert isinstance(result["suggestions"], list) and len(result["suggestions"]) >= 1
    assert result["diagnosis"]
    assert result.get("blocked") is not True


# ---- brief case 2: 周卡返回四段结构 -----------------------------------------

@pytest.mark.asyncio
async def test_weekly_returns_four_sections(monkeypatch):
    monkeypatch.setattr("app.skills.attribution.graph.chat", _fake_chat_weekly)
    monkeypatch.setattr("app.skills.attribution.graph.check", _safe)
    result = await attribution_weekly(
        user_id=42,
        scripts=[
            {"script": "文案 A", "play_count": 3000, "review_state": "hot"},
            {"script": "文案 B", "play_count": 50, "review_state": "flop"},
        ],
    )
    assert set(["summary", "wins", "gaps", "next_focus"]).issubset(result)
    assert result["summary"]
    assert isinstance(result["wins"], list)
    assert isinstance(result["gaps"], list)
    assert result["next_focus"]
    assert result.get("blocked") is not True


# ---- safety.check 在 LLM 产出上被调用（用户可见文本过审——硬不变量）-------

@pytest.mark.asyncio
async def test_single_calls_check_on_llm_output(monkeypatch):
    """check 必须以含 diagnosis+suggestions 的文本被调用。"""
    checked_texts: list[str] = []

    async def _spy_check(text):
        checked_texts.append(text)
        return True

    monkeypatch.setattr("app.skills.attribution.graph.chat", _fake_chat_single)
    monkeypatch.setattr("app.skills.attribution.graph.check", _spy_check)
    await attribution_single(script="一段口播文案", play_count=10, baseline=100.0)
    assert checked_texts, "safety.check 未被调用"
    combined = " ".join(checked_texts)
    assert "钩子不够强" in combined  # diagnosis 文本过审
    assert "反问句" in combined  # suggestions 文本过审


@pytest.mark.asyncio
async def test_weekly_calls_check_on_llm_output(monkeypatch):
    """check 必须以含 summary/wins/gaps/next_focus 的文本被调用。"""
    checked_texts: list[str] = []

    async def _spy_check(text):
        checked_texts.append(text)
        return True

    monkeypatch.setattr("app.skills.attribution.graph.chat", _fake_chat_weekly)
    monkeypatch.setattr("app.skills.attribution.graph.check", _spy_check)
    await attribution_weekly(user_id=42, scripts=[])
    assert checked_texts
    combined = " ".join(checked_texts)
    assert "爆款" in combined  # summary
    assert "周三发布" in combined  # wins
    assert "正文缺少数据支撑" in combined  # gaps
    assert "下周重点" in combined  # next_focus


# ---- LLM 输出命中安全 → {blocked: true}，不返回 unsafe 文本 -----------------

@pytest.mark.asyncio
async def test_single_blocked_returns_blocked_flag_and_no_text(monkeypatch):
    monkeypatch.setattr("app.skills.attribution.graph.chat", _fake_chat_single)
    monkeypatch.setattr("app.skills.attribution.graph.check", _unsafe)
    result = await attribution_single(script="x", play_count=1, baseline=1.0)
    assert result.get("blocked") is True
    # 不返回 unsafe LLM 产出
    assert "diagnosis" not in result
    assert "suggestions" not in result


@pytest.mark.asyncio
async def test_weekly_blocked_returns_blocked_flag_and_no_text(monkeypatch):
    monkeypatch.setattr("app.skills.attribution.graph.chat", _fake_chat_weekly)
    monkeypatch.setattr("app.skills.attribution.graph.check", _unsafe)
    result = await attribution_weekly(user_id=42, scripts=[])
    assert result.get("blocked") is True
    for k in ("summary", "wins", "gaps", "next_focus"):
        assert k not in result


# ---- 端点鉴权 ---------------------------------------------------------------

def test_ai_attribution_single_requires_token(token, monkeypatch):
    """无 X-Service-Token → 422（Header(...) 缺失）。"""
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"diagnosis": "x", "suggestions": []}

    monkeypatch.setattr("app.api.attribution.attribution_single", _fake)
    with TestClient(app) as c:
        r = c.post("/ai/attribution/single", json={"script": "x", "play_count": 1, "baseline": 1.0})
    assert r.status_code == 422


def test_ai_attribution_single_accepts_correct_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"diagnosis": "钩子弱", "suggestions": ["加反问"]}

    monkeypatch.setattr("app.api.attribution.attribution_single", _fake)
    with TestClient(app) as c:
        r = c.post(
            "/ai/attribution/single",
            json={"script": "x", "play_count": 1, "baseline": 1.0},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json()["diagnosis"] == "钩子弱"


def test_ai_attribution_single_blocked_response(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"blocked": True}

    monkeypatch.setattr("app.api.attribution.attribution_single", _fake)
    with TestClient(app) as c:
        r = c.post(
            "/ai/attribution/single",
            json={"script": "x", "play_count": 1, "baseline": 1.0},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json() == {"blocked": True}


def test_ai_attribution_single_wrong_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"diagnosis": "x", "suggestions": []}

    monkeypatch.setattr("app.api.attribution.attribution_single", _fake)
    with TestClient(app) as c:
        r = c.post(
            "/ai/attribution/single",
            json={"script": "x", "play_count": 1, "baseline": 1.0},
            headers={"X-Service-Token": "wrong"},
        )
    assert r.status_code == 403


def test_ai_attribution_weekly_requires_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"summary": "x", "wins": [], "gaps": [], "next_focus": "y"}

    monkeypatch.setattr("app.api.attribution.attribution_weekly", _fake)
    with TestClient(app) as c:
        r = c.post("/ai/attribution/weekly", json={"user_id": 1, "scripts": []})
    assert r.status_code == 422


def test_ai_attribution_weekly_accepts_correct_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"summary": "周总览", "wins": ["w"], "gaps": ["g"], "next_focus": "下周重点"}

    monkeypatch.setattr("app.api.attribution.attribution_weekly", _fake)
    with TestClient(app) as c:
        r = c.post(
            "/ai/attribution/weekly",
            json={"user_id": 1, "scripts": []},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == "周总览"
    assert body["next_focus"] == "下周重点"


def test_ai_attribution_weekly_blocked_response(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"blocked": True}

    monkeypatch.setattr("app.api.attribution.attribution_weekly", _fake)
    with TestClient(app) as c:
        r = c.post(
            "/ai/attribution/weekly",
            json={"user_id": 1, "scripts": []},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json() == {"blocked": True}


def test_ai_attribution_weekly_wrong_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"summary": "x", "wins": [], "gaps": [], "next_focus": "y"}

    monkeypatch.setattr("app.api.attribution.attribution_weekly", _fake)
    with TestClient(app) as c:
        r = c.post(
            "/ai/attribution/weekly",
            json={"user_id": 1, "scripts": []},
            headers={"X-Service-Token": "wrong"},
        )
    assert r.status_code == 403
