import logging
import random
import re
from datetime import time, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from .crm import (
    conversation_phone_key,
    get_lead_by_phone,
    get_lead_funnel_data,
    get_lead_stage,
    is_valid_lead_phone,
    update_lead_meta,
    update_lead_funnel,
    upsert_lead,
)
from crm.models import WhatsAppConversation, WhatsAppMessage, WhatsAppNumber
from .whatsapp_transport import get_transport

logger = logging.getLogger(__name__)


def notify_sales_team(lead, *, reason: str = ''):
    """Notify humans on AI handoff / hot lead (extend with Slack/email later)."""
    if not lead:
        return None
    logger.info(
        'Sales notify lead_id=%s name=%s phone=%s reason=%s',
        lead.id,
        getattr(lead, 'name', ''),
        mask_phone(getattr(lead, 'phone', '')),
        reason or 'handoff',
    )
    return None


# ─────────────────────────────────────────────
#  COPY BANK  —  Kerala-style, high-conversion
#  English: direct chat tone (not marketing)
#  Malayalam: spoken Manglish, not textbook
# ─────────────────────────────────────────────

# ── Step: Language pick ──────────────────────
STEP_LANG_EN = (
    "Hey 👋 I'm Lois.\n\n"
    "Quick question before we start —\n"
    "which language is easier for you?"
)

STEP_LANG_ML = (
    "Hey 👋 ഞാൻ Lois.\n\n"
    "ഒരു quick question —\n"
    "ഏത് language comfortable?"
)

# ── Step 1: What do you need ─────────────────
STEP_1_EN = (
    "What's the main thing you need right now?\n\n"
    "👇 Pick one"
)

STEP_1_ML = (
    "ഇപ്പോൾ most important ആയി വേണ്ടത് എന്താ?\n\n"
    "👇 ഒന്ന് select ചെയ്യൂ"
)

# ── Step 2: Business situation ───────────────
STEP_2_EN = (
    "Got it 👍\n\n"
    "What's your situation right now?"
)

STEP_2_ML = (
    "ശരി 👍\n\n"
    "ഇപ്പോൾ situation എന്താ?"
)

# ── Step 4: Timeline ─────────────────────────
STEP_4_EN = "When are you thinking to start?"

STEP_4_ML = "എപ്പോൾ start ചെയ്യാൻ plan?"

# ── Step 8: Name + call time ─────────────────
STEP_8_EN = (
    "Almost done 👌\n\n"
    "Send your name + a good time to call 👇"
)

STEP_8_ML = (
    "Almost done 👌\n\n"
    "പേര് + വിളിക്കാൻ പറ്റുന്ന time അയക്കൂ 👇"
)

# ── Closing texts ────────────────────────────
CLOSING_THIS_WEEK_EN = (
    "Good move starting this week.\n\n"
    "Businesses that set this up properly\n"
    "start getting enquiries within days.\n\n"
    "Send your name + best time to call 👇"
)

CLOSING_THIS_WEEK_ML = (
    "ഈ ആഴ്ച തന്നെ start ചെയ്യുന്നത് best.\n\n"
    "Properly set ചെയ്താൽ\n"
    "days ഉള്ളിൽ enquiries വരാൻ തുടങ്ങും.\n\n"
    "പേര് + Contact time അയക്കൂ 👇"
)

CLOSING_1_MONTH_EN = (
    "No rush — we'll plan it properly.\n\n"
    "Our team will walk you through\n"
    "everything step by step.\n\n"
    "Send your name + best time to call 👇"
)

CLOSING_1_MONTH_ML = (
    "Rush ഇല്ല — നന്നായി plan ചെയ്യാം.\n\n"
    "Team step by step\n"
    "guide ചെയ്യും.\n\n"
    "പേര് + Contact time അയക്കൂ 👇"
)

# ── Just checking exit ───────────────────────
JUST_CHECKING_EN = (
    "No problem at all 👍\n\n"
    "Our team will share a few ideas\n"
    "that might actually help.\n\n"
    "We'll be in touch soon."
)

JUST_CHECKING_ML = (
    "കൊള്ളാം 👍\n\n"
    "Team useful ideas share ചെയ്യും.\n\n"
    "Soon connect ചെയ്യും."
)

# ── Low budget exit ──────────────────────────
LOW_BUDGET_EN = (
    "Noted 👍\n\n"
    "Our team will still call and\n"
    "suggest what's possible at that range.\n\n"
    "No pressure at all."
)

LOW_BUDGET_ML = (
    "Noted 👍\n\n"
    "Team ആ range ൽ possible ആയത്\n"
    "explain ചെയ്ത് വിളിക്കും.\n\n"
    "Pressure ഒന്നും ഇല്ല."
)

# ── Final confirmations ──────────────────────
FINAL_NORMAL_EN = (
    "Perfect 🙌\n\n"
    "Our team will call you shortly.\n"
    "They'll keep it simple and clear."
)

FINAL_NORMAL_ML = (
    "Perfect 🙌\n\n"
    "Team ഉടൻ വിളിക്കും.\n"
    "Simple ആയി explain ചെയ്യും."
)

