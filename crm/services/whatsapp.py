import logging
import re
from datetime import timedelta

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


STEP_LANG_TEXT = (
    "Hi, this is Lois.\n\n"
    "I’ll quickly understand your business and guide you properly.\n\n"
    "Which language is comfortable for you?"
)

STEP_1_TEXT = (
    "Hi, this is Lois.\n\n"
    "We usually help businesses increase their sales using digital systems.\n\n"
    "Let me quickly understand your situation and guide you properly.\n\n"
    "What are you looking for right now?"
)

STEP_2_TEXT = (
    "Nice 👍\n\n"
    "Can you tell me a bit about your business?"
)

STEP_3_TEXT = (
    "Got it.\n\n"
    "Where are you right now?"
)

STEP_4_TEXT = (
    "When are you planning to move forward with this?"
)

STEP_8_TEXT = (
    "Great 👍\n\n"
    "What’s your name?"
)

STEP_9_TEXT = (
    "When is a good time to connect and explain everything clearly?"
)

FLOW_FALLBACK = {
    'step_lang': STEP_LANG_TEXT,
    'step_1': STEP_1_TEXT,
    'step_2': STEP_2_TEXT,
    'step_3': STEP_3_TEXT,
    'step_4': STEP_4_TEXT,
    'step_8': STEP_8_TEXT,
    'step_9': STEP_9_TEXT,
}

OPTIONS_STEP_LANG = [('1', 'English'), ('2', 'Malayalam')]
OPTIONS_STEP_1 = [('1', 'Get more customers'), ('2', 'Start selling online'), ('3', 'Improve existing business')]
OPTIONS_STEP_2 = [('1', 'Website'), ('2', 'Ecommerce'), ('3', 'Marketing')]
OPTIONS_STEP_3 = [('1', 'Running, but not getting enough sales'), ('2', 'Planning to start'), ('3', 'Just exploring')]
OPTIONS_STEP_4 = [('1', 'Immediately'), ('2', 'Within a month'), ('3', 'Just checking')]
OPTIONS_STEP_6_WEBSITE_STARTING = [('1', '15k-25k'), ('2', '25k-40k')]
OPTIONS_STEP_6_WEBSITE_RUNNING = [('1', '25k-40k'), ('2', '40k-60k')]
OPTIONS_STEP_6_ECOM_STARTING = [('1', '25k-40k'), ('2', '40k-60k')]
OPTIONS_STEP_6_ECOM_RUNNING = [('1', '40k-70k'), ('2', '70k+')]
OPTIONS_STEP_6_MARKETING = [('1', '15k-25k/mo'), ('2', '25k-40k/mo'), ('3', '40k+/mo')]
OPTIONS_STEP_7 = [('1', 'Yes, let’s move forward'), ('2', 'Need some time')]
OPTIONS_STEP_9 = [('1', 'Morning'), ('2', 'Afternoon'), ('3', 'Evening')]
OPTIONS_STEP_2_BUSINESS = [
    ('1', 'Clothing / Boutique'),
    ('2', 'Jewellery'),
    ('3', 'Other'),
]


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
    """True if the message contains Malayalam script (user wrote in Malayalam)."""
    return bool(re.search(r'[\u0D00-\u0D7F]', str(text or '')))


def _text_words(text):
    return re.findall(r'[a-z0-9]+', str(text or '').lower())


def _safe_response_excerpt(body):
    txt = str(body or '').strip()
    return txt[:200]


def is_duplicate_event(message_id):
    if not message_id:
        return False
    key = f'wa:message:{message_id}'
    if cache.get(key):
        return True
    cache.set(key, True, timeout=24 * 60 * 60)
    return False


def send_whatsapp_message(phone, text):
    token = str(getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '') or '').strip()
    phone_number_id = str(getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '') or '').strip()
    to_phone = _normalize_phone(phone)

    if not token or not phone_number_id:
        logger.error('Missing WhatsApp credentials in environment variables')
        return False
    if not to_phone:
        logger.warning('Cannot send WhatsApp message: invalid phone')
        return False

    url = f'https://graph.facebook.com/v22.0/{phone_number_id}/messages'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'text',
        'text': {'body': text},
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        logger.info(
            'WhatsApp send response phone=%s status=%s body=%s',
            mask_phone(to_phone),
            response.status_code,
            _safe_response_excerpt(response.text),
        )
        if response.ok:
            return True
        logger.error(
            'WhatsApp send failed phone=%s status=%s body=%s',
            mask_phone(to_phone),
            response.status_code,
            _safe_response_excerpt(response.text),
        )
        return False
    except requests.RequestException:
        logger.exception('WhatsApp send request failed')
        return False


