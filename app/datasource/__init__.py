"""数据源层：TikHub（拆账号/拆视频取数）+ 阿里云录音文件识别（长音频转写）。

领域异常 `DataSourceError`：TikHub/ASR 失败统一抛此异常，Task 3.2/3.3 捕获后翻译
（拆账号全量取数失败 → 全额退款 + 引导视频粘贴，PRD §11.3）。与 AI/generic error 区分，
便于上游按「数据源故障」而非「LLM 故障」处理。
"""

from __future__ import annotations


class DataSourceError(RuntimeError):
    """TikHub / 阿里云录音文件识别 调用失败（网络/鉴权/解析/业务码非成功）。

    上游（Task 3.2 skills / Task 3.3 Java 经 aiclient）捕获此异常并翻译为退款/重试策略。
    """