FINAL_OFF_TIME_EN = (
    "Got it 🙌\n\n"
    "Our team calls between 10 AM – 7 PM.\n"
    "They'll explain everything clearly."
)

FINAL_OFF_TIME_ML = (
    "Got it 🙌\n\n"
    "Team 10 AM – 7 PM ഇടയ്ക്ക് വിളിക്കും.\n"
    "Everything clearly explain ചെയ്യും."
)

# ─────────────────────────────────────────────
#  BUDGET OFFER COPY  (step 6)
#  Short, punchy, FOMO-driven
# ─────────────────────────────────────────────

def _budget_offer_body(service, stage_value, lang):
    """Emotion-aware, Kerala-optimized copy with believable scarcity."""
    service = str(service or '').strip().lower()
    stage_value = str(stage_value or '').strip().lower()
    slots_left = random.choice([1, 2])  # feels more real than 2–3

    # ---------------- ECOMMERCE ----------------
    if service == 'ecommerce':
        if lang == 'ml':
            return (
                f"സത്യമായി പറഞ്ഞാൽ —\n\n"
                f"Website മാത്രം ഉണ്ടാക്കിയാൽ sales വരില്ല.\n"
                f"Proper setup + marketing വേണം.\n\n"
                f"ഞങ്ങൾ complete ecommerce system build ചെയ്യും.\n"
                f"👉 1 MONTH MARKETING SUPPORT FREE\n\n"
                f"₹20K–30K → Basic setup, starting enquiries\n"
                f"₹30K–45K → Better design + conversion focus\n\n"
                f"Better setup ആണെങ്കിൽ enquiries quality കൂടും.\n\n"
                f"ഞങ്ങൾ quality maintain ചെയ്യാൻ മാസം കുറച്ച് projects മാത്രം എടുക്കും.\n"
                f"ഇപ്പോൾ {slots_left} slot മാത്രം ബാക്കി.\n\n"
                f"താങ്കൾക്ക് comfortable ആയത് select ചെയ്യൂ 👇"
            )
        return (
            f"Let me be direct —\n\n"
            f"A website alone won’t bring sales.\n"
            f"You need proper setup + marketing.\n\n"
            f"We build complete ecommerce systems.\n"
            f"👉 1 MONTH MARKETING SUPPORT FREE\n\n"
            f"₹20K–30K → Basic setup, start getting enquiries\n"
            f"₹30K–45K → Better design + higher conversions\n\n"
            f"Better setup = better results.\n\n"
            f"We take only a few projects each month to maintain quality.\n"
            f"Right now, only {slots_left} slot is available.\n\n"
            f"Pick what feels right for you 👇"
        )

    # ---------------- MARKETING ----------------
    if service == 'marketing':
        if lang == 'ml':
            return (
                f"Ads വഴി enquiries കിട്ടും —\n"
                f"പക്ഷേ setup ശരിയായാൽ മാത്രം.\n\n"
                f"ഞങ്ങൾ focus ചെയ്യുന്നത് real enquiries ആണ്.\n\n"
                f"₹10K–15K → Starting enquiries\n"
                f"₹15K–25K → Consistent leads\n"
                f"₹25K+ → Scaling + കൂടുതൽ reach\n\n"
                f"Budget കൂടുമ്പോൾ leads കൂടും.\n\n"
                f"ഞങ്ങൾ ഒരേസമയം കുറച്ച് clients മാത്രം handle ചെയ്യും.\n"
                f"ഇപ്പോൾ {slots_left} slot മാത്രം available.\n\n"
                f"Budget തിരഞ്ഞെടുക്കൂ 👇"
            )
        return (
            f"Ads can bring enquiries —\n"
            f"but only if the setup is right.\n\n"
            f"We focus on real enquiries, not just ads.\n\n"
            f"₹10K–15K → Initial enquiries\n"
            f"₹15K–25K → Consistent leads\n"
            f"₹25K+ → Scale + more reach\n\n"
            f"More budget = more enquiries.\n\n"
            f"We work with a limited number of clients to ensure results.\n"
            f"Currently, only {slots_left} slot is available.\n\n"
            f"Pick your budget 👇"
        )

    # ---------------- WEBSITE ----------------
    if lang == 'ml':
        return (
            f"Right setup ഉണ്ടെങ്കിൽ enquiries വരും.\n\n"
            f"Proper website trust build ചെയ്യും.\n\n"
            f"₹10K–25K → Basic website (online presence)\n"
            f"₹25K–40K → Better design + trust\n"
            f"₹40K–60K → Conversion-focused setup\n\n"
            f"Better setup → better enquiries.\n\n"
            f"ഞങ്ങൾ quality maintain ചെയ്യാൻ projects limit ചെയ്യും.\n"
            f"ഇപ്പോൾ {slots_left} slot മാത്രം ബാക്കി.\n\n"
            f"Budget തിരഞ്ഞെടുക്കൂ 👇"
        )

    return (
        f"With the right setup,\n"
        f"you start getting enquiries consistently.\n\n"
        f"A good website builds trust.\n\n"
        f"₹10K–25K → Basic website (online presence)\n"
        f"₹25K–40K → Better design + trust\n"
        f"₹40K–60K → Conversion-focused setup\n\n"
        f"Better setup = better results.\n\n"
        f"We limit projects each month to maintain quality.\n"
        f"Right now, only {slots_left} slot is available.\n\n"
        f"Choose your budget 👇"
    )

