"""Candidate emails for the hiring flow: the automatic thank-you, shortlisting, interview invitations (with a
calendar file), reschedules, cancellations, reminders and the polite "not this time" message.

Every email is described once as structured content (greeting, paragraphs, detail rows, a button, a list) and
rendered twice from it: a designed HTML version and a plain-text version. All go through crm.core.notify.send_email,
so each one is recorded in the delivery log and shown on the application page. Nothing here raises into the caller:
a failed send is logged and the HR action still succeeds."""
from __future__ import annotations

import logging
from datetime import timezone as dt_timezone

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from crm.core import notify
from crm.models import CompanySettings

logger = logging.getLogger('crm.careers')


# ---------------------------------------------------------------- content helpers

def _company():
    try:
        return CompanySettings.get()
    except Exception:  # pragma: no cover - before migrate
        return None


def _site(path: str = '') -> str:
    return settings.SITE_BASE_URL + path


def _first_name(app) -> str:
    parts = (app.full_name or '').split()
    return parts[0].capitalize() if parts else 'there'


def _role(app) -> str:
    return app.job.title if app.job else ''


def _when(dt) -> tuple[str, str]:
    """('Monday, 10 November 2026', '11:00 AM')"""
    local = timezone.localtime(dt)
    return local.strftime('%A, %d %B %Y'), local.strftime('%I:%M %p').lstrip('0')


def _signature(company) -> list[str]:
    name = company.display_name if company else 'BThinkX'
    lines = ['Warm regards,', f'The Hiring Team, {name}']
    return lines


def _footer(company) -> list[str]:
    if not company:
        return ['BThinkX, Trivandrum, Kerala']
    addr = ', '.join(x for x in (company.address_line1, company.address_line2) if x)
    parts = [f'{company.display_name}, {addr}'.strip(', ')]
    contact = ' | '.join(x for x in (company.email, company.phone) if x)
    if contact:
        parts.append(contact)
    return parts


def _spec(app, *, subject, heading, preheader, blocks, greeting=None) -> dict:
    company = _company()
    return {
        'subject': subject, 'heading': heading, 'preheader': preheader,
        'greeting': greeting or f'Hi {_first_name(app)},',
        'blocks': blocks, 'signature': _signature(company), 'footer': _footer(company),
        'brand': company.display_name if company else 'BThinkX',
        'logo_url': _site('/static/assets/images/pwa/icon-192.png'),
        'site_url': _site('/'),
    }


def _render(spec: dict) -> tuple[str, str]:
    html = render_to_string('crm/careers/email/layout.html', spec)
    lines = [spec['greeting'], '']
    for b in spec['blocks']:
        kind = b['type']
        if kind == 'p':
            lines += [b['text'], '']
        elif kind == 'quote':
            lines += [f'"{b["text"]}"', '']
        elif kind == 'details':
            lines += [f'{label}: {value}' for label, value in b['rows']] + ['']
        elif kind == 'list':
            if b.get('title'):
                lines.append(b['title'])
            lines += [f'{i}. {t}' if b.get('numbered') else f'- {t}' for i, t in enumerate(b['items'], 1)] + ['']
        elif kind == 'button':
            lines += [f'{b["label"]}: {b["url"]}', '']
    lines += spec['signature'] + [''] + ['--'] + spec['footer']
    return html, '\n'.join(lines)


def _send(app, spec: dict, code: str, *, actor=None, attachments=None) -> bool:
    """Queue one email to the candidate. Returns True when it was queued."""
    try:
        html, text = _render(spec)
        notify.send_email(app.email, spec['subject'], text, sent_by=actor, obj=app, template_code=code,
                          html=html, attachments=attachments)
        return True
    except Exception:
        logger.exception('Candidate email %s failed for application %s', code, app.pk)
        return False


# ---------------------------------------------------------------- calendar file

def _ics_escape(text: str) -> str:
    return (text or '').replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\r\n', '\n').replace('\n', '\\n')


