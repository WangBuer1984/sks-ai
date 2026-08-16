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


class _FakeContent:
    """模拟 rag.retrieve.ContentHit——id 用于 cited_content_ids，其余供 prompt 拼接。"""

    def __init__(self, id):
        self.id = id
        self.source = "manual"
        self.title = f"content-{id}"
        self.body = f"body {id}"


async def _fake_retrieve_two_contents(*args, **kwargs):
    return [_FakeContent(11), _FakeContent(22)]


async def _fake_retrieve_no_contents(*args, **kwargs):
    return []


@pytest.mark.asyncio
async def test_success_returns_three_sections_and_citations(monkeypatch):
    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_ok)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_contents", _fake_retrieve_two_contents)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert set(result) >= {"hook", "body", "cta", "cited_content_ids"}
    assert result["cited_content_ids"] == [11, 22]
    assert result["cited_card_ids"] == []
    assert "blocked" not in result


@pytest.mark.asyncio
async def test_no_cards_retrieved_still_generates(monkeypatch):
    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat_ok)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_contents", _fake_retrieve_no_contents)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert result["cited_content_ids"] == []
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
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_contents", _fake_retrieve_no_contents)
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
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_contents", _fake_retrieve_no_contents)
    result = await generate_script(user_id=1, topic={"title": "x", "rationale": "y"},
                                   profile={}, platform="douyin")
    assert result["hook"] == {}, "非 JSON 字符串应降级为空 dict"
    assert result["cta"] == {}, "空字符串应降级为空 dict"
    assert result["body"]["sentences"][0]["text"] == "正常段"


@pytest.mark.asyncio
async def test_framework_and_platform_hint_enter_prompt(monkeypatch):
    seen: dict = {}

    async def _fake_chat(skill, messages, json_schema=None, **kwargs):
        seen["messages"] = messages
        return {"hook": {}, "body": {}, "cta": {}}

    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_contents", _fake_retrieve_no_contents)
    await generate_script(
        user_id=1,
        topic={"title": "报价差一倍", "rationale": "y"},
        profile={},
        platform="channels",
        framework="钩子-冲突-反转-收尾",
    )
    prompt = "\n".join(m["content"] for m in seen["messages"])
    assert "钩子-冲突-反转-收尾" in prompt
    assert "视频号" in prompt
    assert "本稿只基于定位档案" in prompt or "知识库没有相关内容" in prompt


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


def test_script_gen_accepts_generation_group_and_framework(token, monkeypatch):
    """双平台一轮生成（D21）：Java 对同一 generation_group 各调一次本端点，platform 不同。

    Python 仍是<b>无状态单平台生成</b>——不知道「组」的存在，也不管计费。
    generation_group_id / framework 必须传到 generate_script（framework 进 prompt）。
    """
    from fastapi.testclient import TestClient

    from app.api.script_gen import ScriptGenRequest
    from app.main import app

    assert "generation_group_id" in ScriptGenRequest.model_fields
    assert "framework" in ScriptGenRequest.model_fields
    # 两者都可缺省——既有单平台链路不传
    assert ScriptGenRequest.model_fields["generation_group_id"].default is None
    assert ScriptGenRequest.model_fields["framework"].default is None

    # 解析结果确实带上了这两个字段（不是被 pydantic 当 extra 丢掉）
    parsed = ScriptGenRequest(
        user_id=1, topic={"title": "x"}, platform="channels", generation_group_id=42, framework="钩子-冲突"
    )
    assert parsed.generation_group_id == 42
    assert parsed.framework == "钩子-冲突"

    seen: dict[str, object] = {}

    async def _fake_generate(**kwargs):
        seen.update(kwargs)
        return {"hook": {}, "body": {}, "cta": {}, "cited_card_ids": [], "cited_content_ids": []}

    monkeypatch.setattr("app.api.script_gen.generate_script", _fake_generate)
    with TestClient(app) as c:
        r = c.post(
            "/ai/script_gen",
            json={
                "user_id": 1,
                "topic": {"title": "x", "rationale": "y"},
                "profile": {},
                "platform": "channels",
                "generation_group_id": 42,
                "framework": "钩子-冲突-反转-收尾",
            },
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200, r.text
    assert seen["platform"] == "channels"
    assert seen["generation_group_id"] == 42
    assert seen["framework"] == "钩子-冲突-反转-收尾"


def test_script_gen_rejects_retired_platform(token, monkeypatch):
    """平台取值在 Python 边界也收口（D13）：只接受 douyin/channels，其余 422。

    Java 侧已在生成入口拒退役平台；这里是第二道，防「换个调用方就把 kuaishou 送进 prompt」。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    called = False

    async def _fake_generate(**kwargs):
        nonlocal called
        called = True
        return {"hook": {}, "body": {}, "cta": {}, "cited_card_ids": [], "cited_content_ids": []}

    monkeypatch.setattr("app.api.script_gen.generate_script", _fake_generate)
    with TestClient(app) as c:
        for bad in ("kuaishou", "xiaohongshu", "wechat_channels", ""):
            r = c.post(
                "/ai/script_gen",
                json={"user_id": 1, "topic": {"title": "x"}, "profile": {}, "platform": bad},
                headers={"X-Service-Token": token},
            )
            assert r.status_code == 422, f"{bad} 应被拒: {r.text}"
        for good in ("douyin", "channels"):
            r = c.post(
                "/ai/script_gen",
                json={"user_id": 1, "topic": {"title": "x"}, "profile": {}, "platform": good},
                headers={"X-Service-Token": token},
            )
            assert r.status_code == 200, r.text
    assert called


def test_script_gen_returns_cited_content_ids(token, monkeypatch):
    """整篇内容参考（D2/D18）取代 B 卡引用：出参多 cited_content_ids，Java 据此写 content_reference。"""
    from fastapi.testclient import TestClient

    from app.main import app

    async def _fake_generate(*args, **kwargs):
        return {
            "hook": {},
            "body": {},
            "cta": {},
            "cited_card_ids": [],
            "cited_content_ids": [7, 8],
        }

    monkeypatch.setattr("app.api.script_gen.generate_script", _fake_generate)
    with TestClient(app) as c:
        r = c.post(
            "/ai/script_gen",
            json={"user_id": 1, "topic": {"title": "x"}, "profile": {}, "platform": "douyin"},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["cited_content_ids"] == [7, 8]


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