# ─────────────────────────────────────────────
#  OPTIONS
# ─────────────────────────────────────────────

OPTIONS_STEP_LANG_EN = [('1', 'English'), ('2', 'Malayalam')]
OPTIONS_STEP_LANG_ML = [('1', 'English'),  ('2', 'മലയാളം')]

OPTIONS_STEP_1_EN = [
    ('1', 'Digital Marketing'),
    ('2', 'Ecommerce Website'),
    ('3', 'All Services'),
]
OPTIONS_STEP_1_ML = [
    ('1', 'Digital Marketing'),
    ('2', 'Ecommerce Website'),
    ('3', 'All Services'),
]

OPTIONS_STEP_3_EN = [
    ('1', 'Running - low sales'),
    ('2', 'Planning to start'),
    ('3', 'Just Checking'),
]
OPTIONS_STEP_3_ML = [
    ('1', 'Running – low sales'),
    ('2', 'Planning to start'),
    ('3', 'Just checking'),
]

OPTIONS_STEP_4_EN = [('1', 'This week'),   ('2', 'Within a month'), ('3', 'Just checking')]
OPTIONS_STEP_4_ML = [('1', 'ഈ ആഴ്ച'), ('2', 'ഒരു മാസം ഉള്ളിൽ'), ('3', 'Just checking')]

OPTIONS_STEP_6_WEBSITE   = [('1', '₹10,000 – ₹25,000'), ('2', '₹25,000 – ₹40,000'), ('3', '₹40,000 – ₹60,000')]
OPTIONS_STEP_6_ECOM_NEW  = [('1', '₹20,000 – ₹30,000'), ('2', '₹30,000 – ₹45,000')]
OPTIONS_STEP_6_ECOM_RUN  = [('1', '₹20,000 – ₹30,000'), ('2', '₹30,000 – ₹45,000')]
OPTIONS_STEP_6_MARKETING = [
    ('1', '₹9999 – ₹15000 / mo'),
    ('2', '₹15000 – ₹25000 / mo'),
    ('3', '₹25000+ / mo'),
]


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _normalize_phone(phone):
    return ''.join(ch for ch in str(phone or '') if ch.isdigit())


def mask_phone(phone):
    raw = _normalize_phone(phone)
    if len(raw) <= 7:
        return raw[:2] + '***' if raw else ''
    return f'{raw[:5]}****{raw[-3:]}'


def _normalize_text(text):
    return str(text or '').strip().lower()


def _text_has_malayalam(text):
    return bool(re.search(r'[\u0D00-\u0D7F]', str(text or '')))


def _text_words(text):
    return re.findall(r'[a-z0-9]+', str(text or '').lower())


def _safe_response_excerpt(body):
    return str(body or '').strip()[:200]


def _get_lang(meta):
    lang = str((meta or {}).get('language') or 'en').strip().lower()
    return 'ml' if lang == 'ml' else 'en'


def _pick(en_val, ml_val, lang):
    """Return en_val or ml_val depending on lang."""
    return ml_val if lang == 'ml' else en_val


def _options_step_lang(lang):
    return OPTIONS_STEP_LANG_ML if lang == 'ml' else OPTIONS_STEP_LANG_EN


def _options_step_1(lang):
    return OPTIONS_STEP_1_ML if lang == 'ml' else OPTIONS_STEP_1_EN


def _options_step_3(lang):
    return OPTIONS_STEP_3_ML if lang == 'ml' else OPTIONS_STEP_3_EN


def _options_step_4(lang):
    return OPTIONS_STEP_4_ML if lang == 'ml' else OPTIONS_STEP_4_EN


def _budget_options(service, stage_value):
    service = str(service or '').strip().lower()
    stage_value = str(stage_value or '').strip().lower()
    if service == 'ecommerce':
        return OPTIONS_STEP_6_ECOM_NEW if stage_value == 'planning' else OPTIONS_STEP_6_ECOM_RUN
    if service == 'marketing':
        return OPTIONS_STEP_6_MARKETING
    return OPTIONS_STEP_6_WEBSITE


def _priority_from(service, budget_choice, stage_value):
    service     = str(service or '').strip().lower()
    choice      = str(budget_choice or '').strip()
    stage_value = str(stage_value or '').strip().lower()

    if service == 'ecommerce' and stage_value == 'running':
        return 'high'
    if service == 'ecommerce' and stage_value == 'planning':
        return 'low'
    if service == 'marketing' and choice in {'2', '3'}:
        return 'high'
    if service == 'marketing' and choice == '1':
        return 'low'
    if service == 'website' and choice in {'2', '3'}:
        return 'medium'
    if service == 'website':
        return 'low'
    if stage_value == 'running':
        return 'medium'
    return 'low'


