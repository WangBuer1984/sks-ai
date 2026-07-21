"""文案生成 LangGraph：retrieve → generate → safety → (rewrite once) → done/blocked。

设计文档 §5 + §4.1：
- script_gen 用 glm-4.7 thinking off（MODEL_FOR["script_gen"]）。
- 输出三段 {hook, body, cta}，每段为 {sentences: [{idx, text}]}——逐句编辑（V1.1）的数据基础。
- 无流式（§5.1 硬不变量）：生成完整 → 内容安全 → 一次性返回 JSON。
- 安全命中则重写一次再查，仍命中返回 {blocked: true}——**仅一次重写**，不循环。

模块级别名 chat / check / retrieve_b_cards 是测试 monkeypatch 目标
（app.skills.script_gen.graph.chat / .check / .retrieve_b_cards）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

log = logging.getLogger(__name__)

from app.llm.client import glm_client
from app.rag.retrieve import retrieve_b_cards as _retrieve_b_cards
from app.safety.content_safety import check as _check

# 模块级别名——测试 monkeypatch 目标（app.skills.script_gen.graph.chat / .check / .retrieve_b_cards）
chat = glm_client.chat
check = _check
retrieve_b_cards = _retrieve_b_cards


# ---- 结构化输出 schema ----------------------------------------------------

_SENTENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer", "description": "句子序号，从 0 开始"},
                    "text": {"type": "string", "description": "单句文案文本"},
                },
                "required": ["idx", "text"],
            },
        }
    },
    "required": ["sentences"],
}

SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hook": _SENTENCE_SCHEMA,
        "body": _SENTENCE_SCHEMA,
        "cta": _SENTENCE_SCHEMA,
    },
    "required": ["hook", "body", "cta"],
}


# ---- LangGraph state -------------------------------------------------------

class ScriptGenState(TypedDict, total=False):
    user_id: int
    topic: dict[str, Any]
    profile: dict[str, Any]
    platform: str
    cards: list[Any]
    cited_card_ids: list[int]
    script: dict[str, Any]
    safety_passed: bool
    rewrite_attempted: bool
    blocked: bool


# ---- prompt 构建 -----------------------------------------------------------

def _build_messages(state: ScriptGenState) -> list[dict[str, str]]:
    """构造生成 prompt：注入 A 层全量（定位档案）+ B 层命中卡 + 选题 + 平台。"""
    topic = state["topic"]
    profile = state["profile"]
    platform = state["platform"]
    cards = state.get("cards", [])

    profile_text = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "（无定位档案）"
    cards_text = ""
    if cards:
        cards_text = "\n".join(
            f"- [{c.card_type}] {c.title}: {json.dumps(c.content, ensure_ascii=False)}"
            for c in cards
        )
    else:
        cards_text = "（无 B 层卡命中）"

    system = (
        "你是一名专业口播视频文案创作者。根据选题、定位档案和知识库卡片，"
        "生成一段口播文案。文案分为三段：hook（开场钩子）、body（正文内容）、cta（结尾引导）。"
        "每段由若干句子组成，每句需有 idx（从 0 开始）和 text（单句文本）。"
        "内容须符合平台调性，口吻与定位档案一致，不得包含违禁内容。"
    )
    user = (
        f"平台: {platform}\n"
        f"选题: {topic.get('title', '')}\n"
        f"选题理由: {topic.get('rationale', '')}\n"
        f"定位档案（A 层全量）:\n{profile_text}\n"
        f"知识库 B 层命中卡:\n{cards_text}\n\n"
        "请生成三段文案（hook/body/cta），每段为句子数组。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_rewrite_messages(state: ScriptGenState) -> list[dict[str, str]]:
    """构造重写 prompt：原稿 + 安全未通过指令（重写一次，非循环）。"""
    script = state.get("script", {})
    script_text = _script_to_text(script)

    system = (
        "你是一名专业口播视频文案创作者。之前生成的文案未通过内容安全审核，"
        "请重新生成一段合规的口播文案，保持原意但移除任何违禁内容。"
        "文案分为三段：hook（开场钩子）、body（正文内容）、cta（结尾引导）。"
        "每段由若干句子组成，每句需有 idx（从 0 开始）和 text（单句文本）。"
    )
    user = (
        f"平台: {state['platform']}\n"
        f"选题: {state['topic'].get('title', '')}\n"
        f"定位档案:\n{json.dumps(state.get('profile', {}), ensure_ascii=False, indent=2)}\n"
        f"原稿（未通过安全审核）:\n{script_text}\n\n"
        "请重新生成合规的三段文案（hook/body/cta）。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _script_to_text(script: dict[str, Any]) -> str:
    """把三段结构化文案拼接为纯文本（用于内容安全检查）。"""
    parts: list[str] = []
    for section in ("hook", "body", "cta"):
        section_data = script.get(section, {}) or {}
        for s in section_data.get("sentences", []):
            text = s.get("text", "") if isinstance(s, dict) else str(s)
            if text:
                parts.append(text)
    return " ".join(parts)


# ---- LangGraph nodes -------------------------------------------------------

async def _retrieve_node(state: ScriptGenState) -> dict[str, Any]:
    """召回 B 层 top-5 卡片（user_id 隔离在 retrieve_b_cards SQL 内保证）。

    RAG 检索是 best-effort：若 embed/DB 不可达（如 GLM API 未配置、DB 未起），
    返回空列表——稿仍能生成（只是无 cited_card_ids），不阻断生成流程。
    生产环境 embed/DB 应可用；此降级保证测试（未 mock retrieve 时）与
    瞬时故障不致整体 500。
    """
    try:
        cards = await retrieve_b_cards(
            state["user_id"],
            state["topic"].get("title", ""),
        )
    except Exception:  # noqa: BLE001 — RAG 降级，不阻断生成
        log.warning("retrieve_b_cards failed, generating without RAG context", exc_info=True)
        cards = []
    return {"cards": cards, "cited_card_ids": [c.id for c in cards]}


async def _generate_node(state: ScriptGenState) -> dict[str, Any]:
    """调 GLM 生成结构化三段文案（json_schema 约束输出格式）。"""
    messages = _build_messages(state)
    script = await chat("script_gen", messages, json_schema=SCRIPT_SCHEMA)
    return {"script": script}


async def _safety_node(state: ScriptGenState) -> dict[str, Any]:
    """内容安全检查。命中且未重写 → 走重写；命中且已重写 → blocked。"""
    text = _script_to_text(state.get("script", {}))
    safe = await check(text)
    if safe:
        return {"safety_passed": True}
    # 不安全
    if state.get("rewrite_attempted"):
        return {"blocked": True, "safety_passed": False}
    return {"safety_passed": False}  # 触发重写


async def _rewrite_node(state: ScriptGenState) -> dict[str, Any]:
    """重写一次（安全命中后），非循环——仅一次。"""
    messages = _build_rewrite_messages(state)
    script = await chat("script_gen", messages, json_schema=SCRIPT_SCHEMA)
    return {"script": script, "rewrite_attempted": True}


def _safety_router(state: ScriptGenState) -> str:
    """safety → done / blocked / rewrite。"""
    if state.get("blocked"):
        return "blocked"
    if state.get("safety_passed"):
        return "done"
    return "rewrite"


# ---- graph build -----------------------------------------------------------

def _build_graph():
    g: StateGraph = StateGraph(ScriptGenState)
    g.add_node("retrieve", _retrieve_node)
    g.add_node("generate", _generate_node)
    g.add_node("safety", _safety_node)
    g.add_node("rewrite", _rewrite_node)
    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", "safety")
    g.add_conditional_edges(
        "safety",
        _safety_router,
        {"rewrite": "rewrite", "done": END, "blocked": END},
    )
    g.add_edge("rewrite", "safety")
    return g.compile()


_graph = _build_graph()


# ---- entrypoint ------------------------------------------------------------

async def generate_script(
    user_id: int,
    topic: dict[str, Any],
    profile: dict[str, Any],
    platform: str,
) -> dict[str, Any]:
    """文案生成入口：retrieve → generate → safety → (rewrite once) → done/blocked。

    返回:
      - 成功: {hook, body, cta, cited_card_ids: [...]}
      - blocked: {blocked: true}
    """
    initial: ScriptGenState = {
        "user_id": user_id,
        "topic": topic,
        "profile": profile,
        "platform": platform,
        "rewrite_attempted": False,
        "safety_passed": False,
        "blocked": False,
    }
    result = await _graph.ainvoke(initial)
    if result.get("blocked"):
        return {"blocked": True}
    script = result.get("script", {})
    return {
        "hook": script.get("hook", {}),
        "body": script.get("body", {}),
        "cta": script.get("cta", {}),
        "cited_card_ids": result.get("cited_card_ids", []),
    }
