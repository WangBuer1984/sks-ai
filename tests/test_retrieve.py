"""RAG retrieve_b_cards 测试。

两部分：
1. SQL 字符串断言（无 DB 依赖，始终运行）——验证 SQL 含 user_id 过滤、
   cosine DISTANCE 方向（<=> <= 0.25，非相似度 >=）、deleted/layer 过滤。
2. 真实 pgvector DB 测试（需运行中的 pgvector 容器）——验证跨用户隔离 + 距离方向。

数据泄漏防线测试：用户 A 的 query 不召回用户 B 的 B 层卡。
距离方向测试：cosine DISTANCE <= 0.25（不是相似度 >= 0.75，不反向）。

testcontainers 在 Colima 环境下无法启动容器（Docker socket 挂载问题），
改为探测运行中的 pgvector/pgvector:pg16 容器。若无容器运行，跳过 DB 测试
（SQL 字符串断言仍保证基本正确性）。
"""

import inspect
import json

import pytest

from app.rag.retrieve import retrieve_b_cards

# ---- SQL 源码检查（始终运行，无 DB 依赖）-----------------------------------

_RETRIEVE_SOURCE = inspect.getsource(retrieve_b_cards)


def test_sql_contains_user_id_filter():
    """数据泄漏防线：SQL 必须含 user_id = $1 过滤。"""
    assert "user_id" in _RETRIEVE_SOURCE, "SQL 缺少 user_id 过滤——跨用户数据泄漏风险"
    assert "user_id = $1" in _RETRIEVE_SOURCE


def test_sql_uses_cosine_distance_operator():
    """距离操作符必须是 <=>（cosine distance），不是相似度。"""
    assert "<=>" in _RETRIEVE_SOURCE, "SQL 必须使用 <=> (cosine distance) 操作符"


def test_sql_distance_direction_is_le_not_ge():
    """距离方向：(embedding <=> $query_vec) <= max_distance。

    distance <= 0.25 = similarity >= 0.75。若写反（>= ），远卡会被召回、近卡被排除。
    """
    # 精确匹配 SQL 谓语——不受 docstring 中 >= 0.75 的干扰
    assert "(embedding <=> $2) <= $3" in _RETRIEVE_SOURCE, (
        "距离方向必须用 <= (distance <= 0.25)；若用 >= 则远卡被召回、近卡被排除"
    )


def test_sql_contains_layer_and_deleted_filters():
    """SQL 必须含 layer = 'B' 和 deleted = false 过滤。"""
    assert "layer" in _RETRIEVE_SOURCE
    assert "deleted" in _RETRIEVE_SOURCE
    assert "'B'" in _RETRIEVE_SOURCE


def test_sql_contains_order_by_distance_and_limit():
    """SQL 必须按距离排序 + LIMIT top-k。"""
    assert "ORDER BY" in _RETRIEVE_SOURCE
    assert "ORDER BY embedding <=> $2" in _RETRIEVE_SOURCE
    assert "LIMIT" in _RETRIEVE_SOURCE


def test_sql_uses_parameterized_query():
    """SQL 必须参数化（$1/$2/$3/$4），不拼接 user_id 或向量。"""
    assert "$1" in _RETRIEVE_SOURCE  # user_id
    assert "$2" in _RETRIEVE_SOURCE  # query_vec
    assert "$3" in _RETRIEVE_SOURCE  # max_distance
    assert "$4" in _RETRIEVE_SOURCE  # k


# ---- 真实 pgvector DB 测试 ------------------------------------------------

