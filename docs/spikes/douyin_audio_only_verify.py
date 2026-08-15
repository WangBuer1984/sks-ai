"""验证：抖音 ``video.bit_rate_audio`` 纯音频轨能否替代整片视频喂 ASR。

探针（``douyin_bitrate_probe.py``）已证实该字段存在且比 ``play_addr`` 小 ~7 倍。
但小不等于可用——必须排掉两个证伪点，否则方案作废：
  1. 直链能否在现有下载头下拿到（是否另有防盗链）。
  2. 里面是**原声人声**还是 BGM 音乐轨（是 BGM 则转写全废）。

判据：同一条视频，音频轨转写 vs 整片转写，两份文本一致 → 可替代。

跑法（仓根，消耗 1 次 TikHub + 2 次 ASR 调用）：
    uv run python docs/spikes/douyin_audio_only_verify.py
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.datasource import tikhub
from app.datasource.media.audio import convert_to_wav, get_audio_duration
from app.datasource.media.download import download_url
from app.datasource.media.types import MediaRef
from app.datasource.transcribe import transcribe

_ACCOUNT_URL = "https://v.douyin.com/LIVJSlsfTTU/"


def _mb(path) -> float:
    return path.stat().st_size / 1048576


async def _fetch_and_transcribe(label: str, url: str) -> str:
    """先单独量下载（拿字节数/耗时），再走生产 ``transcribe`` 拿文本。

    不直接 ``recognize_wav`` 整段 wav——DashScope 有单文件体积上限（实测 400
    ``Multimodal file size is too large``），生产链路靠 ``slice_audio`` 切片绕开。
    """
    t0 = time.monotonic()
    src = await download_url(url, total_timeout=180.0)
    t_dl = time.monotonic() - t0
    wav = await convert_to_wav(src)
    dur = get_audio_duration(wav)
    size_mb = _mb(src)

    t1 = time.monotonic()
    text = await transcribe(MediaRef(platform="douyin", download_url=url, download_urls=[url]))
    t_rest = time.monotonic() - t1
    print(
        f"[{label}] 下载 {size_mb:.1f}MB/{t_dl:.1f}s  音频时长 {dur:.0f}s  "
        f"整条 transcribe {t_rest:.1f}s  文本 {len(text)} 字"
    )
    return text


async def main() -> None:
    async with httpx.AsyncClient() as client:
        sec_uid = await tikhub._resolve_sec_user_id(client, _ACCOUNT_URL)
        body = await tikhub._get_json(
            client,
            tikhub._PATH_USER_POST_VIDEOS,
            {"sec_user_id": sec_uid, "count": 5, "max_cursor": 0, "sort_type": 0},
        )
    items = (body.get("data") or {}).get("aweme_list") or []

    # 挑最短的一条压测，省下载时间和 ASR 费用。
    def _dur(it: dict) -> int:
        return int((it.get("video") or {}).get("duration") or 0)

    covered = sum(1 for it in items if ((it.get("video") or {}).get("bit_rate_audio")))
    print(f"覆盖率: {len(items)} 条中 {covered} 条带 bit_rate_audio\n")

    cands = [
        it
        for it in items
        if _dur(it) and ((it.get("video") or {}).get("bit_rate_audio"))
    ]
    if not cands:
        print("本页无带音频轨的条目 → 无法验证")
        return
    item = min(cands, key=_dur)
    video = item.get("video") or {}
    print(f"样本: {str(item.get('desc'))[:40]!r}  时长 {_dur(item) / 1000:.0f}s\n")

    # bit_rate_audio 形状不稳：实测既有 dict 也有 list（空则为 {}）——两种都要吃。
    node = video.get("bit_rate_audio")
    if isinstance(node, list):
        node = node[0] if node else {}
    audio_meta = (node or {}).get("audio_meta") or {}
    audio_urls = audio_meta.get("url_list") or {}
    audio_url = audio_urls.get("main_url") or audio_urls.get("backup_url")
    if not audio_url:
        print(
            f"bit_rate_audio 无可用直链: bit_rate_audio键={sorted((video.get('bit_rate_audio') or {}).keys())} "
            f"audio_meta键={sorted(audio_meta.keys())} url_list={audio_urls!r}"
        )
        covered = sum(1 for it in items if ((it.get("video") or {}).get("bit_rate_audio")))
        print(f"本页 {len(items)} 条中有 bit_rate_audio 的: {covered}")
        return

    video_url = ((video.get("play_addr") or {}).get("url_list") or [None])[0]

    text_audio = await _fetch_and_transcribe("纯音频轨", audio_url)
    text_video = await _fetch_and_transcribe("整片视频", video_url)

    print(f"\n音频轨前 80 字: {text_audio[:80]}")
    print(f"整片前 80 字:   {text_video[:80]}")
    same = text_audio[:200].strip() == text_video[:200].strip()
    print(f"\n前 200 字一致: {same} → {'可替代' if same else '需人工比对全文'}")


if __name__ == "__main__":
    asyncio.run(main())
