"""定位访谈 LangGraph 状态机测试：mock LLM / safety / ASR，绝不发真实网络/DB 请求。

覆盖 brief verbatim resume 用例 + 边界：
- 同一 thread_id 跨多次 /step 请求从 checkpoint 恢复而非重来
- UGC（materials / user_reply）命中安全 → {blocked:true} 不推进状态机
- LLM 产出不过阿里云内容安全（创作链路交给大模型）
- summarize 产出形状 {profile:{...}, a_cards:[...]}
- /ai/interview/result 只读，不推进状态机
- /ai/asr 端点形状 + 错误处理（mock seam）
- /ai/interview/step + /ai/asr 端点鉴权（X-Service-Token）

测试用 MemorySaver（无需 DB）。autouse fixture 每例重置 module-level checkpointer
为全新 MemorySaver，保证 thread_id 隔离（避免 sess-1 跨例污染）。
"""

import pytest

from app.skills.interview.graph import (
    SUMMARIZE_SCHEMA,
    interview_step,
    fetch_result,
)


# ---- 模块 checkpointer 隔离 ------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_memory_checkpointer():
    """每例重置 graph 模块的 checkpointer 为全新 MemorySaver（thread_id 隔离）。

    用 set_checkpointer 重编译图，保证 _graph 真正换用新 saver（编译期绑定）。
    """
    from langgraph.checkpoint.memory import MemorySaver
    from app.skills.interview.graph import set_checkpointer
    set_checkpointer(MemorySaver())


async def _noop_checkpointer():
    """测试用：跳过真实 AsyncPostgresSaver.setup()（避免无 DB 时 30s 超时）。

    TestClient 触发 main.py lifespan，其中 _init_checkpointer 会连真实 Postgres；
    无 DB 的测试环境会卡 30s 超时——所有走 TestClient 的端点测试都 patch 它为 no-op。
    """
    return None


# ---- brief verbatim: 同一 thread_id 跨请求恢复 -----------------------------

@pytest.mark.asyncio
async def test_interview_resumes_from_checkpoint(monkeypatch):
    """brief Step 1 verbatim：同 thread_id 多次 /step，状态从 checkpoint 恢复而非重来。"""
    call_state = {"n": 0}

    async def scripted_chat(skill, messages, json_schema=None, **kwargs):
        call_state["n"] += 1
        if json_schema is SUMMARIZE_SCHEMA:
            return {
                "profile": {
                    "人设": "p", "人群": "a", "差异化": "d",
                    "变现": "m", "红线": "r", "支柱配比": "1:1",
                },
                "a_cards": [{"card_type": "定位", "title": "t", "content": {"x": 1}}],
            }
        # 第 1 次非 schema 调用 = guess_persona；之后 = ask
        if call_state["n"] == 1:
            return {"persona": {"人设": "职场博主"}, "question": "你觉得这个人设对吗？"}
        return {"question": f"第 {call_state['n'] - 1} 轮问题"}

    monkeypatch.setattr("app.skills.interview.graph.chat", scripted_chat)

    async def _safe(_t):
        return True

    monkeypatch.setattr("app.skills.interview.graph.check", _safe)

    s = "sess-1"
    r1 = await interview_step(user_id=1, session_id=s, user_reply=None)   # 猜人设
    assert r1["stage"] == "await_feedback"
    r2 = await interview_step(user_id=1, session_id=s, user_reply="对")   # 进入提问
    assert r2["question"]
    # 用同一 thread_id 再次进入，状态应从 checkpoint 恢复而非重来
    r3 = await interview_step(user_id=1, session_id=s, user_reply="答")
    assert r3["stage"] in {"ask", "summarize"}


# ---- blocked UGC 不推进状态机 ------------------------------------------------

@pytest.mark.asyncio
async def test_blocked_materials_returns_blocked_without_advancing(monkeypatch):
    """materials 命中安全 → {blocked:true}，状态机不推进（thread_id 重来仍从 guess 开始）。"""
    async def _unsafe(_t):
        return False

    monkeypatch.setattr("app.skills.interview.graph.check", _unsafe)

    r = await interview_step(user_id=1, session_id="b1", user_reply=None, materials="违禁素材")
    assert r.get("blocked") is True

    # 重试同一 thread_id（materials 仍违禁）→ 仍 blocked；说明状态机没被推进过
    r2 = await interview_step(user_id=1, session_id="b1", user_reply=None, materials="违禁素材")
    assert r2.get("blocked") is True


