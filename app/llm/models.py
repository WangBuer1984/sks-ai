"""模型档位映射（唯一写模型型号的地方）。

设计文档 §5 模型选型表（tech-design 2026-07-19 §5 风险表 + 模型选型表；线上有修订）：
- 创作类（script_gen / video_analyze）：glm-4.7 thinking 关——创作不需思考链，更快省。
- 访谈对话（interview）：glm-4.5-air thinking 关——原归创作类 4.7，但 4.7 单轮常 >120s 触超时；8371d6e 改 air（5–15s/轮），多轮校准优先时延。
- 轻量抽取（card_gen / rewrite_sentence / account_analyze_item）：glm-4.5-air——便宜快。
- 归纳/归因（account_analyze_summary / attribution）：glm-4.7 thinking 关——原设计 thinking 开，但与结构化输出冲突（function_calling→400 code 1210、json_schema→散文、json_mode→形状不可控），关 thinking 保 function_calling 强制字段类型，~85s→~5-10s 远离超时。

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
    "video_analyze": ModelSpec("glm-4.7", thinking=False),
    # 访谈对话：glm-4.5-air（时延；见模块 docstring）
    "interview": ModelSpec("glm-4.5-air", thinking=False),
    # 轻量抽取：glm-4.5-air
    "card_gen": ModelSpec("glm-4.5-air", thinking=False),
    "rewrite_sentence": ModelSpec("glm-4.5-air", thinking=False),
    "account_analyze_item": ModelSpec("glm-4.5-air", thinking=False),
    # 归纳/归因：glm-4.7，thinking 关（与结构化输出冲突，见模块 docstring）
    "account_analyze_summary": ModelSpec("glm-4.7", thinking=False),
    "attribution": ModelSpec("glm-4.7", thinking=False),
}