def send_interactive_buttons(phone):
    return send_flow_buttons(phone, STEP_1_TEXT, OPTIONS_STEP_1)


def send_budget_buttons(phone):
    return send_flow_buttons(phone, STEP_4_TEXT, OPTIONS_STEP_4)


def send_flow_buttons(phone, body_text, options):
    token = str(getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '') or '').strip()
    phone_number_id = str(getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '') or '').strip()
    to_phone = _normalize_phone(phone)

    if not token or not phone_number_id:
        logger.error('Missing WhatsApp credentials in environment variables')
        return False
    if not to_phone:
        logger.warning('Cannot send interactive WhatsApp message: invalid phone')
        return False

    url = f'https://graph.facebook.com/v22.0/{phone_number_id}/messages'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
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
                        'reply': {'id': str(opt_id)[:256], 'title': str(opt_title)[:20]},
                    }
                    for opt_id, opt_title in (options or [])[:3]
                ]
            },
        },
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        logger.info(
            'WhatsApp interactive response phone=%s status=%s body=%s',
            mask_phone(to_phone),
            response.status_code,
            _safe_response_excerpt(response.text),
        )
        if response.ok:
            return True
        logger.error(
            'WhatsApp interactive failed phone=%s status=%s body=%s',
            mask_phone(to_phone),
            response.status_code,
            _safe_response_excerpt(response.text),
        )
        return False
    except requests.RequestException:
        logger.exception('WhatsApp interactive request failed')
        return False


def send_flow_list(phone, body_text, options, button_text='Select'):
    token = str(getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '') or '').strip()
    phone_number_id = str(getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '') or '').strip()
    to_phone = _normalize_phone(phone)

    if not token or not phone_number_id:
        logger.error('Missing WhatsApp credentials in environment variables')
        return False
    if not to_phone:
        logger.warning('Cannot send interactive WhatsApp list: invalid phone')
        return False

    rows = [
        {'id': str(opt_id)[:200], 'title': str(opt_title)[:24]}
        for opt_id, opt_title in (options or [])[:10]
    ]
    if not rows:
        return send_whatsapp_message(phone, body_text)

    url = f'https://graph.facebook.com/v22.0/{phone_number_id}/messages'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'interactive',
        'interactive': {
            'type': 'list',
            'body': {'text': str(body_text or '')[:1024]},
            'action': {
                'button': str(button_text or 'Select')[:20],
                'sections': [{'title': 'Options', 'rows': rows}],
            },
        },
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        logger.info(
            'WhatsApp list response phone=%s status=%s body=%s',
            mask_phone(to_phone),
            response.status_code,
            _safe_response_excerpt(response.text),
        )
        if response.ok:
            return True
        logger.error(
            'WhatsApp list failed phone=%s status=%s body=%s',
            mask_phone(to_phone),
            response.status_code,
            _safe_response_excerpt(response.text),
        )
        return False
    except requests.RequestException:
        logger.exception('WhatsApp list request failed')
        return False


def _priority_from(service, budget):
    if service in {'Ecommerce Premium', 'Ecommerce Growth', 'Website + Marketing'} or budget == 'Rs.25,000+':
        return 'high'
    if service or budget:
        return 'medium'
    return 'low'


def _budget_recommendation(choice):
    if choice == '1':
        return 'Basic website recommended. Upgrade possible later.'
    if choice == '2':
        return 'Business website or Starter ecommerce recommended.'
    if choice == '3':
        return 'Growth or Premium ecommerce recommended for scaling.'
    return ''


def _set_flow_stage(lead, stage, **extra):
    update_lead_funnel(lead, stage=stage)
    if extra:
        update_lead_meta(lead, **extra)


def _send_step_prompt(lead, phone, text, options):
    opts = options or []
    if not _rate_limit_ok(lead):
        logger.info('Rate-limited WhatsApp prompt phone=%s', mask_phone(phone))
        return False
    if len(opts) <= 3:
        ok = send_flow_buttons(phone, text, opts)
    else:
        listed = [str(text or '').strip(), ""]
        for opt_id, opt_title in opts:
            listed.append(f"{opt_id}. {opt_title}")
        ok = send_whatsapp_message(phone, "\n".join(listed).strip())
    if not ok:
        ok = send_whatsapp_message(phone, text)
    if ok:
        _mark_reply_sent(lead)
    return ok


