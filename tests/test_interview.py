"""定位访谈 LangGraph 状态机测试：mock LLM / safety / ASR，绝不发真实网络/DB 请求。

覆盖 brief verbatim resume 用例 + 边界：
- 同一 thread_id 跨多次 /step 请求从 checkpoint 恢复而非重来
- UGC（materials / user_reply）命中安全 → {blocked:true} 不推进状态机
- LLM 产出（生成的问题）命中安全 → {blocked:true}
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
        # call 1 = materials（safe），call 2 = guess 问题（safe），call 3 = user_reply（unsafe）
        return check_n["n"] != 3

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


# ---- blocked LLM 产出（生成的问题命中安全）----------------------------------

@pytest.mark.asyncio
async def test_blocked_llm_output_returns_blocked(monkeypatch):
    """LLM 生成的问题命中安全 → {blocked:true}。"""
    chat_n = {"n": 0}

    async def _chat(skill, messages, json_schema=None, **kwargs):
        chat_n["n"] += 1
        if json_schema is SUMMARIZE_SCHEMA:
            return {"profile": {}, "a_cards": []}
        # 第 1 次 chat = guess_persona（安全的问题）；之后 = ask（违禁问题）
        if chat_n["n"] == 1:
            return {"persona": {"人设": "x"}, "question": "安全的问题？"}
        return {"question": "违禁问题"}

    check_n = {"n": 0}

    async def _check(text):
        check_n["n"] += 1
        # call 1 = materials（safe），2 = guess 问题（safe），3 = user_reply "对"（safe），4 = ask 问题（unsafe）
        return check_n["n"] <= 3

    monkeypatch.setattr("app.skills.interview.graph.chat", _chat)
    monkeypatch.setattr("app.skills.interview.graph.check", _check)

    # 首次：guess_persona 问题安全 → await_feedback
    r1 = await interview_step(user_id=1, session_id="b3", materials="素材")
    assert r1["stage"] == "await_feedback"

    # 第二次：进入 ask，LLM 生成违禁问题 → blocked
    r2 = await interview_step(user_id=1, session_id="b3", user_reply="对")
    assert r2.get("blocked") is True


# ---- summarize 形状 ---------------------------------------------------------

@pytest.mark.asyncio
async def test_summarize_shape(monkeypatch):
    """完整流程到 summarize：产出 {profile:{6 字段}, a_cards:[{card_type,title,content}]}。"""
    call_n = {"n": 0}

    async def _chat(skill, messages, json_schema=None, **kwargs):
        call_n["n"] += 1
        if json_schema is SUMMARIZE_SCHEMA:
            return {
                "profile": {
                    "人设": "职场博主", "人群": "25-35白领", "差异化": "反内卷",
                    "变现": "咨询", "红线": "不谈政治", "支柱配比": "4:2:2:2",
                },
                "a_cards": [
                    {"card_type": "定位", "title": "反内卷职场", "content": {"k": "v"}},
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
    assert "profile" in pd and "a_cards" in pd
    for k in ("人设", "人群", "差异化", "变现", "红线", "支柱配比"):
        assert k in pd["profile"]
    assert pd["a_cards"][0]["card_type"] == "定位"


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
    """/ai/asr mock seam 返回文本 → {text}。"""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main._init_checkpointer", _noop_checkpointer)

    async def _fake_transcribe(audio_bytes, fmt):
        return "你好世界"

    monkeypatch.setattr("app.api.asr.transcribe_short", _fake_transcribe)

    with TestClient(app) as c:
        r = c.post(
            "/ai/asr",
            files={"audio": ("test.wav", b"\x00\x00\x00\x00", "audio/wav")},
            headers={"X-Service-Token": token},
        )
    assert r.status_code == 200
    assert r.json()["text"] == "你好世界"


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
