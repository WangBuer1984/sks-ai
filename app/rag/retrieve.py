"""RAG 检索：从 kb_content 整篇召回，兼容期仍可读 kb_card B 层。

数据泄漏防线：SQL 必须带 user_id 过滤——漏掉会导致用户 A 召回用户 B 的内容，
注入 prompt 并写进 content_reference → 跨用户数据泄漏事故。照抄勿删。

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


@dataclass
class ContentHit:
    """内容底仓的整篇检索结果（不切片）。"""

    id: int
    title: str
    body: str
    source: str
    platform: str | None
    generation_group_id: int | None
    hot: bool


async def retrieve_contents(
    user_id: int,
    query: str,
    platform: str | None = None,
    k: int = 3,
    max_distance: float = 0.25,
) -> list[ContentHit]:
    """召回用户 user_id 的整篇内容 top-k。

    SQL 全量过滤条件（照抄勿删）：
      - user_id = $1
      - deleted = false
      - embedding IS NOT NULL
      - (embedding <=> $2) <= $3
      - ORDER BY embedding <=> $2
      - LIMIT overfetch（$4）后再按 generation_group_id 去重

    同组两个平台版本最多取一个：优先与当前 platform 一致者，其次相关度，接近时爆款优先。
    """
    query_vec = await embed(query)
    pool = await get_pool()
    overfetch = max(k * 4, 8)
    rows = await pool.fetch(
        """
        SELECT c.id, c.title, c.body, c.source, c.platform, c.generation_group_id,
               EXISTS (
                   SELECT 1 FROM publication p
                    WHERE p.content_id = c.id AND p.state = 'hot'
               ) AS hot,
               (c.embedding <=> $2) AS dist
        FROM kb_content c
        WHERE c.user_id = $1 AND c.deleted = false
          AND c.embedding IS NOT NULL
          AND (c.embedding <=> $2) <= $3
        ORDER BY c.embedding <=> $2
        LIMIT $4
        """,
        user_id,
        query_vec,
        max_distance,
        overfetch,
    )
    hits: list[ContentHit] = []
    for r in rows:
        hits.append(
            ContentHit(
                id=r["id"],
                title=r["title"],
                body=r["body"] or "",
                source=r["source"],
                platform=r["platform"],
                generation_group_id=r["generation_group_id"],
                hot=bool(r["hot"]),
            )
        )
    return _dedupe_group(hits, platform, k)


def _dedupe_group(
    hits: list[ContentHit], platform: str | None, k: int
) -> list[ContentHit]:
    """同 generation_group_id 最多留一篇；无组 id 的各算各的。"""
    chosen: list[ContentHit] = []
    seen_groups: set[int] = set()
    by_group: dict[int, list[ContentHit]] = {}
    for h in hits:
        if h.generation_group_id is not None:
            by_group.setdefault(h.generation_group_id, []).append(h)

    def pick(group_hits: list[ContentHit]) -> ContentHit:
        if platform:
            same = [x for x in group_hits if x.platform == platform]
            if same:
                hot = [x for x in same if x.hot]
                return hot[0] if hot else same[0]
        hot = [x for x in group_hits if x.hot]
        return hot[0] if hot else group_hits[0]

    # 按原距离顺序输出：遍历 hits，遇到新组就挑一篇
    for h in hits:
        if h.generation_group_id is None:
            chosen.append(h)
        elif h.generation_group_id not in seen_groups:
            seen_groups.add(h.generation_group_id)
            chosen.append(pick(by_group[h.generation_group_id]))
        if len(chosen) >= k:
            break
    return chosen[:k]


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


async def load_contents_by_ids(user_id: int, ids: list[int]) -> list[ContentHit]:
    """按稳定 id 取整篇内容（懒生成复用首版引用快照）。SQL 必须带 user_id。"""
    if not ids:
        return []
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT c.id, c.title, c.body, c.source, c.platform, c.generation_group_id,
               EXISTS (
                   SELECT 1 FROM publication p
                    WHERE p.content_id = c.id AND p.state = 'hot'
               ) AS hot
        FROM kb_content c
        WHERE c.user_id = $1 AND c.deleted = false AND c.id = ANY($2::bigint[])
        """,
        user_id,
        ids,
    )
    by_id = {
        r["id"]: ContentHit(
            id=r["id"],
            title=r["title"],
            body=r["body"] or "",
            source=r["source"],
            platform=r["platform"],
            generation_group_id=r["generation_group_id"],
            hot=bool(r["hot"]),
        )
        for r in rows
    }
    return [by_id[i] for i in ids if i in by_id]
