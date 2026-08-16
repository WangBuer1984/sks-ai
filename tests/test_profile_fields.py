"""定位档案七字段在 Python 侧的读法（D19 / D20）。

档案的**唯一真源在共享库**，Java 写、Python 只读——本仓要保证的是「拿到 profile 之后怎么读」这一件事：

- 读的是七个规范键（`persona` / `targetAudience` / `differentiation` / `conversionPath` / `tone` /
  `redlines` / `contentPillars`），不是历史上的中文键；老档案（中文键）仍要能读，否则线上存量档案一改版就哑了。
- **FAQ 不进 script_gen / rewrite 的 prompt**：高频问答是选题来源，不是写稿风格。混进去会让每篇稿子都在
  回答那几个问题——这类污染在生成结果里很难一眼看出来，只能靠断言钉住。
"""

import pytest

from app.skills.profile_fields import PROFILE_FIELDS, canonical_profile, render_profile


# ---- 规范键 / 旧中文键 -------------------------------------------------------

def test_profile_fields_are_the_seven_canonical_keys():
    assert PROFILE_FIELDS == (
        "persona",
        "targetAudience",
        "differentiation",
        "conversionPath",
        "tone",
        "redlines",
        "contentPillars",
    )


def test_canonical_profile_keeps_canonical_keys():
    p = canonical_profile(
        {
            "persona": "说真话的工厂人",
            "tone": "直白、不绕弯",
            "redlines": ["不贬同行"],
            "contentPillars": ["报价拆解"],
        }
    )
    assert p == {
        "persona": "说真话的工厂人",
        "tone": "直白、不绕弯",
        "redlines": ["不贬同行"],
        "contentPillars": ["报价拆解"],
    }


def test_canonical_profile_maps_legacy_chinese_keys():
    """线上还有中文键的老档案（Java 读侧也做同样的投影）——两侧都能读才叫兼容。"""
    p = canonical_profile(
        {"人设": "美妆成分党", "人群": "25-35 女性", "变现": "带货", "红线": "不夸大功效"}
    )
    assert p["persona"] == "美妆成分党"
    assert p["targetAudience"] == "25-35 女性"
    assert p["conversionPath"] == "带货"
    assert p["redlines"] == "不夸大功效"


def test_canonical_profile_drops_unknown_and_faq_keys():
    p = canonical_profile(
        {
            "persona": "p",
            "faqs": [{"question": "报价为什么差一倍"}],
            "faq_candidates": [{"question": "工期多久"}],
            "_interview_turns": [{"role": "ai", "text": "q"}],
            "创作偏好": "偷偷存的第二套人设",
        }
    )
    assert p == {"persona": "p"}


def test_canonical_profile_tolerates_non_dict():
    assert canonical_profile(None) == {}
    assert canonical_profile("不是字典") == {}


# ---- prompt 渲染 -------------------------------------------------------------

def test_render_profile_uses_chinese_labels():
    text = render_profile({"persona": "说真话的工厂人", "redlines": ["不贬同行", "不承诺效果"]})
    assert "人设：说真话的工厂人" in text
    assert "红线：不贬同行 · 不承诺效果" in text
    # 键名本身不该出现在给模型看的文本里
    assert "persona" not in text


def test_render_profile_empty_says_so():
    assert render_profile({}) == "（无定位档案）"
    assert render_profile({"faqs": [{"question": "x"}]}) == "（无定位档案）"


def test_render_profile_excludes_faq():
    text = render_profile(
        {"persona": "p", "faqs": [{"question": "报价为什么差一倍", "answer": "板材不同"}]}
    )
    assert "报价为什么差一倍" not in text
    assert "板材不同" not in text


# ---- 落到两个 prompt 上 ------------------------------------------------------

@pytest.mark.asyncio
async def test_script_gen_prompt_reads_canonical_keys_and_excludes_faq(monkeypatch):
    """script_gen 的 prompt 里必须有档案七字段、必须没有 FAQ。"""
    from app.skills.script_gen import graph as sg

    seen: dict = {}

    async def _fake_chat(skill, messages, json_schema=None, **kwargs):
        seen["messages"] = messages
        return {"hook": {}, "body": {}, "cta": {}}

    async def _no_cards(*args, **kwargs):
        return []

    monkeypatch.setattr("app.skills.script_gen.graph.chat", _fake_chat)
    monkeypatch.setattr("app.skills.script_gen.graph.retrieve_contents", _no_cards)

    await sg.generate_script(
        user_id=1,
        topic={"title": "报价为什么差一倍", "rationale": "常被问"},
        profile={
            "persona": "说真话的工厂人",
            "tone": "直白、不绕弯",
            "faqs": [{"question": "工期一般多久", "answer": "45 天"}],
        },
        platform="douyin",
    )

    prompt = "\n".join(m["content"] for m in seen["messages"])
    assert "说真话的工厂人" in prompt
    assert "直白、不绕弯" in prompt
    assert "工期一般多久" not in prompt, "FAQ 不得注入 script_gen prompt"
    assert "45 天" not in prompt


@pytest.mark.asyncio
async def test_rewrite_prompt_reads_canonical_keys_and_excludes_faq(monkeypatch):
    """单句改写与生成读同一份档案（Java 侧已保证传的是同一个对象），渲染规则也必须一致。"""
    from app.skills.script_gen import rewrite as rw

    seen: dict = {}

    async def _fake_chat(skill, messages, json_schema=None, **kwargs):
        seen["messages"] = messages
        return {"text": "换个说法"}

    monkeypatch.setattr("app.skills.script_gen.rewrite.chat", _fake_chat)

    await rw.rewrite_sentence(
        sentence="原句",
        section="body",
        full_script={"hook": {}, "body": {}, "cta": {}},
        profile={"tone": "直白、不绕弯", "faqs": [{"question": "工期一般多久"}]},
    )

    prompt = "\n".join(m["content"] for m in seen["messages"])
    assert "直白、不绕弯" in prompt
    assert "工期一般多久" not in prompt, "FAQ 不得注入 rewrite prompt"
