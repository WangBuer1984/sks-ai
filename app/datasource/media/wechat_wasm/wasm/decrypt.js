/**
 * 视频解密核心（纯函数，Node.js 端执行）
 *
 * 微信视频号仅加密文件前 KEYSTREAM_SIZE(128KB) 字节，其余为明文。
 * 解密无需浏览器：浏览器只负责用 WASM 生成 128KB 密钥流，XOR 在此完成。
 * 旧实现把整段视频经 CDP 管道传入浏览器，会触发 DevTools 协议 100MB 单条
 * 消息上限，导致大文件解密时页面崩溃（见 issue #4 / #6 / #8）。
 *
 * @module lib/decrypt
 */

// 微信加密作用的字节数（密钥流长度）
const KEYSTREAM_SIZE = 131072;

/**
 * 对加密视频执行 XOR 解密
 * @param {Buffer} encrypted 原始加密视频 Buffer
 * @param {Buffer} keystream 128KB 密钥流 Buffer
 * @returns {Buffer} 解密后的视频 Buffer（明文部分原样保留）
 */
function decryptBuffer(encrypted, keystream) {
    const decrypted = Buffer.from(encrypted); // 拷贝一份，明文部分原样保留
    const decryptLen = Math.min(KEYSTREAM_SIZE, encrypted.length, keystream.length);
    for (let i = 0; i < decryptLen; i++) {
        decrypted[i] = encrypted[i] ^ keystream[i];
    }
    return decrypted;
}

/**
 * 校验解密结果是否为合法 MP4（偏移 4 处应为 ftyp 签名）
 * @param {Buffer} buffer 解密后的视频 Buffer
 * @throws {Error} 当签名缺失时（通常是 decode_key 不匹配）
 */
function assertMp4(buffer) {
    const ftyp = buffer.toString('utf8', 4, 8);
    if (ftyp !== 'ftyp') {
        throw new Error('解密失败：未找到 MP4 ftyp 签名，请检查 decode_key');
    }
}

module.exports = { KEYSTREAM_SIZE, decryptBuffer, assertMp4 };