def _resolve_option_choice(normalized_text, options):
    for opt_id, opt_title in options:
        if normalized_text == _normalize_text(opt_id) or normalized_text == _normalize_text(opt_title):
            return str(opt_id)
    return None


def _business_options():
    return [
        ('1', 'Clothing / Boutique'),
        ('2', 'Jewellery'),
        ('3', 'Other'),
    ]


def _get_lang(meta):
    lang = str((meta or {}).get('language') or 'en').strip().lower()
    return 'ml' if lang == 'ml' else 'en'


def _options_step_lang(lang):
    if lang == 'ml':
        return [('1', 'ഇംഗ്ലീഷ്'), ('2', 'മലയാളം')]
    return OPTIONS_STEP_LANG


def _localized_options_step_1(lang):
    if lang == 'ml':
        return [
            ('1', 'കൂടുതൽ ഗ്രാഹകർ'),
            ('2', 'ഓൺലൈൻ വിൽപ്പന'),
            ('3', 'ബിസിനസ് വളർത്തൽ'),
        ]
    return OPTIONS_STEP_1


def _local_text(key, lang):
    en = {
        'step_lang': STEP_LANG_TEXT,
        'step_1': STEP_1_TEXT,
        'step_2': STEP_2_TEXT,
        'step_3': STEP_3_TEXT,
        'step_4': STEP_4_TEXT,
        'step_8': STEP_8_TEXT,
        'step_9': STEP_9_TEXT,
        'just_checking_end': "No problem at all.\n\nPlease message me anytime when you are ready.",
        'need_time_end': (
            "No problem 👍\n\n"
            "Just to understand,\n\n"
            "What’s stopping you right now?\n\n"
            "1 -> Budget\n"
            "2 -> Need to think\n"
            "3 -> Not sure if it will work"
        ),
        'closing_this_week': (
            "If this is set up properly, you will start seeing real enquiries and sales.\n\n"
            "Would you like me to set this up properly for your business?"
        ),
        'closing_1_month': (
            "If this is set up properly, you will start seeing real enquiries and sales.\n\n"
            "Would you like me to set this up properly for your business?"
        ),
        'closing_2_months': (
            "If this is set up properly, you will start seeing real enquiries and sales.\n\n"
            "Would you like me to set this up properly for your business?"
        ),
        'final': (
            "Perfect, {name}.\n\n"
            "I’ll personally review your requirement and suggest the best approach for your business.\n\n"
            "We’ll keep everything simple and focused on getting you real results.\n\n"
            "Talk soon 👍"
        ),
    }
    ml = {
        'step_lang': "ഹായ്, ഞാൻ ലോയിസ് ആണ്.\n\nതാങ്കൾക്ക് ഏത് ഭാഷയിൽ സംസാരിക്കുന്നത് സൗകര്യമാണ്?",
        'step_1': "നമസ്കാരം.\n\nതാങ്കൾക്ക് ഇപ്പോൾ ഏത് സേവനമാണ് വേണ്ടത്?",
        'step_2': "നന്ദി.\n\nതാങ്കളുടെ ബിസിനസ് തരം ഏതാണ്?",
        'step_3': "ശരി.\n\nഇപ്പോൾ താങ്കളുടെ സ്റ്റേജ് എന്താണ്?",
        'step_4': "തുടങ്ങാൻ താങ്കൾ ആഗ്രഹിക്കുന്നത് എപ്പോൾ?",
        'step_8': "താങ്കളുടെ പേര് അറിയാമോ?",
        'step_9': "ബന്ധപ്പെടാൻ താങ്കൾക്ക് സൗകര്യമുള്ള സമയം ഏതാണ്?",
        'just_checking_end': "പ്രശ്നമില്ല.\n\nതാങ്കൾ തയ്യാറാകുമ്പോൾ ഏത് സമയത്തും മെസേജ് ചെയ്യാം.",
        'need_time_end': "ശരി, സമയം എടുത്തോളൂ.\n\nതാങ്കൾ തയ്യാറാകുമ്പോൾ ഞാൻ ഇവിടെ ഉണ്ടാകും.",
        'closing_this_week': "വളരെ നല്ലത്.\n\nഈ ആഴ്ച തന്നെ നമുക്ക് വേഗത്തിൽ, ഘട്ടം ഘട്ടമായി തുടങ്ങാം.\n\nമുന്നോട്ട് പോവാമോ?",
        'closing_1_month': "അതെ, നല്ലതാണ്.\n\nഅവസരപ്പെടാതെ നന്നായി പ്ലാൻ ചെയ്ത് തുടങ്ങാം.\n\nമുന്നോട്ട് പോവാമോ?",
        'closing_2_months': "ശരി.\n\nമുൻകൂട്ടി എല്ലാം തയ്യാറാക്കാം.\n\nമുന്നോട്ട് പോവാമോ?",
        'final': "നന്ദി {name}.\n\nതാങ്കളുടെ ആവശ്യങ്ങൾ പരിശോധിച്ച് ഉടൻ തന്നെ ബന്ധപ്പെടാം.\n\nഓരോ ഘട്ടത്തിലും വ്യക്തമായി ഞങ്ങൾ ഗൈഡ് ചെയ്യും.",
    }
    table = ml if lang == 'ml' else en
    return table[key]


