const express = require('express');
const { z } = require('zod');
const { requireBridgeAuth } = require('../middleware/auth');

function buildSendMessageRouter({ sessionManager }) {
  const r = express.Router();

  const schema = z.object({
    session: z.string().min(1),
    phone: z.string().min(5),
    message: z.string().min(1)
  });

  // Django (or admin tooling) -> Node send
  r.post('/send-message', requireBridgeAuth, async (req, res) => {
    const parsed = schema.safeParse(req.body || {});
    if (!parsed.success) return res.status(400).json({ error: 'invalid_payload' });
    const { session, phone, message } = parsed.data;
    const wa = sessionManager.get(session);
    if (!wa) return res.status(404).json({ error: 'unknown_session' });
    try {
      const out = await wa.sendText({ phone, text: message });
      return res.json({ ok: true, message_id: out.message_id || '' });
    } catch (err) {
      return res.status(500).json({ ok: false, error: String(err) });
    }
  });

  // Transport endpoint expected by Django `WebJsBridgeTransport`
  // POST /api/whatsapp/send
  r.post('/api/whatsapp/send', requireBridgeAuth, async (req, res) => {
    const optionRow = z.object({
      id: z.string().min(1),
      title: z.string().min(1)
    });
    const zReq = z
      .object({
        account_id: z.string().min(1),
        to: z.string().min(5),
        text: z.string().optional(),
        buttons: z
          .object({
            body: z.string().min(1),
            options: z.array(optionRow).min(1).max(3)
          })
          .optional(),
        list: z
          .object({
            body: z.string().min(1),
            button_text: z.string().optional(),
            options: z.array(optionRow).min(1).max(10)
          })
          .optional()
      })
      .refine(
        (d) =>
          Boolean(d.text && String(d.text).trim()) || Boolean(d.buttons) || Boolean(d.list),
        { message: 'text_buttons_or_list_required' }
      );
    const parsed = zReq.safeParse(req.body || {});
    if (!parsed.success) return res.status(400).json({ ok: false, error: 'invalid_payload' });
    const { account_id, to, text, buttons, list } = parsed.data;
    const wa = sessionManager.get(account_id);
    if (!wa) return res.status(404).json({ ok: false, error: 'unknown_session' });
    try {
      let out;
      if (buttons) {
        out = await wa.sendButtons({
          phone: to,
          body: buttons.body,
          buttons: buttons.options.map((o) => ({ id: o.id, body: o.title }))
        });
      } else if (list) {
        out = await wa.sendList({
          phone: to,
          body: list.body,
          buttonText: list.button_text || 'Select',
          options: list.options.map((o) => ({ id: o.id, title: o.title }))
        });
      } else {
        out = await wa.sendText({ phone: to, text: String(text || '') });
      }
      return res.json({ ok: true, message_id: out.message_id || '' });
    } catch (err) {
      return res.status(500).json({ ok: false, error: String(err) });
    }
  });

  return r;
}

module.exports = { buildSendMessageRouter };

