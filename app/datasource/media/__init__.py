"""媒体引用子包：``MediaRef`` 值对象。

后续 Task（download.py / transcribe / resolve_media）从此处 import：
    from app.datasource.media import MediaRef

不重导出 ``tikhub`` 的 ``video_meta_to_media_ref``——该转换函数承载
抖音特定 header 策略，留在 ``tikhub`` 模块，避免本包反向依赖数据源层。
"""

from __future__ import annotations

from app.datasource.media.types import MediaRef

__all__ = ["MediaRef"]

# channels_decode 不在此重导出——由 ``transcribe.decode_media`` seam 接入。