def _localized_business_options(lang):
    if lang == 'ml':
        return [
            ('1', 'വസ്ത്ര ബൂട്ടീക്'),
            ('2', 'ജ്വല്ലറി'),
            ('3', 'മറ്റുള്ളത്'),
        ]
    return OPTIONS_STEP_2_BUSINESS


def _localized_options_step_3(lang):
    if lang == 'ml':
        return [
            ('1', 'ഇപ്പോൾ നടത്തുന്നു'),
            ('2', 'തുടങ്ങാൻ പദ്ധതി'),
            ('3', 'പരീക്ഷണം മാത്രം'),
        ]
    return OPTIONS_STEP_3


def _localized_options_step_4(lang):
    if lang == 'ml':
        return [('1', 'ഈ ആഴ്ച'), ('2', 'ഒരു മാസത്തിനകം'), ('3', 'രണ്ട് മാസത്തിൽ')]
    return OPTIONS_STEP_4


def _localized_options_step_7(lang):
    if lang == 'ml':
        return [('1', 'അതെ, തുടരാം'), ('2', 'പിന്നീട് പറയാം')]
    return OPTIONS_STEP_7


def _localized_options_step_9(lang):
    if lang == 'ml':
        return [('1', 'പ്രഭാതം'), ('2', 'ഉച്ചയ്ക്ക്'), ('3', 'സായാഹ്നം')]
    return OPTIONS_STEP_9


_ML_BUDGET_TITLE = {
    '15k-25k': '₹15–25k',
    '25k-40k': '₹25–40k',
    '40k-60k': '₹40–60k',
    '40k-70k': '₹40–70k',
    '70k+': '₹70k+',
    '15k-25k/mo': '15–25k/മാസം',
    '25k-40k/mo': '25–40k/മാസം',
    '40k+/mo': '40k+/മാസം',
}


def _localize_option_titles(options, lang):
    if lang != 'ml':
        return options
    return [(opt_id, _ML_BUDGET_TITLE.get(opt_title, opt_title)) for opt_id, opt_title in options]


