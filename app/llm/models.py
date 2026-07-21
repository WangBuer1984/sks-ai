"""模型档位映射（唯一写模型型号的地方）。

设计文档 §5 模型选型表（tech-design 2026-07-19 §5 风险表 + 模型选型表）：
- 创作类（script_gen / interview / video_analyze）：glm-4.7 thinking 关——创作不需思考链，更快省。
- 轻量抽取（card_gen / rewrite_sentence / account_analyze_item）：glm-4.5-air——便宜快。
- 深度归纳/归因（account_analyze_summary / attribution）：glm-4.7 thinking 开——需全局推理。

模型字符串已与智谱开放平台文档核对：
  https://docs.bigmodel.cn/cn/guide/start/model-overview
glm-4.7 与 glm-4.5-air 均为线上当前有效 model id（小写即 API 入参形式）。
thinking 经 OpenAI 兼容协议以 extra_body={"thinking":{"type":"enabled"|"disabled"}} 传递，
详见 https://docs.bigmodel.cn （GLM-4.5+ 支持灵活开关思考模式）。

业务代码永远不感知具体型号；版本号随厂商升级时仅改本文件一处。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    """单个 skill 的模型档位。"""

    model: str
    thinking: bool = False


MODEL_FOR: dict[str, ModelSpec] = {
    # 创作类：glm-4.7，thinking 关
    "script_gen": ModelSpec("glm-4.7", thinking=False),
    "interview": ModelSpec("glm-4.7", thinking=False),
    "video_analyze": ModelSpec("glm-4.7", thinking=False),
    # 轻量抽取：glm-4.5-air
    "card_gen": ModelSpec("glm-4.5-air", thinking=False),
    "rewrite_sentence": ModelSpec("glm-4.5-air", thinking=False),
    "account_analyze_item": ModelSpec("glm-4.5-air", thinking=False),
    # 深度归纳/归因：glm-4.7，thinking 开
    "account_analyze_summary": ModelSpec("glm-4.7", thinking=True),
    "attribution": ModelSpec("glm-4.7", thinking=True),
}
