"""配置：pydantic-settings 读 .env。

所有密钥（DB 密码、GLM key、TikHub key、阿里云 key、服务间共享密钥、JWT secrets）
均经 .env 注入，.env 被 gitignore，密钥不进 git。本模块为配置单例。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 智谱 GLM（OpenAI 兼容协议）。base_url 沿用智谱开放平台 v4 端点。
    # 文档：https://docs.bigmodel.cn/cn/guide/start/model-overview
    ZHIPU_API_KEY: str = ""
    ZHIPU_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4/"

    # 服务间共享密钥：Java→Python 必带 X-Service-Token，Python 仅信此 + 内网。
    SERVICE_TOKEN: str = ""

    # Postgres：业务表 + pgvector + LangGraph 检查点，单库三合一。
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/sks"

    # 阿里云：ASR / 内容安全同厂商（SMS 归 sks-server）。
    ALIYUN_ACCESS_KEY_ID: str = ""
    ALIYUN_ACCESS_KEY_SECRET: str = ""
    # 内容安全 Green 文本审核端点（按 region 选择）。
    ALIYUN_CONTENT_SAFETY_ENDPOINT: str = "https://green.cn-shanghai.aliyuncs.com"
    # 一句话识别（短 ASR）+ 长转写（qwen3-asr-flash）共用——DashScope/百炼 API Key，
    # 非阿里云 ISI。未配置时 /ai/asr 与 transcribe 懒失败（per-request，不阻断启动）。
    # 生产镜像须含 ffmpeg/ffprobe（短 ASR pydub + 长转写管线）与 nodejs（视频号 decode）。
    ALIYUN_ASR_KEY: str = ""

    # TikHub 数据 API（拆账号/拆视频取数）。
    # 主域名 api.tikhub.io 被墙，国内必须用 api.tikhub.dev（计划强约束，不可改）。
    TIKHUB_API_KEY: str = ""
    TIKHUB_BASE_URL: str = "https://api.tikhub.dev"

    # deprecated：长转写已硬切 Qwen，不再读此字段。保留仅为兼容旧 .env，可留空。
    ALIYUN_ASR_APP_KEY: str = ""

    # ASR 媒体下载临时文件目录（空 → 系统 tempfile 目录）。download.py 落盘 + gc_stale_tmp 清扫。
    ASR_TMP_DIR: str = ""


settings = Settings()
