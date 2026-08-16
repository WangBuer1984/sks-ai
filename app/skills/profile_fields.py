"""定位档案七字段的读法（D19）——Python 侧只读，不写。

档案的唯一真源是共享库的 `positioning_profile.content`，由 sks-server 写。本仓拿到的是 Java 传过来的
`profile` dict，需要做的只有两件事：

1. **收口成七个规范键**（`canonical_profile`）：多余的键（FAQ、meta、历史遗留的「创作偏好」）一律丢掉。
   丢弃而不是透传，是因为 prompt 是个什么都能塞的口袋——一旦允许未知键进去，谁往 profile 里多塞一个字段，
   写稿风格就悄悄变了，而且没有任何一处会报错。
2. **渲染成给模型看的中文文本**（`render_profile`）：模型读「人设：…」比读 `{"persona": …}` 稳，
   也不会把 JSON 结构当成要模仿的格式。

旧中文键（`人设` / `人群` / …）在读侧兼容：线上还有那批档案。**但映射的权威实现在 Java**
（`com.sks.profile.ProfileContent`）——这里是同一规则的读侧副本，不做额外加工（不拆分、不补默认值），
免得两套规则各自演化。
"""

from __future__ import annotations

from typing import Any

# 七个规范键，顺序 = 定位页展示顺序 = prompt 里的呈现顺序
PROFILE_FIELDS: tuple[str, ...] = (
    "persona",
    "targetAudience",
    "differentiation",
    "conversionPath",
    "tone",
    "redlines",
    "contentPillars",
)

# 旧中文键 → 规范键（只读侧生效；写侧在 Java，且写侧不认中文键）
_LEGACY_KEYS: dict[str, str] = {
    "人设": "persona",
    "人群": "targetAudience",
    "目标人群": "targetAudience",
    "差异化": "differentiation",
    "变现": "conversionPath",
    "转化路径": "conversionPath",
    "口吻": "tone",
    "红线": "redlines",
    "支柱配比": "contentPillars",
    "内容支柱": "contentPillars",
}

# prompt 里的中文标签——模型看标签，不看键名
_LABELS: dict[str, str] = {
    "persona": "人设",
    "targetAudience": "目标人群",
    "differentiation": "差异化",
    "conversionPath": "转化路径",
    "tone": "口吻",
    "redlines": "红线",
    "contentPillars": "内容支柱",
}

EMPTY_PROFILE_TEXT = "（无定位档案）"


def canonical_profile(profile: Any) -> dict[str, Any]:
    """任意入参 → 只含七个规范键的 dict。

    非 dict（None / 字符串 / 列表）→ `{}`：档案缺失不是错误，稿子照样要能生成
    （prompt 会印「（无定位档案）」）。
    """
    if not isinstance(profile, dict):
        return {}
    by_key: dict[str, Any] = {}
    for raw_key, value in profile.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key if raw_key in PROFILE_FIELDS else _LEGACY_KEYS.get(raw_key)
        if key is None:
            continue  # 未知键（含 faqs / faq_candidates / _interview_turns）不进 prompt
        # 规范键优先：同时存在 persona 与 人设（迁移中途的行）时以 persona 为准
        if raw_key in PROFILE_FIELDS or key not in by_key:
            by_key[key] = value
    return {k: by_key[k] for k in PROFILE_FIELDS if k in by_key and not _is_blank(by_key[k])}


def render_profile(profile: Any) -> str:
    """七字段 → 给模型看的多行中文文本。全空 → `（无定位档案）`。"""
    fields = canonical_profile(profile)
    if not fields:
        return EMPTY_PROFILE_TEXT
    return "\n".join(f"{_LABELS[k]}：{_as_text(v)}" for k, v in fields.items())


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple, dict)):
        return len(v) == 0
    return False


def _as_text(v: Any) -> str:
    """值可能是字符串、清单（红线 / 内容支柱），少数老档案里还是对象——三种都要能显示。"""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return " · ".join(_as_text(item) for item in v if not _is_blank(item))
    if isinstance(v, dict):
        return "；".join(f"{k}：{_as_text(val)}" for k, val in v.items())
    return str(v)
