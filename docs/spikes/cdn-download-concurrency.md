# CDN 下载并发基准（2026-08）

## 起因

拆账号 TOP10 偶发 27min 挂死、progress 恒 0%。怀疑 ASR 慢或下载慢，用真实抖音/视频号
地址跑基准定位。**结论：瓶颈是 CDN 聚合限速 + stall×600s 超时，与 ASR 路数/信号量抬高无关。**

## 测试地址

- 抖音：`https://v.douyin.com/LIVJSlsfTTU/`（账号主页，zjcdn CDN）
- 视频号：`https://weixin.qq.com/sph/A9zLUynrGY`（腾讯 CDN，带 decode_key）

## 1. 串行下载（每连接独占带宽）

| 平台 | 视频 | 时长 | 大小 | 下载耗时 | 速度 |
|---|---|---|---|---|---|
| 抖音 | v0 | 929s | 37.3MB | 55.75s | 0.67 MB/s |
| 抖音 | v1 | 543s | 23.8MB | 34.21s | 0.70 MB/s |
| 抖音 | v2 | 359s | 41.8MB | 58.19s | 0.72 MB/s |
| 视频号 | v0 | 542s | 14.9MB | 13.27s | 1.13 MB/s |
| 视频号 | v1 | 424s | 25.7MB | 22.79s | 1.13 MB/s |
| 视频号 | v2 | 359s | 26.2MB | 23.20s | 1.13 MB/s |

单连接就限速（0.7/1.1 MB/s），远低于正常宽带 5-20 MB/s → **CDN 服务端限速**，非本地网络瓶颈。

## 2. 10 路并发下载（聚合速度）

| 平台 | 串行单连接 | 10 路并发聚合 | 并发每连接 | 结果 |
|---|---|---|---|---|
| 抖音 | 0.70 MB/s | **0.36 MB/s** | 0.12-0.16 | 7/10 超时 |
| 视频号 | 1.13 MB/s | **0.39 MB/s** | 0.12-0.14 | 6/10 超时 |

**两个 CDN 均按客户端聚合限速**，且并发连接越多、聚合反降（连接争用 / CDN 主动惩罚多连接）。
10 路并发比串行慢一半，还触发大量超时。→ **抬 download_sem 不仅无用、反而有害。**

## 3. transcribe 管线分段（佐证下载是瓶颈）

抖音 video#0（929s/37MB）跑 transcribe，60s bench 超时内**未打出任何 "step done"**（连
download done 都没），但 httpx 200 已返回 → 卡在下载 body 流式落盘。ASR 段根本没机会跑。
视频都偏长（抖音 359-929s，均 ~610s），按 270s/片切 → 10 条 ≈ 25-30 次 ASR；但 ASR 即便
30 次、asr_sem=5、每次 ~10s = 30s，仍远小于下载时间。

## 27min 挂死根因

正常下载 55s 能完，但偶发 stall（0 字节）时 `download.py` 原 `httpx.Timeout(600, connect=30)`
要烧 **600s** 才超时；2-3 条 stall 串起来 = 20-30 min。叠加旧 progress bug（gather 后才更新
进度）→ 全程 0%、看不到。**不是常态吞吐慢，是 stall×600s + 无可见性。**

## 落地改动

| 改动 | 旧 | 新 | 依据 |
|---|---|---|---|
| `_DOWNLOAD_TIMEOUT.read` | 600s | **30s** | httpx read=块间无数据上限，0.7MB/s 持续流不触；stall 30s 即 DataSourceError，不烧 600s |
| `download_sem` | 5 | **2** | 并发越多越慢（10 路 0.36 < 串行 0.70）；2 = 少量流水线重叠不触发争用 |
| `asr_sem` | (曾 10) | **5** | ASR 非瓶颈；RPM=100 下 5 路留余量给多任务 |
| `ACCOUNT_ITEM_CONCURRENCY` | 3 | **10** | item 层不瓶颈、stage 层限流；下载/转码/结构化跨条与 ASR 重叠 |
| progress 递增 | gather 后 | **_process_item 内逐条** | 可见性：该条全成即递增 |

**正常态预期**：10 条抖音长视频 ≈ 8-9min（下载 350MB@0.7MB/s ≈ 500s 固有 + ASR/结构化藏后面）；
stall 态 +30s/条 跳过。比 27min 强一个量级，且进度条会动。

下载 ~8min 是 CDN 限速的**固有成本**，无法从客户端绕过（除非代理/IP 轮换，有 ToS 风险，另议）。

## 复跑脚本

需在仓根跑（`.env` + `app/` 在仓根）：

```bash
uv run python docs/spikes/download_serial_bench.py        # 串行单连接，每条速度/大小
uv run python docs/spikes/download_concurrent_bench.py    # 10 路并发，聚合速度
uv run python docs/spikes/transcribe_pipeline_bench.py    # 全管线分段计时（download→asr）
```

调 `download_sem` 前先跑并发脚本对照聚合速度，避免拍脑袋。
