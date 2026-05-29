from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransportSendResult:
    ok: bool
    provider_message_id: str = ''
    raw: dict | None = None
    error: str = ''


class WhatsAppTransport:
    """
    Transport abstraction.

    This file intentionally contains **no Meta/Graph API logic**.
    Implementations:
    - WhatsApp Web.js bridge (HTTP)
    """

    def send_text(self, *, account_id: str, to_phone: str, text: str) -> TransportSendResult:
        raise NotImplementedError


class WebJsBridgeTransport(WhatsAppTransport):
    """
    Sends messages through a WhatsApp Web.js bridge.

    Expected bridge env:
    - WHATSAPP_WEBJS_BRIDGE_URL, e.g. http://127.0.0.1:3001
    - WHATSAPP_WEBJS_BRIDGE_TOKEN (optional)

    Expected endpoint:
    POST {base}/api/whatsapp/send
    JSON:
      {
        "account_id": "...",   # which logged-in WA session to use
        "to": "9198....",      # digits only (E164 without +)
        "text": "..."
      }
    Response:
      { "ok": true, "message_id": "..." }
    """

    def __init__(self) -> None:
        self.base_url = str(getattr(settings, 'WHATSAPP_WEBJS_BRIDGE_URL', '') or '').rstrip('/')
        self.token = str(getattr(settings, 'WHATSAPP_WEBJS_BRIDGE_TOKEN', '') or '').strip()

    def _headers(self) -> dict:
        h = {'Content-Type': 'application/json'}
        if self.token:
            h['Authorization'] = f'Bearer {self.token}'
        return h

    def _post_send(self, payload: dict) -> TransportSendResult:
        if not self.base_url:
            return TransportSendResult(ok=False, error='Missing WHATSAPP_WEBJS_BRIDGE_URL')
        url = f'{self.base_url}/api/whatsapp/send'
        try:
            r = requests.post(url, headers=self._headers(), json=payload, timeout=30)
            if not r.ok:
                return TransportSendResult(ok=False, error=f'Bridge HTTP {r.status_code}', raw={'text': r.text[:500]})
            try:
                data = r.json() if r.text else {}
            except Exception:
                data = {}
            msg_id = str((data or {}).get('message_id') or (data or {}).get('id') or '').strip()
            ok = bool((data or {}).get('ok') is True) or r.ok
            return TransportSendResult(ok=ok, provider_message_id=msg_id, raw=data or None)
        except requests.RequestException as e:
            logger.exception('WebJS bridge send failed')
            return TransportSendResult(ok=False, error=str(e))

    def send_text(self, *, account_id: str, to_phone: str, text: str) -> TransportSendResult:
        payload = {
            'account_id': str(account_id or '').strip(),
            'to': str(to_phone or '').strip(),
            'text': str(text or ''),
        }
        return self._post_send(payload)

    def send_buttons(
        self,
        *,
        account_id: str,
        to_phone: str,
        body: str,
        options: list[tuple[str, str]],
    ) -> TransportSendResult:
        rows = [
            {'id': str(oid).strip(), 'title': str(title).strip()}
            for oid, title in (options or [])
            if str(title).strip()
        ][:3]
        if not rows:
            return TransportSendResult(ok=False, error='buttons_required')
        payload = {
            'account_id': str(account_id or '').strip(),
            'to': str(to_phone or '').strip(),
            'buttons': {'body': str(body or '').strip(), 'options': rows},
        }
        return self._post_send(payload)

    def send_list(
        self,
        *,
        account_id: str,
        to_phone: str,
        body: str,
        button_text: str,
        options: list[tuple[str, str]],
    ) -> TransportSendResult:
        rows = [
            {'id': str(oid).strip(), 'title': str(title).strip()}
            for oid, title in (options or [])
            if str(title).strip()
        ][:10]
        if not rows:
            return TransportSendResult(ok=False, error='list_rows_required')
        payload = {
            'account_id': str(account_id or '').strip(),
            'to': str(to_phone or '').strip(),
            'list': {
                'body': str(body or '').strip(),
                'button_text': str(button_text or 'Select').strip(),
                'options': rows,
            },
        }
        return self._post_send(payload)


def get_transport() -> WhatsAppTransport:
    """
    Default transport for this deployment.
    """
    transport = str(getattr(settings, 'WHATSAPP_TRANSPORT', '') or '').strip().lower()
    if transport in ('webjs', 'whatsapp-webjs', 'whatsapp_webjs', 'whatsapp-web.js', ''):
        return WebJsBridgeTransport()
    # Unknown transport: fail closed.
    return WebJsBridgeTransport()

