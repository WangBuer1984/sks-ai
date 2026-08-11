"""N 路并发下载基准：测聚合速度，判断 CDN 限速是「每连接」还是「聚合」。

聚合 >> 串行单连接速度 → 每连接限速（抬 download_sem 有效）；
聚合 ≈ 或 < 串行 → 聚合限速（抬 download_sem 无用/有害）。

仓根跑：``uv run python docs/spikes/download_concurrent_bench.py``
"""
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

from app.datasource import DataSourceError  # noqa: E402
from app.datasource.media.download import download_url  # noqa: E402
from app.datasource.tikhub import account_top_videos, video_meta_to_media_ref  # noqa: E402

CASES = [
    ("抖音", "https://v.douyin.com/LIVJSlsfTTU/"),
    ("视频号", "https://weixin.qq.com/sph/A9zLUynrGY"),
]
N = 10
PER_DL_TIMEOUT = 120


async def _dl_one(idx, ref):
    t = time.monotonic()
    try:
        path = await asyncio.wait_for(
            download_url(ref.download_url, headers=ref.headers or None),
            timeout=PER_DL_TIMEOUT,
        )
        dt = time.monotonic() - t
        size = path.stat().st_size
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return idx, dt, size, None
    except asyncio.TimeoutError:
        return idx, time.monotonic() - t, 0, "TIMEOUT"
    except DataSourceError as e:
        return idx, time.monotonic() - t, 0, str(e)


async def bench(platform, url):
    print(f"\n===== {platform} {url} =====", flush=True)
    t = time.monotonic()
    try:
        videos = await account_top_videos(url, n=N)
    except DataSourceError as e:
        print(f"[{platform}] scrape FAILED: {e} ({time.monotonic() - t:.2f}s)", flush=True)
        return
    print(f"[{platform}] scrape top{N}: {time.monotonic() - t:.2f}s, got {len(videos)}", flush=True)
    refs = [video_meta_to_media_ref(v) for v in videos]

    t0 = time.monotonic()
    results = await asyncio.gather(*[_dl_one(i, r) for i, r in enumerate(refs)])
    wall = time.monotonic() - t0

    total_mb = 0
    ok = 0
    for idx, dt, size, err in results:
        if err:
            print(f"[{platform}] v{idx}: {err} ({dt:.2f}s)", flush=True)
        else:
            mb = size / 1_048_576
            total_mb += mb
            ok += 1
            print(
                f"[{platform}] v{idx}: {mb:.1f}MB in {dt:.2f}s = {mb / dt:.2f}MB/s",
                flush=True,
            )
    agg = total_mb / wall if wall > 0 else 0
    print(
        f"[{platform}] CONCURRENT {ok}/{len(refs)}: wall={wall:.2f}s, "
        f"total={total_mb:.1f}MB, aggregate={agg:.2f}MB/s",
        flush=True,
    )
    print(
        f"[{platform}] 判读：聚合 > 串行单连接 → 每连接限速（抬 sem 有效）；"
        f"聚合 ≤ 串行 → 聚合限速（抬 sem 无用/有害）。实际聚合={agg:.2f}MB/s",
        flush=True,
    )


async def main():
    for platform, url in CASES:
        await bench(platform, url)


if __name__ == "__main__":
    asyncio.run(main())
