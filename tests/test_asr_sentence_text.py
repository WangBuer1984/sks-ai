"""ASR 回调文本抽取——锁住 dashscope get_sentence() 返回 dict/list 的契约。

历史故障：Collector 实现了 on_result（SDK 调的是 on_event），且用 getattr(sent, "text")
读 dict，真实识别结果被吞成空串，HTTP 仍 200。
"""

from __future__ import annotations

from app.datasource.asr import _sentence_text


def test_sentence_text_from_dict() -> None:
    assert _sentence_text({"text": "你好呀", "end_time": 1200}) == "你好呀"


def test_sentence_text_empty_dict() -> None:
    assert _sentence_text({}) == ""
    assert _sentence_text({"text": ""}) == ""
    assert _sentence_text(None) == ""


def test_sentence_text_from_list() -> None:
    assert (
        _sentence_text(
            [
                {"text": "第一句。", "end_time": 1},
                {"text": "第二句。", "end_time": 2},
            ]
        )
        == "第一句。第二句。"
    )


def test_sentence_text_ignores_non_dict_list_items() -> None:
    assert _sentence_text([{"text": "ok"}, "x", None]) == "ok"
