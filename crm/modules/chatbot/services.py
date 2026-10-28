"""Chat engine for the website assistant: stores the conversation, asks the free AI model for the
reply (falling back to built-in answers), and turns contact details into CRM leads."""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import llm, notify
from crm.models import ChatMessage, ChatSession, Lead, LeadActivity, LeadSource
from crm.modules.leads import services as lead_svc

from . import knowledge

logger = logging.getLogger('crm.chatbot')

SOURCE_NAME = 'Website Chatbot'
MAX_TEXT = 600
HISTORY_FOR_AI = 12


class ChatError(Exception):
    pass


def new_token() -> str:
    return str(uuid.uuid4())


def get_session(token: str | None) -> ChatSession | None:
    try:
        return ChatSession.objects.filter(token=uuid.UUID(str(token))).first()
    except (ValueError, TypeError, AttributeError):
        return None


def start_session(request, page: str = '') -> ChatSession:
    session = ChatSession.objects.create(
        page_url=(page or '')[:300], referrer=request.META.get('HTTP_REFERER', '')[:300],
        ip=_ip(request), user_agent=request.META.get('HTTP_USER_AGENT', '')[:300])
    ChatMessage.objects.create(session=session, role='bot', content=knowledge.GREETING, engine='rules')
    session.message_count = 1
    session.save(update_fields=['message_count'])
    return session


def _ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR', '')) or None
    return ip


def history(session: ChatSession, limit: int = 60) -> list[dict]:
    rows = list(session.messages.order_by('-created_at', '-id')[:limit])[::-1]
    return [{'role': m.role, 'text': m.content} for m in rows]


def _ai_messages(session: ChatSession) -> list[dict]:
    system = knowledge.system_prompt()
    if session.lead_id:
        system += ("\nThe visitor has already shared their contact details and the team will contact them. "
                   "Do not ask for them again; keep helping with their questions.")
    recent = list(session.messages.order_by('-created_at', '-id')[:HISTORY_FOR_AI])[::-1]
    msgs = [{'role': 'system', 'content': system}]
    for m in recent:
        msgs.append({'role': 'user' if m.role == 'visitor' else 'assistant', 'content': m.content[:MAX_TEXT * 2]})
    return msgs


def reply_to(session: ChatSession, text: str) -> dict:
    """Store the visitor's message, capture contact details if present, return the bot reply."""
    text = (text or '').strip()[:MAX_TEXT]
    if not text:
        raise ChatError('Type a message first.')
    ChatMessage.objects.create(session=session, role='visitor', content=text)

    captured = False
    pm, em = knowledge.PHONE_RE.search(text), knowledge.EMAIL_RE.search(text)
    phone, email = (pm.group(0) if pm else ''), (em.group(0) if em else '')
    if phone or email:
        try:
            capture_lead(session, name=session.visitor_name, phone=phone or session.phone, email=email or session.email)
            captured = True
        except lead_svc.LeadError as exc:
            logger.info('chat lead not captured: %s', exc)
    slug = knowledge.package_slug_from_text(text)
    if slug and session.interest != slug:
        session.interest = slug

    try:
        answer, engine = llm.complete(_ai_messages(session))
    except llm.LLMUnavailable as exc:
        logger.info('chat falling back to rules: %s', exc)
        answer, engine = knowledge.rule_based_reply(text), 'rules'
        if captured:
            answer = ("Thank you! I've passed your details to our team and they'll contact you within 24 hours "
                      "on business days. Anything else I can help with?")
    ChatMessage.objects.create(session=session, role='bot', content=answer, engine=engine)
    session.message_count = session.messages.count()
    session.last_message_at = timezone.now()
    session.save(update_fields=['message_count', 'last_message_at', 'interest'])
    return {'reply': answer, 'captured': captured, 'engine': engine}


def _source() -> LeadSource:
    src, _ = LeadSource.objects.get_or_create(
        name=SOURCE_NAME, defaults={'channel': 'website', 'consent_basis': 'consent'})
    return src


def _transcript_note(session: ChatSession) -> str:
    url = reverse('crm:chat_detail', args=[session.pk])
    visitor_lines = [m.content for m in session.messages.filter(role='visitor').order_by('created_at')[:6]]
    said = ' | '.join(l[:120] for l in visitor_lines)
    interest = knowledge.PACKAGES.get(session.interest, {}).get('name', '') if session.interest else ''
    parts = ['Captured by the website chatbot.']
    if interest:
        parts.append(f'Interested in: {interest}.')
    if said:
        parts.append(f'Visitor said: {said}')
    parts.append(f'Full chat: {url}')
    return ' '.join(parts)[:1900]


@transaction.atomic
def capture_lead(session: ChatSession, *, name: str = '', phone: str = '', email: str = '', business: str = '') -> Lead:
    """Create (or attach to) a CRM lead from what the visitor shared. Raises LeadError if unusable."""
    phone_n, email_n = lead_svc.normalize_contact(phone, email)
    if not phone_n and not email_n:
        raise lead_svc.LeadError('Enter a valid phone number or email.')
    name = (name or session.visitor_name or '').strip()[:100]
    session.visitor_name = name or session.visitor_name
    session.phone = phone_n or session.phone
    session.email = email_n or session.email
    src = _source()
    note = _transcript_note(session)

    if session.lead_id:
        lead = session.lead
        LeadActivity.objects.create(lead=lead, kind='inbound', summary=f'More details from the website chat: {phone_n} {email_n}'.strip(),
                                    source_label='website', metadata={'chat_session': session.pk})
        session.save(update_fields=['visitor_name', 'phone', 'email'])
        if name and lead.name.startswith('Website visitor'):
            Lead.objects.filter(pk=lead.pk).update(name=name)   # they later told us who they are
            lead.name = name
        return lead

    dupes = lead_svc.find_duplicates(phone_n, email_n)
    if dupes:
        lead = lead_svc.attach_enquiry(dupes[0], f'Returning visitor on the website chat. {note}', source=src,
                                       metadata={'chat_session': session.pk})
    else:
        lead = lead_svc.create_lead(lead_svc.LeadInput(
            name=name or (f'Website visitor {phone_n[-4:]}' if phone_n else 'Website visitor'),
            phone=phone_n, email=email_n, company=business[:150], notes=note, source=src), actor=None, rule='round_robin')
        LeadActivity.objects.create(lead=lead, kind='inbound', summary=note, source_label='website',
                                    metadata={'chat_session': session.pk})
    session.lead = lead
    session.lead_captured_at = timezone.now()
    session.save(update_fields=['lead', 'lead_captured_at', 'visitor_name', 'phone', 'email'])
    _alert_managers(session, lead)
    return lead


def _alert_managers(session: ChatSession, lead: Lead) -> None:
    try:
        notify.notify(notify.users_with_perm('chats', 'view'), f'Chatbot lead: {lead.name}',
                      body=f'{session.phone or session.email} came through the website chat.',
                      url=reverse('crm:chat_detail', args=[session.pk]), event_type='chat_lead', level='info',
                      dedupe_key=f'chat-lead:{session.pk}')
        session.manager_alerted_at = timezone.now()
        session.save(update_fields=['manager_alerted_at'])
    except Exception:  # an alert must never lose the lead
        logger.exception('chat lead alert failed')