def _closing_after_budget(timeline, lang):
    if timeline == 'this_week':
        return _pick(CLOSING_THIS_WEEK_EN, CLOSING_THIS_WEEK_ML, lang)
    return _pick(CLOSING_1_MONTH_EN, CLOSING_1_MONTH_ML, lang)


# ─────────────────────────────────────────────
#  RATE LIMITING
# ─────────────────────────────────────────────

def is_duplicate_event(message_id):
    if not message_id:
        return False
    key = f'wa:message:{message_id}'
    if cache.get(key):
        return True
    cache.set(key, True, timeout=24 * 60 * 60)
    return False


def _rate_limit_ok(lead):
    now  = timezone.now()
    meta = get_lead_funnel_data(lead) or {}
    last = meta.get('last_reply_time')
    if not last:
        return True
    try:
        last_dt = timezone.datetime.fromisoformat(str(last))
        if timezone.is_naive(last_dt):
            last_dt = timezone.make_aware(last_dt, timezone.get_current_timezone())
    except Exception:
        return True
    return (now - last_dt) >= timedelta(seconds=3)


def _mark_reply_sent(lead):
    update_lead_meta(lead, last_reply_time=timezone.now().isoformat())


# ─────────────────────────────────────────────
#  WHATSAPP TRANSPORT (Web.js)
# ─────────────────────────────────────────────

def _account_id_for(wa_number: WhatsAppNumber | None) -> str:
    """
    For WhatsApp Web.js transport we need an account/session identifier.

    We reuse WhatsAppNumber.phone_number_id as the account_id (bridge session key)
    to avoid a breaking schema change during migration.
    """
    if wa_number and wa_number.is_active:
        return str(wa_number.phone_number_id or '').strip()
    return str(getattr(settings, 'WHATSAPP_DEFAULT_ACCOUNT_ID', '') or '').strip()


def get_active_wa_number(phone_number_id: str | None) -> WhatsAppNumber | None:
    if not phone_number_id:
        return None
    pid = str(phone_number_id).strip()
    if not pid:
        return None
    return (
        WhatsAppNumber.objects.filter(phone_number_id=pid, is_active=True)
        .select_related('executive')
        .first()
    )


def get_or_create_conversation(*, wa_number: WhatsAppNumber, customer_phone: str, lead=None) -> WhatsAppConversation:
    """
    Idempotent conversation creation (unique on wa_number + customer_phone).
    """
    customer_phone = _normalize_phone(customer_phone)
    with transaction.atomic():
        convo = (
            WhatsAppConversation.objects.select_for_update()
            .filter(wa_number=wa_number, customer_phone=customer_phone)
            .first()
        )
        if convo:
            if lead and not convo.lead_id:
                convo.lead = lead
                convo.save(update_fields=['lead', 'updated_at'])
            return convo
        try:
            return WhatsAppConversation.objects.create(
                wa_number=wa_number,
                executive=wa_number.executive,
                lead=lead,
                customer_phone=customer_phone,
            )
        except IntegrityError:
            # Race: another worker created it.
            return WhatsAppConversation.objects.get(
                wa_number=wa_number, customer_phone=customer_phone
            )


def record_inbound_message(
    *,
    convo: WhatsAppConversation,
    lead,
    message_id: str,
    customer_phone: str,
    text: str,
    message_type: str,
    raw_payload: dict | None,
    provider_timestamp: timezone.datetime | None = None,
) -> WhatsAppMessage | None:
    """
    Creates (or returns existing) inbound message row. Unique constraint is per (wa_number, message_id).
    """
    msg_id = str(message_id or '').strip()
    try:
        with transaction.atomic():
            msg = WhatsAppMessage.objects.create(
                conversation=convo,
                wa_number=convo.wa_number,
                executive=convo.executive,
                lead=lead,
                direction=WhatsAppMessage.Direction.INBOUND,
                source=WhatsAppMessage.Source.SYSTEM,
                message_id=msg_id,
                customer_phone=_normalize_phone(customer_phone),
                text=str(text or ''),
                message_type=str(message_type or ''),
                status=WhatsAppMessage.Status.RECEIVED,
                provider_timestamp=provider_timestamp,
                raw_payload=raw_payload or None,
            )
            return msg
    except IntegrityError:
        if not msg_id:
            return None
        return (
            WhatsAppMessage.objects.filter(wa_number=convo.wa_number, message_id=msg_id)
            .order_by('-id')
            .first()
        )


