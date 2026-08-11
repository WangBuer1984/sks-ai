"""跨模块共享的媒体/心跳常量。

集中放置以消除 ``audio.py`` / ``transcribe.py`` / 两个 skill graph 中的重复定义。
属基础设施层概念，skill 层与 datasource 层均从此 import。
"""

from __future__ import annotations

# qwen3-asr-flash 单次 wav 体积上限（10MB）——整段守卫；切片单段 270s≈8.24MB 天然满足。
WAV_SIZE_LIMIT: int = 10 * 1024 * 1024

# 心跳间隔（秒）：长转写/scrape 期间每 N 秒 touch updated_at，短于 Java running-timeout 5min。
HEARTBEAT_INTERVAL: float = 60.0

# 拆账号逐条 item 层并发上限。设到 >= _TOP_N 让全部条目同时进 transcribe 管线，
# 下载/转码/结构化跨条与 ASR 重叠（流水线并行）。真正的限流由 per-stage 信号量兜底：
# asr_sem=5（DashScope RPM=100，ASR 非瓶颈）、download_sem=2（CDN 聚合限速，实测并发
# 越多越慢，见 docs/spikes/cdn-download-concurrency.md）、convert_sem=4。
ACCOUNT_ITEM_CONCURRENCY: int = 10
