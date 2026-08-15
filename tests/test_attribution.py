"""归因 skill 测试：mock LLM，绝不发真实网络/DB 请求。

覆盖：
- 单条归因返回 {diagnosis, suggestions}
- 周卡返回 {summary, wins, gaps, next_focus}
- skill="attribution"（→ glm-4.7 thinking off；原 thinking on 与结构化输出冲突，已关）
- /ai/attribution/single + /ai/attribution/weekly 鉴权
创作链路不调阿里云内容安全。
"""

import pytest

from app.skills.attribution.graph import attribution_single, attribution_weekly


async def _fake_chat_single(*args, **kwargs):
    return {
        "diagnosis": "钩子不够强，前 3 秒未留住观众；正文信息密度低。",
        "suggestions": [
            "开场用反问句或冲突数字钩住注意力",
            "正文压缩到 3 个核心论点",
            "结尾 CTA 更直接，给出明确动作",
        ],
    }


async def _fake_chat_weekly(*args, **kwargs):
    return {
        "summary": "本周发布 5 条，1 条爆款（3 倍均值），2 条 flop，整体表现低于均值。",
        "wins": ["爆款采用强反问钩子，留存率高", "周三发布时段表现最佳"],
        "gaps": ["2 条 flop 开场冗长", "正文缺少数据支撑"],
        "next_focus": "下周重点打磨开场 3 秒，并增加案例数据。",
    }


@pytest.mark.asyncio
async def test_single_passes_skill_attribution_to_chat(monkeypatch):
    seen = {}

    async def _spy_chat(skill, messages, json_schema=None):
        seen["skill"] = skill
        seen["json_schema"] = json_schema
        return await _fake_chat_single()

    monkeypatch.setattr("app.skills.attribution.graph.chat", _spy_chat)
    await attribution_single(script="一段口播文案", play_count=1200, baseline=500.0)
    assert seen["skill"] == "attribution"
    assert seen["json_schema"] is not None


@pytest.mark.asyncio
async def test_weekly_passes_skill_attribution_to_chat(monkeypatch):
    seen = {}

    async def _spy_chat(skill, messages, json_schema=None):
        seen["skill"] = skill
        seen["json_schema"] = json_schema
        return await _fake_chat_weekly()

    monkeypatch.setattr("app.skills.attribution.graph.chat", _spy_chat)
    await attribution_weekly(user_id=42, scripts=[])
    assert seen["skill"] == "attribution"
    assert seen["json_schema"] is not None


@pytest.mark.asyncio
async def test_single_returns_diagnosis_and_suggestions(monkeypatch):
    monkeypatch.setattr("app.skills.attribution.graph.chat", _fake_chat_single)
    result = await attribution_single(script="一段口播文案", play_count=1200, baseline=500.0)
    assert "diagnosis" in result and "suggestions" in result
    assert isinstance(result["suggestions"], list) and len(result["suggestions"]) >= 1
    assert result["diagnosis"]
    assert "blocked" not in result


@pytest.mark.asyncio
async def test_weekly_returns_four_sections(monkeypatch):
    monkeypatch.setattr("app.skills.attribution.graph.chat", _fake_chat_weekly)
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
    assert "blocked" not in result


def test_ai_attribution_single_requires_token(token, monkeypatch):
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


def test_ai_attribution_weekly_requires_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"summary": "s", "wins": [], "gaps": [], "next_focus": "n"}

    monkeypatch.setattr("app.api.attribution.attribution_weekly", _fake)
    with TestClient(app) as c:
        r = c.post("/ai/attribution/weekly", json={"user_id": 1, "scripts": []})
    assert r.status_code == 422


def test_ai_attribution_weekly_accepts_correct_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    async def _fake(*args, **kwargs):
        return {"summary": "s", "wins": ["w"], "gaps": ["g"], "next_focus": "n"}

    monkeypatch.setattr("app.api.attribution.attribution_weekly", _fake)
    with TestClient(app) as c:
        r = c.post(
            "/ai/attribution/weekly",
            json={"user_id": 1, "scripts": []},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json()["summary"] == "s"