def _dynamic_step_5(service, business, lang='en'):
    if lang == 'ml':
        if service == 'website' and business == 'clinic':
            return (
                "ക്ലിനിക്കിനായി ആളുകൾ ആദ്യം ഓൺലൈനിൽ നോക്കും.\n\n"
                "സിംപിൾ, ക്ലീൻ വെബ്സൈറ്റ് വിശ്വാസവും എൻക്വയറിയും കൂട്ടാൻ സഹായിക്കും."
            )
        if service == 'website' and business == 'coaching':
            return "കോച്ചിംഗിനായി വെബ്സൈറ്റ് കോഴ്സുകൾ വ്യക്തമായി കാണിക്കാനും വിശ്വാസം നൽകാനും സഹായിക്കും."
        if service == 'website':
            return f"{business.title()} ബിസിനസിന് വെബ്സൈറ്റ് ആളുകൾക്ക് നിങ്ങളെ എളുപ്പത്തിൽ കണ്ടെത്താനും ബന്ധപ്പെടാനും സഹായിക്കും."
        if service == 'ecommerce' and business == 'clothing store':
            return (
                "ക്ലോത്തിംഗ് ഓൺലൈനിൽ നല്ല രീതിയിൽ വിറ്റഴിക്കാം.\n\n"
                "പ്രോഡക്ട്സ്, പേയ്മെന്റ്സ്, ഓർഡേഴ്സ് എല്ലാം സ്മൂത്ത് ആയി സജ്ജമാക്കാം."
            )
        if service == 'ecommerce' and business == 'jewellery':
            return (
                "ജ്വല്ലറിക്ക് ഒരു പ്രീമിയം ഓൺലൈൻ സ്റ്റോർ ആവശ്യമാണ്.\n\n"
                "ഞങ്ങൾ ക്ലീൻ, സ്മൂത്ത് ഇ-കൊമേഴ്സ് സിസ്റ്റം ഒരുക്കും."
            )
        if service == 'ecommerce':
            return (
                "ഇ-കൊമേഴ്സ് ശരിയായി സെറ്റ് ചെയ്‌താൽ ബിസിനസിന് നല്ല വളർച്ച ലഭിക്കും.\n\n"
                "സ്മൂത്ത് ആയി പ്രവർത്തിക്കുന്ന ഫുൾ സിസ്റ്റം നമുക്ക് ഒരുക്കാം."
            )
        if service == 'marketing' and business == 'clinic':
            return (
                "ക്ലിനിക്കിനായി ആഡ്സ് വഴി ദിനംപ്രതി എൻക്വയറി നേടാം.\n\n"
                "അടുത്തുള്ള ആളുകളെ ടാർഗെറ്റ് ചെയ്യാം."
            )
        if service == 'marketing' and business == 'coaching':
            return "കോച്ചിംഗിനായി ആഡ്സ് ബാച്ചുകൾ വേഗത്തിൽ നിറയ്ക്കാൻ സഹായിക്കും."
        return (
            f"{business.title()} ബിസിനസിന് ശരിയായ മാർക്കറ്റിംഗ് വഴി അനുയോജ്യമായ കസ്റ്റമേഴ്സിനെ നേടാം.\n\n"
            "ഞങ്ങൾ ഫോകസ് ചെയ്യുന്നത് യഥാർത്ഥ എൻക്വയറികളിലാണ്."
        )

    if service == 'website' and business == 'clinic':
        return (
            "For a clinic, people check online first.\n\n"
            "A clean website builds trust and brings enquiries."
        )
    if service == 'website' and business == 'coaching':
        return "For coaching, a website helps show your courses and build trust."
    if service == 'website':
        return f"For a {business}, a website helps people find you and contact you easily."
    if service == 'ecommerce' and business == 'clothing store':
        return (
            "Most businesses think just creating a website will bring sales.\n\n"
            "But in reality, that’s why many stores fail.\n\n"
            "You need a proper system:\n"
            "– Right structure\n"
            "– Trust building\n"
            "– Smooth buying experience\n\n"
            "That’s what actually brings sales.\n\n"
            "Many businesses invest in websites but don’t see results because the system is not set up properly.\n\n"
            "We’ve worked with similar businesses and helped improve their enquiries.\n\n"
            "Don’t worry, we’ll guide you step by step 👍"
        )
    if service == 'ecommerce' and business == 'jewellery':
        return (
            "Most businesses think just creating a website will bring sales.\n\n"
            "But in reality, that’s why many stores fail.\n\n"
            "You need a proper system:\n"
            "– Right structure\n"
            "– Trust building\n"
            "– Smooth buying experience\n\n"
            "That’s what actually brings sales.\n\n"
            "Many businesses invest in websites but don’t see results because the system is not set up properly.\n\n"
            "We’ve worked with similar businesses and helped improve their enquiries.\n\n"
            "Don’t worry, we’ll guide you step by step 👍"
        )
    if service == 'ecommerce':
        return (
            "Most businesses think just creating a website will bring sales.\n\n"
            "But in reality, that’s why many stores fail.\n\n"
            "You need a proper system:\n"
            "– Right structure\n"
            "– Trust building\n"
            "– Smooth buying experience\n\n"
            "That’s what actually brings sales.\n\n"
            "Many businesses invest in websites but don’t see results because the system is not set up properly.\n\n"
            "We’ve worked with similar businesses and helped improve their enquiries.\n\n"
            "Don’t worry, we’ll guide you step by step 👍"
        )
    if service == 'marketing' and business == 'clinic':
        return (
            "For clinics, ads can bring daily enquiries.\n\n"
            "We target nearby people."
        )
    if service == 'marketing' and business == 'coaching':
        return "For coaching, ads help fill batches faster."
    return (
        "Marketing helps bring the right customers to your business.\n\n"
        "We focus on getting real enquiries."
    )


