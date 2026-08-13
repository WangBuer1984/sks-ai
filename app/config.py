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

    # 单条转写墙钟硬上限（秒）。transcribe.py 的 wait_for 据此翻译为 DataSourceError，
    # 超时 item 被 account_analyze._process_item 捕获后跳过（弃此条不拖垮整任务）。
    # 1200→300 fail-fast：正常 item ~60-100s（下载 13-58s + ASR ~30s），300≈3× 最差
    # 观测；卡死的 CDN 慢-but-进展流（read=30 只杀真 0 字节 stall，不杀慢流）300s 即
    # 弃条，10 条弃 1 不影响任务。真 stall 由 httpx read=30 早死，此值兜底慢流。
    # 详见 docs/spikes/cdn-download-concurrency.md。
    TRANSCRIBE_TIMEOUT: int = 300


settings = Settings()