def send_whatsapp_message(phone, text, *, wa_number: WhatsAppNumber | None = None, convo: WhatsAppConversation | None = None, lead=None, source=WhatsAppMessage.Source.BOT):
    to_phone = _normalize_phone(phone)
    if not to_phone:
        logger.warning('Cannot send WhatsApp message: invalid phone')
        return False

    transport = get_transport()
    account_id = _account_id_for(wa_number)
    if not account_id:
        logger.error('Missing WhatsApp Web.js account_id mapping')
        return False

    result = transport.send_text(account_id=account_id, to_phone=to_phone, text=str(text or ''))
    logger.info('WA(webjs) send phone=%s ok=%s msg_id=%s err=%s',
                mask_phone(to_phone), result.ok, (result.provider_message_id or ''), (result.error or '')[:200])
    if not result.ok:
        return False

    if convo and wa_number:
        try:
            WhatsAppMessage.objects.create(
                conversation=convo,
                wa_number=wa_number,
                executive=wa_number.executive,
                lead=lead or convo.lead,
                direction=WhatsAppMessage.Direction.OUTBOUND,
                source=source,
                message_id=str(result.provider_message_id or '').strip(),
                customer_phone=to_phone,
                text=str(text or ''),
                message_type='text',
                status=WhatsAppMessage.Status.SENT,
                status_updated_at=timezone.now(),
                raw_payload=result.raw or None,
            )
        except IntegrityError:
            pass
        convo.last_outbound_at = timezone.now()
        convo.save(update_fields=['last_outbound_at', 'updated_at'])
    return True


def _record_interactive_outbound(*, convo, wa_number, lead, source, phone, body, message_type, result, extra=None):
    if not (convo and wa_number and result.ok):
        return
    try:
        WhatsAppMessage.objects.create(
            conversation=convo,
            wa_number=wa_number,
            executive=wa_number.executive,
            lead=lead or convo.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            source=source,
            message_id=str(result.provider_message_id or '').strip(),
            customer_phone=_normalize_phone(phone),
            text=str(body or '')[:2000],
            message_type=message_type,
            status=WhatsAppMessage.Status.SENT,
            status_updated_at=timezone.now(),
            raw_payload=extra or None,
        )
    except IntegrityError:
        pass
    convo.last_outbound_at = timezone.now()
    convo.save(update_fields=['last_outbound_at', 'updated_at'])


def _send_reply_buttons_menu(phone, body_text, options, *, wa_number, convo, lead, source) -> bool:
    """Green reply buttons under the message (max 3)."""
    if not getattr(settings, 'WHATSAPP_USE_REPLY_BUTTONS', True):
        return False
    transport = get_transport()
    account_id = _account_id_for(wa_number)
    if not account_id or not hasattr(transport, 'send_buttons'):
        return False
    opts = [(str(oid), str(title).strip()) for oid, title in (options or []) if str(title).strip()][:3]
    if not opts:
        return False
    body = str(body_text or 'Choose one').strip()
    result = transport.send_buttons(
        account_id=account_id,
        to_phone=_normalize_phone(phone),
        body=body,
        options=opts,
    )
    if not result.ok:
        logger.warning('Reply buttons failed phone=%s err=%s', mask_phone(phone), (result.error or '')[:120])
        return False
    _record_interactive_outbound(
        convo=convo,
        wa_number=wa_number,
        lead=lead,
        source=source,
        phone=phone,
        body=body,
        message_type='buttons',
        result=result,
        extra={'options': opts},
    )
    return True


def _send_reply_list_menu(
    phone, body_text, options, *, button_text, wa_number, convo, lead, source
) -> bool:
    """List picker for 4+ options."""
    if not getattr(settings, 'WHATSAPP_USE_REPLY_BUTTONS', True):
        return False
    transport = get_transport()
    account_id = _account_id_for(wa_number)
    if not account_id or not hasattr(transport, 'send_list'):
        return False
    opts = [(str(oid), str(title).strip()) for oid, title in (options or []) if str(title).strip()][:10]
    if not opts:
        return False
    body = str(body_text or 'Choose one').strip()
    result = transport.send_list(
        account_id=account_id,
        to_phone=_normalize_phone(phone),
        body=body,
        button_text=button_text,
        options=opts,
    )
    if not result.ok:
        logger.warning('Reply list failed phone=%s err=%s', mask_phone(phone), (result.error or '')[:120])
        return False
    _record_interactive_outbound(
        convo=convo,
        wa_number=wa_number,
        lead=lead,
        source=source,
        phone=phone,
        body=body,
        message_type='list',
        result=result,
        extra={'options': opts, 'button_text': button_text},
    )
    return True


def _send_text_menu(phone, body_text, options, *, wa_number, convo, lead, source) -> bool:
    opts = options or []
    lang = 'en'
    if lead:
        lang = _get_lang(get_lead_funnel_data(lead) or {})
    lines = [str(body_text or '').strip(), '']
    for oid, title in opts:
        lines.append(f'{oid}. {title}')
    hint = 'ഒരു number reply ചെയ്യൂ (1, 2, 3...)' if lang == 'ml' else 'Reply with a number (1, 2, 3...)'
    lines.extend(['', hint])
    return send_whatsapp_message(
        phone,
        '\n'.join(lines).strip(),
        wa_number=wa_number,
        convo=convo,
        lead=lead,
        source=source,
    )


