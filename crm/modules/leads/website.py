"""
Website -> CRM bridge. The public site (the `pages` app) calls these from its form views so every
enquiry becomes a CRM lead and the right people are told about it.

Store first, process second (same rule as the signed inbound API): the payload is saved to
LeadInbox, then ingested. Nothing here may raise into the public view; a failure leaves the
payload in the inbox for the retry job and the review queue.
"""
from __future__ import annotations

import json
import logging

from django.urls import reverse

from crm.core import notify
from crm.models import LeadInbox, LeadSource

from . import services as svc

logger = logging.getLogger('crm.website')


def _source() -> LeadSource | None:
    qs = LeadSource.objects.filter(channel='website', is_active=True).order_by('id')
    return qs.first() or LeadSource.objects.create(name='Website form', channel='website')


def capture(external_id: str, payload: dict) -> LeadInbox | None:
    """Create (or reuse) the inbox row for one website submission and turn it into a lead."""
    try:
        source = _source()
        item, created = LeadInbox.objects.get_or_create(
            source=source, external_id=external_id[:120],
            defaults={'payload': payload, 'raw_body': json.dumps(payload, default=str)[:200000],
                      'content_type': 'application/x-website-form', 'signature_valid': True})
        if not created and item.status in ('processed', 'duplicate', 'resolved', 'ignored'):
            return item
        item = svc.ingest(item)
        _alert(item, payload)
        return item
    except Exception:
        logger.exception('Website lead capture failed for %s', external_id)
        return None


def _alert(item: LeadInbox, payload: dict) -> None:
    """Tell the lead's owner (assign() already does) and everyone who triages leads."""
    if item.status not in ('processed', 'duplicate') or item.lead is None:
        return  # failures go to the review queue, which raises its own alert
    lead = item.lead
    returning = item.status == 'duplicate'
    title = f'{"Returning" if returning else "New"} website enquiry: {lead.name}'
    body = (payload.get('message') or payload.get('kind_label') or '')[:200]
    recipients = {u.pk: u for u in notify.users_with_perm('leads', 'assign')}
    if lead.owner_id:
        recipients[lead.owner_id] = lead.owner
    notify.notify(recipients.values(), title, body=body, url=reverse('crm:lead_detail', args=[lead.pk]),
                  event_type='website_lead', level='info', dedupe_key=f'website-lead:{item.pk}')


def contact_form(submission) -> None:
    msg = [submission.message]
    extras = [f'Project: {submission.project_type}'] + [f'{k}: {v}' for k, v in (
        ('Budget', submission.budget), ('Timeline', submission.timeline)) if v]
    capture(f'contact-{submission.pk}', {
        'name': submission.name, 'email': submission.email, 'company': submission.company,
        'message': ' | '.join(extras) + '\n' + '\n'.join(msg), 'campaign': 'Website contact form',
        'kind_label': 'Contact form'})


def quote_request(lead) -> None:
    capture(f'quote-{lead.pk}', {
        'name': lead.name, 'phone': lead.phone, 'company': lead.business_name,
        'message': f'Downloaded the {lead.get_package_display()} quotation. Business type: {lead.get_business_type_display()}.',
        'campaign': 'Website pricing quotation', 'kind_label': 'Quotation download'})


def offer_signup(signup, *, paid: bool = False) -> None:
    state = 'PAID' if paid else 'payment started'
    capture(f'offer-{signup.pk}-{"paid" if paid else "started"}', {
        'name': signup.name, 'phone': signup.phone, 'company': signup.business_name, 'city': signup.place,
        'message': f'{signup.offer_slug}: {signup.get_plan_display()} (Rs. {signup.amount}) - {state}.',
        'campaign': f'Offer {signup.offer_slug}', 'kind_label': 'Offer signup'})


def career_application(app) -> None:
    """Job applications are not sales leads: alert HR in-app instead."""
    try:
        role = app.job.title if app.job else 'General application'
        notify.notify(notify.users_with_perm('employees', 'edit'), f'New job application: {app.full_name}',
                      body=role, url=reverse('crm:career_applications'), event_type='career_application',
                      dedupe_key=f'career-app:{app.pk}')
    except Exception:
        logger.exception('Career application alert failed for %s', app.pk)