def _step_6_offer(service, stage_value, lang='en'):
    if service == 'website' and stage_value == 'planning':
        return (
            "Based on what you told, this is what usually works for businesses like yours:\n\n"
            "We focus more on getting results than just building a website.\n\n"
            "We usually take only a limited number of projects at a time to keep quality high.\n\n"
            "This week we have only a few slots available."
            if lang == 'en'
            else "തുടങ്ങുന്ന ബിസിനസുകൾക്ക് സാധാരണ ഒരു ലളിതമായ സെറ്റപ്പ് ആണ് ഞങ്ങൾ നിർദേശിക്കുന്നത്.\n\n"
            "ഇതാണ് സാധാരണ റേഞ്ച്:",
            _localize_option_titles(OPTIONS_STEP_6_WEBSITE_STARTING, lang),
        )
    if service == 'website':
        return (
            "Based on what you told, this is what usually works for businesses like yours:\n\n"
            "We focus more on getting results than just building a website.\n\n"
            "We usually take only a limited number of projects at a time to keep quality high.\n\n"
            "This week we have only a few slots available."
            if lang == 'en'
            else "വളരുന്ന ബിസിനസുകൾക്ക് നല്ല ഒരു സെറ്റപ്പ് കൂടുതൽ ഫലം നൽകും.\n\n"
            "ഇതാണ് സാധാരണ റേഞ്ച്:",
            _localize_option_titles(OPTIONS_STEP_6_WEBSITE_RUNNING, lang),
        )
    if service == 'ecommerce' and stage_value == 'planning':
        return (
            "Based on what you told, this is what usually works for businesses like yours:\n\n"
            "We focus more on getting results than just building a website.\n\n"
            "We usually take only a limited number of projects at a time to keep quality high.\n\n"
            "This week we have only a few slots available."
            if lang == 'en'
            else "ഇ-കൊമേഴ്സ് സ്മൂത്ത് ആയി പ്രവർത്തിക്കാൻ ശരിയായ സെറ്റപ്പ് ആവശ്യമാണ്.\n\n"
            "ഇതാണ് സാധാരണ റേഞ്ച്:",
            _localize_option_titles(OPTIONS_STEP_6_ECOM_STARTING, lang),
        )
    if service == 'ecommerce':
        return (
            "Based on what you told, this is what usually works for businesses like yours:\n\n"
            "We focus more on getting results than just building a website.\n\n"
            "We usually take only a limited number of projects at a time to keep quality high.\n\n"
            "This week we have only a few slots available."
            if lang == 'en'
            else "ഇ-കൊമേഴ്സ് സ്മൂത്ത് ആയി പ്രവർത്തിക്കാൻ ശരിയായ സെറ്റപ്പ് ആവശ്യമാണ്.\n\n"
            "ഇതാണ് സാധാരണ റേഞ്ച്:",
            _localize_option_titles(OPTIONS_STEP_6_ECOM_RUNNING, lang),
        )
    return (
        "Based on what you told, this is what usually works for businesses like yours:\n\n"
        "We focus more on getting results than just building a website.\n\n"
        "We usually take only a limited number of projects at a time to keep quality high.\n\n"
        "This week we have only a few slots available."
        if lang == 'en'
        else "മാർക്കറ്റിംഗ് ഒരു മാസാന്ത പ്ലാൻ ആയി പ്രവർത്തിക്കുന്നു.\n\n"
        "ഇതാണ് സാധാരണ റേഞ്ച്:",
        _localize_option_titles(OPTIONS_STEP_6_MARKETING, lang),
    )


def _step_7_text(timeline, lang='en'):
    if timeline == 'this_week':
        return _local_text('closing_this_week', lang)
    if timeline == 'within_1_month':
        return _local_text('closing_1_month', lang)
    return _local_text('closing_2_months', lang)


def _rate_limit_ok(lead):
    return True


def _mark_reply_sent(lead):
    update_lead_meta(lead, last_reply_time=timezone.now().isoformat())


def _send_rate_limited_text(lead, phone, text):
    if not _rate_limit_ok(lead):
        logger.info('Rate-limited WhatsApp reply phone=%s', mask_phone(phone))
        return False
    ok = send_whatsapp_message(phone, text)
    if ok:
        _mark_reply_sent(lead)
    return ok


def _send_rate_limited_interactive(lead, phone, kind):
    if not _rate_limit_ok(lead):
        logger.info('Rate-limited WhatsApp %s phone=%s', kind, mask_phone(phone))
        return False
    ok = send_interactive_buttons(phone) if kind == 'interactive' else send_budget_buttons(phone)
    if ok:
        _mark_reply_sent(lead)
    return ok


