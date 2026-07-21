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
    """设计文档模型选型表：创作类 glm-4.7 thinking 关；轻量抽取 glm-4.5-air；深度 glm-4.7 thinking 开。"""
    from app.llm.models import MODEL_FOR

    assert MODEL_FOR["script_gen"].model == "glm-4.7"
    assert MODEL_FOR["script_gen"].thinking is False
    assert MODEL_FOR["interview"].model == "glm-4.7"
    assert MODEL_FOR["interview"].thinking is False
    assert MODEL_FOR["video_analyze"].model == "glm-4.7"
    assert MODEL_FOR["video_analyze"].thinking is False

    assert MODEL_FOR["card_gen"].model == "glm-4.5-air"
    assert MODEL_FOR["rewrite_sentence"].model == "glm-4.5-air"
    assert MODEL_FOR["account_analyze_item"].model == "glm-4.5-air"

    assert MODEL_FOR["account_analyze_summary"].model == "glm-4.7"
    assert MODEL_FOR["account_analyze_summary"].thinking is True
    assert MODEL_FOR["attribution"].model == "glm-4.7"
    assert MODEL_FOR["attribution"].thinking is True