def _fold(line: str) -> str:
    """RFC 5545: lines are at most 75 octets; continuation lines start with a space."""
    raw = line.encode('utf-8')
    if len(raw) <= 75:
        return line
    out, chunk = [], b''
    for ch in line:
        enc = ch.encode('utf-8')
        if len(chunk) + len(enc) > (75 if not out else 74):
            out.append(chunk.decode('utf-8'))
            chunk = b''
        chunk += enc
    out.append(chunk.decode('utf-8'))
    return '\r\n '.join(out)


def build_ics(interview, *, cancel: bool = False) -> bytes:
    app = interview.application
    company = _company()
    organizer = (company.email if company else '') or settings.DEFAULT_FROM_EMAIL
    stamp = timezone.now().astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    def utc(dt):
        return dt.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    role = _role(app) or 'General application'
    where = interview.meeting_link or interview.location or interview.get_mode_display()
    desc = [f'{interview.round_name} for {role}.', f'Format: {interview.get_mode_display()}.']
    if interview.meeting_link:
        desc.append(f'Join: {interview.meeting_link}')
    if interview.location:
        desc.append(interview.location)
    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//BThinkX//Careers//EN', 'CALSCALE:GREGORIAN',
        f'METHOD:{"CANCEL" if cancel else "REQUEST"}',
        'BEGIN:VEVENT',
        f'UID:{interview.uid}@bthinkx',
        f'SEQUENCE:{interview.sequence}',
        f'DTSTAMP:{stamp}',
        f'DTSTART:{utc(interview.scheduled_at)}',
        f'DTEND:{utc(interview.ends_at)}',
        f'SUMMARY:{_ics_escape(f"BThinkX interview: {role}" if role else "BThinkX interview")}',
        f'DESCRIPTION:{_ics_escape(chr(10).join(desc))}',
        f'LOCATION:{_ics_escape(where)}',
        f'ORGANIZER;CN=BThinkX Hiring:mailto:{organizer}',
        f'ATTENDEE;CN={_ics_escape(app.full_name)};RSVP=FALSE:mailto:{app.email}',
        f'STATUS:{"CANCELLED" if cancel else "CONFIRMED"}',
    ]
    if interview.meeting_link:
        lines.append(f'URL:{interview.meeting_link}')
    if not cancel:
        lines += ['BEGIN:VALARM', 'TRIGGER:-PT30M', 'ACTION:DISPLAY', 'DESCRIPTION:Interview starts in 30 minutes', 'END:VALARM']
    lines += ['END:VEVENT', 'END:VCALENDAR']
    return ('\r\n'.join(_fold(x) for x in lines) + '\r\n').encode('utf-8')


# ---------------------------------------------------------------- the emails

def send_received(app) -> bool:
    """Automatic thank-you the moment an application arrives from the website."""
    role = _role(app)
    submitted = timezone.localtime(app.created_at)
    about = f'the {role} position' if role else 'a role with us'
    rows = [('Applied for', role or 'General application'),
            ('Submitted', submitted.strftime('%d %B %Y, %I:%M %p').replace(' 0', ' ')),
            ('Reference', f'APP-{app.pk:05d}')]
    blocks = [
        {'type': 'p', 'text': f'Thank you for applying for {about} at BThinkX. We have received your application and '
                              'your resume, and they are now with our hiring team.'},
        {'type': 'details', 'rows': rows},
        {'type': 'list', 'title': 'What happens next', 'numbered': True, 'items': [
            'Our hiring team reads every application. This usually takes three to five working days.',
            'If your profile matches what we are looking for, we will email or call you to shortlist you and to fix a '
            'time for an interview.',
            'Interviews are usually held online on Google Meet. We will send you the date, time and joining link in advance.',
            'If this role is not the right fit right now, we keep your resume on file and will reach out when a '
            'suitable opening comes up.']},
        {'type': 'p', 'text': 'There is nothing else you need to do. If you would like to add something to your '
                              'application, such as a portfolio or a project link, just reply to this email and it '
                              'will reach our HR team directly.'},
        {'type': 'p', 'text': 'While you wait, you are welcome to look at the kind of work we do for growing businesses.'},
        {'type': 'button', 'label': 'See our work', 'url': _site('/portfolio/')},
    ]
    spec = _spec(app, subject=f'Thank you for applying to BThinkX{f": {role}" if role else ""}',
                 heading='We have received your application',
                 preheader='Thank you for applying. Here is what happens next.', blocks=blocks)
    ok = _send(app, spec, 'career:received')
    if ok:
        type(app).objects.filter(pk=app.pk).update(acknowledged_at=timezone.now())
    return ok