def _send_rate_limited_buttons(lead, phone, text, options):
    if not _rate_limit_ok(lead):
        logger.info('Rate-limited WhatsApp buttons phone=%s', mask_phone(phone))
        return False
    ok = send_flow_buttons(phone, text, options)
    if not ok:
        ok = send_whatsapp_message(phone, text)
    if ok:
        _mark_reply_sent(lead)
    return ok


def handle_message(phone, text):
    normalized_text = _normalize_text(text)
    masked_phone = mask_phone(phone)
    logger.info('Incoming WhatsApp message phone=%s', masked_phone)

    lead = upsert_lead(phone, normalized_text, source='WhatsApp Ads')
    lead = get_lead_by_phone(phone) if lead is None else lead
    if _text_has_malayalam(text):
        update_lead_meta(lead, language='ml')
    stage = get_lead_stage(lead) or 'new'
    meta = get_lead_funnel_data(lead)
    lang = _get_lang(meta)
    words = _text_words(normalized_text)
    is_greeting = any(word in {'hi', 'hello', 'hey'} for word in words)
    is_fresh = stage in {'new', 'completed'}

    if is_greeting or is_fresh:
        _set_flow_stage(lead, 'step_lang')
        lang_opts = _options_step_lang(lang)
        _send_step_prompt(lead, phone, _local_text('step_lang', lang), lang_opts)
        return

    if stage == 'step_lang':
        lang_opts = _options_step_lang(lang)
        selected = _resolve_option_choice(normalized_text, lang_opts)
        if selected in {'1', '2'}:
            selected_lang = 'en' if selected == '1' else 'ml'
            update_lead_meta(lead, language=selected_lang)
            _set_flow_stage(lead, 'step_1')
            _send_step_prompt(
                lead, phone, _local_text('step_1', selected_lang), _localized_options_step_1(selected_lang)
            )
            return
        _send_step_prompt(lead, phone, _local_text('step_lang', lang), lang_opts)
        return

    if stage == 'step_1':
        opts_1 = _localized_options_step_1(lang)
        selected = _resolve_option_choice(normalized_text, opts_1)
        if selected in {'1', '2', '3'}:
            service_map = {'1': 'website', '2': 'ecommerce', '3': 'marketing'}
            label_map = {'1': 'Website', '2': 'Ecommerce', '3': 'Marketing'}
            service_value = service_map[selected]
            _set_flow_stage(lead, 'step_2', service=service_value)
            update_lead_funnel(lead, service=label_map[selected], set_qualified=True)
            _send_step_prompt(lead, phone, _local_text('step_2', lang), _localized_business_options(lang))
            return
        _send_step_prompt(lead, phone, _local_text('step_1', lang), opts_1)
        return

    if stage == 'step_2':
        business_options = _localized_business_options(lang)
        selected = _resolve_option_choice(normalized_text, business_options)
        if selected in {'1', '2', '3'}:
            business_map = {
                '1': 'clothing store',
                '2': 'jewellery',
                '3': 'others',
            }
            _set_flow_stage(lead, 'step_3', business=business_map[selected])
            _send_step_prompt(lead, phone, _local_text('step_3', lang), _localized_options_step_3(lang))
            return
        _send_step_prompt(lead, phone, _local_text('step_2', lang), business_options)
        return

    if stage == 'step_3':
        stage_options = _localized_options_step_3(lang)
        selected = _resolve_option_choice(normalized_text, stage_options)
        if selected in {'1', '2'}:
            stage_map = {'1': 'running', '2': 'planning'}
            _set_flow_stage(lead, 'step_4', stage_value=stage_map[selected])
            _send_step_prompt(lead, phone, _local_text('step_4', lang), _localized_options_step_4(lang))
            return
        if selected == '3':
            update_lead_meta(lead, stage_value='just_checking', readiness='need_time', flow_exit='just_checking')
            update_lead_funnel(lead, stage='completed')
            _send_rate_limited_text(lead, phone, _local_text('just_checking_end', lang))
            return
        _send_step_prompt(lead, phone, _local_text('step_3', lang), stage_options)
        return

    if stage == 'step_4':
        timeline_options = _localized_options_step_4(lang)
        selected = _resolve_option_choice(normalized_text, timeline_options)
        if selected in {'1', '2', '3'}:
            timeline_map = {'1': 'this_week', '2': 'within_1_month', '3': 'within_2_months'}
            timeline_value = timeline_map[selected]
            update_lead_meta(lead, timeline=timeline_value)
            meta = get_lead_funnel_data(lead)
            service = str(meta.get('service', '') or '').strip().lower()
            business = str(meta.get('business', '') or 'business').strip().lower()
            stage_value = str(meta.get('stage_value', '') or '').strip().lower()
            step_5_text = _dynamic_step_5(service, business, lang)
            step_6_text, step_6_options = _step_6_offer(service, stage_value, lang)
            _set_flow_stage(lead, 'step_6')
            combined_text = f"{step_5_text}\n\n{step_6_text}"
            _send_step_prompt(lead, phone, combined_text, step_6_options)
            return
        _send_step_prompt(lead, phone, _local_text('step_4', lang), timeline_options)
        return

    if stage == 'step_5':
        # Backward compatibility for leads that were already at step_5.
        meta = get_lead_funnel_data(lead)
        service = str(meta.get('service', '') or '').strip().lower()
        stage_value = str(meta.get('stage_value', '') or '').strip().lower()
        business = str(meta.get('business', '') or 'business').strip().lower()
        step_5_text = _dynamic_step_5(service, business, lang)
        step_6_text, step_6_options = _step_6_offer(service, stage_value, lang)
        _set_flow_stage(lead, 'step_6')
        _send_step_prompt(lead, phone, f"{step_5_text}\n\n{step_6_text}", step_6_options)
        return

    if stage == 'step_6':
        meta = get_lead_funnel_data(lead)
        service = str(meta.get('service', '') or '').strip().lower()
        stage_value = str(meta.get('stage_value', '') or '').strip().lower()
        _offer_text, offer_options = _step_6_offer(service, stage_value, lang)
        selected = _resolve_option_choice(normalized_text, offer_options)
        valid_ids = {opt_id for opt_id, _title in offer_options}
        if selected in valid_ids:
            budget_lookup = {opt_id: title for opt_id, title in offer_options}
            budget_range = budget_lookup[selected]
            update_lead_meta(lead, budget_range=budget_range)
            timeline = str(meta.get('timeline', '') or '').strip().lower()
            _set_flow_stage(lead, 'step_7')
            _send_step_prompt(lead, phone, _step_7_text(timeline, lang), _localized_options_step_7(lang))
            return
        _send_step_prompt(lead, phone, _step_6_offer(service, stage_value, lang)[0], offer_options)
        return

    if stage == 'step_7':
        closing_options = _localized_options_step_7(lang)
        selected = _resolve_option_choice(normalized_text, closing_options)
        if selected == '1':
            update_lead_meta(lead, readiness='yes')
            _set_flow_stage(lead, 'step_8')
            _send_rate_limited_text(lead, phone, _local_text('step_8', lang))
            return
        if selected == '2':
            update_lead_meta(lead, readiness='need_time', flow_exit='need_time')
            update_lead_funnel(lead, stage='completed')
            _send_rate_limited_text(lead, phone, _local_text('need_time_end', lang))
            return
        meta = get_lead_funnel_data(lead)
        timeline = str(meta.get('timeline', '') or '').strip().lower()
        _send_step_prompt(lead, phone, _step_7_text(timeline, lang), closing_options)
        return

    if stage == 'step_8':
        if normalized_text:
            clean_name = text.strip()[:120]
            update_lead_funnel(lead, name=clean_name)
            update_lead_meta(lead, name=clean_name)
            _set_flow_stage(lead, 'step_9')
            _send_step_prompt(lead, phone, _local_text('step_9', lang), _localized_options_step_9(lang))
            return
        _send_rate_limited_text(lead, phone, _local_text('step_8', lang))
        return

    if stage == 'step_9':
        contact_options = _localized_options_step_9(lang)
        selected = _resolve_option_choice(normalized_text, contact_options)
        if selected in {'1', '2', '3'}:
            contact_map = {'1': 'morning', '2': 'afternoon', '3': 'evening'}
            update_lead_meta(lead, contact_time=contact_map[selected])
            _set_flow_stage(lead, 'step_10')
            meta = get_lead_funnel_data(lead)
            name = str(meta.get('name', '') or getattr(lead, 'name', '') or '').strip()
            if not name:
                name = "there"
            final_text = _local_text('final', lang).format(name=name)
            _send_rate_limited_text(lead, phone, final_text)
            update_lead_funnel(lead, stage='completed', set_qualified=True)
            update_lead_meta(lead, final_message_sent=True)
            return
        _send_step_prompt(lead, phone, _local_text('step_9', lang), contact_options)
        return

    if stage == 'step_10':
        update_lead_funnel(lead, stage='completed')
        return

    _set_flow_stage(lead, 'step_lang')
    _send_step_prompt(lead, phone, _local_text('step_lang', lang), _options_step_lang(lang))