@pytest.mark.asyncio
async def test_blocked_user_reply_returns_blocked_without_advancing(monkeypatch):
    """user_reply 命中安全 → {blocked:true}，状态机不推进。"""
    check_n = {"n": 0}

    async def _check(text):
        check_n["n"] += 1
        # call 1 = materials（safe），call 2 = user_reply（unsafe）——LLM 产出不再过审
        return check_n["n"] != 2

    async def _chat(skill, messages, json_schema=None, **kwargs):
        if json_schema is SUMMARIZE_SCHEMA:
            return {"profile": {}, "a_cards": []}
        return {"persona": {"人设": "x"}, "question": "人对吗？"}

    monkeypatch.setattr("app.skills.interview.graph.check", _check)
    monkeypatch.setattr("app.skills.interview.graph.chat", _chat)

    # 首次：materials 安全 → guess_persona 推进 → stage=await_feedback
    r1 = await interview_step(user_id=1, session_id="b2", materials="正常素材")
    assert r1["stage"] == "await_feedback"

    # 第二次：user_reply="违禁" → blocked，状态机不应推进（仍停在 await_feedback）
    r2 = await interview_step(user_id=1, session_id="b2", user_reply="违禁回复")
    assert r2.get("blocked") is True

    # 第三次：user_reply 正常 → 应回到 ask（说明第二次没推进）
    r3 = await interview_step(user_id=1, session_id="b2", user_reply="对")
    assert r3["stage"] in {"ask", "summarize"}



# ---- summarize schema / 形状 -------------------------------------------------

def test_summarize_schema_declares_seven_canonical_fields_and_faq_candidates():
    """schema 是与 Java 的实际契约面（D19/D20）：七个规范键 + FAQ 候选，中文键退场。

    这条不驱动状态机、只读 schema——因为线上真正决定 LLM 输出形状的就是它，
    而形状漂了 Java 那边只会安静地少存几个字段。
    """
    profile = SUMMARIZE_SCHEMA["properties"]["profile"]
    assert tuple(profile["properties"]) == (
        "persona",
        "targetAudience",
        "differentiation",
        "conversionPath",
        "tone",
        "redlines",
        "contentPillars",
    )
    # 七字段**全部** required：「允许为空」由空数组表达，而不是把键变成可省略。
    # 少一个 required，一份缺 redlines/contentPillars 的响应仍是 schema-valid，Java 侧只会安静地少存字段。
    assert profile["required"] == [
        "persona",
        "targetAudience",
        "differentiation",
        "conversionPath",
        "tone",
        "redlines",
        "contentPillars",
    ]
    # 多值字段是数组而不是一段文本——Java 侧 redlines/contentPillars 落 string[]
    assert profile["properties"]["redlines"]["type"] == "array"
    assert profile["properties"]["contentPillars"]["type"] == "array"

    candidates = SUMMARIZE_SCHEMA["properties"]["faq_candidates"]
    assert candidates["type"] == "array"
    assert tuple(candidates["items"]["properties"]) == ("question", "answer")
    assert candidates["items"]["required"] == ["question"], "答案可空：先记问题、答案后补"

    # 根对象同理：faq_candidates 必给，没有候选就给 []（省略与「一条都没提取到」在下游不可区分）
    assert SUMMARIZE_SCHEMA["required"] == ["profile", "faq_candidates"]

    # A/B/C 卡片概念已退场（D1/D5）：不再要求 LLM 产 a_cards
    assert "a_cards" not in SUMMARIZE_SCHEMA["properties"]


