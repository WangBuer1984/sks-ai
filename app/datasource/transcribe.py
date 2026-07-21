"""阿里云录音文件识别（长音频异步批量）。

与 ``app/datasource/asr.py`` 的「一句话识别」（短音频 ≤60s，dashscope 同步）是**不同
API**：本模块走阿里云 ISI 录音文件识别（filetrans POP API，异步 submit→poll→fetch），
面向 P3 拆视频/拆账号的完整音频下载转写。

SDK 选型：``aliyun-python-sdk-core`` 的 ``AcsClient`` + ``CommonRequest``（官方文档
https://help.aliyun.com/zh/isi/developer-reference/sdk-for-python-3 的标准路径）。
域 ``filetrans.cn-shanghai.aliyuncs.com``，版本 ``2018-08-17``，action ``SubmitTask`` /
``GetTaskResult``。lazy-import：未配置 key 时不触发 SDK 加载，import 不崩溃。

file_link 直传：阿里云录音文件识别**只接受公网可访问 URL**（不支持本地文件）。因此
``transcribe(download_url)`` 直接把 ``download_url`` 作为 ``file_link`` 透传给阿里云，
**不下载到临时文件**（brief §7 resolution：API 接受 URL 即直传，省去下载→OSS 中转）。
若联调期发现 TikHub 签名直链阿里云拉不到，需在此处先下载→上传 OSS→传 OSS URL（待联调）。

鉴权：``AcsClient(ALIYUN_ACCESS_KEY_ID, ALIYUN_ACCESS_KEY_SECRET, "cn-shanghai")``。
AppKey 是 NLS 项目维度（``ALIYUN_ASR_APP_KEY``），联调期在 ISI 控制台获取，与 AccessKey 不同。

模块级 seam ``_submit_task`` / ``_get_task_result`` 是测试 monkeypatch 目标
（app.datasource.transcribe._submit_task / ._get_task_result）——测试 mock 它们，
不发真实网络请求。真实 阿里云 调用需联调期用真实 key + AppKey 核对（POP 签名 / 状态机时序）。

失败语义：未配置 / 提交异常 / 任务 FAILED / 解析失败 → ``DataSourceError``，
Task 3.2/3.3 捕获后翻译（拆账号转写失败 → 全额退款 + 引导视频粘贴，PRD §11.3）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from app.config import settings
from app.datasource import DataSourceError

log = logging.getLogger(__name__)

# filetrans POP API 固定参数（联调期如阿里云改域/版本仅改这里）。
_DOMAIN = "filetrans.cn-shanghai.aliyuncs.com"
_VERSION = "2018-08-17"
_REGION = "cn-shanghai"
# 轮询间隔与上限：长音频通常数分钟内完成，上限 10min 兜底（超时抛 DataSourceError）。
_POLL_INTERVAL = 5.0
_POLL_TIMEOUT = 600.0


def _is_configured() -> bool:
    return bool(
        getattr(settings, "ALIYUN_ACCESS_KEY_ID", "")
        and getattr(settings, "ALIYUN_ACCESS_KEY_SECRET", "")
        and getattr(settings, "ALIYUN_ASR_APP_KEY", "")
    )


def _submit_task(file_link: str) -> str:
    """同步阻塞：提交录音文件识别任务，返回 TaskId。

    联调注意：POP RPC 风格 CommonRequest，Task 体 JSON 序列化后塞 body 参数 ``Task``。
    签名由 aliyunsdkcore 内部完成。此处实现按官方 demo 结构正确，未用真实 key 验证。
    """
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest

    client = AcsClient(
        settings.ALIYUN_ACCESS_KEY_ID,
        settings.ALIYUN_ACCESS_KEY_SECRET,
        _REGION,
    )
    req = CommonRequest()
    req.set_method("POST")
    req.set_domain(_DOMAIN)
    req.set_version(_VERSION)
    req.set_action_name("SubmitTask")
    task = {
        "appkey": settings.ALIYUN_ASR_APP_KEY,
        "file_link": file_link,
        "version": "4.0",
        "enable_words": False,
        "enable_sample_rate_adaptive": True,
    }
    req.add_body_params("Task", json.dumps(task, ensure_ascii=False))
    try:
        raw = client.do_action_with_exception(req)
    except Exception as e:  # noqa: BLE001
        raise DataSourceError(f"asr submit_task transport failed: {e}") from e
    try:
        result = json.loads(raw)
    except ValueError as e:
        raise DataSourceError(f"asr submit_task bad json: {e}") from e
    if result.get("StatusText") != "SUCCESS":
        raise DataSourceError(
            f"asr submit_task not SUCCESS: {result.get('StatusText')} / {result.get('ErrorMessage', '')}"
        )
    task_id = result.get("TaskId")
    if not task_id:
        raise DataSourceError(f"asr submit_task: no TaskId in response {result}")
    return str(task_id)


def _get_task_result(task_id: str) -> dict:
    """同步阻塞：查询任务结果，返回原始 result dict。

    StatusText ∈ {QUEUEING, RUNNING, SUCCESS, FAILED}；由 ``transcribe`` 解释状态并决策
    重试 / 抛错 / 拼接。本函数仅做 POP 调用 + JSON 解析，业务态判断在调用方。
    """
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest

    client = AcsClient(
        settings.ALIYUN_ACCESS_KEY_ID,
        settings.ALIYUN_ACCESS_KEY_SECRET,
        _REGION,
    )
    req = CommonRequest()
    req.set_method("GET")
    req.set_domain(_DOMAIN)
    req.set_version(_VERSION)
    req.set_action_name("GetTaskResult")
    req.add_query_param("TaskId", task_id)
    try:
        raw = client.do_action_with_exception(req)
    except Exception as e:  # noqa: BLE001
        raise DataSourceError(f"asr get_task_result transport failed: {e}") from e
    try:
        return json.loads(raw)
    except ValueError as e:
        raise DataSourceError(f"asr get_task_result bad json: {e}") from e


async def transcribe(download_url: str) -> str:
    """下载 URL → 完整文案（阿里云录音文件识别异步转写）。

    阿里云 filetrans 接受公网 URL，``download_url`` 直传为 ``file_link``（不下载临时文件）。
    submit → poll(5s) → SUCCESS 后拼接所有 Sentence.Text 返回全文。

    未配置 AppKey/AccessKey → DataSourceError（懒初始化失败，per-request，不阻断 import）。
    任务 FAILED / 超时(10min) / 解析失败 → DataSourceError。
    """
    if not _is_configured():
        raise DataSourceError("ALIYUN_ASR_APP_KEY / AccessKey not configured")

    # _submit_task / _get_task_result 是同步 POP 调用，经 to_thread 避免阻塞事件循环。
    # 模块级别名使测试可 monkeypatch（app.datasource.transcribe._submit_task / ._get_task_result）。
    # seam 抛出的非 DataSourceError 异常（如 SDK RuntimeError）统一包成 DataSourceError，
    # 上游只依赖单一领域异常做退款/重试分诊。
    try:
        task_id = await asyncio.to_thread(_submit_task, download_url)
    except DataSourceError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DataSourceError(f"asr submit_task failed: {e}") from e

    deadline = time.monotonic() + _POLL_TIMEOUT
    while True:
        try:
            result = await asyncio.to_thread(_get_task_result, task_id)
        except DataSourceError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DataSourceError(f"asr get_task_result failed: {e}") from e
        status = result.get("StatusText")
        if status == "FAILED":
            raise DataSourceError(
                f"asr task {task_id} FAILED: {result.get('ErrorMessage', '') or result}"
            )
        if status == "SUCCESS":
            sentences = (result.get("Result") or {}).get("Sentences") or []
            return "".join(str(s.get("Text") or "") for s in sentences)
        # QUEUEING / RUNNING / 其它 → 仍运行中
        if time.monotonic() >= deadline:
            raise DataSourceError(f"asr task {task_id} timed out after {_POLL_TIMEOUT}s")
        await asyncio.sleep(_POLL_INTERVAL)
