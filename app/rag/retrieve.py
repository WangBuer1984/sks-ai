"""RAG 检索：从 kb_card B 层召回用户相关的 top-k 卡片。

数据泄漏防线：SQL 必须带 user_id 过滤——漏掉会导致用户 A 召回用户 B 的 B 层卡，
注入 prompt 并写进 card_citation → 跨用户数据泄漏事故。照抄勿删。

距离方向注意：pgvector `<=>` 返回余弦「距离」（0=相同, 2=相反），不是相似度。
`<= 0.25` = 相似度 >= 0.75。方向别写反——最常见的 RAG 翻车点。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from app.db import get_pool
from app.rag.embedding import embed


@dataclass
class Card:
    """B 层知识卡片的检索结果。content 为 JSONB 原始 dict（注入 prompt 用）。"""

    id: int
    card_type: str
    title: str
    content: dict[str, Any]


async def retrieve_b_cards(
    user_id: int,
    query: str,
    k: int = 5,
    max_distance: float = 0.25,
) -> list[Card]:
    """召回用户 user_id 的 B 层 top-k 卡片。

    SQL 全量过滤条件（照抄勿删）：
      - user_id = $1       ← 跨用户隔离，漏掉 = 数据泄漏
      - layer = 'B'        ← 只取 B 层（A 层是定位档案全量注入，C 层是归因，非检索目标）
      - deleted = false    ← 软删卡不召回
      - (embedding <=> $query_vec) <= 0.25  ← cosine DISTANCE <= 0.25 (similarity >= 0.75)
      - ORDER BY embedding <=> $query_vec  ← 按距离升序（近→远）
      - LIMIT 5            ← top-5

    query_vec 来自 embed(query)——与 kb_card.embedding 同模型同维度（embedding-3, 1024）。
    """
    query_vec = await embed(query)
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, card_type, title, content
        FROM kb_card
        WHERE user_id = $1 AND layer = 'B' AND deleted = false
          AND (embedding <=> $2) <= $3
        ORDER BY embedding <=> $2
        LIMIT $4
        """,
        user_id,
        query_vec,
        max_distance,
        k,
    )
    cards: list[Card] = []
    for r in rows:
        raw = r["content"]
        if isinstance(raw, str):
            content = json.loads(raw)
        elif isinstance(raw, dict):
            content = raw
        else:
            content = {}
        cards.append(Card(
            id=r["id"],
            card_type=r["card_type"],
            title=r["title"],
            content=content,
        ))
    return cards
