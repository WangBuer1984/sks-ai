"""GLMClient.chat() 测试：注入 fake LLM，断言按 skill 路由到正确档位 + 返回 dict。"""

import pytest

from app.llm.client import GLMClient
from app.llm.models import MODEL_FOR


class _FakeLLM:
    """记录被构造时的 spec，ainvoke 返回固定 dict。"""

    def __init__(self, spec):
        self.spec = spec
        self.structured_with = None

    def with_structured_output(self, schema, method=None):
        self.structured_with = (schema, method)
        return self

    async def ainvoke(self, messages):
        return {"model_used": self.spec.model, "thinking": self.spec.thinking, "messages": messages}


def _fake_factory(spec):
    return _FakeLLM(spec)


async def test_chat_routes_to_correct_model_per_skill():
    client = GLMClient(llm_factory=_fake_factory)
    for skill, spec in MODEL_FOR.items():
        result = await client.chat(skill, [{"role": "user", "content": "hi"}])
        assert result["model_used"] == spec.model
        assert result["thinking"] == spec.thinking


async def test_chat_returns_dict():
    client = GLMClient(llm_factory=_fake_factory)
    result = await client.chat("script_gen", [{"role": "user", "content": "hi"}])
    assert isinstance(result, dict)
    assert result["model_used"] == "glm-4.7"


async def test_chat_passes_messages_through():
    client = GLMClient(llm_factory=_fake_factory)
    msgs = [{"role": "user", "content": "写一段口播"}]
    result = await client.chat("script_gen", msgs)
    assert result["messages"] is msgs


async def test_chat_with_json_schema_uses_structured_output():
    client = GLMClient(llm_factory=_fake_factory)
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    result = await client.chat("script_gen", [{"role": "user", "content": "x"}], json_schema=schema)
    assert result["model_used"] == "glm-4.7"


async def test_chat_unknown_skill_raises():
    client = GLMClient(llm_factory=_fake_factory)
    with pytest.raises(KeyError):
        await client.chat("not_a_skill", [{"role": "user", "content": "x"}])
