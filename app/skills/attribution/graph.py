"""归因 skill：单条归因 + 周卡。

设计文档 §5 + §4.4（复盘状态机：归因是「看归因」动作，FREE 不扣费）+
§5.1（LLM 用户可见产出先审后展示——硬不变量）。

- skill=`attribution` 走 glm-4.7 thinking **on**（MODEL_FOR["attribution"]，
  档位规则：深度归纳/归因 GLM-4.7 thinking on）。业务代码不感知型号。
- 无流式（硬不变量）：生成完整 → 内容安全过审 → 一次性返回 JSON。
- 归因是 review aid，FREE（PRD：flop「看归因」不扣费；周归因是定时聚合，非用户扣费）。
- **无 DB**：skill 不查库。单条归因读取请求体传入的 script/play_count/baseline；
  周归因读取 Java 组装好的 scripts 数组（含 play_count/review_state 等）。
  Java 拥有近 30 天均值计算与 scripts 组装；本 skill 仅做 LLM 归纳。

线性流程（生成 → 过审 → 返回/blocked），无 script_gen 的「命中→重写」分支，
故不引入 LangGraph——一个 async 函数即可（与 card_gen 同口径）。
模块级别名 chat / check 是测试 monkeypatch 目标
（app.skills.attribution.graph.chat / .check），与 script_gen / card_gen 同模式。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.llm.client import glm_client
from app.safety.content_safety import check as _check

log = logging.getLogger(__name__)

# 模块级别名——测试 monkeypatch 目标（app.skills.attribution.graph.chat / .check）
chat = glm_client.chat
check = _check


# ---- 结构化输出 schema ------------------------------------------------------

ATTRIBUTION_SINGLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "diagnosis": {
            "type": "string",
            "description": "诊断：为什么这条文案表现如此（hook/正文/CTA/选题 等原因分析）",
        },
        "suggestions": {
            "type": "array",
            "items": {"type": "string", "description": "可执行的改进建议（每条一句话）"},
        },
    },
    "required": ["diagnosis", "suggestions"],
}

WEEKLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "周总览：本周整体表现（相对均值、爆款/flop 分布、关键趋势）",
        },
        "wins": {
            "type": "array",
            "items": {"type": "string", "description": "做对的事（爆款共性、有效动作）"},
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string", "description": "待改进点（flop 共性、薄弱环节）"},
        },
        "next_focus": {
            "type": "string",
            "description": "下周重点（一两条可执行的方向）",
        },
    },
    "required": ["summary", "wins", "gaps", "next_focus"],
}


# ---- prompt 构建 -----------------------------------------------------------

def _build_single_messages(script: str, play_count: int, baseline: float) -> list[dict[str, str]]:
    """单条归因 prompt：注入文案 + 实际播放 + 该用户近 30 天均值（baseline 由 Java 计算）。"""
    system = (
        "你是一名口播视频复盘归因师。给定一条已发布文案的文本、实际播放量与该账号近 30 天均值，"
        "请诊断这条文案表现（爆款 / 平稳 / flop）的原因，并给出可执行的改进建议。"
        "诊断需结合 hook（开场钩子）、正文结构/信息密度、CTA、选题与定位匹配度等维度。"
        "不得包含违禁内容。"
    )
    ratio = (play_count / baseline) if baseline > 0 else 0.0
    user = (
        f"文案文本：\n{script or '（空）'}\n\n"
        f"实际播放量：{play_count}\n"
        f"该账号近 30 天均值（baseline）：{baseline}\n"
        f"相对均值倍数：{ratio:.2f}x\n\n"
        "请输出 diagnosis（诊断）+ suggestions（改进建议列表）。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_weekly_messages(user_id: int, scripts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """周归因 prompt：注入本周 scripts（含 play_count/review_state），给出归纳卡。"""
    system = (
        "你是一名口播视频周度归因师。给定一名创作者本周已发布文案及其表现数据，"
        "请归纳周总览、做对的事（wins）、待改进点（gaps）、下周重点（next_focus）。"
        "归因需找出爆款/flop 的共性模式（钩子风格、选题方向、发布时段、正文结构等），"
        "给出可执行的下周方向。不得包含违禁内容。"
    )
    if scripts:
        lines = []
        for i, s in enumerate(scripts, 1):
            text = (s.get("script") or s.get("text") or "").strip()
            if len(text) > 400:
                text = text[:400] + "…"
            lines.append(
                f"- [{i}] review_state={s.get('review_state', 'unknown')} "
                f"play_count={s.get('play_count', 0)} baseline={s.get('baseline', 'N/A')} "
                f"文案：{text}"
            )
        scripts_text = "\n".join(lines)
        states = [s.get("review_state") for s in scripts]
        hot_n = states.count("hot")
        plain_n = states.count("plain")
        flop_n = states.count("flop")
        spread = f"本周分布：hot={hot_n} plain={plain_n} flop={flop_n} 共 {len(scripts)} 条"
    else:
        scripts_text = "（本周无发布数据）"
        spread = "本周无发布数据"

    user = (
        f"创作者 user_id：{user_id}\n"
        f"{spread}\n\n"
        f"本周文案与表现：\n{scripts_text}\n\n"
        "请输出 summary（周总览）+ wins（做对的列表）+ gaps（待改进列表）+ next_focus（下周重点）。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---- 文本扁平化（用于内容安全检查）-----------------------------------------

def _single_to_text(result: dict[str, Any]) -> str:
    """单条归因产出 → 纯文本（diagnosis + 所有 suggestions）。"""
    parts: list[str] = []
    diag = result.get("diagnosis", "")
    if diag:
        parts.append(str(diag))
    for s in result.get("suggestions", []) or []:
        if s:
            parts.append(str(s))
    return " ".join(parts)


def _weekly_to_text(result: dict[str, Any]) -> str:
    """周卡产出 → 纯文本（summary + wins + gaps + next_focus 全部拼接）。"""
    parts: list[str] = []
    for key in ("summary", "next_focus"):
        v = result.get(key, "")
        if v:
            parts.append(str(v))
    for key in ("wins", "gaps"):
        for item in result.get(key, []) or []:
            if item:
                parts.append(str(item))
    return " ".join(parts)


# ---- entrypoint ------------------------------------------------------------

async def attribution_single(script: str, play_count: int, baseline: float) -> dict[str, Any]:
    """单条归因：生成 → 过审 → 返回 / blocked。

    参数:
      script: 文案文本（caller 传它持有的形式——全文本或 hook+body+cta 拼接，本处一律当文本）
      play_count: 实际播放量
      baseline: 该用户近 30 天均值（Java 计算 owns）

    返回:
      - 成功: {diagnosis: str, suggestions: list[str]}
      - blocked: {blocked: true}  （LLM 产出命中安全，不返回 unsafe 文本）
    """
    messages = _build_single_messages(script, play_count, baseline)
    result = await chat("attribution", messages, json_schema=ATTRIBUTION_SINGLE_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    # 硬不变量：LLM 用户可见产出过审后展示
    if not await check(_single_to_text(result)):
        return {"blocked": True}
    return {
        "diagnosis": result.get("diagnosis", ""),
        "suggestions": list(result.get("suggestions", []) or []),
    }


async def attribution_weekly(user_id: int, scripts: list[dict[str, Any]]) -> dict[str, Any]:
    """周归因卡：生成 → 过审 → 返回 / blocked。

    参数:
      user_id: 创作者 ID（Java 传入真实 uid；skill 不鉴权——X-Service-Token + 内网是信任边界）
      scripts: 本周 scripts（Java 组装，含 play_count/review_state/baseline/script 等）

    返回:
      - 成功: {summary, wins:[...], gaps:[...], next_focus}
      - blocked: {blocked: true}
    """
    messages = _build_weekly_messages(user_id, scripts)
    result = await chat("attribution", messages, json_schema=WEEKLY_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    if not await check(_weekly_to_text(result)):
        return {"blocked": True}
    return {
        "summary": result.get("summary", ""),
        "wins": list(result.get("wins", []) or []),
        "gaps": list(result.get("gaps", []) or []),
        "next_focus": result.get("next_focus", ""),
    }
