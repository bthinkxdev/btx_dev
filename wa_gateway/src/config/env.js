const dotenv = require('dotenv');
dotenv.config();

function req(name) {
  const v = String(process.env[name] || '').trim();
  if (!v) throw new Error(`Missing required env var ${name}`);
  return v;
}

function int(name, def) {
  const raw = String(process.env[name] || '').trim();
  if (!raw) return def;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) ? n : def;
}

function list(name, def = []) {
  const raw = String(process.env[name] || '').trim();
  if (!raw) return def;
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function bool(name, def = false) {
  const raw = String(process.env[name] || '').trim().toLowerCase();
  if (!raw) return def;
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'y' || raw === 'on';
}

module.exports = {
  port: int('PORT', 3001),
  nodeEnv: String(process.env.NODE_ENV || 'development').trim(),
  logLevel: String(process.env.LOG_LEVEL || 'info').trim(),

  waSessions: list('WA_SESSIONS', []),

  djangoApiUrl: req('DJANGO_API_URL').replace(/\/+$/, ''),
  djangoApiToken: req('DJANGO_API_TOKEN'),

  bridgeToken: String(process.env.WHATSAPP_WEBJS_BRIDGE_TOKEN || '').trim(),

  waHeadless: bool('WA_HEADLESS', true),
  puppeteerExecutablePath: String(process.env.PUPPETEER_EXECUTABLE_PATH || '').trim(),

  qrInTerminal: bool('QR_IN_TERMINAL', false),

  humanTakeoverCooldownMinutes: int('HUMAN_TAKEOVER_COOLDOWN_MINUTES', 30),

  sendMinDelayMs: int('SEND_MIN_DELAY_MS', 1200),
  sendMaxDelayMs: int('SEND_MAX_DELAY_MS', 3500),
  sendQueueConcurrency: int('SEND_QUEUE_CONCURRENCY', 1),

  mediaMaxBytes: int('MEDIA_MAX_BYTES', 10 * 1024 * 1024)
};