@pytest.mark.asyncio
async def test_summarize_shape(monkeypatch):
    """完整流程到 summarize：产出 {profile:{七字段}, faq_candidates:[{question,answer?}]}。"""
    call_n = {"n": 0}

    async def _chat(skill, messages, json_schema=None, **kwargs):
        call_n["n"] += 1
        if json_schema is SUMMARIZE_SCHEMA:
            return {
                "profile": {
                    "persona": "职场博主",
                    "targetAudience": "25-35白领",
                    "differentiation": "反内卷",
                    "conversionPath": "咨询",
                    "tone": "犀利但不刻薄",
                    "redlines": ["不谈政治"],
                    "contentPillars": ["职场避坑", "简历门诊"],
                },
                "faq_candidates": [
                    {"question": "简历怎么写才有面试邀约", "answer": "先对齐 JD 关键词"},
                    {"question": "该不该裸辞"},
                ],
            }
        if call_n["n"] == 1:
            return {"persona": {"人设": "x"}, "question": "人对吗？"}
        return {"question": f"问题 {call_n['n'] - 1}"}

    async def _safe(_t):
        return True

    monkeypatch.setattr("app.skills.interview.graph.chat", _chat)
    monkeypatch.setattr("app.skills.interview.graph.check", _safe)

    sid = "sum-1"
    # 1: guess → await_feedback
    r = await interview_step(user_id=1, session_id=sid, materials="素材")
    assert r["stage"] == "await_feedback"
    # 2: feedback → ask round 1
    r = await interview_step(user_id=1, session_id=sid, user_reply="对")
    assert r["stage"] == "ask"
    # 驱动剩余 ask 轮（N=5）→ 最后一答触发 summarize
    from app.skills.interview.graph import MAX_ROUNDS
    for i in range(MAX_ROUNDS):  # 回答剩余轮次 + 触发 summarize
        r = await interview_step(user_id=1, session_id=sid, user_reply=f"答{i}")
        if r.get("done"):
            break
    assert r["stage"] == "summarize"
    assert r["done"] is True
    pd = r["profile_draft"]
    assert "profile" in pd and "faq_candidates" in pd
    for k in (
        "persona",
        "targetAudience",
        "differentiation",
        "conversionPath",
        "tone",
        "redlines",
        "contentPillars",
    ):
        assert k in pd["profile"]
    assert pd["faq_candidates"][0]["question"] == "简历怎么写才有面试邀约"
    assert "answer" not in pd["faq_candidates"][1], "只记问题的候选照原样返回"


@pytest.mark.asyncio
async def test_summarize_prompt_asks_for_faq_candidates(monkeypatch):
    """候选是从访谈里**提取**的，不是凭空生成的——prompt 必须交代这件事。"""
    from app.skills.interview import graph as g

    messages = g._build_summarize_messages(
        {"人设": "工厂人"}, "基本对", ["常有人问报价为什么差一倍"]
    )
    text = "\n".join(m["content"] for m in messages)
    assert "高频问答" in text
    assert "常有人问报价为什么差一倍" in text


@pytest.mark.asyncio
async def test_summarize_never_writes_shared_db(monkeypatch):
    """候选只回给用户确认，**AI 不写共享库**（D20）：整条 summarize 路径不碰连接池。

    做法是把 `app.db.get_pool` 换成会炸的桩——真有人在这条路上加一句写库，这个测试就红。
    """
    async def _boom(*a, **k):
        raise AssertionError("interview 不得访问共享库")

    monkeypatch.setattr("app.db.get_pool", _boom)
    monkeypatch.setattr("app.db.init_pool", _boom)

    call_n = {"n": 0}

    async def _chat(skill, messages, json_schema=None, **kwargs):
        call_n["n"] += 1
        if json_schema is SUMMARIZE_SCHEMA:
            return {
                "profile": {"persona": "p", "targetAudience": "a", "differentiation": "d",
                            "conversionPath": "c", "tone": "t", "redlines": [], "contentPillars": []},
                "faq_candidates": [{"question": "报价为什么差一倍"}],
            }
        if call_n["n"] == 1:
            return {"persona": {"人设": "x"}, "question": "对吗？"}
        return {"question": "q"}

    async def _safe(_t):
        return True

    monkeypatch.setattr("app.skills.interview.graph.chat", _chat)
    monkeypatch.setattr("app.skills.interview.graph.check", _safe)

    sid = "nodb-1"
    await interview_step(user_id=1, session_id=sid, materials="素材")
    await interview_step(user_id=1, session_id=sid, user_reply="对")
    from app.skills.interview.graph import MAX_ROUNDS
    r = None
    for i in range(MAX_ROUNDS + 2):
        r = await interview_step(user_id=1, session_id=sid, user_reply=f"答{i}")
        if r.get("done"):
            break
    assert r and r["done"] is True
    assert r["profile_draft"]["faq_candidates"][0]["question"] == "报价为什么差一倍"


@pytest.mark.asyncio
async def test_fetch_result_passes_through_legacy_profile(monkeypatch):
    """旧 checkpoint（中文键 + a_cards）原样读出，不报错、不改写。

    映射成规范键的责任在 Java 写档案那一步（`ProfileContent`）——Python 这边硬要"顺手修一下"，
    就会出现两套映射规则各自演化。
    """
    from app.skills.interview import graph as g

    legacy = {
        "profile": {"人设": "美妆成分党", "人群": "25-35 女性", "支柱配比": "5:3:2"},
        "a_cards": [{"card_type": "定位", "title": "人设卡", "content": {"x": 1}}],
    }

    class _SV:
        values = {"profile": legacy}
        next = ()
        tasks = ()

    class _FakeGraph:
        async def aget_state(self, config):
            return _SV()

    monkeypatch.setattr(g, "_graph", _FakeGraph())

    r = await fetch_result(thread_id="1:legacy")
    assert r == legacy, "旧 checkpoint 原样透出（含 a_cards），Java 侧负责投影"





