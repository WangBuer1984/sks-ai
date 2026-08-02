"""媒体引用值对象（MediaRef）。

跨数据源/下载/转写管的统一引用类型——承载下载直链、必带请求头、解码键、
标题/作者元数据与原始 ID。Task 1 引入；后续 Task 2（download.py）/ Task 4
（qwen_asr.py）/ Task 6（resolve_media）等消费。

设计约束：
- ``headers`` 默认 ``None``，由调用方（如 ``video_meta_to_media_ref``）通过
  ``dict(DOUYIN_DOWNLOAD_HEADERS)`` 注入**新鲜字典**，避免多个 ref 共享
  同一可变模块字典（mutate-one-affects-all bug）。
- 不依赖 ``tikhub``——避免循环 import；本模块只定义数据形状。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MediaRef:
    """单一媒体资源的统一引用。

    字段：
      platform:    来源平台标识（"douyin" / "shipinhao" / ...）。
      download_url: 下载直链（已解析出真实 URL，下载层直接 GET）。
      headers:     下载所需请求头（Referer/UA 等）；默认 None，调用方应注入
                   新鲜 dict（``dict(...)``），切勿跨实例共享。
      decode_key:  解码键（如需），None 表示无需解码。
      title:       标题（可空），用于日志/展示。
      author:      作者昵称（可空），用于展示与去重。
      raw_id:      平台原生 ID（可空），用于幂等去重。
    """

    platform: str
    download_url: str
    headers: dict[str, str] | None = None
    decode_key: str | None = None
    title: str | None = None
    author: str | None = None
    raw_id: str | None = None
