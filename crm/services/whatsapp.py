import logging
import random
import re
from datetime import time, timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .crm import (
    get_lead_by_phone,
    get_lead_funnel_data,
    get_lead_stage,
    update_lead_meta,
    update_lead_funnel,
    upsert_lead,
)

logger = logging.getLogger(__name__)


def notify_sales_team(lead):
    # placeholder (later integrate WhatsApp/Slack)
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
#  WHATSAPP SEND HELPERS
# ─────────────────────────────────────────────

def _wa_credentials():
    token           = str(getattr(settings, 'WHATSAPP_ACCESS_TOKEN',    '') or '').strip()
    phone_number_id = str(getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '') or '').strip()
    return token, phone_number_id


def send_whatsapp_message(phone, text):
    token, phone_number_id = _wa_credentials()
    to_phone = _normalize_phone(phone)

    if not token or not phone_number_id:
        logger.error('Missing WhatsApp credentials in environment variables')
        return False
    if not to_phone:
        logger.warning('Cannot send WhatsApp message: invalid phone')
        return False

    url     = f'https://graph.facebook.com/v22.0/{phone_number_id}/messages'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'text',
        'text': {'body': text},
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        logger.info('WA send phone=%s status=%s body=%s',
                    mask_phone(to_phone), r.status_code, _safe_response_excerpt(r.text))
        if r.ok:
            return True
        logger.error('WA send failed phone=%s status=%s body=%s',
                     mask_phone(to_phone), r.status_code, _safe_response_excerpt(r.text))
        return False
    except requests.RequestException:
        logger.exception('WA send request failed')
        return False


def send_flow_buttons(phone, body_text, options):
    token, phone_number_id = _wa_credentials()
    to_phone = _normalize_phone(phone)

    if not token or not phone_number_id:
        logger.error('Missing WhatsApp credentials in environment variables')
        return False
    if not to_phone:
        logger.warning('Cannot send interactive WhatsApp message: invalid phone')
        return False

    url     = f'https://graph.facebook.com/v22.0/{phone_number_id}/messages'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'interactive',
        'interactive': {
            'type': 'button',
            'body': {'text': str(body_text or '')[:1024]},
            'action': {
                'buttons': [
                    {
                        'type': 'reply',
                        'reply': {
                            'id':    str(opt_id)[:256],
                            'title': str(opt_title)[:20],
                        },
                    }
                    for opt_id, opt_title in (options or [])[:3]
                ]
            },
        },
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        logger.info('WA buttons phone=%s status=%s body=%s',
                    mask_phone(to_phone), r.status_code, _safe_response_excerpt(r.text))
        if r.ok:
            return True
        logger.error('WA buttons failed phone=%s status=%s body=%s',
                     mask_phone(to_phone), r.status_code, _safe_response_excerpt(r.text))
        return False
    except requests.RequestException:
        logger.exception('WA buttons request failed')
        return False


def send_flow_list(phone, body_text, options, button_text='Select'):
    token, phone_number_id = _wa_credentials()
    to_phone = _normalize_phone(phone)

    if not token or not phone_number_id:
        logger.error('Missing WhatsApp credentials in environment variables')
        return False
    if not to_phone:
        logger.warning('Cannot send interactive WhatsApp list: invalid phone')
        return False

    rows = [
        {'id': str(oid)[:200], 'title': str(title)[:24]}
        for oid, title in (options or [])[:10]
    ]
    if not rows:
        return send_whatsapp_message(phone, body_text)

    url     = f'https://graph.facebook.com/v22.0/{phone_number_id}/messages'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'interactive',
        'interactive': {
            'type': 'list',
            'body': {'text': str(body_text or '')[:1024]},
            'action': {
                'button':   str(button_text or 'Select')[:20],
                'sections': [{'title': 'Options', 'rows': rows}],
            },
        },
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        logger.info('WA list phone=%s status=%s body=%s',
                    mask_phone(to_phone), r.status_code, _safe_response_excerpt(r.text))
        if r.ok:
            return True
        logger.error('WA list failed phone=%s status=%s body=%s',
                     mask_phone(to_phone), r.status_code, _safe_response_excerpt(r.text))
        return False
    except requests.RequestException:
        logger.exception('WA list request failed')
        return False


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

    if use_list and len(opts) <= 10:
        ok = send_flow_list(phone, text, opts, button_text=_list_cta(_get_lang(get_lead_funnel_data(lead) or {})))
    elif len(opts) <= 3:
        ok = send_flow_buttons(phone, text, opts)
    else:
        lines = [str(text or '').strip(), ''] + [f'{i}. {t}' for i, t in opts]
        ok = send_whatsapp_message(phone, '\n'.join(lines).strip())

    if not ok:
        ok = send_whatsapp_message(phone, text)
    if ok:
        _mark_reply_sent(lead)
    return ok


