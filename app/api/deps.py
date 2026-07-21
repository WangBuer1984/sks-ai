"""服务间鉴权依赖：校验 X-Service-Token。

Java 是唯一公网入口；Java→Python 每请求带 X-Service-Token（共享密钥，来自 .env
SERVICE_TOKEN）+ X-Request-Id（Java 生成，Python 仅记录用于串联日志，不校验）。
Python 仅信内网来源 + 此 token，不感知用户/JWT/额度（§设计文档 §5.1）。
"""

from fastapi import Header, HTTPException, status

from app.config import settings


def verify_service_token(x_service_token: str = Header(...)) -> str:
    """校验 X-Service-Token。不匹配 settings.SERVICE_TOKEN 则 403。"""
    if not settings.SERVICE_TOKEN:
        # 未配置 token（如本地未注入 .env）——按最严格处理，拒绝一切 /ai/* 调用。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SERVICE_TOKEN not configured",
        )
    if x_service_token != settings.SERVICE_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid X-Service-Token",
        )
    return x_service_token
