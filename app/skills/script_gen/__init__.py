"""文案生成 skill：RAG 检索 → GLM 结构化三段生成 → 内容安全 → (重写一次) → done/blocked。

设计文档 §5：script_gen 用 glm-4.7 thinking off；输出三段（hook/body/cta）每段为句数组，
是逐句编辑（V1.1）的数据基础。无流式——生成完整经内容安全后一次性返回 JSON。
"""
