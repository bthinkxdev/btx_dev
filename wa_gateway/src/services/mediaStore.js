const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

function safeExt(mime) {
  const m = String(mime || '').toLowerCase();
  if (m.includes('jpeg')) return '.jpg';
  if (m.includes('png')) return '.png';
  if (m.includes('gif')) return '.gif';
  if (m.includes('webp')) return '.webp';
  if (m.includes('pdf')) return '.pdf';
  if (m.includes('mp4')) return '.mp4';
  if (m.includes('mpeg') || m.includes('mp3')) return '.mp3';
  if (m.includes('ogg')) return '.ogg';
  return '';
}

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function writeTempMedia({ base64, mimeType, maxBytes }) {
  const buf = Buffer.from(String(base64 || ''), 'base64');
  if (!buf.length) throw new Error('empty_media');
  if (buf.length > maxBytes) throw new Error('media_too_large');
  const dir = path.resolve(process.cwd(), 'tmp_media');
  ensureDir(dir);
  const id = crypto.randomBytes(16).toString('hex');
  const filePath = path.join(dir, `${id}${safeExt(mimeType)}`);
  fs.writeFileSync(filePath, buf);
  return { filePath, bytes: buf.length, id };
}

module.exports = { writeTempMedia };

