"""基准：拆账号 TOP10 的转写段总墙钟（`analyze_account` 里占大头的那段）。

只跑 scrape + 10 路并发 `transcribe`，不落库、不调 LLM——正好覆盖「换低字节源」和
「转写缓存」两项改造影响的范围，改造前后各跑一次即可对比。

跑法（仓根，消耗 TikHub 取数 + 10 条下载 + 10 组 ASR）：
    uv run python docs/spikes/account_transcribe_bench.py
"""

from __future__ import annotations

import asyncio
import time

from app.datasource.media.constants import ACCOUNT_ITEM_CONCURRENCY
from app.datasource.tikhub import account_top_videos, video_meta_to_media_ref
from app.datasource.transcribe import transcribe

_ACCOUNT_URL = "https://v.douyin.com/LIVJSlsfTTU/"
_TOP_N = 10


async def main() -> None:
    t0 = time.monotonic()
    videos = await account_top_videos(_ACCOUNT_URL, n=_TOP_N)
    videos = videos[:_TOP_N]
    t_scrape = time.monotonic() - t0
    print(f"scrape: {len(videos)} 条 / {t_scrape:.1f}s  (item并发={ACCOUNT_ITEM_CONCURRENCY})\n")

    sem = asyncio.Semaphore(ACCOUNT_ITEM_CONCURRENCY)
    results: list[tuple[int, float, int, str]] = []

    async def one(i: int, v) -> None:
        async with sem:
            ts = time.monotonic()
            try:
                text = await transcribe(video_meta_to_media_ref(v))
                results.append((i, time.monotonic() - ts, len(text), ""))
            except Exception as exc:  # noqa: BLE001 — bench 要看全部失败原因
                results.append((i, time.monotonic() - ts, 0, str(exc)[:60]))

    t1 = time.monotonic()
    await asyncio.gather(*(one(i, v) for i, v in enumerate(videos)))
    t_transcribe = time.monotonic() - t1

    for i, dt, n, err in sorted(results):
        dur = videos[i].duration_sec or 0
        tag = f"FAIL {err}" if err else f"{n} 字"
        print(f"  [{i}] 时长{dur:>4}s  {dt:>6.1f}s  {tag}")

    ok = sum(1 for _, _, n, e in results if not e)
    print(
        f"\n转写段总墙钟: {t_transcribe:.1f}s  成功 {ok}/{len(videos)}"
        f"  含 scrape 合计 {time.monotonic() - t0:.1f}s"
    )


if __name__ == "__main__":
    asyncio.run(main())
