# Spike: 微信视频号单视频 decode（2026-08-02）

## 结论

**2a 通过。** 真实调用 TikHub `POST /api/v1/wechat_channels/v2/fetch_video_detail`
（`share_url` + `raw=false`）可拿到可下载 `full_url` 与配对 `decode_key`；
加密 MP4 前 128KiB 需 Isaac64 密钥流 XOR，**纯 Python Isaac64 与微信 WASM 不一致**，
生产路径 vendored WASM + `node` CLI。

## 样例（脱敏）

| 字段 | 值 |
|------|-----|
| share_url | `https://weixin.qq.com/sph/ADk6xBh2hq` |
| object id | `14919266588327413890` |
| nickname | 前进的胖掌柜 |
| title（归一后） | 职能部门正在杀死公司 |
| media.decode_key | 数字字符串（每次请求变化，例 `910035402`） |
| media.full_url | `url + url_token`（含鉴权 token，短时有效） |
| media.file_size | ~313MB（本样例） |
| media.duration | 570s |

原始响应字段（`raw=false`）：`data.{id,username,nickname,title,media}`；
`media.{url,url_token,full_url,decode_key,file_size,duration,...}`。

`title` 偶发为 `[{'shortTitle': '...', ...}]` 字符串——解析取 `shortTitle`。

## 算法

```
decode_key → WxIsaac64 WASM → 131072-byte keystream → reverse()
→ XOR 文件前 128KiB → MP4（bytes[4:8]==ftyp）
```

参考：
- [TikHub fetch_video_detail](https://docs.tikhub.io/472974842e0)
- [Evil0ctal/WeChat-Channels-Video-File-Decryption](https://github.com/Evil0ctal/WeChat-Channels-Video-File-Decryption)
- Node glue 改编自 [RongleCat/n8n-nodes-wechat-channels](https://github.com/RongleCat/n8n-nodes-wechat-channels)

## 实现落点

- `tikhub.channels_video_meta` / `resolve_media` 视频号分支
- `media/channels_decode.py` + `media/wechat_wasm/*`
- `transcribe.decode_media = decode_channels_media`
- **运行时依赖：** PATH 上有 `node`（Task 9 Dockerfile 需装 nodejs；与 ffmpeg 一并）

## 账号路径

视频号**只做单视频**；拆账号仍 douyin-only（Task 7 门禁不变）。
