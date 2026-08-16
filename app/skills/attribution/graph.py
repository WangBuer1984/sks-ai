"""归因 skill：单条归因 + 周卡。

设计文档 §5 + §4.4（复盘状态机：归因是「看归因」动作，FREE 不扣费）。

- skill=`attribution` 走 glm-4.7 thinking **off**（MODEL_FOR["attribution"]）。原 thinking on
  与结构化输出冲突（FC→1210、json_schema→散文、json_mode→形状不可控），已关；不变量见
  GLMClient.chat 的 thinking 降级 guard + test_llm_models.test_structured_skills_must_not_think。
- 无流式：生成完整 → 一次性返回 JSON。
- **不调阿里云内容安全**：创作/归纳产出交给大模型自身合规；阿里云只审用户输入/录音。
- 归因是 review aid，FREE（PRD：flop「看归因」不扣费；周归因是定时聚合，非用户扣费）。
- **无 DB**：skill 不查库。单条归因读取请求体传入的 script/play_count/baseline；
  周归因读取 Java 组装好的 scripts 数组。本 skill 仅做 LLM 归纳。

模块级别名 chat 是测试 monkeypatch 目标。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.llm.client import glm_client

log = logging.getLogger(__name__)

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat


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
        "tone_suggestion": {
            "type": "string",
            "description": "若诊断认为口吻该改，给出一句可写入档案的新口吻；否则空串",
        },
        "redlines_suggestion": {
            "type": "string",
            "description": "若诊断认为红线该改，给出一句可写入档案的新红线；否则空串",
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
        "若口吻或红线需要改，另给 tone_suggestion / redlines_suggestion（可写入档案的短句）；不需要改则留空。"
        "不得把建议写成已经改过档案。"
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


# ---- entrypoint ------------------------------------------------------------

async def attribution_single(script: str, play_count: int, baseline: float) -> dict[str, Any]:
    """单条归因：生成 → 返回。不做阿里云内容安全。

    返回: {diagnosis: str, suggestions: list[str]}
    """
    messages = _build_single_messages(script, play_count, baseline)
    result = await chat("attribution", messages, json_schema=ATTRIBUTION_SINGLE_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    return {
        "diagnosis": result.get("diagnosis", ""),
        "suggestions": list(result.get("suggestions", []) or []),
        "tone_suggestion": (result.get("tone_suggestion") or "") or None,
        "redlines_suggestion": (result.get("redlines_suggestion") or "") or None,
    }


async def attribution_weekly(user_id: int, scripts: list[dict[str, Any]]) -> dict[str, Any]:
    """周归因卡：生成 → 返回。不做阿里云内容安全。

    返回: {summary, wins:[...], gaps:[...], next_focus}
    """
    messages = _build_weekly_messages(user_id, scripts)
    result = await chat("attribution", messages, json_schema=WEEKLY_SCHEMA)
    if not isinstance(result, dict):
        result = {}
    return {
        "summary": result.get("summary", ""),
        "wins": list(result.get("wins", []) or []),
        "gaps": list(result.get("gaps", []) or []),
        "next_focus": result.get("next_focus", ""),
    }
