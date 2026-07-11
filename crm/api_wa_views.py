from __future__ import annotations

import hmac
import json
import logging
import threading
from dataclasses import dataclass

from django.conf import settings  # noqa: F401 — used in wa_status logging
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from crm.models import WhatsAppConversation, WhatsAppMessage
from crm.services.ai.voice_processor import is_voice_message
from crm.services.crm import conversation_phone_key
from crm.services.whatsapp import (
    get_active_wa_number,
    get_or_create_conversation,
    handle_message,
    handle_voice_message,
    mask_phone,
)
from crm.services.wa_gateway_sync import apply_gateway_status, executive_bot_enabled
from crm.services.whatsapp_bot import record_human_takeover

logger = logging.getLogger(__name__)


def _normalize_bearer(auth_header: str | None) -> str:
    raw = str(auth_header or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw.strip()


def _auth_ok(request) -> bool:
    """
    Auth for Node <-> Django APIs.

    Supports either:
    - Bearer token: header Authorization: Bearer <token>
      Uses env/settings DJANGO_API_TOKEN (preferred) or WHATSAPP_WEBJS_BRIDGE_TOKEN (back-compat)

    - HMAC signature (optional future): X-Signature with shared secret
      Not enabled by default in this CRM.
    """
    expected = (
        str(getattr(settings, "DJANGO_API_TOKEN", "") or "").strip()
        or str(getattr(settings, "WHATSAPP_WEBJS_BRIDGE_TOKEN", "") or "").strip()
    )
    if not expected:
        logger.error("Missing Django API token configuration")
        return False
    provided = _normalize_bearer(request.headers.get("Authorization"))
    return bool(provided and hmac.compare_digest(provided, expected))


def _json_body(request) -> dict:
    try:
        return json.loads(request.body.decode("utf-8"))
    except Exception:
        return {}


@dataclass(frozen=True)
class IncomingPayload:
    session: str
    phone: str
    message: str
    message_id: str
    timestamp: int | None
    message_type: str
    chat_id: str
    media: dict | None
    raw: dict | None


def _is_bot_text_message(message_type: str, message: str) -> bool:
    """
  WhatsApp Web.js uses type "chat" for normal text messages.
  Only skip obvious system/notification events.
    """
    mt = str(message_type or "").strip().lower()
    if mt in {"notification_template", "e2e_notification", "protocol", "call_log"}:
        return False
    if mt in {"text", "chat", "buttons_response", "list_response", ""}:
        return bool(str(message or "").strip())
    return False


def _parse_incoming(data: dict) -> IncomingPayload | None:
    session = str(data.get("session") or data.get("account_id") or "").strip()
    phone = str(data.get("phone") or data.get("from") or data.get("customer_phone") or "").strip()
    message = str(data.get("message") or data.get("text") or "").strip()
    message_id = str(data.get("message_id") or data.get("id") or "").strip()
    ts = data.get("timestamp")
    try:
        ts_int = int(ts) if ts is not None and str(ts).strip() else None
    except Exception:
        ts_int = None
    message_type = str(data.get("message_type") or data.get("type") or "text").strip().lower()
    chat_id = str(data.get("chat_id") or data.get("chatId") or "").strip()
    media = data.get("media") if isinstance(data.get("media"), dict) else None
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else (data if isinstance(data, dict) else None)

    if not session or (not phone and not chat_id):
        return None
    return IncomingPayload(
        session=session,
        phone=phone,
        message=message,
        message_id=message_id,
        timestamp=ts_int,
        message_type=message_type,
        chat_id=chat_id,
        media=media,
        raw=raw,
    )


@csrf_exempt
@require_http_methods(["POST"])
def wa_incoming(request):
    """
    Node -> Django: inbound WhatsApp events.

    IMPORTANT:
    - This endpoint **does not duplicate CRM bot logic**.
    - It delegates to existing `crm.services.whatsapp.handle_message()`, which already:
      - upserts leads
      - routes by WhatsAppNumber (executive ownership)
      - stores WhatsAppConversation/WhatsAppMessage
      - applies bot/human takeover gating
      - sends replies using the configured transport (WebJS bridge)
    """
    if not _auth_ok(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    data = _json_body(request)
    incoming = _parse_incoming(data)
    if not incoming:
        return JsonResponse({"error": "invalid_payload"}, status=400)

    logger.info(
        "wa_incoming session=%s phone=%s type=%s id=%s",
        incoming.session,
        mask_phone(incoming.phone),
        incoming.message_type,
        incoming.message_id[:24] if incoming.message_id else "",
    )

    wa_number = get_active_wa_number(incoming.session)
    if wa_number:
        exec_user = wa_number.executive
        logger.info(
            'wa_incoming route session=%s -> exec=%s bot=%s display=%s',
            incoming.session,
            getattr(exec_user, 'username', None),
            executive_bot_enabled(exec_user),
            wa_number.display_phone_number,
        )
    else:
        logger.error(
            'wa_incoming unknown session=%s — no active WhatsAppNumber with phone_number_id=%s',
            incoming.session,
            incoming.session,
        )
    provider_dt = None
    if incoming.timestamp:
        try:
            provider_dt = timezone.datetime.fromtimestamp(incoming.timestamp, tz=timezone.utc)
        except Exception:
            provider_dt = None

    # Extra server-side idempotency: avoid duplicate processing during retries.
    if incoming.message_id:
        cache_key = f"wa:webjs:incoming:{incoming.session}:{incoming.message_id}"
        if cache.get(cache_key):
            logger.info(
                "Deduped incoming message session=%s id=%s phone=%s",
                incoming.session,
                incoming.message_id,
                mask_phone(incoming.phone),
            )
            return JsonResponse(
                {"reply": "", "reply_type": "text", "stop_bot": False, "deduped": True}
            )
        cache.set(cache_key, True, timeout=24 * 60 * 60)

    if not _is_bot_text_message(incoming.message_type, incoming.message):
        if wa_number and is_voice_message(incoming.message_type, incoming.media):
            handle_voice_message(
                incoming.phone,
                wa_number=wa_number,
                message_id=incoming.message_id,
                raw_payload=data,
                provider_timestamp=provider_dt,
                media=incoming.media,
                message_type=incoming.message_type,
            )
            return JsonResponse(
                {"reply": "", "reply_type": "text", "stop_bot": False, "accepted": True}
            )

        # Other non-text: persist only (images/docs — no auto-reply yet).
        if wa_number:
            convo = get_or_create_conversation(
                wa_number=wa_number, customer_phone=incoming.phone, lead=None
            )
            try:
                WhatsAppMessage.objects.create(
                    conversation=convo,
                    wa_number=convo.wa_number,
                    executive=convo.executive,
                    lead=convo.lead,
                    direction=WhatsAppMessage.Direction.INBOUND,
                    source=WhatsAppMessage.Source.SYSTEM,
                    message_id=incoming.message_id,
                    customer_phone=incoming.phone,
                    text=incoming.message[:2000],
                    message_type=incoming.message_type,
                    status=WhatsAppMessage.Status.RECEIVED,
                    provider_timestamp=provider_dt,
                    raw_payload=incoming.raw or None,
                )
            except Exception:
                logger.exception("Failed to persist non-text inbound message")
        return JsonResponse(
            {"reply": "", "reply_type": "text", "stop_bot": False, "accepted": True}
        )

    def _process_inbound():
        try:
            handle_message(
                incoming.phone,
                incoming.message,
                wa_number=wa_number,
                message_id=incoming.message_id,
                raw_payload=data,
                provider_timestamp=provider_dt,
            )
        except Exception:
            logger.exception(
                "handle_message failed session=%s phone=%s",
                incoming.session,
                mask_phone(incoming.phone),
            )

    # Return immediately so Node gateway is not blocked by Gemini (can take 10–60s).
    threading.Thread(target=_process_inbound, daemon=True).start()
    return JsonResponse({"reply": "", "reply_type": "text", "stop_bot": False, "accepted": True})


@csrf_exempt
@require_http_methods(["POST"])
def wa_status(request):
    """
    Node -> Django: session + operational events (status, human takeover, resume).

    This endpoint is intentionally lightweight and uses cache for status snapshots.
    """
    if not _auth_ok(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    data = _json_body(request)
    session = str(data.get("session") or "").strip()
    event = str(data.get("event") or data.get("status") or "").strip().lower()
    phone = str(data.get("phone") or "").strip()

    if not session:
        return JsonResponse({"error": "invalid_payload"}, status=400)

    linked_phone = str(data.get("linked_phone") or data.get("linkedPhone") or "").strip()
    pushname = str(data.get("pushname") or "").strip()
    reason = str(data.get("reason") or data.get("message") or "").strip()
    apply_gateway_status(
        session_id=session,
        event=event,
        linked_phone=linked_phone,
        pushname=pushname,
        reason=reason,
    )

    cache.set(
        f"wa:webjs:session_status:{session}",
        {
            "session": session,
            "event": event or "unknown",
            "at": timezone.now().isoformat(),
            "meta": {k: v for k, v in (data or {}).items() if k not in {"token"}},
        },
        timeout=24 * 60 * 60,
    )

    if event in {"human_takeover", "human", "manual_message"} and phone:
        wa_number = get_active_wa_number(session)
        if wa_number:
            chat_id = str(data.get("chat_id") or data.get("chatId") or "").strip()
            convo_key = conversation_phone_key(phone, chat_id)
            convo = get_or_create_conversation(
                wa_number=wa_number, customer_phone=convo_key, lead=None
            )
            record_human_takeover(convo)
            logger.info(
                "Bot paused (human takeover, %s min) session=%s phone=%s convo=%s",
                getattr(settings, "WHATSAPP_HUMAN_TAKEOVER_COOLDOWN_MINUTES", 30),
                session,
                mask_phone(phone),
                convo.id,
            )
        return JsonResponse({"ok": True, "paused": True})

    if event in {"resume_bot", "bot_resume"} and phone:
        wa_number = get_active_wa_number(session)
        if wa_number:
            convo = WhatsAppConversation.objects.filter(
                wa_number=wa_number, customer_phone=phone
            ).first()
            if convo and not convo.bot_enabled:
                convo.bot_enabled = True
                convo.bot_disabled_reason = ""
                convo.human_takeover_at = None
                convo.save(
                    update_fields=[
                        "bot_enabled",
                        "bot_disabled_reason",
                        "human_takeover_at",
                        "updated_at",
                    ]
                )
                logger.info(
                    "Bot resumed session=%s phone=%s convo=%s",
                    session,
                    mask_phone(phone),
                    convo.id,
                )
        return JsonResponse({"ok": True, "resumed": True})

    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["POST"])
def wa_outgoing(request):
    """
    Node -> Django: outbound delivery acknowledgements / failures (optional).

    Current CRM does not depend on these callbacks, but they are useful for analytics.
    """
    if not _auth_ok(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    data = _json_body(request)
    session = str(data.get("session") or "").strip()
    message_id = str(data.get("message_id") or data.get("id") or "").strip()
    status = str(data.get("status") or "").strip().lower()
    error = str(data.get("error") or "").strip()

    if not session or not message_id:
        return JsonResponse({"error": "invalid_payload"}, status=400)

    msg = (
        WhatsAppMessage.objects.filter(message_id=message_id)
        .order_by("-id")
        .first()
    )
    if not msg:
        return JsonResponse({"ok": True, "updated": 0})

    status_map = {
        "sent": WhatsAppMessage.Status.SENT,
        "delivered": WhatsAppMessage.Status.DELIVERED,
        "read": WhatsAppMessage.Status.READ,
        "failed": WhatsAppMessage.Status.FAILED,
    }
    if status in status_map:
        msg.status = status_map[status]
        msg.status_updated_at = timezone.now()
    if error and status == "failed":
        msg.error_details = (error or "")[:2000]
    msg.save(update_fields=["status", "status_updated_at", "error_details", "updated_at"])
    return JsonResponse({"ok": True, "updated": 1})

