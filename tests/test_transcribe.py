"""ASR 录音文件识别（长音频异步）测试：mock 阿里云 seam，绝不发真实网络请求。

覆盖：transcribe 下载→提交→轮询→取结果→拼接全文；file_link 透传 download_url；
阿里云失败 → DataSourceError；未配置 AppKey → DataSourceError（懒初始化失败）。
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.datasource import DataSourceError
from app.datasource import transcribe as tr


async def test_transcribe_returns_concatenated_sentence_text(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_APP_KEY", "nls-app-key")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_ID", "akid")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_SECRET", "secret")

    submitted: dict = {}

    def _submit(file_link: str) -> str:
        submitted["file_link"] = file_link
        return "TASK-123"

    def _get_result(task_id: str) -> dict:
        assert task_id == "TASK-123"
        return {
            "StatusText": "SUCCESS",
            "Result": {
                "Sentences": [
                    {"Text": "第一句。"},
                    {"Text": "第二句。"},
                ]
            },
        }

    monkeypatch.setattr(tr, "_submit_task", _submit)
    monkeypatch.setattr(tr, "_get_task_result", _get_result)

    text = await tr.transcribe("https://dl.example.com/audio.wav")
    assert text == "第一句。第二句。"
    assert submitted["file_link"] == "https://dl.example.com/audio.wav"


async def test_transcribe_passes_download_url_directly_as_file_link(monkeypatch):
    # 阿里云 录音文件识别 接受公网 URL（file_link），无需下载到临时文件再上传 OSS。
    monkeypatch.setattr(settings, "ALIYUN_ASR_APP_KEY", "nls-app-key")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_ID", "akid")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_SECRET", "secret")

    seen_url: dict = {}

    def _submit(file_link: str) -> str:
        seen_url["file_link"] = file_link
        return "T1"

    def _get_result(task_id: str) -> dict:
        return {"StatusText": "SUCCESS", "Result": {"Sentences": [{"Text": "hi"}]}}

    monkeypatch.setattr(tr, "_submit_task", _submit)
    monkeypatch.setattr(tr, "_get_task_result", _get_result)

    await tr.transcribe("https://cdn.example.com/x.mp4")
    assert seen_url["file_link"] == "https://cdn.example.com/x.mp4"


async def test_transcribe_polls_until_success(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_APP_KEY", "nls-app-key")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_ID", "akid")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_SECRET", "secret")
    # 缩短轮询间隔，避免真实 5s sleep 拖慢测试。
    monkeypatch.setattr(tr, "_POLL_INTERVAL", 0.01)

    states = iter(["QUEUEING", "RUNNING", "SUCCESS"])

    def _submit(file_link: str) -> str:
        return "T"

    def _get_result(task_id: str) -> dict:
        status = next(states)
        if status != "SUCCESS":
            return {"StatusText": status}
        return {"StatusText": "SUCCESS", "Result": {"Sentences": [{"Text": "done"}]}}

    monkeypatch.setattr(tr, "_submit_task", _submit)
    monkeypatch.setattr(tr, "_get_task_result", _get_result)

    text = await tr.transcribe("https://x/y.wav")
    assert text == "done"


async def test_transcribe_failure_raises_datasource_error(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_APP_KEY", "nls-app-key")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_ID", "akid")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_SECRET", "secret")

    monkeypatch.setattr(tr, "_submit_task", lambda fl: "T")
    monkeypatch.setattr(
        tr,
        "_get_task_result",
        lambda tid: {"StatusText": "FAILED", "Message": "audio too short"},
    )

    with pytest.raises(DataSourceError):
        await tr.transcribe("https://x/y.wav")


async def test_transcribe_submit_exception_raises_datasource_error(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_APP_KEY", "nls-app-key")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_ID", "akid")
    monkeypatch.setattr(settings, "ALIYUN_ACCESS_KEY_SECRET", "secret")

    def _boom(file_link: str) -> str:
        raise RuntimeError("acs client transport")

    monkeypatch.setattr(tr, "_submit_task", _boom)
    with pytest.raises(DataSourceError):
        await tr.transcribe("https://x/y.wav")


async def test_transcribe_unconfigured_appkey_raises_datasource_error(monkeypatch):
    monkeypatch.setattr(settings, "ALIYUN_ASR_APP_KEY", "")
    with pytest.raises(DataSourceError):
        await tr.transcribe("https://x/y.wav")
