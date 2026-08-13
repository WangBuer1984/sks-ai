"""transcribe 全管线分段计时基准：scrape → download → convert → probe → slice → asr → merge。

管线自带 per-stage log.info（download/decode/convert_wav/probe/asr elapsed），本脚本另加
scrape 与单条 total。每条 120s 超时防挂死堵 bench。

仓根跑：``uv run python docs/spikes/transcribe_pipeline_bench.py``
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

from app.datasource import DataSourceError  # noqa: E402
from app.datasource.tikhub import account_top_videos, video_meta_to_media_ref  # noqa: E402
from app.datasource.transcribe import transcribe  # noqa: E402

CASES = [
    ("抖音", "https://v.douyin.com/LIVJSlsfTTU/"),
    ("视频号", "https://weixin.qq.com/sph/A9zLUynrGY"),
]
N = 2
PER_VIDEO_TIMEOUT = 120  # 秒：防 300s 外层挂死堵 bench


async def bench_one(platform: str, url: str) -> None:
    print(f"\n===== {platform} {url} =====", flush=True)
    t = time.monotonic()
    try:
        videos = await account_top_videos(url, n=N)
    except DataSourceError as e:
        print(f"[{platform}] scrape FAILED: {e} ({time.monotonic() - t:.2f}s)", flush=True)
        return
    print(f"[{platform}] scrape top{N}: {time.monotonic() - t:.2f}s, got {len(videos)} videos", flush=True)

    for i, v in enumerate(videos):
        ref = video_meta_to_media_ref(v)
        print(
            f"[{platform}] video#{i}: title={v.title!r} play={v.play_count} "
            f"dur={getattr(v, 'duration_sec', None)} platform={ref.platform} "
            f"decode_key={'Y' if ref.decode_key else 'N'}",
            flush=True,
        )
        t2 = time.monotonic()
        try:
            text = await asyncio.wait_for(transcribe(ref), timeout=PER_VIDEO_TIMEOUT)
            dt = time.monotonic() - t2
            print(
                f"[{platform}] video#{i} transcribe TOTAL: {dt:.2f}s, "
                f"transcript_len={len(text)}",
                flush=True,
            )
            print(f"[{platform}] transcript preview: {text[:150]!r}", flush=True)
        except asyncio.TimeoutError:
            print(
                f"[{platform}] video#{i} transcribe TIMEOUT >{PER_VIDEO_TIMEOUT}s "
                f"(疑似挂死；看上方最后一条 step done 定位卡在哪段)",
                flush=True,
            )
        except DataSourceError as e:
            print(f"[{platform}] video#{i} transcribe FAILED: {e} ({time.monotonic() - t2:.2f}s)", flush=True)


async def main() -> None:
    for platform, url in CASES:
        await bench_one(platform, url)


if __name__ == "__main__":
    asyncio.run(main())
