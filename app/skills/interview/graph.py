"""定位访谈 LangGraph 状态机：guess_persona → ask(多轮) → summarize → END。

设计文档 §5 + PRD §5.2（校准第一步贴素材 → AI 先猜一版人设）+ §11.4（断点续答）。
- skill=`interview` 走 glm-4.5-air thinking off（MODEL_FOR["interview"]；4.7 单轮易超时，见 models.py）。
- 多轮一问一答，每 `/step` 一次请求返回一次 JSON（无流式——硬不变量）。
- AsyncPostgresSaver（生产）/ MemorySaver（测试）持久化检查点，
  thread_id=f"{user_id}:{session_id}"，同 thread_id + 新请求从 checkpoint 恢复。
  生产 main.py 启动调 set_checkpointer 注入 AsyncPostgresSaver 并 setup() 自建检查点表
  （LangGraph 私有，迁移例外）。
- UGC（materials/user_reply）过 safety.check 后才推进；命中返回 {blocked:true} 不推进。
- LLM 产出（问题/档案）**不调**阿里云内容安全——创作链路交给大模型自身合规。

**节点拆分（保证 interrupt 确定性）：** 每轮拆成「生成节点」（chat，写 state，
无 interrupt）+「应答节点」（读 state、interrupt 等用户输入）。生成节点完成后 state
已落 checkpoint，应答节点 re-exec 时读到的 current_question 稳定，
interrupt 路径确定性可重放。

模块级别名 chat / check 是测试 monkeypatch 目标（check 仅审 UGC）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from app.llm.client import glm_client
from app.safety.content_safety import check as _check
from app.skills.interview.state import InterviewState

log = logging.getLogger(__name__)

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat
check = _check

# 提问轮数（PRD/设计文档 §5：5-8 轮收敛；MVP 取下界 5）
MAX_ROUNDS = 5


# ---- 结构化输出 schema ----------------------------------------------------

_GUESS_SCHEMA: dict[str, Any] = {
    "title": "guess_persona",
    "type": "object",
    "properties": {
        "persona": {
            "type": "object",
            "description": "AI 猜出的人设草稿（自由结构 JSON）",
            "additionalProperties": True,
        },
        "question": {
            "type": "string",
            "description": "给用户的反馈问题（确认/调整人设）",
        },
    },
    "required": ["persona", "question"],
}

_QUESTION_SCHEMA: dict[str, Any] = {
    "title": "interview_question",
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "本轮访谈问题（简短、聚焦一个维度）"},
    },
    "required": ["question"],
}

SUMMARIZE_SCHEMA: dict[str, Any] = {
    "title": "summarize_profile",
    "type": "object",
    "properties": {
        "profile": {
            "type": "object",
            "properties": {
                "人设": {"type": "string"},
                "人群": {"type": "string"},
                "差异化": {"type": "string"},
                "变现": {"type": "string"},
                "红线": {"type": "string"},
                "支柱配比": {"type": "string"},
            },
            "required": ["人设", "人群", "差异化", "变现", "红线", "支柱配比"],
        },
        "a_cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "card_type": {"type": "string", "description": "A 层卡片分类（定位/人设）"},
                    "title": {"type": "string"},
                    "content": {"type": "object", "additionalProperties": True},
                },
                "required": ["card_type", "title", "content"],
            },
        },
    },
    "required": ["profile", "a_cards"],
}


# ---- prompt 构建 -----------------------------------------------------------

def _build_guess_messages(materials: str) -> list[dict[str, str]]:
    system = (
        "你是一名口播视频定位访谈官。用户会贴一段素材（主页说明/过往文案/朋友圈长文，纯文本拼接），"
        "请你据此猜一版人设草稿，并给用户一个反馈问题（确认或调整）。"
        "素材为空时按行业/身份冷启动猜。不得包含违禁内容。"
    )
    user = f"素材：\n{materials or '（空，冷启动）'}\n\n请给出人设草稿 + 反馈问题。"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_ask_messages(
    persona: dict[str, Any], feedback: str, answers: list[str]
) -> list[dict[str, str]]:
    system = (
        "你是一名口播视频定位访谈官。基于人设草稿与已有回答，提出下一个聚焦问题，"
        "每轮只问一个维度（人群/差异化/变现/红线/支柱配比 等），简短可答。"
        "不得包含违禁内容。"
    )
    ans_text = "\n".join(f"- {a}" for a in answers) if answers else "（尚无回答）"
    user = (
        f"人设草稿：{json.dumps(persona, ensure_ascii=False)}\n"
        f"用户对人设的反馈：{feedback or '（无）'}\n"
        f"已有回答：\n{ans_text}\n\n请提下一个问题。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_summarize_messages(
    persona: dict[str, Any], feedback: str, answers: list[str]
) -> list[dict[str, str]]:
    system = (
        "你是一名口播视频定位归纳师。基于人设草稿、用户反馈与访谈回答，归纳最终定位档案 "
        "（人设/人群/差异化/变现/红线/支柱配比）+ A 层卡草稿（定位/人设）。"
        "不得包含违禁内容。"
    )
    ans_text = "\n".join(f"- {a}" for a in answers) if answers else "（无回答）"
    user = (
        f"人设草稿：{json.dumps(persona, ensure_ascii=False)}\n"
        f"用户反馈：{feedback or '（无）'}\n"
        f"访谈回答：\n{ans_text}\n\n请归纳档案 + A 层卡草稿。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---- LangGraph nodes（生成/应答拆分保证 interrupt 确定性）-------------------

async def _guess_generate(state: InterviewState) -> dict[str, Any]:
    """生成人设草稿 + 反馈问题，写 state（无 interrupt）。不做阿里云过审。"""
    messages = _build_guess_messages(state.get("materials", ""))
    result = await chat("interview", messages, json_schema=_GUESS_SCHEMA)
    persona = result.get("persona", {}) if isinstance(result, dict) else {}
    question = result.get("question", "") if isinstance(result, dict) else ""
    # current_safe 恒 True：创作产出不再走阿里云；保留字段以兼容既有 interrupt 重放路径。
    return {"persona": persona, "current_question": question, "current_safe": True}


async def _guess_feedback(state: InterviewState) -> dict[str, Any]:
    """interrupt 等用户确认/调整人设。"""
    question = state.get("current_question", "")
    feedback = interrupt({
        "stage": "await_feedback",
        "question": question,
        "persona": state.get("persona", {}),
    })
    return {"feedback": feedback or ""}


async def _ask_generate(state: InterviewState) -> dict[str, Any]:
    """生成本轮问题，写 state（无 interrupt）。不做阿里云过审。"""
    messages = _build_ask_messages(
        state.get("persona", {}), state.get("feedback", ""), state.get("answers", [])
    )
    result = await chat("interview", messages, json_schema=_QUESTION_SCHEMA)
    question = result.get("question", "") if isinstance(result, dict) else ""
    return {"current_question": question, "current_safe": True}


async def _ask_answer(state: InterviewState) -> dict[str, Any]:
    """interrupt 等本轮回答。"""
    answers = list(state.get("answers", []))
    question = state.get("current_question", "")
    answer = interrupt({"stage": "ask", "question": question})
    answers.append(answer or "")
    return {"answers": answers}


async def _summarize(state: InterviewState) -> dict[str, Any]:
    """归纳最终档案 + A 层卡草稿。不做阿里云过审。"""
    messages = _build_summarize_messages(
        state.get("persona", {}), state.get("feedback", ""), state.get("answers", [])
    )
    result = await chat("interview", messages, json_schema=SUMMARIZE_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    return {"profile": result}


# ---- 路由 ------------------------------------------------------------------

def _ask_router(state: InterviewState) -> str:
    """ask_answer 后：answers 未满 MAX_ROUNDS → 下一轮；满 → summarize。"""
    if len(state.get("answers", [])) < MAX_ROUNDS:
        return "ask_generate"
    return "summarize"


# ---- graph build -----------------------------------------------------------

def build_graph(saver: Any) -> Any:
    """编译状态机。saver 为 checkpointer。

    guess_generate → guess_feedback → ask_generate → ask_answer
    → [ask_generate（下一轮）| summarize] → END
    """
    g: StateGraph = StateGraph(InterviewState)
    g.add_node("guess_generate", _guess_generate)
    g.add_node("guess_feedback", _guess_feedback)
    g.add_node("ask_generate", _ask_generate)
    g.add_node("ask_answer", _ask_answer)
    g.add_node("summarize", _summarize)
    g.set_entry_point("guess_generate")
    g.add_edge("guess_generate", "guess_feedback")
    g.add_edge("guess_feedback", "ask_generate")
    g.add_edge("ask_generate", "ask_answer")
    g.add_conditional_edges(
        "ask_answer",
        _ask_router,
        {"ask_generate": "ask_generate", "summarize": "summarize"},
    )
    g.add_edge("summarize", END)
    return g.compile(checkpointer=saver)


# 默认 MemorySaver（测试用）；生产 main.py lifespan 调 set_checkpointer 注入 AsyncPostgresSaver
checkpointer: Any = MemorySaver()
_graph = build_graph(checkpointer)


def set_checkpointer(saver: Any) -> None:
    """替换 checkpointer 并重编译图（生产启动注入 PostgresSaver；测试隔离用）。"""
    global _graph, checkpointer
    checkpointer = saver
    _graph = build_graph(saver)


# ---- response 构建 --------------------------------------------------------

def _interrupt_value(sv: Any) -> dict[str, Any] | None:
    """从 state view 提取当前 interrupt 值（若有）。"""
    if not sv or not getattr(sv, "tasks", None):
        return None
    for t in sv.tasks:
        for iv in getattr(t, "interrupts", ()) or ():
            val = getattr(iv, "value", iv)
            if isinstance(val, dict):
                return val
    return None


def _build_response(sv: Any) -> dict[str, Any]:
    """把 graph state view 翻译成 /step 响应。"""
    if not sv or not sv.values:
        return {"stage": "guess_persona", "done": False}

    vals = sv.values
    # summarize 阶段 LLM 产出命中安全
    if vals.get("blocked"):
        return {"blocked": True}

    iv = _interrupt_value(sv)
    if iv is not None:
        if iv.get("blocked"):
            return {"blocked": True}
        return {
            "stage": iv.get("stage", "ask"),
            "question": iv.get("question"),
            "done": False,
        }

    # 无 interrupt → 图跑完（summarize 完成）
    profile = vals.get("profile")
    if profile is not None:
        return {"stage": "summarize", "profile_draft": profile, "done": True}
    return {"stage": "summarize", "done": True}


# ---- entrypoint -----------------------------------------------------------

async def interview_step(
    user_id: int,
    session_id: str,
    user_reply: str | None = None,
    materials: str | None = None,
) -> dict[str, Any]:
    """单步推进访谈状态机。一次请求一次 JSON 返回（无流式）。

    首次调用（无 checkpoint）传 materials → guess_generate 猜人设。
    后续调用传 user_reply → 从 checkpoint 恢复并推进一轮。
    UGC（materials/user_reply）命中安全 → {blocked:true}，不推进。
    LLM 产出不做阿里云内容安全。

    幂等终态约定：图跑完（END）后再次 /step 必须幂等返回同一终态响应，
    绝不在同 thread_id 上重启访谈。
    """
    thread_id = f"{user_id}:{session_id}"
    config = {"configurable": {"thread_id": thread_id}}

    # UGC 安全先过审（§5.1）——命中则不推进状态机
    if materials:
        if not await check(materials):
            return {"blocked": True}
    if user_reply:
        if not await check(user_reply):
            return {"blocked": True}

    sv = await _graph.aget_state(config)
    has_active = sv is not None and bool(sv.next)

    if has_active:
        # paused at interrupt → resume with user_reply
        await _graph.ainvoke(Command(resume=user_reply), config=config)
    else:
        # 已完成（summarize done，成功或 blocked）→ 幂等返回当前，不再推进
        vals = sv.values if (sv is not None and sv.values) else {}
        if vals.get("profile") is not None or vals.get("blocked"):
            # 成功（profile 产出）或 summarize-blocked（LLM 产出命中安全）：
            # 都是 thread 的终态，重试 /step 必须幂等返回，禁止重启
            return _build_response(sv)
        # 首次调用（无 checkpoint） → guess_generate
        await _graph.ainvoke(
            {"user_id": user_id, "materials": materials or ""}, config=config
        )

    sv = await _graph.aget_state(config)
    return _build_response(sv)


async def fetch_result(thread_id: str) -> dict[str, Any] | None:
    """/ai/interview/result 只读：取最新 checkpoint 的 summarize 产出，不推进状态机。"""
    config = {"configurable": {"thread_id": thread_id}}
    sv = await _graph.aget_state(config)
    if not sv or not sv.values:
        return None
    return sv.values.get("profile")
