"""_default_factory 构造参数测试（§5.3 timeout-chain 可证伪化）。

§5.3 链：Python 240s < Java 270s < nginx 300s（内层短于外层）。
"240s" 的推导 = timeout=120 × 最多 2 次（原始 + 1 重试）= 240s。
该推导成立的充要条件：ChatOpenAI 显式 max_retries=1（2 次 attempts）。
若依赖 openai SDK 默认（max_retries=2 → 3 次 ≈ 360s），链被违反（360 > 270），
将复现 brief 命名的 #1 load-bearing 风险：Java 对仍在工作的 Python 打 AI_FAILED。
"""

from app.config import settings
from app.llm.client import _default_factory
from app.llm.models import MODEL_FOR


def test_default_factory_sets_max_retries_one():
    """§5.3：必须显式 max_retries=1，使 2×120=240s < Java 270s 可证。"""
    # 构造是 lazy 的（ZHIPU_API_KEY/base_url 仅在 ainvoke 时用到，不触网）。
    settings.ZHIPU_API_KEY = "test-key"
    settings.ZHIPU_BASE_URL = "https://test.example/api/"

    llm = _default_factory(MODEL_FOR["script_gen"])
    assert llm.max_retries == 1, (
        f"max_retries must be 1 (2 attempts × 120s = 240s < Java 270s), got {llm.max_retries!r}"
    )


def test_default_factory_sets_timeout_120():
    """§5.1：LLM 单次超时 120s（内层最短于外层 270/300）。"""
    settings.ZHIPU_API_KEY = "test-key"
    settings.ZHIPU_BASE_URL = "https://test.example/api/"

    llm = _default_factory(MODEL_FOR["script_gen"])
    # ChatOpenAI 把 timeout= 暴露为 request_timeout 字段。
    assert float(llm.request_timeout) == 120.0
