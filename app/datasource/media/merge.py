"""分段 transcript 文本拼接 — overlap 区域去重。

算法逐字移植自 clever-hans ``backend/app/core/pipeline.py`` 的
``_merge_transcript_parts`` / ``_find_overlap_text``（仅去掉私有前导下划线，
改为公开 API；算法体一致）。纯文本模块：不依赖 ffmpeg / subprocess / tikhub /
transcribe，可在任何环境直接 import。

- ``merge_transcript_parts(parts, overlap)``：拼接分段，对相邻段在尾部/头部
  的最长公共子串做一次去重，避免 overlap 秒数造成的重复文本。
- ``find_overlap_text(tail, head)``：在 ``tail`` 末尾与 ``head`` 开头找最长
  公共子串，长度上限 ``min(len(tail), len(head), 50)``。
"""

from __future__ import annotations


def merge_transcript_parts(parts: list[str], overlap: int = 3) -> str:
    """拼接分段 transcript，overlap 区域做文本去重。

    与 clever-hans ``_merge_transcript_parts`` 算法体逐字一致：
    取 ``merged[-200:]`` 与 ``parts[i][:200]`` 两个窗口，调用
    ``find_overlap_text`` 找最长公共前后缀，命中则削掉前段末尾的重复再拼接。
    """
    if not parts:
        return ""
    merged = parts[0]
    for i in range(1, len(parts)):
        overlap_text = find_overlap_text(merged[-200:], parts[i][:200])
        if overlap_text:
            merged = merged[:-len(overlap_text)] + parts[i]
        else:
            merged += parts[i]
    return merged


def find_overlap_text(tail: str, head: str) -> str:
    """在 ``tail`` 末尾与 ``head`` 开头找最长公共子串。

    长度上限 ``max_len = min(len(tail), len(head), 50)``，从长到短枚举，
    命中即返回；无公共前后缀返回 ``""``。
    """
    max_len = min(len(tail), len(head), 50)
    for l in range(max_len, 0, -1):
        if tail[-l:] == head[:l]:
            return tail[-l:]
    return ""
