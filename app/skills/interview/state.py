"""定位访谈 LangGraph 状态定义。

节点：guess_persona → ask(多轮) → summarize → END（await_confirm 由 Java 侧
confirm 触发 /result 只读取数，不在此状态机内）。

thread_id = f"{user_id}:{session_id}"（graph.py 内构造），同 thread_id + 新请求
从 checkpoint 恢复——天然支持断点续答（PRD §11.4）。

current_question / current_safe 是「生成节点」写、「应答节点」读的中转字段——
拆分两节点保证 interrupt 确定性（见 graph.py 模块 docstring）。必须在本 TypedDict
中声明，否则 LangGraph 会丢弃未声明键（导致应答节点读不到、误走 blocked 分支）。
"""

from __future__ import annotations

from typing import Any, TypedDict


class InterviewState(TypedDict, total=False):
    user_id: int
    materials: str              # 首轮 UGC 素材（贴素材入口，PRD §5.2）
    persona: dict[str, Any]     # guess 产出的人设草稿
    current_question: str       # 本轮生成的问题（生成节点写，应答节点读）
    current_safe: bool          # 本轮问题是否过审（生成节点写，应答节点读）
    feedback: str               # 用户对人设的确认/调整
    answers: list[str]          # ask 阶段累积的回答
    profile: dict[str, Any]     # summarize 产出 {profile:{...}, a_cards:[...]}
    blocked: bool               # LLM 产出命中安全（summarize 阶段）
