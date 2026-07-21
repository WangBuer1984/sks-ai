"""单句重写 skill（独立于 script_gen 内部的安全重写）。

POST /ai/rewrite_sentence 的核心：带上整稿 + 定位档案做上下文保持口吻连贯，
走轻量档（skill=rewrite_sentence → glm-4.5-air），产出过 safety.check。
不循环重写——一次不通过即 {blocked: true}。
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.client import glm_client
from app.safety.content_safety import check as _check

# 模块级别名——测试 monkeypatch 目标（app.skills.script_gen.rewrite.chat / .check）
chat = glm_client.chat
check = _check


REWRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "重写后的单句文案"},
    },
    "required": ["text"],
}


def _build_messages(
    sentence: str,
    section: str,
    full_script: dict[str, Any],
    profile: dict[str, Any],
) -> list[dict[str, str]]:
    """构造重写 prompt：原句 + 所属段 + 整稿 + 定位档案（保持口吻连贯）。"""
    system = (
        "你是一名口播文案编辑助手。请将给定的句子换个说法，"
        "保持原意但用不同的表达方式。口吻须与整稿和定位档案一致，不得包含违禁内容。"
    )
    user = (
        f"需要重写的句子: {sentence}\n"
        f"所属段落: {section}\n"
        f"完整文案上下文:\n{json.dumps(full_script, ensure_ascii=False, indent=2)}\n"
        f"定位档案:\n{json.dumps(profile, ensure_ascii=False, indent=2)}\n\n"
        "请重写这句文案，返回一个新的版本。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def rewrite_sentence(
    sentence: str,
    section: str,
    full_script: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """单句重写：调 glm-4.5-air 重写 → safety.check → {text} 或 {blocked: true}。

    不循环重写——一次不通过即 blocked（与 script_gen 内部的安全重写不同，那个有一次重试）。
    """
    messages = _build_messages(sentence, section, full_script, profile)
    result = await chat("rewrite_sentence", messages, json_schema=REWRITE_SCHEMA)
    text = result.get("text", "")
    safe = await check(text)
    if not safe:
        return {"blocked": True}
    return {"text": text}
