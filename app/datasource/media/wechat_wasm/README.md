# WeChat Channels video decrypt (WASM)

Vendored runtime for Isaac64 keystream generation used by WeChat Channels
encrypted MP4 (first 128 KiB XOR).

- `wasm/wasm_video_decode.{js,wasm}`: WeChat client WASM (via community redistributions)
- `lib/wasm-decrypt.js` / `wasm/decrypt.js`: Node glue adapted from
  [RongleCat/n8n-nodes-wechat-channels](https://github.com/RongleCat/n8n-nodes-wechat-channels)
  and algorithm documented by
  [Evil0ctal/WeChat-Channels-Video-File-Decryption](https://github.com/Evil0ctal/WeChat-Channels-Video-File-Decryption)

Requires `node` on PATH at runtime. Decode is only invoked when `MediaRef.decode_key` is set.

## Integrity (Task 8 retro-review)

| File | Size | SHA-256 |
|------|------|---------|
| `wasm/wasm_video_decode.wasm` | 3785516 | `dca796bacec37d8522c7983b3945e5d579bd74164e3b21f0ebc773be6dfc8b6e` |

WeChat binary has no clear open-source license (reverse-engineered redistribution).
Accepted as product dependency for Channels download; do not expose as a public decrypt API.

## Failure semantics

Missing `node` / missing assets / CLI non-zero / output without `ftyp` → Python
`DataSourceError` from `channels_decode.decode_channels_media` (never bare OSError).