def send_shortlisted(app, note: str = '', actor=None) -> bool:
    role = _role(app)
    about = f'the {role} position' if role else 'a role with us'
    blocks = [
        {'type': 'p', 'text': f'Good news. After going through your application for {about}, our hiring team would '
                              'like to take it forward. You have been shortlisted.'},
    ]
    if note.strip():
        blocks.append({'type': 'quote', 'text': note.strip()})
    blocks += [
        {'type': 'list', 'title': 'What happens next', 'numbered': True, 'items': [
            'We will contact you shortly to schedule your interview. The date, time and joining link will come to you by email.',
            f'Please keep your phone ({app.phone}) nearby. A member of our team may call you to confirm the slot.',
            'If your contact details, availability or notice period have changed, simply reply to this email.']},
        {'type': 'list', 'title': 'A few tips to prepare', 'items': [
            'Re-read the role description and be ready to talk about the work you have done that relates to it.',
            'Keep two or three projects ready to walk us through: what the problem was, what you did and what changed.',
            'For online interviews, choose a quiet place with a steady internet connection.']},
        {'type': 'p', 'text': 'Congratulations on reaching this stage. We look forward to speaking with you.'},
    ]
    spec = _spec(app, subject=f'You have been shortlisted{f" for {role}" if role else ""} at BThinkX',
                 heading='You have been shortlisted', preheader='Our hiring team would like to take your application forward.',
                 blocks=blocks)
    return _send(app, spec, 'career:shortlisted', actor=actor)


def send_rejected(app, note: str = '', actor=None) -> bool:
    role = _role(app)
    about = f'the {role} position' if role else 'a role with us'
    blocks = [
        {'type': 'p', 'text': f'Thank you for the time you put into applying for {about} at BThinkX, and for sharing your '
                              'work with us.'},
        {'type': 'p', 'text': 'After careful consideration, we have decided not to take your application forward for this '
                              'role. This was not an easy decision, as we received many strong applications.'},
    ]
    if note.strip():
        blocks.append({'type': 'quote', 'text': note.strip()})
    blocks += [
        {'type': 'p', 'text': 'This is not a judgement of your potential. We will keep your resume on file, and if an '
                              'opening comes up that suits your profile, we will get in touch. You are always welcome '
                              'to apply again.'},
        {'type': 'p', 'text': 'We wish you every success in your search and in your career.'},
    ]
    spec = _spec(app, subject='An update on your application to BThinkX', heading='An update on your application',
                 preheader='Thank you for applying. Here is our decision.', blocks=blocks)
    return _send(app, spec, 'career:rejected', actor=actor)


def _interview_rows(iv) -> list[tuple[str, str]]:
    day, start = _when(iv.scheduled_at)
    _, end = _when(iv.ends_at)
    rows = [('Round', iv.round_name), ('Date', day), ('Time', f'{start} to {end} IST'),
            ('Duration', f'{iv.duration_minutes} minutes'), ('Format', iv.get_mode_display())]
    if iv.meeting_link:
        rows.append(('Meeting link', iv.meeting_link))
    if iv.location:
        rows.append(('Phone / address' if iv.mode in ('phone', 'in_person') else 'Details', iv.location))
    names = [u.get_full_name() or u.get_username() for u in iv.interviewers.all()]
    if names:
        rows.append(('Interviewers', ', '.join(names)))
    return rows


