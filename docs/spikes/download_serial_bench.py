"""串行下载基准：每连接独占带宽，量单文件速度/大小。

仓根跑：``uv run python docs/spikes/download_serial_bench.py``
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
N = 3
PER_DOWNLOAD_TIMEOUT = 60  # 秒


async def bench_dl(platform: str, url: str) -> None:
    print(f"\n===== {platform} {url} =====", flush=True)
    t = time.monotonic()
    try:
        videos = await account_top_videos(url, n=N)
    except DataSourceError as e:
        print(f"[{platform}] scrape FAILED: {e} ({time.monotonic() - t:.2f}s)", flush=True)
        return
    print(f"[{platform}] scrape top{N}: {time.monotonic() - t:.2f}s, got {len(videos)}", flush=True)

    for i, v in enumerate(videos):
        ref = video_meta_to_media_ref(v)
        print(
            f"[{platform}] v{i}: dur={getattr(v, 'duration_sec', None)}s "
            f"platform={ref.platform} decode_key={'Y' if ref.decode_key else 'N'}",
            flush=True,
        )
        t2 = time.monotonic()
        try:
            path = await asyncio.wait_for(
                download_url(ref.download_url, headers=ref.headers or None),
                timeout=PER_DOWNLOAD_TIMEOUT,
            )
            dt = time.monotonic() - t2
            size = path.stat().st_size
            mbs = (size / 1_048_576) / dt if dt > 0 else 0
            print(
                f"[{platform}] v{i} download OK: {dt:.2f}s, "
                f"{size / 1_048_576:.1f}MB, {mbs:.2f}MB/s -> {path.name}",
                flush=True,
            )
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        except asyncio.TimeoutError:
            print(
                f"[{platform}] v{i} download TIMEOUT >{PER_DOWNLOAD_TIMEOUT}s "
                f"(CDN stalled 或极慢；download.py 内层 read=30s 应已先触)",
                flush=True,
            )
        except DataSourceError as e:
            print(f"[{platform}] v{i} download FAILED: {e} ({time.monotonic() - t2:.2f}s)", flush=True)


async def main() -> None:
    for platform, url in CASES:
        await bench_dl(platform, url)


if __name__ == "__main__":
    asyncio.run(main())