def send_flow_buttons(phone, body_text, options, *, wa_number: WhatsAppNumber | None = None, convo: WhatsAppConversation | None = None, lead=None, source=WhatsAppMessage.Source.BOT):
    to_phone = _normalize_phone(phone)
    if not to_phone:
        logger.warning('Cannot send interactive WhatsApp message: invalid phone')
        return False
    opts = (options or [])[:3]
    if _send_reply_buttons_menu(phone, body_text, opts, wa_number=wa_number, convo=convo, lead=lead, source=source):
        return True
    return _send_text_menu(phone, body_text, opts, wa_number=wa_number, convo=convo, lead=lead, source=source)


def send_flow_list(phone, body_text, options, button_text='Select', *, wa_number: WhatsAppNumber | None = None, convo: WhatsAppConversation | None = None, lead=None, source=WhatsAppMessage.Source.BOT):
    to_phone = _normalize_phone(phone)
    if not to_phone:
        logger.warning('Cannot send interactive WhatsApp list: invalid phone')
        return False
    opts = (options or [])[:12]
    if not opts:
        return send_whatsapp_message(phone, body_text, wa_number=wa_number, convo=convo, lead=lead, source=source)
    lang = _get_lang(get_lead_funnel_data(lead) or {}) if lead else 'en'
    if _send_reply_list_menu(
        phone,
        body_text,
        opts,
        button_text=_list_cta(lang),
        wa_number=wa_number,
        convo=convo,
        lead=lead,
        source=source,
    ):
        return True
    return _send_text_menu(phone, body_text, opts, wa_number=wa_number, convo=convo, lead=lead, source=source)


def send_human_message(*, wa_number: WhatsAppNumber, customer_phone: str, text: str, lead=None) -> bool:
    """
    Human takeover helper:
    - disables bot on the conversation
    - sends message from the executive's number
    - stores message as source=human
    """
    convo = get_or_create_conversation(
        wa_number=wa_number, customer_phone=customer_phone, lead=lead
    )
    from crm.services.whatsapp_bot import record_human_takeover

    record_human_takeover(convo)
    return send_whatsapp_message(
        customer_phone,
        text,
        wa_number=wa_number,
        convo=convo,
        lead=lead or convo.lead,
        source=WhatsAppMessage.Source.HUMAN,
    )


# ─────────────────────────────────────────────
#  FLOW SEND DISPATCHER
# ─────────────────────────────────────────────

def _option_titles_too_long(options, max_len=20):
    return any(len(str(t)) > max_len for _, t in (options or []))


def _list_cta(lang):
    return 'താഴെ select ചെയ്യൂ' if lang == 'ml' else 'Pick one 👇'


def _send_step_prompt(lead, phone, text, options):
    if not _rate_limit_ok(lead):
        logger.info('Rate-limited prompt phone=%s', mask_phone(phone))
        return False

    opts     = options or []
    use_list = bool(opts) and (
        len(opts) > 3 or _option_titles_too_long(opts)
    )

    # Transport routing: pick the conversation account_id from lead meta if present.
    # We keep using WhatsAppNumber mapping for multi-executive routing.
    meta = get_lead_funnel_data(lead) or {}
    wa_pid = str(meta.get('wa_account_id') or meta.get('wa_phone_number_id') or '').strip()
    wa_number = get_active_wa_number(wa_pid) if wa_pid else None
    convo = None
    if wa_number:
        convo = get_or_create_conversation(wa_number=wa_number, customer_phone=phone, lead=lead)

    if use_list and len(opts) <= 10:
        ok = send_flow_list(
            phone,
            text,
            opts,
            button_text=_list_cta(_get_lang(get_lead_funnel_data(lead) or {})),
            wa_number=wa_number,
            convo=convo,
            lead=lead,
        )
    elif len(opts) <= 3:
        ok = send_flow_buttons(phone, text, opts, wa_number=wa_number, convo=convo, lead=lead)
    else:
        lines = [str(text or '').strip(), ''] + [f'{i}. {t}' for i, t in opts]
        ok = send_whatsapp_message(phone, '\n'.join(lines).strip(), wa_number=wa_number, convo=convo, lead=lead)

    if not ok:
        ok = send_whatsapp_message(phone, text, wa_number=wa_number, convo=convo, lead=lead)
    if ok:
        _mark_reply_sent(lead)
    return ok


def _send_text(lead, phone, text):
    if not _rate_limit_ok(lead):
        logger.info('Rate-limited text phone=%s', mask_phone(phone))
        return False
    meta = get_lead_funnel_data(lead) or {}
    wa_pid = str(meta.get('wa_account_id') or meta.get('wa_phone_number_id') or '').strip()
    wa_number = get_active_wa_number(wa_pid) if wa_pid else None
    convo = None
    if wa_number:
        convo = get_or_create_conversation(wa_number=wa_number, customer_phone=phone, lead=lead)
    ok = send_whatsapp_message(phone, text, wa_number=wa_number, convo=convo, lead=lead)
    if ok:
        _mark_reply_sent(lead)
    return ok


