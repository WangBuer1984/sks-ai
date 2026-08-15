"""探针：抖音 aweme.video 是否带多档码率（bit_rate 数组）。

决定「只下最低码率档给 ASR」这条优化能不能做——ASR 只要 16k 单声道音频，
若上游给了 540p/360p 低档直链，字节数可直接砍数倍，而下载是当前唯一的大头。

跑法（仓根，读 .env 的 TIKHUB_API_KEY，约消耗 2 次 TikHub 调用）：
    uv run python docs/spikes/douyin_bitrate_probe.py
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.datasource import tikhub

_ACCOUNT_URL = "https://v.douyin.com/LIVJSlsfTTU/"


def _fmt_mb(n: object) -> str:
    try:
        return f"{int(n) / 1048576:.1f}MB"
    except (TypeError, ValueError):
        return "?"


async def main() -> None:
    async with httpx.AsyncClient() as client:
        sec_uid = await tikhub._resolve_sec_user_id(client, _ACCOUNT_URL)
        print(f"sec_user_id: {sec_uid}")
        body = await tikhub._get_json(
            client,
            tikhub._PATH_USER_POST_VIDEOS,
            {"sec_user_id": sec_uid, "count": 2, "max_cursor": 0, "sort_type": 0},
        )
        items = (body.get("data") or {}).get("aweme_list") or []
        print(f"aweme_list: {len(items)} 条\n")

        for i, item in enumerate(items[:2]):
            video = item.get("video") or {}
            print(f"--- video[{i}] desc={str(item.get('desc'))[:30]!r}")
            print(f"    video 顶层键: {sorted(video.keys())}")

            play_addr = video.get("play_addr") or {}
            print(
                f"    play_addr: data_size={_fmt_mb(play_addr.get('data_size'))} "
                f"urls={len(play_addr.get('url_list') or [])}"
            )

            gears = video.get("bit_rate")
            if not isinstance(gears, list) or not gears:
                print("    bit_rate: 无 → 只能下默认档")
            else:
                print(f"    bit_rate: {len(gears)} 档")
                for g in gears:
                    if not isinstance(g, dict):
                        continue
                    addr = g.get("play_addr") or {}
                    print(
                        f"      gear={g.get('gear_name')} quality={g.get('quality_type')} "
                        f"br={g.get('bit_rate')} size={_fmt_mb(addr.get('data_size'))} "
                        f"urls={len(addr.get('url_list') or [])}"
                    )

            # 纯音频轨——若存在，ASR 只需它，字节数比最低视频档再低一个量级。
            for key in ("bit_rate_audio", "audio"):
                node = video.get(key)
                if not node:
                    print(f"    {key}: 无")
                    continue
                sample = node[0] if isinstance(node, list) and node else node
                if not isinstance(sample, dict):
                    print(f"    {key}: {type(node).__name__}")
                    continue
                meta = sample.get("audio_meta") or {}
                urls = meta.get("url_list") or {}
                main_url = urls.get("main_url") or urls.get("backup_url") or ""
                print(
                    f"    {key}: quality={meta.get('quality')} "
                    f"size={_fmt_mb(meta.get('size'))} "
                    f"loudness_keys={sorted(k for k in meta if k != 'url_list')}"
                )
                print(f"      audio_url={main_url[:90]}…")
            print()


if __name__ == "__main__":
    asyncio.run(main())
