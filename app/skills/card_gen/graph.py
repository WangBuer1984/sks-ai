"""补卡 card_gen skill：UGC 安全 → LLM 结构化抽卡 → 缺口检测 → 冲突检测。

设计文档 §5（card_gen 走 glm-4.5-air 轻量档）+ §5.1（UGC raw_text 先过审）+ §7（KB 三层）+
§11.4（覆盖时旧值写 card_history——归档动作在 Java 侧 supplement/confirm 流程内完成）。

无流式（硬不变量）：生成完整 → 安全过审 → 一次性返回 JSON。

本 skill 是线性流程（安全 → 抽卡 → 缺口 → 冲突），无 script_gen 那种「安全命中→重写」的
条件分支，故<b>不</b>引入 LangGraph——一个 async 函数即可，避免为单条路径套图。
模块级别名 chat / check / fetch_existing_cards 是测试 monkeypatch 目标
（app.skills.card_gen.graph.chat / .check / .fetch_existing_cards），与 script_gen 同模式。

语义选择（brief 决策 #3/#4/#5，已文档化）：
- <b>card_type 分类表</b>（#3，MVP 合理选择，非完美 taxonomy）：各层「完整集」——
  A 层 {定位, 人设}；B 层 {产品, 受众, 风格, 场景, 卖点}；C 层 {话术, 钩子}。
  LLM 抽出的 card_type 须落在该集合内（prompt 约束 + 后处理归一）。
- <b>缺口检测</b>（#4）：gaps = 完整集 − 本次抽到的 card_type 集合（本段 raw_text 的覆盖度）。
  选「本次覆盖度」而非「用户整库缺口」：无需 DB、最简可测，且对「这段大白话还差什么」直接作答。
  更丰富的「整库缺口」需查现有卡，留待 V1.1。
- <b>冲突检测</b>（#5）：与用户「现有卡」同 card_type 且标题重叠（大小写不敏感子串）→ 冲突。
  选标题+card_type 重叠而非 B 层 embedding 相似：A/C 层无 embedding，标题重叠是唯一<b>跨层统一</b>口径；
  B 层 embedding 相似虽更准但需现有卡向量且仅适用 B 层。MVP 简单优先（YAGNI）。
  冲突对象 {card_id, card_index, reason}——card_index 让 Java 侧 confirm 能把新卡映射到要覆盖的旧 card_id。
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from app.db import get_pool
from app.llm.client import glm_client
from app.safety.content_safety import check as _check

log = logging.getLogger(__name__)

# 模块级别名——测试 monkeypatch 目标
chat = glm_client.chat
check = _check


# ---- card_type 完整集（MVP 合理 taxonomy，非完美）---------------------------
# 各层「应覆盖」的分类；LLM 抽出的 card_type 须在此集合内。后续迭代可扩。
CARD_TYPES_BY_LAYER: dict[str, list[str]] = {
    "A": ["定位", "人设"],          # 人物画像
    "B": ["产品", "受众", "风格", "场景", "卖点"],  # 产品/选题知识
    "C": ["话术", "钩子"],          # 金句/素材
}


# ---- 结构化输出 schema ------------------------------------------------------

_CARDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "card_type": {"type": "string", "description": "卡片分类，须为该层完整集内之一"},
                    "title": {"type": "string", "description": "卡片标题（简短，≤20字）"},
                    "content": {
                        "type": "object",
                        "description": "卡片内容（自由结构 JSON，如 {price:'99元'}）",
                        "additionalProperties": True,
                    },
                },
                "required": ["card_type", "title", "content"],
            },
        }
    },
    "required": ["cards"],
}


# ---- LangGraph state（保留 TypedDict 形式与 script_gen 一致，虽无分支）------

class CardGenState(TypedDict, total=False):
    user_id: int
    raw_text: str
    target_layer: str
    cards: list[dict[str, Any]]
    gaps: list[str]
    conflicts: list[dict[str, Any]]
    blocked: bool


# ---- prompt 构建 -----------------------------------------------------------

def _build_messages(state: CardGenState) -> list[dict[str, str]]:
    """构造抽卡 prompt：注入 target_layer 的完整 card_type 集合，约束 LLM 只产出该集合内的分类。"""
    layer = state["target_layer"]
    raw_text = state["raw_text"]
    taxonomy = CARD_TYPES_BY_LAYER.get(layer, [])
    taxonomy_text = "、".join(taxonomy) if taxonomy else "（未知层）"

    system = (
        "你是一名口播视频知识库梳理助手。用户会给一段大白话描述，请把它拆解成结构化知识卡片。"
        f"目标层为 {layer} 层，卡片分类（card_type）必须从以下集合中选择：{taxonomy_text}。"
        "每张卡含 card_type（分类）、title（简短标题，≤20字）、content（自由结构 JSON 对象，"
        "如 {price:'99元', desc:'...'}）。只抽取 raw_text 中明确涉及的内容，不要臆造。"
        "若 raw_text 未覆盖某分类，就不产出该分类的卡。不得包含违禁内容。"
    )
    user = f"用户的大白话：\n{raw_text}\n\n请抽取结构化卡片（仅限上述分类）。"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---- 缺口检测（#4：本次覆盖度）---------------------------------------------

def _detect_gaps(cards: list[dict[str, Any]], layer: str) -> list[str]:
    """gaps = 该层完整集 − 本次抽到的 card_type 集合。"""
    complete = CARD_TYPES_BY_LAYER.get(layer, [])
    present = {c.get("card_type", "") for c in cards}
    return [t for t in complete if t not in present]


# ---- 冲突检测（#5：同 card_type + 标题重叠，跨层统一）----------------------

def _title_overlap(new_title: str, existing_title: str) -> bool:
    """标题重叠：大小写不敏感子串包含（双向——任一包含另一即算重叠，处理「精华」vs「精华液」）。"""
    a = (new_title or "").lower().strip()
    b = (existing_title or "").lower().strip()
    if not a or not b:
        return False
    return a in b or b in a


def _detect_conflicts(
    cards: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """对每张新卡，找首张同 card_type + 标题重叠的现有卡 → 冲突。

    每张新卡至多产生一条冲突（取首个命中），保证 Java confirm 的 card_index→card_id 映射 1:1。
    """
    conflicts: list[dict[str, Any]] = []
    for i, new_card in enumerate(cards):
        new_type = new_card.get("card_type", "")
        new_title = new_card.get("title", "")
        for ex in existing:
            if ex.get("card_type") == new_type and _title_overlap(new_title, ex.get("title", "")):
                conflicts.append({
                    "card_id": ex.get("id"),
                    "card_index": i,
                    "reason": f"与现有卡「{ex.get('title', '')}」同分类「{new_type}」且标题重叠",
                })
                break  # 每张新卡至多一条冲突
    return conflicts


# ---- 现有卡查询（monkeypatch 目标）-----------------------------------------

async def fetch_existing_cards(user_id: int, layer: str) -> list[dict[str, Any]]:
    """查用户某层现有未删卡（id, card_type, title），供冲突检测。

    best-effort：DB 不可达时返回空（不阻断抽卡——缺口/冲突降级为「无现有卡」，用户仍能拿到 cards+gaps）。
    """
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT id, card_type, title FROM kb_card "
            "WHERE user_id = $1 AND layer = $2 AND deleted = false",
            user_id, layer,
        )
        return [{"id": r["id"], "card_type": r["card_type"], "title": r["title"]} for r in rows]
    except Exception:  # noqa: BLE001 — 冲突检测降级，不阻断抽卡
        log.warning("fetch_existing_cards failed, skipping conflict detection", exc_info=True)
        return []


# ---- entrypoint ------------------------------------------------------------

async def generate_cards(user_id: int, raw_text: str, target_layer: str) -> dict[str, Any]:
    """补卡入口：UGC 安全 → LLM 抽卡 → 缺口 → 冲突 → 返回。

    返回:
      - 成功: {cards:[{card_type,title,content}], gaps:[...], conflicts:[{card_id,card_index,reason}]}
      - blocked: {blocked: true}  （raw_text 命中安全）
    """
    # 1. UGC 安全先过审（§5.1）：raw_text 是用户直接输入，必须先检
    safe = await check(raw_text)
    if not safe:
        return {"blocked": True}

    # 2. LLM 结构化抽卡（glm-4.5-air 轻量档，json_schema 约束输出格式）
    state: CardGenState = {
        "user_id": user_id,
        "raw_text": raw_text,
        "target_layer": target_layer,
    }
    messages = _build_messages(state)
    result = await chat("card_gen", messages, json_schema=_CARDS_SCHEMA)
    cards: list[dict[str, Any]] = result.get("cards", []) if isinstance(result, dict) else []

    # 3. 缺口检测（本次 raw_text 覆盖度）
    gaps = _detect_gaps(cards, target_layer)

    # 4. 冲突检测（与用户现有卡同 card_type + 标题重叠）
    existing = await fetch_existing_cards(user_id, target_layer)
    conflicts = _detect_conflicts(cards, existing)

    return {"cards": cards, "gaps": gaps, "conflicts": conflicts}
