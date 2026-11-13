"""Webinar follow-up rules: a registrant is tracked in the Webinar section, and is moved into the main CRM (Lead, then
Client) only when someone decides they are qualified. Registrants are never added to the Leads list automatically."""
from __future__ import annotations

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import audit, notify
from crm.models import (Campaign, Lead, LeadSource, WebinarLead, WebinarLeadActivity)
from crm.models.webinars import (STAGE_CONVERTED, STAGE_NOT_INTERESTED, STAGE_QUALIFIED, STAGE_REGISTERED,
                                 WEBINAR_STAGES)

STAGE_LABELS = dict(WEBINAR_STAGES)


def detail_url(wl: WebinarLead) -> str:
    return reverse('crm:webinar_lead_detail', args=[wl.pk])


def log(wl: WebinarLead, kind: str, text: str = '', actor=None) -> WebinarLeadActivity:
    return WebinarLeadActivity.objects.create(webinar_lead=wl, kind=kind, text=text[:2000], actor=actor)


def ensure_lead(reg) -> WebinarLead:
    """The follow-up record for a registration (created on first sight, never duplicated)."""
    wl, _ = WebinarLead.objects.get_or_create(registration=reg)
    return wl


def on_registration_paid(reg) -> None:
    """A seat was paid: record it and tell the people who work webinar leads."""
    wl = ensure_lead(reg)
    if not wl.activities.filter(kind='paid').exists():
        log(wl, 'paid', f'Paid Rs. {reg.amount:,.0f} for "{reg.webinar.title}".')
    notify.notify(notify.users_with_perm('leads', 'assign'), f'Webinar seat paid: {reg.name}',
                  body=f'{reg.business_name or reg.phone} · {reg.webinar.title}', url=detail_url(wl),
                  event_type='webinar_registration', dedupe_key=f'webinar-paid:{reg.pk}')


def set_stage(wl: WebinarLead, stage: str, actor, note: str = '') -> None:
    if stage not in STAGE_LABELS or stage == STAGE_CONVERTED:
        raise ValueError('Unknown stage.')
    old = wl.stage
    if stage == old and not note:
        return
    wl.stage = stage
    if stage == STAGE_QUALIFIED and not wl.qualified_at:
        wl.qualified_at = timezone.now()
    wl.updated_by = actor
    wl.save(update_fields=['stage', 'qualified_at', 'updated_by', 'updated_at'])
    if stage != old:
        log(wl, 'stage', f'{STAGE_LABELS[old]} to {STAGE_LABELS[stage]}.' + (f' {note}' if note else ''), actor)
    elif note:
        log(wl, 'note', note, actor)
    audit.log('webinar_lead.stage', wl, actor=actor, changes={'from': old, 'to': stage})


def assign(wl: WebinarLead, user, actor) -> None:
    if wl.owner_id == (user.pk if user else None):
        return
    wl.owner = user
    wl.updated_by = actor
    wl.save(update_fields=['owner', 'updated_by', 'updated_at'])
    log(wl, 'assigned', f'Assigned to {user.get_full_name() or user.get_username()}.' if user else 'Unassigned.', actor)
    if user and (actor is None or user.pk != actor.pk):
        notify.notify([user], f'Webinar lead assigned: {wl.registration.name}', body=wl.registration.business_name,
                      url=detail_url(wl), event_type='webinar_assigned', dedupe_key=f'webinar-assign:{wl.pk}:{user.pk}')


def add_note(wl: WebinarLead, kind: str, text: str, actor, follow_up=None) -> None:
    log(wl, kind if kind in ('note', 'call', 'whatsapp', 'email') else 'note', text, actor)
    fields = ['updated_at']
    if follow_up is not None:
        wl.next_follow_up_at = follow_up
        fields.append('next_follow_up_at')
    wl.updated_by = actor
    wl.save(update_fields=fields + ['updated_by'])


def _source() -> LeadSource:
    return LeadSource.objects.filter(name='Webinar', is_active=True).first() or LeadSource.objects.create(
        name='Webinar', channel='other')


@transaction.atomic
def convert_to_lead(wl: WebinarLead, actor) -> Lead:
    """Move a qualified registrant into the Leads pipeline. An existing lead with the same phone or email is linked
    instead of creating a second one."""
    if wl.lead_id:
        return wl.lead
    from crm.modules.leads import services as leads

    reg = wl.registration
    matches = list(leads.find_duplicates(reg.phone, reg.email))
    existing = matches[0] if matches else None
    if existing is not None:
        lead = existing
        leads.attach_enquiry(lead, f'Registered for webinar "{reg.webinar.title}".', actor=actor,
                             metadata={'webinar_registration': reg.pk})
    else:
        source = _source()
        campaign, _ = Campaign.objects.get_or_create(source=source, name=f'Webinar: {reg.webinar.title}'[:150])
        notes = (f'Attended the webinar "{reg.webinar.title}" ({reg.get_status_display().lower()}).'
                 + (f' Ad: {reg.utm_campaign or reg.utm_source}.' if reg.utm_campaign or reg.utm_source else ''))
        lead = leads.create_lead(leads.LeadInput(
            name=reg.name, phone=reg.phone, email=reg.email, company=reg.business_name, notes=notes, source=source,
            campaign=campaign, owner=wl.owner), actor, rule='webinar')
    wl.lead = lead
    wl.stage = STAGE_CONVERTED
    wl.converted_at = timezone.now()
    wl.updated_by = actor
    wl.save(update_fields=['lead', 'stage', 'converted_at', 'updated_by', 'updated_at'])
    log(wl, 'converted', f'Moved to the Leads pipeline as {lead.name}.', actor)
    audit.log('webinar_lead.to_lead', wl, actor=actor, changes={'lead': lead.pk})
    return lead


@transaction.atomic
def convert_to_client(wl: WebinarLead, actor):
    """Make the registrant a client (through a lead, so the history is kept). Onboarding projects, contracts and
    invoices then start from the client page."""
    from crm.modules.clients import services as clients

    lead = convert_to_lead(wl, actor)
    client = clients.ensure_client_for_lead(lead, actor)
    wl.client = client
    wl.stage = STAGE_CONVERTED
    wl.updated_by = actor
    wl.save(update_fields=['client', 'stage', 'updated_by', 'updated_at'])
    log(wl, 'converted', f'Created client {client}.', actor)
    audit.log('webinar_lead.to_client', wl, actor=actor, changes={'client': client.pk})
    return client


def mark_not_interested(wl: WebinarLead, actor, note: str = '') -> None:
    set_stage(wl, STAGE_NOT_INTERESTED, actor, note)


__all__ = ['ensure_lead', 'on_registration_paid', 'set_stage', 'assign', 'add_note', 'convert_to_lead',
           'convert_to_client', 'STAGE_REGISTERED']
