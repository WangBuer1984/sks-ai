'use strict';
/**
 * 视频号加密 MP4 解密 CLI：node decrypt_cli.js <enc> <decode_key> <out>
 * 依赖同目录 wasm/ + lib/wasm-decrypt.js（微信官方 WASM Isaac64）。
 */
const fs = require('fs');
const path = require('path');
const { createDecryptor } = require('./lib/wasm-decrypt');

async function main() {
  const [encPath, decodeKey, outPath] = process.argv.slice(2);
  if (!encPath || !decodeKey || !outPath) {
    console.error('usage: node decrypt_cli.js <encrypted> <decode_key> <output>');
    process.exit(2);
  }
  if (!fs.existsSync(encPath)) {
    console.error('encrypted file not found:', encPath);
    process.exit(2);
  }
  const wasmDir = path.join(__dirname, 'wasm');
  const dec = await createDecryptor(wasmDir);
  await dec.decryptFile(encPath, decodeKey, outPath);
  process.stdout.write('ok\n');
}

main().catch((e) => {
  console.error(e && (e.stack || e.message || e));
  process.exit(1);
});
