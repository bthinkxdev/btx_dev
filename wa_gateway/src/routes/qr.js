const express = require('express');
const QRCode = require('qrcode');

function isLocalhost(req) {
  const ip = String(req.ip || '');
  return ip === '127.0.0.1' || ip === '::1' || ip.endsWith('127.0.0.1');
}

function tokenOk(req, expectedToken) {
  const t = String(req.query.token || '').trim();
  return Boolean(expectedToken && t && t === expectedToken);
}

function requireQrAccess({ token }) {
  return (req, res, next) => {
    if (isLocalhost(req) || tokenOk(req, token)) return next();
    return res.status(401).send('Unauthorized');
  };
}

function htmlPage({ title, body }) {
  return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>${title}</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 24px; }
      .wrap { max-width: 760px; margin: 0 auto; }
      .qr { width: min(var(--qr-size, 280px), 88vw); height: auto; display:block; border: 1px solid #ddd; }
      code { background: #f6f8fa; padding: 2px 6px; border-radius: 6px; }
      .muted { color: #666; }
    </style>
  </head>
  <body>
    <div class="wrap">
      ${body}
    </div>
  </body>
</html>`;
}

function buildQrRouter({ sessionManager, qrToken }) {
  const r = express.Router();
  const guard = requireQrAccess({ token: qrToken });

  r.get('/qr', guard, (req, res) => {
    const sessions = sessionManager.statusAll();
    const items = sessions
      .map((s) => {
        const url = `/qr/${encodeURIComponent(s.session)}`;
        return `<li><a href="${url}">${s.session}</a> <span class="muted">(${s.state})</span></li>`;
      })
      .join('');
    res.send(
      htmlPage({
        title: 'WhatsApp QR Sessions',
        body: `<h2>WhatsApp sessions</h2><p class="muted">Open a session to scan the QR.</p><ul>${items}</ul>`
      })
    );
  });

  r.get('/qr/:id', guard, async (req, res) => {
    const id = String(req.params.id || '').trim();
    const qr = sessionManager.getLastQr(id);
    if (!qr) {
      return res.send(
        htmlPage({
          title: `QR - ${id}`,
          body: `<h2>Session <code>${id}</code></h2><p class="muted">No QR currently available. If the session is already logged in, you won't see a QR.</p>`
        })
      );
    }
    const rawSize = String(req.query.size || '').trim();
    let size = Number.parseInt(rawSize, 10);
    if (!Number.isFinite(size)) size = 280;
    size = Math.max(160, Math.min(size, 360)); // keep fully visible on most screens

    const dataUrl = await QRCode.toDataURL(qr, { margin: 1, width: size });
    res.send(
      htmlPage({
        title: `QR - ${id}`,
        body: `<h2>Scan QR for <code>${id}</code></h2>
<div style="--qr-size:${size}px">
  <img class="qr" src="${dataUrl}" alt="QR"/>
</div>
<p class="muted">Tip: use <code>?size=220</code> or <code>?size=320</code> to resize.</p>
<p class="muted">On phone: WhatsApp → Linked devices → Link a device.</p>`
      })
    );
  });

  return r;
}

module.exports = { buildQrRouter };