# ---- /ai/interview/result 只读 ---------------------------------------------

@pytest.mark.asyncio
async def test_result_endpoint_read_only(monkeypatch):
    """result 只读取 summarize 产出，不推进状态机。"""
    call_n = {"n": 0}

    async def _chat(skill, messages, json_schema=None, **kwargs):
        call_n["n"] += 1
        if json_schema is SUMMARIZE_SCHEMA:
            return {"profile": {"人设": "x", "人群": "y", "差异化": "z",
                                "变现": "m", "红线": "r", "支柱配比": "1:1"},
                    "a_cards": []}
        if call_n["n"] == 1:
            return {"persona": {"人设": "x"}, "question": "q?"}
        return {"question": "q"}

    async def _safe(_t):
        return True

    monkeypatch.setattr("app.skills.interview.graph.chat", _chat)
    monkeypatch.setattr("app.skills.interview.graph.check", _safe)

    sid = "ro-1"
    # 驱动到 done
    await interview_step(user_id=1, session_id=sid, materials="m")
    await interview_step(user_id=1, session_id=sid, user_reply="对")
    from app.skills.interview.graph import MAX_ROUNDS
    r = None
    for i in range(MAX_ROUNDS + 2):
        r = await interview_step(user_id=1, session_id=sid, user_reply=f"a{i}")
        if r.get("done"):
            break
    assert r and r.get("done")

    # 调 fetch_result（只读）—— 不应推进状态机
    chat_before = call_n["n"]
    result = await fetch_result(thread_id="1:ro-1")
    assert call_n["n"] == chat_before  # 无 LLM 调用 → 未推进
    assert result is not None
    assert "profile" in result and "a_cards" in result


@pytest.mark.asyncio
async def test_result_returns_none_when_no_checkpoint(monkeypatch):
    """无 checkpoint 时 result 返回 None（Java 端可判空）。"""
    r = await fetch_result(thread_id="nonexistent:thread")
    assert r is None


# ---- /ai/asr 端点 -----------------------------------------------------------

def test_asr_success(token, monkeypatch):
    """/ai/asr mock seam 返回文本 → {text}（过审通过）。"""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)

    async def _fake_transcribe(audio_bytes, fmt):
        return "你好世界"

    async def _safe(_t):
        return True

    monkeypatch.setattr("app.api.asr.transcribe_short", _fake_transcribe)
    monkeypatch.setattr("app.api.asr.safety_check", _safe)

    with TestClient(app) as c:
        r = c.post(
            "/ai/asr",
            files={"audio": ("test.wav", b"\x00\x00\x00\x00", "audio/wav")},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json()["text"] == "你好世界"


def test_asr_blocked_by_content_safety(token, monkeypatch):
    """用户录音转写命中安全 → 422 CONTENT_BLOCKED。"""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)

    async def _fake_transcribe(audio_bytes, fmt):
        return "违规转写"

    async def _unsafe(_t):
        return False

    monkeypatch.setattr("app.api.asr.transcribe_short", _fake_transcribe)
    monkeypatch.setattr("app.api.asr.safety_check", _unsafe)

    with TestClient(app) as c:
        r = c.post(
            "/ai/asr",
            files={"audio": ("test.wav", b"\x00\x00", "audio/wav")},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "CONTENT_BLOCKED"


def test_asr_recognition_failure_returns_error(token, monkeypatch):
    """ASR 识别失败 → 502 错误码（Java 提示用户改用文字）。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.datasource.asr import ASRRecognitionError

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)

    async def _fake_transcribe(audio_bytes, fmt):
        raise ASRRecognitionError("asr failed")

    monkeypatch.setattr("app.api.asr.transcribe_short", _fake_transcribe)

    with TestClient(app) as c:
        r = c.post(
            "/ai/asr",
            files={"audio": ("test.wav", b"\x00\x00", "audio/wav")},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "ASR_FAILED"


def test_asr_key_unset_returns_error(token, monkeypatch):
    """ALIYUN_ASR_KEY 未配置 → 503（懒初始化失败，per-request）。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import settings

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)
    monkeypatch.setattr(settings, "ALIYUN_ASR_KEY", "")

    # transcribe_short 未 mock——走真实模块（懒初始化应返回 ASRNotConfigured）
    with TestClient(app) as c:
        r = c.post(
            "/ai/asr",
            files={"audio": ("test.wav", b"\x00\x00", "audio/wav")},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "ASR_NOT_CONFIGURED"


# ---- 端点鉴权 ---------------------------------------------------------------

def test_ai_interview_step_requires_token(monkeypatch):
    """无 X-Service-Token → 422。"""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)

    async def _fake_step(*a, **k):
        return {"stage": "await_feedback", "question": "q?", "done": False}

    monkeypatch.setattr("app.api.interview.interview_step", _fake_step)
    with TestClient(app) as c:
        r = c.post("/ai/interview/step", json={"user_id": 1, "session_id": "s"})
    assert r.status_code == 422


