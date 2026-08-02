# WeChat Channels video decrypt (WASM)

Vendored runtime for Isaac64 keystream generation used by WeChat Channels
encrypted MP4 (first 128 KiB XOR).

- `wasm/wasm_video_decode.{js,wasm}`: WeChat client WASM (via community redistributions)
- `lib/wasm-decrypt.js` / `wasm/decrypt.js`: Node glue adapted from
  [RongleCat/n8n-nodes-wechat-channels](https://github.com/RongleCat/n8n-nodes-wechat-channels)
  and algorithm documented by
  [Evil0ctal/WeChat-Channels-Video-File-Decryption](https://github.com/Evil0ctal/WeChat-Channels-Video-File-Decryption)

Requires `node` on PATH at runtime. Decode is only invoked when `MediaRef.decode_key` is set.