def _send_text(lead, phone, text):
    if not _rate_limit_ok(lead):
        logger.info('Rate-limited text phone=%s', mask_phone(phone))
        return False
    ok = send_whatsapp_message(phone, text)
    if ok:
        _mark_reply_sent(lead)
    return ok


def _resolve_option(normalized_text, options):
    for opt_id, opt_title in options:
        if normalized_text in (_normalize_text(opt_id), _normalize_text(opt_title)):
            return str(opt_id)
    return None


def _set_flow_stage(lead, stage, **extra):
    update_lead_funnel(lead, stage=stage)
    if extra:
        update_lead_meta(lead, **extra)


# ─────────────────────────────────────────────
#  MAIN HANDLER
# ─────────────────────────────────────────────

def handle_message(phone, text):
    normalized = _normalize_text(text)
    logger.info('Incoming WA message phone=%s', mask_phone(phone))

    lead = upsert_lead(phone, normalized, source='WhatsApp Ads')
    lead = get_lead_by_phone(phone) if lead is None else lead

    if _text_has_malayalam(text):
        update_lead_meta(lead, language='ml')

    stage = get_lead_stage(lead) or 'new'
    meta  = get_lead_funnel_data(lead) or {}
    lang  = _get_lang(meta)
    words = _text_words(normalized)

    is_greeting = any(w in {'hi', 'hello', 'hey'} for w in words)
    is_fresh    = stage in {'new', 'completed'}

    # ── Backward-compatibility: collapse old stages ──────────────
    if stage in {'step_3', 'step_5'}:
        _set_flow_stage(lead, 'step_2')
        _send_step_prompt(lead, phone,
                          _pick(STEP_2_EN, STEP_2_ML, lang),
                          _options_step_3(lang))
        return

    if stage in {'step_7', 'step_9', 'step_need_time', 'step_10'}:
        _set_flow_stage(lead, 'step_8')
        _send_text(lead, phone, _pick(STEP_8_EN, STEP_8_ML, lang))
        return

    # ── Greeting / fresh start ───────────────────────────────────
    if is_greeting or is_fresh:
        _set_flow_stage(lead, 'step_lang')
        _send_step_prompt(lead, phone,
                          _pick(STEP_LANG_EN, STEP_LANG_ML, lang),
                          _options_step_lang(lang))
        return

    # ── step_lang: choose language ───────────────────────────────
    if stage == 'step_lang':
        opts     = _options_step_lang(lang)
        selected = _resolve_option(normalized, opts)
        if selected in {'1', '2'}:
            selected_lang = 'en' if selected == '1' else 'ml'
            update_lead_meta(lead, language=selected_lang)
            _set_flow_stage(lead, 'step_1')
            _send_step_prompt(lead, phone,
                              _pick(STEP_1_EN, STEP_1_ML, selected_lang),
                              _options_step_1(selected_lang))
            return
        # didn't pick — re-prompt
        _send_step_prompt(lead, phone,
                          _pick(STEP_LANG_EN, STEP_LANG_ML, lang),
                          opts)
        return

    # ── step_1: what do you need ─────────────────────────────────
    if stage == 'step_1':
        opts     = _options_step_1(lang)
        selected = _resolve_option(normalized, opts)
        # legacy label fallback
        if selected is None and normalized == _normalize_text('Improve existing business'):
            selected = '3'
        if selected in {'1', '2', '3'}:
            service_map = {'1': 'marketing', '2': 'ecommerce', '3': 'website'}
            label_map   = {'1': 'Marketing',  '2': 'Ecommerce', '3': 'Improve business'}
            service     = service_map[selected]
            _set_flow_stage(lead, 'step_2', service=service, business='business')
            update_lead_meta(lead, service=service, business='business')
            update_lead_funnel(lead, service=label_map[selected], set_qualified=True)
            _send_step_prompt(lead, phone,
                              _pick(STEP_2_EN, STEP_2_ML, lang),
                              _options_step_3(lang))
            return
        _send_step_prompt(lead, phone,
                          _pick(STEP_1_EN, STEP_1_ML, lang),
                          opts)
        return

    # ── step_2: business situation ───────────────────────────────
    if stage == 'step_2':
        opts     = _options_step_3(lang)
        selected = _resolve_option(normalized, opts)
        if selected in {'1', '2'}:
            stage_value = {'1': 'running', '2': 'planning'}[selected]
            update_lead_meta(lead, stage_value=stage_value)
            _set_flow_stage(lead, 'step_4', stage_value=stage_value)
            _send_step_prompt(lead, phone,
                              _pick(STEP_4_EN, STEP_4_ML, lang),
                              _options_step_4(lang))
            return
        if selected == '3':
            update_lead_meta(lead, stage_value='just_checking', flow_exit='just_exploring')
            update_lead_funnel(lead, stage='completed')
            _send_text(lead, phone, _pick(JUST_CHECKING_EN, JUST_CHECKING_ML, lang))
            return
        _send_step_prompt(lead, phone,
                          _pick(STEP_2_EN, STEP_2_ML, lang),
                          opts)
        return

    # ── step_4: timeline ─────────────────────────────────────────
    if stage == 'step_4':
        opts     = _options_step_4(lang)
        selected = _resolve_option(normalized, opts)
        if selected in {'1', '2'}:
            timeline = {'1': 'this_week', '2': 'within_1_month'}[selected]
            update_lead_meta(lead, timeline=timeline)

            meta        = get_lead_funnel_data(lead) or {}
            service     = str(meta.get('service',     '') or '').strip().lower()
            stage_value = str(meta.get('stage_value', '') or '').strip().lower()

            offer_text = _budget_offer_body(service, stage_value, lang)
            budget_opts = _budget_options(service, stage_value)

            _set_flow_stage(lead, 'step_6')
            _send_step_prompt(lead, phone, offer_text, budget_opts)
            return

        if selected == '3':
            update_lead_meta(lead, timeline='just_checking', flow_exit='timeline_just_checking')
            update_lead_funnel(lead, stage='completed')
            _send_text(lead, phone, _pick(JUST_CHECKING_EN, JUST_CHECKING_ML, lang))
            return

        _send_step_prompt(lead, phone,
                          _pick(STEP_4_EN, STEP_4_ML, lang),
                          opts)
        return

    # ── step_6: budget selection ──────────────────────────────────
    if stage == 'step_6':
        meta        = get_lead_funnel_data(lead) or {}
        service     = str(meta.get('service',     '') or '').strip().lower()
        stage_value = str(meta.get('stage_value', '') or '').strip().lower()
        timeline    = str(meta.get('timeline',    '') or '').strip().lower()

        offer_opts = _budget_options(service, stage_value)
        selected   = _resolve_option(normalized, offer_opts)
        valid_ids  = {oid for oid, _ in offer_opts}

        if selected in valid_ids:
            budget_range = dict(offer_opts)[selected]
            priority     = _priority_from(service, selected, stage_value)

            update_lead_meta(lead,
                             budget_range=budget_range,
                             budget_choice=str(selected),
                             priority=priority)

            if priority == 'high':
                update_lead_meta(lead, hot_lead=True)
                notify_sales_team(lead)

            if priority == 'low':
                update_lead_funnel(lead, stage='completed')
                _send_text(lead, phone, _pick(LOW_BUDGET_EN, LOW_BUDGET_ML, lang))
                return

            close_text = _closing_after_budget(timeline, lang)
            _set_flow_stage(lead, 'step_8')
            _send_text(lead, phone, close_text)
            return

        # invalid input — re-prompt
        retry_text = ('Tap one option below 👇'
                      if lang != 'ml' else
                      'ഒരു option tap ചെയ്യൂ 👇')
        _send_step_prompt(lead, phone, retry_text, offer_opts)
        return

    # ── step_8: collect name + call time ─────────────────────────
    if stage == 'step_8':
        raw = str(text or '').strip()
        if not raw:
            _send_text(lead, phone, _pick(STEP_8_EN, STEP_8_ML, lang))
            return

        low = raw.lower()
        if   any(k in low for k in ['morning',   'രാവിലെ', 'പ്രഭാത', 'am']):
            contact_time = 'morning'
        elif any(k in low for k in ['afternoon',  'ഉച്ച',    'pm']):
            contact_time = 'afternoon'
        elif any(k in low for k in ['evening',   'സായാഹ്ന', 'രാത്രി']):
            contact_time = 'evening'
        else:
            contact_time = 'any'

        # strip time keywords from name
        cleaned = re.sub(r'[,|–\-]+', ' ', raw).strip()
        for kw in ['morning', 'afternoon', 'evening', 'am', 'pm']:
            cleaned = re.sub(rf'\b{kw}\b', '', cleaned, flags=re.I)
        for kw in ['രാവിലെ', 'ഉച്ച', 'സായാഹ്നം', 'സായാഹ്ന', 'രാത്രി']:
            cleaned = cleaned.replace(kw, '')
        clean_name = cleaned.strip()[:120] or raw[:120]

        update_lead_funnel(lead, name=clean_name, stage='completed', set_qualified=True)
        update_lead_meta(lead,
                         name=clean_name,
                         contact_time=contact_time,
                         final_message_sent=True)

        now = timezone.localtime().time()
        if now >= time(19, 0) or now < time(10, 0):
            final = _pick(FINAL_OFF_TIME_EN, FINAL_OFF_TIME_ML, lang)
        else:
            final = _pick(FINAL_NORMAL_EN, FINAL_NORMAL_ML, lang)

        _send_text(lead, phone, final)
        return

    # ── Fallback: restart ────────────────────────────────────────
    _set_flow_stage(lead, 'step_lang')
    _send_step_prompt(lead, phone,
                      _pick(STEP_LANG_EN, STEP_LANG_ML, lang),
                      _options_step_lang(lang))