def _interview_blocks(iv, *, intro: str) -> list[dict]:
    blocks = [{'type': 'p', 'text': intro}, {'type': 'details', 'rows': _interview_rows(iv)}]
    if iv.is_online and iv.meeting_link:
        blocks.append({'type': 'button', 'label': f'Join on {iv.get_mode_display()}', 'url': iv.meeting_link})
    if iv.message.strip():
        blocks.append({'type': 'quote', 'text': iv.message.strip()})
    tips = ['Please join five minutes early so we can start on time.',
            'Keep your resume and one or two projects ready to talk through.']
    if iv.is_online:
        tips.append('Check your camera, microphone and internet connection beforehand, and pick a quiet spot.')
    elif iv.mode == 'in_person':
        tips.append('Please carry a photo ID. Our reception will guide you to the interview room.')
    elif iv.mode == 'phone':
        tips.append('Keep your phone charged and in a place with good network coverage.')
    tips.append('If you cannot make this time, reply to this email at least 24 hours before and we will find another slot.')
    blocks.append({'type': 'list', 'title': 'Before the interview', 'items': tips})
    blocks.append({'type': 'p', 'text': 'A calendar file is attached. Open it to add the interview to your calendar '
                                         'and get a reminder.'})
    return blocks


def send_interview_invite(iv, *, updated: bool = False, actor=None) -> bool:
    app = iv.application
    role = _role(app)
    about = f'the {role} position' if role else 'a role with us'
    day, start = _when(iv.scheduled_at)
    if updated:
        intro = (f'The details of your {iv.round_name.lower()} for {about} have changed. Please use the details '
                 'below, they replace the earlier ones.')
        subject = f'Updated: interview for {role or "BThinkX"} on {day}, {start}'
        heading = 'Your interview details have changed'
    else:
        intro = (f'Thank you for your interest in BThinkX. We would like to invite you to a {iv.round_name.lower()} '
                 f'for {about}. Here are the details.')
        subject = f'Interview invitation: {role or "BThinkX"} on {day}, {start}'
        heading = 'Your interview is scheduled'
    spec = _spec(app, subject=subject, heading=heading, preheader=f'{day} at {start} IST',
                 blocks=_interview_blocks(iv, intro=intro))
    ics = build_ics(iv)
    ok = _send(app, spec, 'career:interview_update' if updated else 'career:interview_invite', actor=actor,
               attachments=[('interview.ics', ics, 'text/calendar; method=REQUEST; charset=UTF-8')])
    if ok:
        type(iv).objects.filter(pk=iv.pk).update(candidate_notified_at=timezone.now())
    return ok


def send_interview_cancelled(iv, reason: str = '', actor=None) -> bool:
    app = iv.application
    role = _role(app)
    day, start = _when(iv.scheduled_at)
    blocks = [{'type': 'p', 'text': f'We are sorry, but we have to cancel your {iv.round_name.lower()}'
                                     f'{f" for the {role} position" if role else ""} that was planned for {day} at {start} IST.'}]
    if reason.strip():
        blocks.append({'type': 'quote', 'text': reason.strip()})
    blocks += [
        {'type': 'p', 'text': 'We will get back to you soon with a new time. If you have any questions in the meantime, '
                              'just reply to this email.'},
        {'type': 'p', 'text': 'Thank you for your patience and understanding.'},
    ]
    spec = _spec(app, subject=f'Your interview on {day} has been cancelled', heading='Your interview has been cancelled',
                 preheader='We will share a new time soon.', blocks=blocks)
    return _send(app, spec, 'career:interview_cancel', actor=actor,
                 attachments=[('interview-cancelled.ics', build_ics(iv, cancel=True),
                               'text/calendar; method=CANCEL; charset=UTF-8')])


def send_interview_reminder(iv) -> bool:
    app = iv.application
    role = _role(app)
    day, start = _when(iv.scheduled_at)
    intro = (f'This is a friendly reminder about your {iv.round_name.lower()}'
             f'{f" for the {role} position" if role else ""} at BThinkX. Here are the details again.')
    blocks = _interview_blocks(iv, intro=intro)[:-1]       # no calendar-file sentence: it was sent with the invitation
    spec = _spec(app, subject=f'Reminder: your BThinkX interview on {day} at {start}', heading='Your interview is coming up',
                 preheader=f'{day} at {start} IST', blocks=blocks)
    return _send(app, spec, 'career:interview_reminder')