def _resolve_option(normalized_text, options):
    text = str(normalized_text or '').strip()
    if not text:
        return None
    for opt_id, opt_title in options:
        if text in (_normalize_text(opt_id), _normalize_text(opt_title)):
            return str(opt_id)
    # Button/list taps often send only the label — partial match
    for opt_id, opt_title in options:
        title_norm = _normalize_text(opt_title)
        if title_norm and (title_norm in text or text in title_norm):
            return str(opt_id)
    return None


def _set_flow_stage(lead, stage, **extra):
    update_lead_funnel(lead, stage=stage)
    if extra:
        update_lead_meta(lead, **extra)


def _send_bot_unavailable(lead, phone, executive):
    from crm.services.whatsapp_bot import should_send_unavailable_reply, unavailable_reply_text

    if not should_send_unavailable_reply(lead):
        logger.info('Skipping duplicate unavailable reply lead=%s', lead.id)
        return

    meta = get_lead_funnel_data(lead) or {}
    lang = 'ml' if str(meta.get('language') or '').lower() == 'ml' else 'en'
    if _send_text(lead, phone, unavailable_reply_text(executive, lang=lang)):
        update_lead_meta(lead, last_unavailable_sent_at=timezone.now().isoformat())


def _run_ai_qualification_or_unavailable(*, lead, convo, text, phone, wa_number, executive):
    from crm.services.ai import gemini_client
    from crm.services.ai.conversation_manager import process_lead_message
    from crm.services.whatsapp_bot import should_run_ai_qualification

    normalized = _normalize_text(text)
    if not should_run_ai_qualification(lead, text, normalized):
        _send_bot_unavailable(lead, phone, executive)
        return

    if not getattr(settings, 'GEMINI_AI_QUALIFICATION_ENABLED', False):
        logger.error('Qualification lead=%s but GEMINI_AI_QUALIFICATION_ENABLED is off', lead.id)
        _send_bot_unavailable(lead, phone, executive)
        return

    if not gemini_client.is_configured():
        logger.error('Qualification lead=%s but GEMINI_API_KEY missing', lead.id)
        _send_bot_unavailable(lead, phone, executive)
        return

    try:
        process_lead_message(
            lead=lead,
            convo=convo,
            text=text,
            phone=phone,
            wa_number=wa_number,
        )
    except Exception as exc:
        err = str(exc).lower()
        if 'resourceexhausted' in type(exc).__name__.lower() or 'quota' in err or '429' in err:
            logger.error('Gemini quota exceeded for lead=%s', lead.id)
        else:
            logger.exception('Gemini AI failed for lead=%s', lead.id)
        _send_bot_unavailable(lead, phone, executive)


# ─────────────────────────────────────────────
#  MAIN HANDLER
# ─────────────────────────────────────────────

def _resolve_outbound_phone(phone, lead) -> str:
    for candidate in (_normalize_phone(phone), _normalize_phone(getattr(lead, 'phone', ''))):
        if is_valid_lead_phone(candidate):
            return candidate
    return _normalize_phone(phone)


def handle_message(phone, text, *, wa_number: WhatsAppNumber | None = None, message_id: str | None = None, raw_payload: dict | None = None, provider_timestamp=None):
    # Button/list taps may arrive via selected_button_id when body is empty
    if isinstance(raw_payload, dict):
        if not str(text or '').strip():
            text = str(
                raw_payload.get('selected_button_id')
                or raw_payload.get('selected_row_id')
                or text
                or ''
            ).strip()
    normalized = _normalize_text(text)
    chat_id = ''
    contact_name = ''
    if isinstance(raw_payload, dict):
        chat_id = str(raw_payload.get('chat_id') or raw_payload.get('chatId') or '').strip()
        contact_name = str(raw_payload.get('contact_name') or raw_payload.get('contactName') or '').strip()

    upsert_phone = _normalize_phone(phone)
    if not is_valid_lead_phone(upsert_phone):
        upsert_phone = ''

    logger.info('Incoming WA message phone=%s chat_id=%s', mask_phone(upsert_phone or phone), chat_id[:24])

    exec_owner = wa_number.executive if wa_number else None
    lead = upsert_lead(
        upsert_phone,
        normalized,
        source='WhatsApp Ads',
        owner=exec_owner,
        wa_chat_id=chat_id,
        contact_name=contact_name,
    )
    if lead is None and upsert_phone:
        lead = get_lead_by_phone(upsert_phone)
    if not lead:
        logger.error('Cannot handle WA message: lead not found phone=%s chat_id=%s', mask_phone(phone), chat_id[:24])
        return

    phone = _resolve_outbound_phone(upsert_phone or phone, lead)

    # Persist routing on lead meta (used for correct outbound routing).
    if wa_number and lead:
        meta_updates = {
            'wa_account_id': str(wa_number.phone_number_id),
            'wa_executive_id': getattr(wa_number.executive, 'id', None),
        }
        if chat_id:
            meta_updates['wa_chat_id'] = chat_id
        update_lead_meta(lead, **meta_updates)

    convo = None
    executive = wa_number.executive if wa_number else getattr(lead, 'employee', None)

    if wa_number and lead:
        convo_key = conversation_phone_key(phone, chat_id)
        convo = get_or_create_conversation(wa_number=wa_number, customer_phone=convo_key, lead=lead)
        convo.last_inbound_at = timezone.now()
        convo.save(update_fields=['last_inbound_at', 'updated_at'])

        record_inbound_message(
            convo=convo,
            lead=lead,
            message_id=str(message_id or ''),
            customer_phone=phone,
            text=text,
            message_type='text',
            raw_payload=raw_payload,
            provider_timestamp=provider_timestamp,
        )

        from crm.services.whatsapp_bot import (
            crm_whatsapp_bot_enabled,
            human_takeover_blocks_reply,
            is_sender_excluded,
        )

        if is_sender_excluded(executive, phone):
            logger.info(
                'Sender on bot exclude list — exec=%s phone=%s — stored only',
                getattr(executive, 'username', None),
                mask_phone(phone),
            )
            return

        if not crm_whatsapp_bot_enabled(executive):
            logger.warning(
                'CRM WA bot is OFF — session=%s exec=%s lead=%s — '
                'message saved, no auto-reply. Enable bot on that executive profile.',
                str(wa_number.phone_number_id),
                getattr(executive, 'username', None) or getattr(executive, 'id', None),
                lead.id,
            )
            return

        if human_takeover_blocks_reply(convo):
            logger.info(
                'No auto-reply — human takeover active convo=%s phone=%s reason=%s',
                convo.id,
                mask_phone(phone),
                convo.bot_disabled_reason or 'unknown',
            )
            return

        _run_ai_qualification_or_unavailable(
            lead=lead,
            convo=convo,
            text=text,
            phone=phone,
            wa_number=wa_number,
            executive=executive,
        )
        return


