"""MODEL_FOR 唯一模型型号来源的校验测试（brief Step 1 原文）。"""


def test_every_skill_has_model_spec():
    from app.llm.models import MODEL_FOR

    required = {
        "script_gen",
        "interview",
        "card_gen",
        "rewrite_sentence",
        "video_analyze",
        "account_analyze_item",
        "account_analyze_summary",
        "attribution",
    }
    assert required <= set(MODEL_FOR.keys())
    for spec in MODEL_FOR.values():
        assert spec.model.startswith("glm-")


def test_tiering_matches_design():
    """选型表：创作 4.7；访谈 air（时延）；轻量 air；归纳/归因 4.7 thinking 关。

    归纳/归因原设计 thinking 开，但 GLM-4.7 thinking 与结构化输出冲突（FC→1210、
    json_schema→散文、json_mode→形状不可控），已统一关 thinking；不变量见
    test_structured_skills_must_not_think 与 GLMClient.chat 的降级 guard。
    """
    from app.llm.models import MODEL_FOR

    assert MODEL_FOR["script_gen"].model == "glm-4.7"
    assert MODEL_FOR["script_gen"].thinking is False
    # interview：原创作类 4.7，8371d6e 因超时改 air；多轮校准锁时延（见 models.py docstring）
    assert MODEL_FOR["interview"].model == "glm-4.5-air"
    assert MODEL_FOR["interview"].thinking is False
    assert MODEL_FOR["video_analyze"].model == "glm-4.7"
    assert MODEL_FOR["video_analyze"].thinking is False

    assert MODEL_FOR["card_gen"].model == "glm-4.5-air"
    assert MODEL_FOR["rewrite_sentence"].model == "glm-4.5-air"
    assert MODEL_FOR["account_analyze_item"].model == "glm-4.5-air"

    assert MODEL_FOR["account_analyze_summary"].model == "glm-4.7"
    assert MODEL_FOR["account_analyze_summary"].thinking is False
    assert MODEL_FOR["attribution"].model == "glm-4.7"
    assert MODEL_FOR["attribution"].thinking is False


def test_structured_skills_must_not_think():
    """不变量锁：走结构化输出（json_schema）的归纳/归因 skill 必须 thinking=False。

    GLM-4.7 thinking 开 + function_calling → 400 code 1210。此锁在配置层把漂移显式炸出来，
    早于运行时 1210；GLMClient.chat 另有运行时降级 guard 兜底。
    """
    from app.llm.models import MODEL_FOR

    structured_thinking_skills = ("account_analyze_summary", "attribution")
    for skill in structured_thinking_skills:
        assert MODEL_FOR[skill].thinking is False, (
            f"{skill} 必须 thinking=False：GLM-4.7 thinking+结构化输出触发 1210"
        )
