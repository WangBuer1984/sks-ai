"""文案生成 LangGraph：retrieve → generate → done。

设计文档 §5 + §4.1：
- script_gen 用 glm-4.7 thinking off（MODEL_FOR["script_gen"]）。
- 输出三段 {hook, body, cta}，每段为 {sentences: [{idx, text}]}——逐句编辑（V1.1）的数据基础。
- 无流式（§5.1 硬不变量）：生成完整 → 一次性返回 JSON。
- **不调阿里云内容安全**：创作产出交给大模型自身合规；阿里云只审用户输入/录音。

模块级别名 chat / retrieve_b_cards 是测试 monkeypatch 目标
（app.skills.script_gen.graph.chat / .retrieve_b_cards）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

log = logging.getLogger(__name__)

from app.llm.client import glm_client
from app.rag.retrieve import retrieve_b_cards as _retrieve_b_cards

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat
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


def _normalize_section(v: Any) -> dict[str, Any]:
    """hook/body/cta 归一为 ``{sentences: [...]}`` dict。

    GLM function_calling 结构化输出偶尔把某段双重编码成 JSON 字符串（线上实测
    cta 中招，返回 ``'{"sentences":[…]}'``），原样透传会让
    ``ScriptGenResponse(cta: dict)`` 校验炸 500。str → ``json.loads``；
    解析失败/非对象 → 空 dict——空段不阻断生成，比整条 500 友好（用户能看出该段
    空、可手改/重生）。
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            log.warning("script section not JSON-decodable, returning empty: %.80s", s)
            return {}
        if isinstance(parsed, dict):
            return parsed
        log.warning(
            "script section JSON decoded to %s, not dict; returning empty",
            type(parsed).__name__,
        )
        return {}
    if v is None:
        return {}
    log.warning("script section unexpected type %s, returning empty", type(v).__name__)
    return {}


# ---- LangGraph state -------------------------------------------------------

class ScriptGenState(TypedDict, total=False):
    user_id: int
    topic: dict[str, Any]
    profile: dict[str, Any]
    platform: str
    duration: str
    cards: list[Any]
    cited_card_ids: list[int]
    script: dict[str, Any]


# ---- prompt 构建 -----------------------------------------------------------

def _build_messages(state: ScriptGenState) -> list[dict[str, str]]:
    """构造生成 prompt：注入 A 层全量（定位档案）+ B 层命中卡 + 选题 + 平台。"""
    topic = state["topic"]
    profile = state["profile"]
    platform = state["platform"]
    duration_label = {"45": "45 秒口播", "90": "90 秒", "180": "3 分钟深度"}.get(
        state.get("duration", "45"), "45 秒口播"
    )
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
        "内容须符合平台调性，口吻与定位档案一致。"
    )
    user = (
        f"平台: {platform}\n"
        f"目标时长: {duration_label}（按此时长控制篇幅与结构）\n"
        f"选题: {topic.get('title', '')}\n"
        f"选题理由: {topic.get('rationale', '')}\n"
        f"定位档案（A 层全量）:\n{profile_text}\n"
        f"知识库 B 层命中卡:\n{cards_text}\n\n"
        "请生成三段文案（hook/body/cta），每段为句子数组。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---- LangGraph nodes -------------------------------------------------------

async def _retrieve_node(state: ScriptGenState) -> dict[str, Any]:
    """召回 B 层 top-5 卡片（user_id 隔离在 retrieve_b_cards SQL 内保证）。

    RAG 检索是 best-effort：若 embed/DB 不可达（如 GLM API 未配置、DB 未起），
    返回空列表——稿仍能生成（只是无 cited_card_ids），不阻断生成流程。
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


# ---- graph build -----------------------------------------------------------

def _build_graph():
    g: StateGraph = StateGraph(ScriptGenState)
    g.add_node("retrieve", _retrieve_node)
    g.add_node("generate", _generate_node)
    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile()


_graph = _build_graph()


# ---- entrypoint ------------------------------------------------------------

async def generate_script(
    user_id: int,
    topic: dict[str, Any],
    profile: dict[str, Any],
    platform: str,
    duration: str = "45",
) -> dict[str, Any]:
    """文案生成入口：retrieve → generate → 返回三段。

    返回: {hook, body, cta, cited_card_ids: [...]}
    不做阿里云内容安全（创作链路交给大模型自身合规）。
    """
    initial: ScriptGenState = {
        "user_id": user_id,
        "topic": topic,
        "profile": profile,
        "platform": platform,
        "duration": duration,
    }
    result = await _graph.ainvoke(initial)
    script = result.get("script", {})
    if not isinstance(script, dict):
        log.warning("script not a dict (%s), treating as empty", type(script).__name__)
        script = {}
    return {
        "hook": _normalize_section(script.get("hook", {})),
        "body": _normalize_section(script.get("body", {})),
        "cta": _normalize_section(script.get("cta", {})),
        "cited_card_ids": result.get("cited_card_ids", []),
    }
