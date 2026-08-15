"""智谱 GLM 客户端：OpenAI 兼容协议统一封装。

按 skill 取 MODEL_FOR 档位构造 ChatOpenAI（base_url=智谱兼容端点），
thinking 经 extra_body 传递。支持结构化 JSON 输出（with_structured_output json_schema）。
**不流式**——一次 ainvoke 返回完整 dict（设计文档 §5：内容安全需先审后展示）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langchain_openai import ChatOpenAI

from app.config import settings
from app.llm.models import MODEL_FOR, ModelSpec

log = logging.getLogger(__name__)


def _default_factory(spec: ModelSpec) -> ChatOpenAI:
    """按档位构造 ChatOpenAI。thinking 经 extra_body 传递（智谱 GLM-4.5+ 思考模式开关）。"""
    return ChatOpenAI(
        base_url=settings.ZHIPU_BASE_URL,
        api_key=settings.ZHIPU_API_KEY,
        model=spec.model,
        timeout=120,  # §5.1 LLM 单次超时 120s
        # §5.3 timeout-chain 可证化：max_retries=1 → 最多 2 次 × 120s = 240s < Java 270s < nginx 300s
        # （内层短于外层）。显式设 1 而非依赖 openai SDK 默认（max_retries=2 → 3 次 ≈ 360s，
        #  将越过 Java 270s read-timeout，触发对仍在重试的 Python 打 AI_FAILED 的 #1 风险）。
        # 与 Java AiClient 对 ResourceAccessException 的传输层重试正交（不同层），两者并存。
        max_retries=1,
        extra_body={"thinking": {"type": "enabled" if spec.thinking else "disabled"}},
    )


class GLMClient:
    """GLM 调用统一出口。llm_factory 可注入用于测试（默认 _default_factory 走真实 GLM）。"""

    def __init__(self, *, llm_factory: Callable[[ModelSpec], Any] | None = None) -> None:
        self._llm_factory = llm_factory or _default_factory

    async def chat(
        self,
        skill: str,
        messages: list[dict],
        json_schema: dict | None = None,
    ) -> dict:
        """按 skill 调 GLM，返回完整 dict。若给 json_schema 则走结构化输出。

        硬不变量：不流式；所有业务产出落 JSONB 前经本方法返回完整结构。
        """
        spec = MODEL_FOR[skill]
        # 不变量：结构化输出（json_schema）+ thinking 不可共存——GLM-4.7 thinking 开时
        # function_calling 触发 400 code 1210、json_schema 返回散文、json_mode 不强制 schema。
        # 若配置漂移（MODEL_FOR 误改回 thinking=True），此处强制降级 thinking=False 并告警，
        # 保证结构化路径永远走 function_calling（强制字段类型），而非在运行时撞 1210。
        if json_schema is not None and spec.thinking:
            log.warning(
                "skill=%s 配置 thinking=True 但走结构化输出；GLM-4.7 thinking+结构化冲突"
                "（1210/散文/形状不可控），强制降级 thinking=False 走 function_calling",
                skill,
            )
            spec = ModelSpec(spec.model, thinking=False)
        llm = self._llm_factory(spec)

        if json_schema is not None:
            # langchain function_calling 要求 schema 顶层有 title（作 function name）；
            # 缺省时用 skill 名兜底，避免各 skill 漏写 title 在运行时炸。
            schema = (
                json_schema
                if json_schema.get("title")
                else {**json_schema, "title": skill}
            )
            structured = llm.with_structured_output(schema, method="function_calling")
            result = await structured.ainvoke(messages)
        else:
            result = await llm.ainvoke(messages)

        return _to_dict(result)


def _to_dict(result: Any) -> dict:
    """把 langchain 返回值归一为 dict。"""
    if isinstance(result, dict):
        return result
    # Pydantic / dataclass / BaseModel 等
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if hasattr(result, "dict") and callable(getattr(result, "dict")):
        return result.dict()
    if hasattr(result, "__dict__"):
        return dict(result.__dict__)
    # langchain AIMessage：取 content + metadata，保证可落 JSONB
    content = getattr(result, "content", None)
    if content is not None:
        return {"content": content}
    return {"value": str(result)}


# 默认单例（业务代码 from app.llm.client import glm_client; await glm_client.chat(...)）
glm_client = GLMClient()
