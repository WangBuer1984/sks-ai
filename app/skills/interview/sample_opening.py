"""样例开头（试试效果对比块）——读 checkpoint profile，一个 prompt 产「无档案/有档案」两版开场钩子。

不过阿里云 safetyCheck（与 interview summarize 一致）。一次 chat() 调用绑单 schema，
故两版 hook 放进一个 schema 的 `without`/`with` 两字段一次产出（照 SUMMARIZE_SCHEMA 双字段模式）。
"""
import json
from typing import Any

from app.llm.client import glm_client
from app.skills.interview.graph import _graph

chat = glm_client.chat  # 测试 monkeypatch 目标：app.skills.interview.sample_opening.chat

DEFAULT_TOPIC = "报价为什么差一倍"

SAMPLE_OPENING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "without": {"type": "string", "description": "无档案版开场钩子（通用口吻，一句话）"},
        "with": {"type": "string", "description": "有档案版开场钩子（严格按定位档案口吻，一句话）"},
    },
    "required": ["without", "with"],
}


def _build_messages(profile: dict[str, Any], topic: str) -> list[dict[str, str]]:
    system = (
        "你是一名口播视频开场钩子撰写师。给定一个选题，写两版开场钩子（各一两句）："
        "without 版「无档案版」——不带入任何个人定位，写一句谁都能用的通用开头；"
        "with 版「有档案版」——严格按给定的定位档案口吻写一句开头，凸显该人设的差异化。"
        "两版都只输出钩子本身，不要解释、不要引号。"
    )
    user = (
        f"选题：{topic}\n"
        f"定位档案：{json.dumps(profile, ensure_ascii=False)}\n"
        "请按 schema 返回 without（无档案版）与 with（有档案版）。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def sample_opening(thread_id: str, topic: str | None) -> dict[str, Any] | None:
    """读 checkpoint profile → 一个 prompt 产两版 hook。无 checkpoint/profile → None。"""
    config = {"configurable": {"thread_id": thread_id}}
    sv = await _graph.aget_state(config)
    if not sv or not sv.values:
        return None
    raw = sv.values.get("profile")  # {profile:{人设,...}, a_cards:[...]} 整块
    if not isinstance(raw, dict):
        return None
    inner = raw.get("profile")  # {人设,人群,差异化,变现,红线,支柱配比}
    if not isinstance(inner, dict) or not inner:
        return None
    t = topic or DEFAULT_TOPIC
    messages = _build_messages(inner, t)
    result = await chat("interview", messages, json_schema=SAMPLE_OPENING_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    return {
        "topic": t,
        "without": str(result.get("without", "") or ""),
        "with": str(result.get("with", "") or ""),
    }
