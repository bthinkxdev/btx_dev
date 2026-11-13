"""Emails for webinar attendees: the confirmation after payment, reminders, the joining link, and a note to the team.

Built on the same designed HTML layout and delivery log as the hiring emails (crm.modules.careers.mail), so every
message is recorded in the CRM and a failed send never breaks a payment."""
from __future__ import annotations

import logging
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from crm.core import notify
from crm.modules.careers import mail as base

from .models import WebinarRegistration

logger = logging.getLogger(__name__)


def _money(amount) -> str:
    return f'Rs. {amount:,.0f}'


def _first(reg) -> str:
    parts = (reg.name or '').split()
    return parts[0].capitalize() if parts else 'there'


def _when(w) -> tuple[str, str, str]:
    """('Sunday, 15 November 2026', '7:00 PM', '8:30 PM')"""
    day, start = base._when(w.starts_at)
    _, end = base._when(w.ends_at)
    return day, start, end


def _details(reg, *, paid_row: bool = True) -> list[tuple[str, str]]:
    w = reg.webinar
    day, start, end = _when(w)
    rows = [('Webinar', w.title), ('Date', day), ('Time', f'{start} to {end} IST'),
            ('Duration', f'{w.duration_minutes} minutes'), ('Where', f'Live online on {w.platform}')]
    rows.append(('Joining link', w.join_url) if w.join_url else ('Joining link', 'We will email it to you before the session'))
    rows.append(('Registration ID', f'WEB-{reg.pk:05d}'))
    if paid_row:
        rows.append(('Amount paid', _money(reg.amount)))
    return rows


def _spec(reg, *, subject, heading, preheader, blocks) -> dict:
    company = base._company()
    name = company.display_name if company else 'BThinkX'
    return {
        'subject': subject, 'heading': heading, 'preheader': preheader, 'greeting': f'Hi {_first(reg)},',
        'blocks': blocks, 'signature': ['See you there,', f'Team {name}'], 'footer': base._footer(company),
        'brand': name, 'logo_url': base._site('/static/assets/images/pwa/icon-192.png'), 'site_url': base._site('/'),
        'footer_note': f'You received this email because you registered for a {name} webinar. '
                       'You can reply to this message to reach us.',
    }


def build_ics(w, reg=None) -> bytes:
    company = base._company()
    organizer = (company.email if company else '') or settings.DEFAULT_FROM_EMAIL
    stamp = timezone.now().astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    def utc(dt):
        return dt.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    desc = [w.subtitle, f'Live on {w.platform}.']
    if w.join_url:
        desc.append(f'Join: {w.join_url}')
    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//BThinkX//Webinar//EN', 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
        'BEGIN:VEVENT', f'UID:webinar-{w.pk}@bthinkx', 'SEQUENCE:0', f'DTSTAMP:{stamp}',
        f'DTSTART:{utc(w.starts_at)}', f'DTEND:{utc(w.ends_at)}',
        f'SUMMARY:{base._ics_escape(w.title)}', f'DESCRIPTION:{base._ics_escape(chr(10).join(desc))}',
        f'LOCATION:{base._ics_escape(w.join_url or w.platform)}',
        f'ORGANIZER;CN=BThinkX:mailto:{organizer}', 'STATUS:CONFIRMED',
    ]
    if w.join_url:
        lines.append(f'URL:{w.join_url}')
    lines += ['BEGIN:VALARM', 'TRIGGER:-PT30M', 'ACTION:DISPLAY', 'DESCRIPTION:Webinar starts in 30 minutes', 'END:VALARM',
              'END:VEVENT', 'END:VCALENDAR']
    return ('\r\n'.join(base._fold(x) for x in lines) + '\r\n').encode('utf-8')


def _send(reg, spec: dict, code: str, *, attachments=None, actor=None) -> bool:
    try:
        html, text = base._render(spec)
        notify.send_email(reg.email, spec['subject'], text, sent_by=actor, obj=reg, template_code=code, html=html,
                          attachments=attachments)
        return True
    except Exception:
        logger.exception('Webinar email %s failed for registration %s', code, reg.pk)
        return False


def _cover_list(w) -> dict:
    return {'type': 'list', 'title': 'What we will cover', 'items': [h for h, _ in w.learn_items][:6]}


