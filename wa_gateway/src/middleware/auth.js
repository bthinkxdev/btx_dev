const crypto = require('crypto');
const env = require('../config/env');

function bearerToken(req) {
  const h = String(req.headers.authorization || '').trim();
  if (!h) return '';
  if (h.toLowerCase().startsWith('bearer ')) return h.slice(7).trim();
  return h;
}

function constantEq(a, b) {
  const aa = Buffer.from(String(a || ''), 'utf8');
  const bb = Buffer.from(String(b || ''), 'utf8');
  if (aa.length !== bb.length) return false;
  return crypto.timingSafeEqual(aa, bb);
}

function requireBridgeAuth(req, res, next) {
  // For Django -> Node transport calls (send)
  const expected = String(env.bridgeToken || '').trim();
  if (!expected) return res.status(500).json({ error: 'bridge_token_not_configured' });
  const provided = bearerToken(req);
  if (!provided || !constantEq(provided, expected)) return res.status(401).json({ error: 'unauthorized' });
  return next();
}

function requireAdminAuth(req, res, next) {
  // For operators calling session/admin APIs, reuse bridge token for simplicity
  return requireBridgeAuth(req, res, next);
}

module.exports = { requireBridgeAuth, requireAdminAuth };