def test_ai_interview_step_accepts_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)

    async def _fake_step(*a, **k):
        return {"stage": "await_feedback", "question": "q?", "done": False}

    monkeypatch.setattr("app.api.interview.interview_step", _fake_step)
    with TestClient(app) as c:
        r = c.post(
            "/ai/interview/step",
            json={"user_id": 1, "session_id": "s", "materials": "m"},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json()["stage"] == "await_feedback"


def test_ai_interview_result_requires_token(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)

    async def _fake_fetch(*a, **k):
        return None

    monkeypatch.setattr("app.api.interview.fetch_result", _fake_fetch)
    with TestClient(app) as c:
        r = c.get("/ai/interview/result", params={"thread_id": "1:s"})
    assert r.status_code == 422


def test_ai_interview_result_accepts_token(token, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)

    async def _fake_fetch(*a, **k):
        return {"profile": {"人设": "x"}, "a_cards": []}

    monkeypatch.setattr("app.api.interview.fetch_result", _fake_fetch)
    with TestClient(app) as c:
        r = c.get(
            "/ai/interview/result",
            params={"thread_id": "1:s"},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert "profile" in r.json()


# ---- /ai/interview/sample-opening（试试效果对比块）-------------------------

class _FakeGraph:
    """假 LangGraph，可控 aget_state 返回值。monkeypatch 替 sample_opening 模块的 _graph。"""
    def __init__(self, values):
        self._values = values

    async def aget_state(self, config):
        class _SV:
            pass
        sv = _SV()
        sv.values = self._values
        return sv


@pytest.mark.asyncio
async def test_sample_opening_returns_two_hooks(monkeypatch):
    """有 profile 的 checkpoint → 一次 chat 产 {topic, without, with}。"""
    from app.skills.interview import sample_opening as so

    captured = {}

    async def _chat(skill, messages, json_schema=None, **kwargs):
        captured["skill"] = skill
        captured["schema"] = json_schema
        return {"without": "今天教大家看懂报价单", "with": "别人报3万我报1万6，我在做慈善吗"}

    monkeypatch.setattr("app.skills.interview.sample_opening.chat", _chat)
    inner = {"人设": "说真话的工厂人", "人群": "30-45 业主", "差异化": "工厂直营",
             "变现": "到店", "红线": "不贬同行", "支柱配比": "4:2:2:2"}
    monkeypatch.setattr(
        "app.skills.interview.sample_opening._graph",
        _FakeGraph({"profile": {"profile": inner, "a_cards": []}}),
    )

    r = await so.sample_opening("1:sess", None)
    assert r is not None
    assert r["topic"] == "报价为什么差一倍"  # 默认 topic
    assert r["without"] and r["with"]
    assert captured["skill"] == "interview"
    assert captured["schema"] is so.SAMPLE_OPENING_SCHEMA


@pytest.mark.asyncio
async def test_sample_opening_no_checkpoint_returns_none(monkeypatch):
    """无 checkpoint → None（路由层返 found=false）。"""
    from app.skills.interview import sample_opening as so

    async def _chat(*a, **k):  # 不应被调
        raise AssertionError("不应调 LLM")

    monkeypatch.setattr("app.skills.interview.sample_opening.chat", _chat)
    monkeypatch.setattr("app.skills.interview.sample_opening._graph", _FakeGraph(None))

    r = await so.sample_opening("nobody:none", "某选题")
    assert r is None


def test_sample_opening_endpoint_requires_token(monkeypatch):
    """无 token → 422（照 step 端点鉴权测）。"""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)
    async def _fake(*a, **k):
        return {"found": True, "topic": "x", "without": "a", "with": "b"}
    monkeypatch.setattr("app.api.interview.sample_opening", _fake)
    with TestClient(app) as c:
        r = c.post("/ai/interview/sample-opening",
                   json={"user_id": 1, "thread_id": "1:s", "topic": None})
    assert r.status_code == 422
