## WhatsApp Web.js Gateway (Multi-executive)

This is the **transport/provider layer only**. Your Django CRM remains the source of truth for:
- lead ownership & assignment
- WhatsAppConversation / WhatsAppMessage persistence
- bot + funnel logic
- followups + analytics

The gateway handles:
- multiple `whatsapp-web.js` sessions (LocalAuth)
- QR login per executive session
- inbound forwarding to Django `/api/wa/incoming/`
- outbound sending for Django transport endpoint `/api/whatsapp/send`
- human takeover cooldown (bot pause signal)
- dedupe + per-session send queue

### Folder structure

- `src/`
  - `config/` env loader
  - `middleware/` auth
  - `routes/` admin + send routes
  - `services/` session manager, Django client, queues
  - `utils/` logger + dedupe
- `logs/` (created at runtime)
- `sessions/` LocalAuth persistence (created at runtime)

### Environment

Copy `.env.example` to `.env` and fill:

- `DJANGO_API_URL` points to your Django app base URL
- `DJANGO_API_TOKEN` must match Django `DJANGO_API_TOKEN` (or Django falls back to `WHATSAPP_WEBJS_BRIDGE_TOKEN`)
- `WHATSAPP_WEBJS_BRIDGE_TOKEN` must match Django `WHATSAPP_WEBJS_BRIDGE_TOKEN`
- `WA_SESSIONS=admin1,admin2,admin3` (these must match your `WhatsAppNumber.phone_number_id` values for routing)

### Install + run (local)

```bash
cd wa_gateway
npm install
npm run dev
```

Scan each QR in the terminal using the corresponding executive’s WhatsApp mobile app:
`Linked devices` → `Link a device`.

### Admin APIs (secured with Bearer token)

Use header `Authorization: Bearer <WHATSAPP_WEBJS_BRIDGE_TOKEN>`

- `GET /sessions`
- `GET /session/:id/status`
- `POST /session/:id/restart`
- `POST /session/:id/logout`

### Outbound send API (secured)

- `POST /send-message`

Body:
```json
{ "session": "admin1", "phone": "917736094292", "message": "Hello" }
```

### Transport endpoint used by Django

Django `WebJsBridgeTransport` calls:
- `POST /api/whatsapp/send`

Body:
```json
{ "account_id": "admin1", "to": "917736094292", "text": "..." }
```

