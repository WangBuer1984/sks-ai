"""跨模块共享的媒体/心跳常量。

集中放置以消除 ``audio.py`` / ``transcribe.py`` / 两个 skill graph 中的重复定义。
属基础设施层概念，skill 层与 datasource 层均从此 import。
"""

from __future__ import annotations

# qwen3-asr-flash 单次 wav 体积上限（10MB）——整段守卫；切片单段 270s≈8.24MB 天然满足。
WAV_SIZE_LIMIT: int = 10 * 1024 * 1024

# 心跳间隔（秒）：长转写/scrape 期间每 N 秒 touch updated_at，短于 Java running-timeout 5min。
HEARTBEAT_INTERVAL: float = 60.0

# 拆账号逐条（转写+结构化）有界并发——吃满 asr_sem=3，避免 TOP20 纯串行墙钟。
ACCOUNT_ITEM_CONCURRENCY: int = 3