def send_confirmation(reg: WebinarRegistration) -> bool:
    w = reg.webinar
    day, start, _ = _when(w)
    blocks = [
        {'type': 'p', 'text': f'Thank you for registering. Your payment of {_money(reg.amount)} is received and your seat '
                              f'for {w.title} is confirmed.'},
        {'type': 'details', 'rows': _details(reg)},
    ]
    if w.join_url:
        blocks.append({'type': 'button', 'label': f'Join on {w.platform}', 'url': w.join_url})
    else:
        blocks.append({'type': 'p', 'text': 'We will email you the joining link before the session starts, along with '
                                            'a reminder.'})
    blocks += [
        {'type': 'list', 'title': 'Before the session', 'items': [
            'Add the session to your calendar. A calendar file is attached to this email.',
            f'Join a few minutes early so you do not miss the start. It works on your phone or laptop.',
            'Think of one or two questions about your business. We take live questions at the end.']},
        _cover_list(w),
        {'type': 'p', 'text': 'If you have any trouble, just reply to this email and we will help you.'},
    ]
    spec = _spec(reg, subject=f'You are registered: {w.title}', heading='Your seat is confirmed',
                 preheader=f'{day} at {start} IST', blocks=blocks)
    ok = _send(reg, spec, 'webinar:confirmation',
               attachments=[('webinar.ics', build_ics(w, reg), 'text/calendar; method=PUBLISH; charset=UTF-8')])
    if ok:
        type(reg).objects.filter(pk=reg.pk).update(confirmation_sent_at=timezone.now())
    return ok


def send_reminder(reg: WebinarRegistration, kind: str) -> bool:
    """kind: '24h' or '1h'"""
    w = reg.webinar
    day, start, _ = _when(w)
    soon = kind == '1h'
    intro = (f'{w.title} starts in about an hour. Here are the details to join.' if soon
             else f'A quick reminder that {w.title} is tomorrow. Here are the details.')
    blocks = [{'type': 'p', 'text': intro}, {'type': 'details', 'rows': _details(reg, paid_row=False)}]
    if w.join_url:
        blocks.append({'type': 'button', 'label': f'Join on {w.platform}', 'url': w.join_url})
    else:
        blocks.append({'type': 'p', 'text': 'Your joining link will reach you by email shortly.'})
    blocks.append({'type': 'p', 'text': 'Join a few minutes early, with a steady internet connection.'})
    subject = (f'Starting in 1 hour: {w.title}' if soon else f'Tomorrow at {start} IST: {w.title}')
    spec = _spec(reg, subject=subject, heading='Starting soon' if soon else 'See you tomorrow',
                 preheader=f'{day} at {start} IST', blocks=blocks)
    return _send(reg, spec, f'webinar:reminder_{kind}')


def send_joining_link(reg: WebinarRegistration, actor=None) -> bool:
    w = reg.webinar
    day, start, _ = _when(w)
    blocks = [{'type': 'p', 'text': f'Here is your joining link for {w.title}.'},
              {'type': 'details', 'rows': _details(reg, paid_row=False)},
              {'type': 'button', 'label': f'Join on {w.platform}', 'url': w.join_url},
              {'type': 'p', 'text': 'Please join a few minutes early. If the link does not open, reply to this email.'}]
    spec = _spec(reg, subject=f'Your joining link: {w.title}', heading='Your joining link',
                 preheader=f'{day} at {start} IST', blocks=blocks)
    ok = _send(reg, spec, 'webinar:joining_link', actor=actor,
               attachments=[('webinar.ics', build_ics(w, reg), 'text/calendar; method=PUBLISH; charset=UTF-8')])
    if ok:
        type(reg).objects.filter(pk=reg.pk).update(joining_sent_at=timezone.now())
    return ok


def notify_team(reg: WebinarRegistration) -> None:
    """A plain note to the team inbox for every paid seat (the CRM also gets the lead and an in-app alert)."""
    try:
        send_mail(
            subject=f'[BThinkX] Webinar seat paid: {reg.name}',
            message=(f'New paid webinar registration.\n\nWebinar: {reg.webinar.title}\nName: {reg.name}\n'
                     f'Email: {reg.email}\nPhone: {reg.phone}\nBusiness: {reg.business_name or "(not given)"}\n'
                     f'Amount: Rs. {reg.amount}\nOrder ID: {reg.cashfree_order_id}\n'
                     f'Campaign: {reg.utm_campaign or reg.utm_source or "(direct)"}\n'
                     f'Seats paid so far: {reg.webinar.paid_count()}'),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[getattr(settings, 'CONTACT_EMAIL_TO', 'hr@bthinkx.com')],
            fail_silently=True)
    except Exception:
        logger.exception('Webinar team notification failed for %s', reg.pk)