def handle_voice_message(
    phone,
    *,
    wa_number: WhatsAppNumber | None = None,
    message_id: str | None = None,
    raw_payload: dict | None = None,
    provider_timestamp=None,
    media: dict | None = None,
    message_type: str = 'ptt',
):
    """Inbound voice note → transcribe → Gemini qualification pipeline."""
    chat_id = ''
    contact_name = ''
    if isinstance(raw_payload, dict):
        chat_id = str(raw_payload.get('chat_id') or raw_payload.get('chatId') or '').strip()
        contact_name = str(raw_payload.get('contact_name') or raw_payload.get('contactName') or '').strip()

    upsert_phone = _normalize_phone(phone)
    if not is_valid_lead_phone(upsert_phone):
        upsert_phone = ''

    exec_owner = wa_number.executive if wa_number else None
    lead = upsert_lead(
        upsert_phone,
        '[voice note]',
        source='WhatsApp Ads',
        owner=exec_owner,
        wa_chat_id=chat_id,
        contact_name=contact_name,
    )
    if lead is None and upsert_phone:
        lead = get_lead_by_phone(upsert_phone)
    if not lead or not wa_number:
        return

    phone = _resolve_outbound_phone(upsert_phone or phone, lead)
    meta_updates = {
        'wa_account_id': str(wa_number.phone_number_id),
        'wa_executive_id': getattr(wa_number.executive, 'id', None),
    }
    if chat_id:
        meta_updates['wa_chat_id'] = chat_id
    update_lead_meta(lead, **meta_updates)

    convo_key = conversation_phone_key(phone, chat_id)
    convo = get_or_create_conversation(wa_number=wa_number, customer_phone=convo_key, lead=lead)
    convo.last_inbound_at = timezone.now()
    convo.save(update_fields=['last_inbound_at', 'updated_at'])
    executive = wa_number.executive

    record_inbound_message(
        convo=convo,
        lead=lead,
        message_id=str(message_id or ''),
        customer_phone=phone,
        text='[voice note]',
        message_type=message_type,
        raw_payload=raw_payload,
        provider_timestamp=provider_timestamp,
    )

    from crm.services.whatsapp_bot import (
        crm_whatsapp_bot_enabled,
        human_takeover_blocks_reply,
        is_sender_excluded,
    )

    if is_sender_excluded(executive, phone):
        return

    if not crm_whatsapp_bot_enabled(executive):
        return

    if human_takeover_blocks_reply(convo):
        return

    meta = get_lead_funnel_data(lead) or {}
    in_qual = meta.get('ai_qualification_track') == 'full_qualification'

    if not in_qual:
        _send_bot_unavailable(lead, phone, executive)
        return

    if getattr(settings, 'GEMINI_AI_QUALIFICATION_ENABLED', False) and _rate_limit_ok(lead):
        from crm.services.ai.conversation_manager import process_voice_inbound

        result = process_voice_inbound(
            lead=lead,
            convo=convo,
            media=media,
            phone=phone,
            wa_number=wa_number,
            raw_payload=raw_payload,
        )
        if result and result.reply:
            transcript = str((get_lead_funnel_data(lead) or {}).get('voice_transcript') or '').strip()
            if transcript:
                update_lead_meta(lead, last_voice_display=transcript[:200])
        return

    _send_bot_unavailable(lead, phone, executive)