_KB_CARD_DDL_NO_EXT = """
CREATE TABLE IF NOT EXISTS kb_card (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  layer CHAR(1) NOT NULL,
  card_type VARCHAR(20) NOT NULL,
  title VARCHAR(100) NOT NULL,
  content JSONB NOT NULL,
  embedding vector(1024),
  deleted BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _find_pgvector_dsn() -> str | None:
    """探测运行中的 pgvector 容器，返回 DSN。无则 None。"""
    try:
        import docker
        client = docker.from_env()
        for c in client.containers.list():
            tags = c.image.tags or []
            if any("pgvector" in t for t in tags):
                ports = c.ports.get("5432/tcp") or []
                if not ports:
                    continue
                host_port = int(ports[0]["HostPort"])
                env = c.attrs.get("Config", {}).get("Env", [])
                user = "postgres"
                password = "postgres"
                dbname = "postgres"
                for e in env:
                    if e.startswith("POSTGRES_USER="):
                        user = e.split("=", 1)[1]
                    elif e.startswith("POSTGRES_PASSWORD="):
                        password = e.split("=", 1)[1]
                    elif e.startswith("POSTGRES_DB="):
                        dbname = e.split("=", 1)[1]
                return f"postgresql://{user}:{password}@localhost:{host_port}/{dbname}"
        return None
    except Exception:  # noqa: BLE001
        return None


@pytest.fixture(scope="module")
def pg_dsn():
    """同步 fixture：探测运行中的 pgvector 容器，返回 DSN 或 skip。"""
    dsn = _find_pgvector_dsn()
    if dsn is None:
        pytest.skip("无运行中的 pgvector 容器——跳过真实 DB 测试（SQL 字符串断言仍通过）")
    return dsn


@pytest.fixture
async def db_pool(pg_dsn):
    """每个测试：创建独立测试库 + 建表 + init pool → yield DSN → 关 pool + 删库。

    用独立数据库避免与生产 schema 的 FK 约束（kb_card → app_user）冲突。
    """
    import uuid

    import asyncpg

    test_db = f"sks_test_{uuid.uuid4().hex[:8]}"
    # 在默认库中创建测试库
    admin_conn = await asyncpg.connect(pg_dsn)
    await admin_conn.execute(f'CREATE DATABASE "{test_db}"')
    await admin_conn.close()
    # 测试库 DSN
    test_dsn = pg_dsn.rsplit("/", 1)[0] + "/" + test_db

    from app.db import close_pool, init_pool
    from pgvector.asyncpg import register_vector

    # 先用直连创建 vector 扩展（pool 的 _init_connection 会调 register_vector，
    # 若扩展不存在会报 'unknown type: public.vector'）
    setup_conn = await asyncpg.connect(test_dsn)
    await setup_conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await setup_conn.close()

    pool = await init_pool(dsn=test_dsn, min_size=1, max_size=5)
    # 建表（IF NOT EXISTS，幂等；无 FK 约束——测试库无 app_user 表）
    async with pool.acquire() as conn:
        await conn.execute(_KB_CARD_DDL_NO_EXT)
    try:
        yield test_dsn
    finally:
        await close_pool()
        # 删测试库
        admin_conn = await asyncpg.connect(pg_dsn)
        await admin_conn.execute(f'DROP DATABASE "{test_db}"')
        await admin_conn.close()


def _make_vec(base_val: float, dim: int = 1024) -> list[float]:
    """构造 1024 维向量。base_val=1.0 → 全 1.0；base_val=-1.0 → 全 -1.0。"""
    return [base_val] * dim


def _make_orthogonal_vec(dim: int = 1024) -> list[float]:
    """构造与全 1.0 向量正交的向量 [1,-1,1,-1,...] → cosine distance=1.0。"""
    return [1.0 if i % 2 == 0 else -1.0 for i in range(dim)]


async def _insert_card(conn, user_id, card_id, embedding, card_type="topic", title="card", deleted=False):
    await conn.execute(
        """INSERT INTO kb_card (id, user_id, layer, card_type, title, content, embedding, deleted)
           VALUES ($1, $2, 'B', $3, $4, $5, $6, $7)""",
        card_id, user_id, card_type, title,
        json.dumps({"text": f"card-{card_id}"}, ensure_ascii=False),
        embedding, deleted,
    )


# ---- 跨用户隔离测试（数据泄漏防线）------------------------------------------

async def test_cross_user_isolation(db_pool, monkeypatch):
    """用户 A 的 query 只召回用户 A 的 B 层卡，不召回用户 B 的。"""
    from app.db import get_pool

    pool = await get_pool()

    async with pool.acquire() as conn:
        await _insert_card(conn, user_id=1, card_id=11, embedding=_make_vec(1.0))
        await _insert_card(conn, user_id=1, card_id=12, embedding=_make_vec(1.0))
        await _insert_card(conn, user_id=2, card_id=21, embedding=_make_vec(1.0))
        await _insert_card(conn, user_id=2, card_id=22, embedding=_make_vec(1.0))

    async def _fake_embed(text, *, client=None):
        return _make_vec(1.0)

    monkeypatch.setattr("app.rag.retrieve.embed", _fake_embed)

    cards = await retrieve_b_cards(user_id=1, query="some query")
    card_ids = [c.id for c in cards]
    assert card_ids == [11, 12], f"跨用户泄漏: 召回了用户 2 的卡 {card_ids}"
    assert all(c.id in (11, 12) for c in cards)


# ---- 距离方向测试 ----------------------------------------------------------

async def test_distance_direction_not_inverted(db_pool, monkeypatch):
    """distance <= 0.25 → 只召回近邻卡；远卡（distance=2.0）不召回。

    若方向写反（>= 0.25 或用了 similarity），远卡会被召回，近卡被排除。
    """
    from app.db import get_pool

    pool = await get_pool()

    async with pool.acquire() as conn:
        # 近邻卡 (distance=0) → 应召回
        await _insert_card(conn, user_id=1, card_id=10, embedding=_make_vec(1.0))
        # 远卡 (distance=2.0, 完全相反) → 不应召回
        await _insert_card(conn, user_id=1, card_id=20, embedding=_make_vec(-1.0))
        # 正交卡 (distance=1.0) → 不应召回
        await _insert_card(conn, user_id=1, card_id=30, embedding=_make_orthogonal_vec())

    async def _fake_embed(text, *, client=None):
        return _make_vec(1.0)

    monkeypatch.setattr("app.rag.retrieve.embed", _fake_embed)

    cards = await retrieve_b_cards(user_id=1, query="some query")
    card_ids = [c.id for c in cards]
    assert 10 in card_ids, "近邻卡（distance=0）应被召回"
    assert 20 not in card_ids, "远卡（distance=2.0）不应被召回——若召回则距离方向写反"
    assert 30 not in card_ids, "正交卡（distance=1.0）不应被召回"


# ---- deleted 卡不召回 ------------------------------------------------------

async def test_deleted_cards_excluded(db_pool, monkeypatch):
    from app.db import get_pool

    pool = await get_pool()

    async with pool.acquire() as conn:
        await _insert_card(conn, user_id=1, card_id=11, embedding=_make_vec(1.0), deleted=False)
        await _insert_card(conn, user_id=1, card_id=12, embedding=_make_vec(1.0), deleted=True)

    async def _fake_embed(text, *, client=None):
        return _make_vec(1.0)

    monkeypatch.setattr("app.rag.retrieve.embed", _fake_embed)

    cards = await retrieve_b_cards(user_id=1, query="some query")
    card_ids = [c.id for c in cards]
    assert card_ids == [11]
    assert 12 not in card_ids


# ---- 非 B 层卡不召回 -------------------------------------------------------

async def test_non_b_layer_cards_excluded(db_pool, monkeypatch):
    from app.db import get_pool

    pool = await get_pool()

    async with pool.acquire() as conn:
        await _insert_card(conn, user_id=1, card_id=11, embedding=_make_vec(1.0))
        # 插入一张 A 层卡
        await conn.execute(
            """INSERT INTO kb_card (id, user_id, layer, card_type, title, content, embedding)
               VALUES ($1, $2, 'A', $3, $4, $5, $6)""",
            12, 1, "profile", "a-card",
            json.dumps({"text": "a-card"}, ensure_ascii=False),
            _make_vec(1.0),
        )

    async def _fake_embed(text, *, client=None):
        return _make_vec(1.0)

    monkeypatch.setattr("app.rag.retrieve.embed", _fake_embed)

    cards = await retrieve_b_cards(user_id=1, query="some query")
    card_ids = [c.id for c in cards]
    assert card_ids == [11]


# ---- k 参数限制返回数量 ----------------------------------------------------

async def test_k_limits_results(db_pool, monkeypatch):
    from app.db import get_pool

    pool = await get_pool()

    async with pool.acquire() as conn:
        for i in range(10):
            await _insert_card(conn, user_id=1, card_id=100 + i, embedding=_make_vec(1.0))

    async def _fake_embed(text, *, client=None):
        return _make_vec(1.0)

    monkeypatch.setattr("app.rag.retrieve.embed", _fake_embed)

    cards = await retrieve_b_cards(user_id=1, query="some query", k=3)
    assert len(cards) == 3
