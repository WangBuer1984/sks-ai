"""文案生成 LangGraph：retrieve → generate → done。

设计文档 §5 + §4.1：
- script_gen 用 glm-4.7 thinking off（MODEL_FOR["script_gen"]）。
- 输出三段 {hook, body, cta}，每段为 {sentences: [{idx, text}]}——逐句编辑（V1.1）的数据基础。
- 无流式（§5.1 硬不变量）：生成完整 → 一次性返回 JSON。
- **不调阿里云内容安全**：创作产出交给大模型自身合规；阿里云只审用户输入/录音。

模块级别名 chat / retrieve_contents 是测试 monkeypatch 目标
（app.skills.script_gen.graph.chat / .retrieve_contents）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

log = logging.getLogger(__name__)

from app.llm.client import glm_client
from app.rag.retrieve import load_contents_by_ids as _load_contents_by_ids
from app.rag.retrieve import retrieve_contents as _retrieve_contents
from app.skills.profile_fields import render_profile

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat
retrieve_contents = _retrieve_contents
load_contents_by_ids = _load_contents_by_ids


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
    framework: str | None
    generation_group_id: int | None
    preset_cited_ids: list[int]
    contents: list[Any]
    cited_content_ids: list[int]
    cited_card_ids: list[int]
    script: dict[str, Any]


# ---- prompt 构建 -----------------------------------------------------------

def _build_messages(state: ScriptGenState) -> list[dict[str, str]]:
    """构造生成 prompt：注入 A 层全量（定位档案）+ B 层命中卡 + 选题 + 平台。"""
    topic = state["topic"]
    profile = state["profile"]
    platform = state["platform"]
    duration_label = {"45": "45 秒口播", "90": "90 秒", "180": "3 分钟深度", "300": "5 分钟"}.get(
        state.get("duration", "45"), "45 秒口播"
    )
    contents = state.get("contents", [])
    framework = (state.get("framework") or "").strip()
    platform_hint = {
        "douyin": "抖音口播：前 3 秒强钩子，口语短句，适合竖屏停驻。",
        "channels": "视频号口播：语气更稳，适合微信生态转发，结尾引导更克制。",
    }.get(platform, "")

    profile_text = render_profile(profile)
    if contents:
        contents_text = "\n".join(
            f"- [{c.source}] {c.title}: {(c.body or '')[:400]}"
            for c in contents
        )
    else:
        contents_text = "（知识库没有相关内容，本稿只基于定位档案）"

    framework_text = framework or "默认口播结构：钩子 → 冲突/干货 → 收尾引导"
    system = (
        "你是一名专业口播视频文案创作者。根据选题、定位档案和用户自己写过的相关内容，"
        "生成一段口播文案。文案分为三段：hook（开场钩子）、body（正文内容）、cta（结尾引导）。"
        "每段由若干句子组成，每句需有 idx（从 0 开始）和 text（单句文本）。"
        "内容须符合平台调性，口吻与定位档案一致。参考内容按篇使用，不要编造库里没有的事实。"
    )
    user = (
        f"平台: {platform}\n"
        f"平台要求: {platform_hint}\n"
        f"目标时长: {duration_label}（按此时长控制篇幅与结构）\n"
        f"结构框架: {framework_text}\n"
        f"选题: {topic.get('title', '')}\n"
        f"选题理由: {topic.get('rationale', '')}\n"
        f"定位档案:\n{profile_text}\n"
        f"知识库里相关的内容（整篇）:\n{contents_text}\n\n"
        "请生成三段文案（hook/body/cta），每段为句子数组。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---- LangGraph nodes -------------------------------------------------------

async def _retrieve_node(state: ScriptGenState) -> dict[str, Any]:
    """召回整篇内容 top 2–3（user_id 隔离在 retrieve_contents SQL 内保证）。

    懒生成传入 preset_cited_ids 时复用首版引用快照，不再检索。
    RAG 检索是 best-effort：embed/DB 不可达时返回空列表，不阻断生成。
    """
    preset = [i for i in (state.get("preset_cited_ids") or []) if isinstance(i, int)]
    try:
        if preset:
            contents = await load_contents_by_ids(state["user_id"], preset)
        else:
            contents = await retrieve_contents(
                state["user_id"],
                state["topic"].get("title", ""),
                platform=state.get("platform"),
                k=3,
            )
    except Exception:  # noqa: BLE001 — RAG 降级，不阻断生成
        log.warning("retrieve_contents failed, generating without RAG context", exc_info=True)
        contents = []
    return {"contents": contents, "cited_content_ids": [c.id for c in contents], "cited_card_ids": []}


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
    framework: str | None = None,
    generation_group_id: int | None = None,
    cited_content_ids: list[int] | None = None,
) -> dict[str, Any]:
    """文案生成入口：retrieve → generate → 返回三段 + cited_content_ids。"""
    initial: ScriptGenState = {
        "user_id": user_id,
        "topic": topic,
        "profile": profile,
        "platform": platform,
        "duration": duration,
        "framework": framework,
        "generation_group_id": generation_group_id,
        "preset_cited_ids": cited_content_ids or [],
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
        "cited_content_ids": result.get("cited_content_ids", []),
        "cited_card_ids": result.get("cited_card_ids", []),
    }
