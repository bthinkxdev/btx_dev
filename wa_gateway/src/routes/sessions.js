const express = require('express');
const { requireAdminAuth } = require('../middleware/auth');

function buildSessionsRouter({ sessionManager }) {
  const r = express.Router();

  r.get('/sessions', requireAdminAuth, (req, res) => {
    return res.json({ sessions: sessionManager.statusAll() });
  });

  r.get('/session/:id/status', requireAdminAuth, (req, res) => {
    const id = String(req.params.id || '').trim();
    const wa = sessionManager.get(id);
    if (!wa) return res.status(404).json({ error: 'unknown_session' });
    return res.json(wa.status());
  });

  r.post('/session/:id/restart', requireAdminAuth, async (req, res) => {
    const id = String(req.params.id || '').trim();
    try {
      await sessionManager.restart(id);
      return res.json({ ok: true });
    } catch (err) {
      return res.status(400).json({ ok: false, error: String(err) });
    }
  });

  r.post('/session/:id/logout', requireAdminAuth, async (req, res) => {
    const id = String(req.params.id || '').trim();
    try {
      await sessionManager.logout(id);
      return res.json({ ok: true });
    } catch (err) {
      return res.status(400).json({ ok: false, error: String(err) });
    }
  });

  return r;
}

module.exports = { buildSessionsRouter };

