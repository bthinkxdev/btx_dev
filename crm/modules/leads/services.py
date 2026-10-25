"""
Lead engine service layer (Part 4). Status change functions here are the only way
to change a lead's stage (Part 14.5). Views call these; they never set fields directly.

The seven guarantees (Part 4.1):
 1 arrival is never silent      -> ingest() stores raw first, retries, review queue, silent-source monitor (jobs.py)
 2 no duplicates split history  -> find_duplicates() at ingest attaches as an interaction
 3 assignment within SLA        -> auto_assign() + unassigned alerts (jobs.py)
 4 always an owner              -> DB check constraint + assign() + offboarding reassignment
 5 always a dated next action   -> DB check constraint + follow-up rules here
 6 escalation                   -> jobs.py timers
 7 nothing is deleted           -> archive only; merge archives with a pointer
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, india, notify, permissions
from crm.core.money import D
from crm.models import (CompanySettings, DuplicateCandidate, FollowUp, Lead, LeadActivity, LeadAssignment, LeadInbox,
                        LeadSource, StageHistory, UserProfile)
from crm.models.leads import (CLOSED_STAGES, DEFAULT_PROBABILITY, OPEN_STAGES, STAGE_CONTACTED, STAGE_LOST,
                              STAGE_NEGOTIATION, STAGE_NEW, STAGE_NURTURING, STAGE_QUALIFIED, STAGE_QUOTE_PREPARED,
                              STAGE_QUOTE_SENT, STAGE_WON, STAGES)

logger = logging.getLogger('crm.leads')
STAGE_LABELS = dict(STAGES)
MAIN_PATH = [STAGE_NEW, STAGE_CONTACTED, STAGE_QUALIFIED, STAGE_QUOTE_PREPARED, STAGE_QUOTE_SENT, STAGE_NEGOTIATION]
QUALIFIED_PLUS = [STAGE_QUALIFIED, STAGE_QUOTE_PREPARED, STAGE_QUOTE_SENT, STAGE_NEGOTIATION]
OPEN_QUOTATION_STATUSES = ['draft', 'awaiting_approval', 'approved', 'sent', 'viewed', 'accepted']


class LeadError(ValidationError):
    pass


def lead_url(lead) -> str:
    return reverse('crm:lead_detail', args=[lead.pk])


def _lock(lead) -> Lead:
    return Lead.objects.select_for_update().get(pk=lead.pk)


def _activity(lead, kind, summary='', actor=None, **kw):
    return LeadActivity.objects.create(lead=lead, kind=kind, summary=summary, actor=actor, **kw)


def _touch(lead, when=None):
    lead.last_activity_at = when or timezone.now()
    lead.stale_alert_level = 0


def _save(lead, actor, fields):
    lead.updated_by = actor
    lead.row_version = (lead.row_version or 0) + 1
    lead.save(update_fields=list(set(fields) | {'updated_by', 'row_version', 'updated_at'}))


# ---------------------------------------------------------------- dedupe (guarantee 2)

def normalize_contact(phone: str = '', email: str = '') -> tuple[str, str]:
    p = ''
    if phone:
        try:
            p = india.normalize_phone(phone)
        except ValueError:
            p = ''
    return p, (email or '').strip().lower()


def find_duplicates(phone: str = '', email: str = '', exclude_id: int | None = None):
    phone, email = normalize_contact(phone, email)
    if not phone and not email:
        return Lead.objects.none()
    q = Q()
    if phone:
        q |= Q(phone=phone) | Q(alt_phone=phone)
    if email:
        q |= Q(email__iexact=email)
    qs = Lead.objects.filter(q, archived_at__isnull=True, merged_into__isnull=True)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    # Open leads first, then most recent.
    return sorted(qs.select_related('owner')[:20], key=lambda l: (l.stage not in OPEN_STAGES, -l.pk))


def matching_clients(phone: str = '', email: str = '', gstin: str = '', name: str = ''):
    from crm.models import Client
    phone, email = normalize_contact(phone, email)
    q = Q()
    if phone:
        q |= Q(phone=phone) | Q(contacts__phone=phone)
    if email:
        q |= Q(email__iexact=email) | Q(contacts__email__iexact=email)
    if gstin:
        q |= Q(gstin=gstin.upper())
    if not q:
        return Client.objects.none()
    return Client.objects.filter(q, archived_at__isnull=True).distinct()


def record_duplicate_pair(a: Lead, b: Lead, reason: str, score: int = 80):
    first, second = (a, b) if a.pk < b.pk else (b, a)
    DuplicateCandidate.objects.get_or_create(lead_a=first, lead_b=second,
                                             defaults={'reason': reason[:120], 'score': score})


# ---------------------------------------------------------------- assignment (guarantee 3/4)

def open_lead_count(user) -> int:
    return Lead.objects.filter(owner=user, stage__in=OPEN_STAGES, archived_at__isnull=True).count()


def available_assignees(exclude_ids=()):
    """Active, available sales users under their open-lead cap (Part 4.9)."""
    User = get_user_model()
    today = india.today()
    company = CompanySettings.get()
    profiles = (UserProfile.objects.select_related('user', 'team')
                .filter(receives_leads=True, user__is_active=True, is_available=True)
                .filter(Q(unavailable_until__isnull=True) | Q(unavailable_until__lt=today))
                .exclude(user_id__in=exclude_ids))
    counts = dict(Lead.objects.filter(owner_id__in=[p.user_id for p in profiles], stage__in=OPEN_STAGES,
                                      archived_at__isnull=True).values_list('owner_id').annotate(n=Count('id')))
    out = []
    for p in profiles:
        cap = p.open_lead_cap or company.default_open_lead_cap
        if p.team_id and not p.team.is_sales:
            continue
        if counts.get(p.user_id, 0) >= cap:
            continue
        out.append(p)
    return out


def pick_round_robin(exclude_ids=()):
    """Round-robin weighted by capacity: fewest recent assignments per unit of weight, then oldest turn."""
    candidates = available_assignees(exclude_ids)
    if not candidates:
        return None
    since = timezone.now() - timedelta(hours=24)
    recent = dict(LeadAssignment.objects.filter(created_at__gte=since, to_user_id__in=[p.user_id for p in candidates])
                  .values_list('to_user_id').annotate(n=Count('id')))
    epoch = timezone.make_aware(datetime(2000, 1, 1))

    def score(p):
        return (Decimal(recent.get(p.user_id, 0)) / Decimal(max(p.assignment_weight, 1)),
                p.last_assigned_at or epoch, p.user_id)
    chosen = min(candidates, key=score)
    UserProfile.objects.filter(pk=chosen.pk).update(last_assigned_at=timezone.now())
    return chosen.user


def assign(lead: Lead, to_user, actor=None, *, rule: str = 'manual', reason: str = '', transfer_follow_ups: bool = True,
           override_availability: bool = False, notify_users: bool = True, undo_hours: int = 0) -> Lead:
    if to_user is None:
        raise LeadError('Choose who should own this lead.')
    if not to_user.is_active:
        raise LeadError(f'{to_user.get_full_name() or to_user} is not an active user.')
    profile = getattr(to_user, 'crm_profile', None)
    if profile and not override_availability:
        today = india.today()
        if not profile.is_available or (profile.unavailable_until and profile.unavailable_until >= today):
            raise LeadError(f'{to_user.get_full_name() or to_user} is on leave or unavailable. Tick override to assign anyway.')
    with transaction.atomic():
        lead = _lock(lead)
        if lead.archived_at:
            raise LeadError('This lead is archived.')
        previous = lead.owner
        if previous and previous.pk == to_user.pk:
            return lead
        lead.owner = to_user
        lead.team = profile.team if profile else None
        lead.assigned_at = timezone.now()
        lead.unassigned_alert_level = 0
        if lead.next_follow_up_at is None and lead.is_open:
            lead.next_follow_up_at = sla_due(timezone.now())
        _save(lead, actor, ['owner', 'team', 'assigned_at', 'unassigned_alert_level', 'next_follow_up_at'])
        LeadAssignment.objects.create(lead=lead, from_user=previous, to_user=to_user, rule=rule, reason=reason[:300],
                                      actor=actor, undo_until=(timezone.now() + timedelta(hours=undo_hours)) if undo_hours else None)
        if transfer_follow_ups:
            FollowUp.objects.filter(lead=lead, status='pending').update(assignee=to_user)
        if not FollowUp.objects.filter(lead=lead, status='pending').exists() and lead.is_open:
            FollowUp.objects.create(lead=lead, kind='call', due_at=lead.next_follow_up_at, assignee=to_user,
                                    purpose='First contact' if lead.stage == STAGE_NEW else 'Follow up', created_by=actor)
        who = actor.get_full_name() if actor else 'System'
        _activity(lead, 'assignment', f'Assigned to {to_user.get_full_name() or to_user}'
                  + (f' from {previous.get_full_name() or previous}' if previous else '') + f' ({rule.replace("_", " ")})'
                  + (f'. Reason: {reason}' if reason else ''), actor=actor)
        audit.log('lead.reassigned' if previous else 'lead.assigned', lead, actor=actor,
                  changes={'owner': [str(previous) if previous else None, str(to_user)], 'rule': rule}, reason=reason)
    if notify_users:
        url = lead_url(lead)
        notify.notify([to_user], f'New lead assigned: {lead}', body=f'{who} assigned it to you. Contact within the SLA.',
                      url=url, event_type='lead_assigned', level='info', email=True)
        if previous and (actor is None or previous.pk != actor.pk):
            notify.notify([previous], f'Lead reassigned: {lead}', body=f'Now owned by {to_user.get_full_name() or to_user}.',
                          url=url, event_type='lead_reassigned')
    return lead


def auto_assign(lead: Lead, actor=None, exclude_ids=()) -> Lead:
    if lead.source.default_owner_id and lead.source.default_owner.is_active and lead.source.default_owner_id not in exclude_ids:
        return assign(lead, lead.source.default_owner, actor, rule='source_default', notify_users=True)
    user = pick_round_robin(exclude_ids)
    if user is None:
        return lead   # stays Unassigned; jobs alert manager at 5 min and owner at 30
    return assign(lead, user, actor, rule='round_robin', override_availability=True)


def sla_due(start: datetime) -> datetime:
    """First-response SLA (Part 4.9): 15 min during working hours, else next working day 10:30."""
    company = CompanySettings.get()
    local = timezone.localtime(start)
    within = local.weekday() != 6 and company.work_start_hour <= local.hour < company.work_end_hour
    if within:
        return start + timedelta(minutes=company.first_response_minutes)
    if local.hour < company.work_start_hour and local.weekday() != 6:
        return timezone.make_aware(datetime.combine(local.date(), time(10, 30)))
    return india.next_working_day_at(10, 30, start)


# ---------------------------------------------------------------- creation

@dataclass
class LeadInput:
    name: str
    phone: str = ''
    email: str = ''
    company: str = ''
    website: str = ''
    city: str = ''
    state: str = ''
    notes: str = ''
    source: LeadSource | None = None
    campaign: object = None
    owner: object = None
    priority: str = 'warm'
    next_follow_up_at: datetime | None = None
    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    expected_close_date: object = None
    referrer: str = ''
    value: Decimal | None = None


def create_lead(data: LeadInput, actor=None, *, services=(), tags=(), rule: str = 'creator',
                duplicate_reason: str = '', import_batch=None, inbox=None) -> Lead:
    phone, email = normalize_contact(data.phone, data.email)
    if not data.name or len(data.name.strip()) < 2:
        raise LeadError('Enter a name of at least 2 characters.')
    if not phone and not email:
        raise LeadError('Enter a phone number or an email address.')
    if data.source is None:
        raise LeadError('Choose a source.')
    company = CompanySettings.get()
    now = timezone.now()
    with transaction.atomic():
        lead = Lead(
            name=data.name.strip()[:100], phone=phone, email=email, company=(data.company or '').strip()[:150],
            website=data.website or '', city=(data.city or '')[:80], state=data.state or '', notes=(data.notes or '')[:2000],
            source=data.source, campaign=data.campaign, priority=data.priority or 'warm', stage=STAGE_NEW,
            budget_min=data.budget_min, budget_max=data.budget_max, expected_close_date=data.expected_close_date,
            referrer=data.referrer or '', value=D(data.value or data.budget_max or 0),
            probability=DEFAULT_PROBABILITY[STAGE_NEW], created_by=actor, updated_by=actor,
            next_follow_up_at=data.next_follow_up_at or sla_due(now), last_activity_at=now,
            duplicate_override_reason=duplicate_reason[:300], flagged_for_merge=bool(duplicate_reason),
            import_batch=import_batch, consent_basis=data.source.consent_basis,
        )
        lead.save()
        if services:
            lead.services.set(services)
        if tags:
            lead.tags.set(tags)
        LeadSource.objects.filter(pk=data.source.pk).update(last_lead_at=now, is_silent=False, silent_alerted_at=None)
        if inbox is not None:
            _activity(lead, 'inbound', f'Enquiry received via {data.source}.', metadata={'inbox_id': inbox.pk},
                      source_label=data.source.channel)
        if duplicate_reason:
            for other in find_duplicates(phone, email, exclude_id=lead.pk):
                record_duplicate_pair(lead, other, 'Created anyway: ' + duplicate_reason[:80], 90)
        audit.log('lead.created', lead, actor=actor,
                  changes={'source': str(data.source), 'rule': rule, 'creator': str(actor) if actor else 'system'})
        if data.owner is not None:
            assign(lead, data.owner, actor, rule=rule, override_availability=True,
                   notify_users=bool(actor is None or data.owner.pk != actor.pk))
            lead.refresh_from_db()
        else:
            auto_assign(lead, actor)
            lead.refresh_from_db()
    return lead


def attach_enquiry(existing: Lead, summary: str, *, source=None, actor=None, metadata=None) -> Lead:
    """Second enquiry with the same phone/email becomes an interaction (Part 4.1 guarantee 2)."""
    with transaction.atomic():
        lead = _lock(existing)
        _activity(lead, 'inbound', summary[:2000], actor=actor, metadata=metadata or {},
                  source_label=source.channel if source else 'web')
        _touch(lead)
        _save(lead, actor, ['last_activity_at', 'stale_alert_level'])
        if source:
            LeadSource.objects.filter(pk=source.pk).update(last_lead_at=timezone.now(), is_silent=False, silent_alerted_at=None)
        audit.log('lead.enquiry_attached', lead, actor=actor, changes={'source': str(source) if source else ''})
    if lead.owner:
        notify.notify([lead.owner], f'Returning enquiry: {lead}', body=summary[:200], url=lead_url(lead),
                      event_type='lead_returning', level='warning')
    return lead


# ---------------------------------------------------------------- inbox ingest (guarantee 1)

FIELD_ALIASES = {
    'name': ['name', 'full_name', 'fullname', 'your_name', 'contact_name', 'first_name'],
    'phone': ['phone', 'phone_number', 'mobile', 'mobile_number', 'whatsapp', 'tel', 'contact_number'],
    'email': ['email', 'email_address', 'mail'],
    'company': ['company', 'company_name', 'business', 'business_name', 'organization'],
    'city': ['city', 'location', 'place'],
    'message': ['message', 'comments', 'requirement', 'notes', 'enquiry', 'query'],
    'service': ['service', 'services', 'interest', 'product'],
    'campaign': ['campaign', 'utm_campaign', 'campaign_name'],
    'external_id': ['id', 'lead_id', 'leadgen_id', 'submission_id', 'external_id'],
    'website': ['website', 'url'],
}
RETRY_MINUTES = [1, 5, 30]


def _flatten(payload) -> dict:
    out = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, (dict, list)):
                if k == 'field_data' and isinstance(v, list):   # Meta lead form shape
                    for item in v:
                        if isinstance(item, dict) and item.get('name'):
                            vals = item.get('values') or ['']
                            out[str(item['name']).lower()] = str(vals[0])
                continue
            out[str(k).lower().strip()] = '' if v is None else str(v).strip()
    return out


def parse_payload(source: LeadSource, payload) -> dict:
    flat = _flatten(payload)
    mapping = {str(k).lower(): v for k, v in (source.field_mapping or {}).items()}
    parsed = {}
    for key, value in flat.items():
        if key in mapping:
            parsed.setdefault(mapping[key], value)
    for field, aliases in FIELD_ALIASES.items():
        if parsed.get(field):
            continue
        for alias in aliases:
            if flat.get(alias):
                parsed[field] = flat[alias]
                break
    if not parsed.get('name') and (flat.get('first_name') or flat.get('last_name')):
        parsed['name'] = f"{flat.get('first_name', '')} {flat.get('last_name', '')}".strip()
    return parsed


def ingest(item: LeadInbox) -> LeadInbox:
    """Process one stored inbox payload. Safe to retry: an already processed item is a no-op."""
    from crm.models import Campaign, Service
    with transaction.atomic():
        item = LeadInbox.objects.select_for_update(of=('self',)).select_related('source').get(pk=item.pk)
        if item.status in ('processed', 'duplicate', 'resolved', 'ignored'):
            return item
        if item.source.is_paused:
            return item   # stored safely; processed when the source is resumed
        item.attempts += 1
        try:
            with transaction.atomic():
                data = parse_payload(item.source, item.payload)
                name = (data.get('name') or '').strip()
                phone_raw, email = data.get('phone', ''), data.get('email', '')
                try:
                    phone = india.normalize_phone(phone_raw) if phone_raw else ''
                except ValueError:
                    phone = ''
                email = email.lower() if email and '@' in email else ''
                if not phone and not email:
                    raise LeadError('No valid phone number or email in the payload.')
                if not name:
                    name = email.split('@')[0] if email else f'Enquiry {phone[-4:]}'
                summary = data.get('message') or 'New enquiry'
                dupes = find_duplicates(phone, email)
                if dupes:
                    existing = dupes[0]
                    attach_enquiry(existing, f'Repeat enquiry via {item.source}: {summary}', source=item.source,
                                   metadata={'inbox_id': item.pk})
                    item.status = 'duplicate'
                    item.lead = existing
                else:
                    campaign = None
                    if data.get('campaign'):
                        campaign, _ = Campaign.objects.get_or_create(source=item.source, name=data['campaign'][:150])
                    lead = create_lead(LeadInput(name=name, phone=phone, email=email, company=data.get('company', ''),
                                                 city=data.get('city', ''), notes=summary, source=item.source,
                                                 campaign=campaign, website=data.get('website', '') if data.get('website', '').startswith('http') else ''),
                                       actor=None, rule='round_robin', inbox=item)
                    if data.get('service'):
                        svc = Service.objects.filter(is_active=True, name__iexact=data['service']).first()
                        if svc:
                            lead.services.add(svc)
                    item.status = 'processed'
                    item.lead = lead
                item.processed_at = timezone.now()
                item.last_error = ''
                item.next_retry_at = None
        except Exception as exc:
            item.last_error = str(exc)[:2000]
            if item.attempts > len(RETRY_MINUTES) or isinstance(exc, LeadError):
                item.status = 'review'
                item.next_retry_at = None
            else:
                item.status = 'failed'
                item.next_retry_at = timezone.now() + timedelta(minutes=RETRY_MINUTES[item.attempts - 1])
            logger.warning('Inbox %s failed: %s', item.pk, exc)
        item.save()
    if item.status == 'review':
        notify.notify(notify.users_with_perm('leads', 'assign'), 'Lead needs manual review',
                      body=f'{item.source}: {item.last_error[:150]}', url=reverse('crm:inbox_detail', args=[item.pk]),
                      event_type='inbox_review', level='warning', dedupe_key=f'inbox-review:{item.pk}')
    return item


# ---------------------------------------------------------------- follow-ups (guarantee 5)

def sync_next_action(lead: Lead) -> None:
    nxt = lead.follow_ups.filter(status='pending').order_by('due_at').first()
    if nxt:
        lead.next_follow_up_at = nxt.due_at
        lead.next_action = (nxt.purpose or nxt.get_kind_display())[:200]


def schedule_follow_up(lead: Lead, *, kind: str, due_at: datetime, assignee, purpose: str = '',
                       reminder_minutes: int = 15, actor=None) -> FollowUp:
    if due_at < timezone.now() - timedelta(minutes=1):
        raise LeadError('Choose a follow-up time that is not in the past.')
    if assignee is None or not assignee.is_active:
        raise LeadError('Choose an active assignee.')
    if not (permissions.in_scope(assignee, 'leads', 'view', lead)):
        raise LeadError('The assignee cannot see this lead. Choose someone with access to it.')
    with transaction.atomic():
        lead = _lock(lead)
        fu = FollowUp.objects.create(lead=lead, kind=kind, due_at=due_at, assignee=assignee, purpose=purpose[:200],
                                     reminder_minutes=reminder_minutes, created_by=actor)
        sync_next_action(lead)
        _save(lead, actor, ['next_follow_up_at', 'next_action'])
        _activity(lead, 'follow_up', f'{fu.get_kind_display()} scheduled for {timezone.localtime(due_at):%d-%b-%Y %H:%M}'
                  + (f': {purpose}' if purpose else ''), actor=actor)
        audit.log('followup.scheduled', lead, actor=actor, changes={'due_at': due_at, 'kind': kind, 'assignee': str(assignee)})
    return fu


def complete_follow_up(fu: FollowUp, *, outcome: str, notes: str = '', next_due: datetime | None, next_kind: str = 'call',
                       next_purpose: str = '', actor=None) -> FollowUp | None:
    with transaction.atomic():
        fu = FollowUp.objects.select_for_update(of=('self',)).select_related('lead').get(pk=fu.pk)
        if fu.status != 'pending':
            raise LeadError('This follow-up is already closed.')
        lead = _lock(fu.lead)
        others = lead.follow_ups.filter(status='pending').exclude(pk=fu.pk).exists()
        if lead.is_open and not next_due and not others:
            raise LeadError('Schedule the next follow-up. Every open lead needs a dated next action.')
        fu.status = 'done'
        fu.outcome = outcome[:200]
        fu.notes = notes
        fu.completed_at = timezone.now()
        fu.completed_by = actor
        fu.save()
        _activity(lead, 'follow_up', f'Completed {fu.get_kind_display().lower()}: {outcome}' + (f'. {notes}' if notes else ''),
                  actor=actor)
        nxt = None
        if next_due:
            if next_due < timezone.now() - timedelta(minutes=1):
                raise LeadError('Choose a next follow-up time that is not in the past.')
            nxt = FollowUp.objects.create(lead=lead, kind=next_kind, due_at=next_due, assignee=lead.owner or fu.assignee,
                                          purpose=next_purpose[:200], created_by=actor)
        _maybe_first_contact(lead, actor)
        sync_next_action(lead)
        _touch(lead)
        _save(lead, actor, ['next_follow_up_at', 'next_action', 'last_activity_at', 'stale_alert_level',
                            'first_contacted_at', 'first_response_seconds', 'stage', 'stage_changed_at', 'probability'])
        audit.log('followup.completed', lead, actor=actor, changes={'outcome': outcome})
    return nxt


def postpone_follow_up(fu: FollowUp, *, new_due: datetime, reason: str, actor=None) -> FollowUp:
    if not reason:
        raise LeadError('Choose a reason for postponing.')
    if new_due < timezone.now():
        raise LeadError('Choose a new time that is not in the past.')
    company = CompanySettings.get()
    with transaction.atomic():
        fu = FollowUp.objects.select_for_update().get(pk=fu.pk)
        if fu.status != 'pending':
            raise LeadError('This follow-up is already closed.')
        lead = _lock(fu.lead)
        old = fu.due_at
        fu.due_at = new_due
        fu.postpone_count += 1
        fu.reminded_at = None
        fu.escalation_level = 0
        fu.save()
        lead.postpone_count += 1
        sync_next_action(lead)
        _save(lead, actor, ['postpone_count', 'next_follow_up_at', 'next_action'])
        _activity(lead, 'follow_up', f'Postponed from {timezone.localtime(old):%d-%b %H:%M} to '
                  f'{timezone.localtime(new_due):%d-%b %H:%M}. Reason: {reason}', actor=actor)
        audit.log('followup.postponed', lead, actor=actor, changes={'from': old, 'to': new_due}, reason=reason)
    if lead.postpone_count > company.postpone_review_count:
        mgr = notify.manager_of(lead.owner) if lead.owner else None
        notify.notify([mgr] if mgr else notify.users_with_perm('leads', 'assign'),
                      f'Review needed: {lead} postponed {lead.postpone_count} times', url=lead_url(lead),
                      event_type='postpone_review', level='warning', dedupe_key=f'postpone:{lead.pk}:{lead.postpone_count}')
    return fu


def cancel_follow_up(fu: FollowUp, *, reason: str, actor=None):
    with transaction.atomic():
        fu = FollowUp.objects.select_for_update().get(pk=fu.pk)
        lead = _lock(fu.lead)
        if fu.status != 'pending':
            raise LeadError('This follow-up is already closed.')
        if lead.is_open and not lead.follow_ups.filter(status='pending').exclude(pk=fu.pk).exists():
            raise LeadError('This is the only next action on an open lead. Schedule another follow-up first, or postpone this one.')
        fu.status = 'cancelled'
        fu.notes = reason
        fu.completed_at = timezone.now()
        fu.completed_by = actor
        fu.save()
        sync_next_action(lead)
        _save(lead, actor, ['next_follow_up_at', 'next_action'])
        _activity(lead, 'follow_up', f'Cancelled {fu.get_kind_display().lower()}: {reason}', actor=actor)


# ---------------------------------------------------------------- activities (Part 4.7)

def _maybe_first_contact(lead: Lead, actor):
    if lead.first_contacted_at is None:
        lead.first_contacted_at = timezone.now()
        base = lead.assigned_at or lead.created_at
        lead.first_response_seconds = max(0, int((lead.first_contacted_at - base).total_seconds()))
    if lead.stage == STAGE_NEW:
        _apply_stage(lead, STAGE_CONTACTED, actor, 'First interaction logged')


def log_activity(lead: Lead, *, kind: str, summary: str = '', actor=None, direction: str = '', outcome: str = '',
                 duration_minutes: int | None = None, occurred_at: datetime | None = None, attendees: str = '',
                 visibility: str = 'team', next_due: datetime | None = None, next_kind: str = 'call',
                 next_purpose: str = '') -> LeadActivity:
    if kind == 'call' and outcome == 'connected' and not summary.strip():
        raise LeadError('Add a short summary of the call.')
    if kind == 'note' and not summary.strip():
        raise LeadError('Write the note before saving.')
    if kind == 'meeting' and occurred_at and occurred_at > timezone.now() + timedelta(minutes=5):
        raise LeadError('A completed meeting cannot be in the future. Schedule a follow-up instead.')
    with transaction.atomic():
        lead = _lock(lead)
        has_pending = lead.follow_ups.filter(status='pending', due_at__gt=timezone.now()).exists()
        if kind in ('call', 'meeting') and lead.is_open and not next_due and not has_pending:
            raise LeadError('Schedule the next follow-up. Every open lead needs a dated next action.')
        act = _activity(lead, kind, summary.strip()[:5000], actor=actor, direction=direction, outcome=outcome,
                        duration_minutes=duration_minutes, occurred_at=occurred_at or timezone.now(),
                        attendees=attendees[:300], visibility=visibility)
        if kind in ('call', 'meeting', 'email', 'whatsapp'):
            contacted = kind != 'call' or outcome in ('connected', 'call_back')
            if lead.first_contacted_at is None:
                lead.first_contacted_at = timezone.now()
                base = lead.assigned_at or lead.created_at
                lead.first_response_seconds = max(0, int((lead.first_contacted_at - base).total_seconds()))
            if contacted and lead.stage == STAGE_NEW:
                _apply_stage(lead, STAGE_CONTACTED, actor, f'{act.get_kind_display()} logged')
            # The interaction answers the earliest due follow-up.
            due = lead.follow_ups.filter(status='pending', due_at__lte=timezone.now() + timedelta(hours=2)).order_by('due_at').first()
            if due and next_due:
                due.status = 'done'
                due.outcome = (outcome or kind)[:200]
                due.completed_at = timezone.now()
                due.completed_by = actor
                due.save()
        if next_due:
            if next_due < timezone.now() - timedelta(minutes=1):
                raise LeadError('Choose a next follow-up time that is not in the past.')
            FollowUp.objects.create(lead=lead, kind=next_kind, due_at=next_due, assignee=lead.owner or actor,
                                    purpose=next_purpose[:200], created_by=actor)
        sync_next_action(lead)
        _touch(lead)
        _save(lead, actor, ['last_activity_at', 'stale_alert_level', 'first_contacted_at', 'first_response_seconds',
                            'stage', 'stage_changed_at', 'probability', 'next_follow_up_at', 'next_action'])
        audit.log('activity.logged', lead, actor=actor, changes={'kind': kind, 'outcome': outcome})
    return act


# ---------------------------------------------------------------- stage machine (Part 4.3)

def qualification_gaps(lead: Lead) -> list[str]:
    gaps = []
    if not lead.services.exists():
        gaps.append('service interest')
    if lead.budget_min is None and lead.budget_max is None and not lead.value:
        gaps.append('budget range or deal value')
    if not lead.decision_maker:
        gaps.append('decision-maker')
    if not lead.expected_close_date:
        gaps.append('expected closing date')
    elif lead.expected_close_date < india.today():
        gaps.append('an expected closing date that is not in the past')
    return gaps


def transition_rules(lead: Lead, to: str) -> dict:
    """Describes what moving to `to` requires. Used by the popup, the board and the server check."""
    frm = lead.stage
    info = {'allowed': True, 'error': '', 'needs_reason': False}
    if lead.archived_at:
        return {**info, 'allowed': False, 'error': 'This lead is archived.'}
    if frm in CLOSED_STAGES:
        return {**info, 'allowed': False, 'error': 'Lead is closed — re-open first.'}
    if to == frm:
        return {**info, 'allowed': False, 'error': 'The lead is already in this stage.'}
    if to in CLOSED_STAGES:
        return {**info, 'allowed': False, 'error': f'Use Mark {STAGE_LABELS[to].lower()} to close a lead.'}
    if to == STAGE_QUOTE_SENT:
        return {**info, 'allowed': False, 'error': 'Send a quotation to move to Quotation sent. The system moves it when the quotation is sent.'}
    if to == STAGE_NEW:
        return {**info, 'allowed': False, 'error': 'A lead cannot go back to New.'}
    if to == STAGE_QUOTE_PREPARED and not lead.quotations.exclude(status='void').exists():
        return {**info, 'allowed': False, 'error': 'Create a quotation draft before moving to Quotation prepared.'}
    if to in QUALIFIED_PLUS:
        gaps = qualification_gaps(lead)
        if gaps:
            return {**info, 'allowed': False, 'error': 'Add ' + ', '.join(gaps) + ' to qualify this lead.', 'gaps': gaps}
    if to == STAGE_CONTACTED and frm == STAGE_NEW and not lead.activities.filter(
            kind__in=['call', 'meeting', 'email', 'whatsapp']).exists():
        return {**info, 'allowed': False, 'error': 'Log a call, meeting or message first. Contacted needs an interaction.'}
    order = {s: i for i, s in enumerate(MAIN_PATH)}
    if to == STAGE_NURTURING or frm == STAGE_NURTURING:
        info['needs_reason'] = to == STAGE_NURTURING or order.get(to, 0) < order[STAGE_CONTACTED]
    elif order.get(to, 0) < order.get(frm, 0):
        info['needs_reason'] = True        # backward move
    elif order.get(to, 0) - order.get(frm, 0) > 1:
        info['needs_reason'] = True        # skipping stages
    return info


def _apply_stage(lead: Lead, to: str, actor, reason: str = ''):
    """Low-level stage write with history. Caller holds the row lock."""
    frm = lead.stage
    now = timezone.now()
    StageHistory.objects.create(lead=lead, from_stage=frm, to_stage=to, actor=actor, reason=reason[:500],
                                seconds_in_previous=max(0, int((now - lead.stage_changed_at).total_seconds())))
    lead.stage = to
    lead.stage_changed_at = now
    lead.aging_alerted_stage = ''
    default_prob = DEFAULT_PROBABILITY.get(to)
    if default_prob is not None:
        lead.probability = default_prob
    _activity(lead, 'stage', f'Stage changed from {STAGE_LABELS.get(frm, frm)} to {STAGE_LABELS.get(to, to)}'
              + (f'. Reason: {reason}' if reason else ''), actor=actor, metadata={'from': frm, 'to': to})
    audit.log('lead.stage_changed', lead, actor=actor, changes={'stage': [frm, to]}, reason=reason)


def change_stage(lead: Lead, to: str, actor, *, reason: str = '', next_due: datetime | None = None,
                 next_kind: str = 'call', next_purpose: str = '') -> Lead:
    with transaction.atomic():
        lead = _lock(lead)
        rules = transition_rules(lead, to)
        if not rules['allowed']:
            raise LeadError(rules['error'])
        if rules['needs_reason'] and len(reason.strip()) < 3:
            raise LeadError('Give a reason for moving backwards, skipping stages or nurturing.')
        has_future = lead.follow_ups.filter(status='pending', due_at__gt=timezone.now()).exists()
        if not next_due and not has_future:
            raise LeadError('Schedule the next follow-up. The stage cannot change without a dated next action.')
        if next_due:
            if next_due < timezone.now() - timedelta(minutes=1):
                raise LeadError('Choose a next follow-up time that is not in the past.')
            FollowUp.objects.create(lead=lead, kind=next_kind, due_at=next_due, assignee=lead.owner or actor,
                                    purpose=next_purpose[:200] or f'Follow up ({STAGE_LABELS[to]})', created_by=actor)
        _apply_stage(lead, to, actor, reason)
        sync_next_action(lead)
        _touch(lead)
        _save(lead, actor, ['stage', 'stage_changed_at', 'probability', 'aging_alerted_stage', 'next_follow_up_at',
                            'next_action', 'last_activity_at', 'stale_alert_level'])
    return lead


def system_stage(lead: Lead, to: str, reason: str, actor=None):
    """Stage moves the system makes itself (quotation prepared/sent, negotiation on new version)."""
    with transaction.atomic():
        lead = _lock(lead)
        if not lead.is_open or lead.stage == to:
            return lead
        order = {s: i for i, s in enumerate(MAIN_PATH)}
        if to in order and lead.stage in order and order[to] < order[lead.stage] and to != STAGE_QUOTE_SENT:
            return lead
        _apply_stage(lead, to, actor, reason)
        _touch(lead)
        _save(lead, actor, ['stage', 'stage_changed_at', 'probability', 'aging_alerted_stage', 'last_activity_at',
                            'stale_alert_level'])
    return lead


def mark_won(lead: Lead, actor, *, won_value: Decimal, quotation=None, note: str = '', expected_start=None,
             client=None, create_client: bool = True) -> Lead:
    from crm.models import Quotation
    from crm.modules.clients.services import client_from_lead
    if D(won_value) <= 0:
        raise LeadError('Enter the won value (more than ₹0).')
    with transaction.atomic():
        lead = _lock(lead)
        if not lead.is_open:
            raise LeadError('Only an open lead can be marked won.')
        accepted = lead.quotations.filter(status__in=['accepted', 'converted']).exists()
        if quotation is not None and quotation.lead_id != lead.pk:
            raise LeadError('That quotation belongs to another lead.')
        if not accepted and not (quotation is not None and quotation.status in ('accepted', 'converted')) and len(note.strip()) < 10:
            raise LeadError('Needs an accepted quotation or a confirmed-order note of at least 10 characters.')
        if client is None and lead.client_id:
            client = lead.client
        if client is None and create_client:
            client = client_from_lead(lead, actor)
        lead.client = client
        lead.won_value = D(won_value)
        lead.value = D(won_value)
        lead.won_at = timezone.now()
        lead.expected_start = expected_start
        lead.follow_ups.filter(status='pending').update(status='cancelled', completed_at=timezone.now(),
                                                        notes='Lead won', completed_by=actor)
        _apply_stage(lead, STAGE_WON, actor, note or (f'Accepted {quotation}' if quotation else 'Won'))
        lead.next_follow_up_at = None
        lead.next_action = ''
        _touch(lead)
        _save(lead, actor, ['client', 'won_value', 'value', 'won_at', 'expected_start', 'stage', 'stage_changed_at',
                            'probability', 'next_follow_up_at', 'next_action', 'last_activity_at', 'stale_alert_level'])
        if client is not None:
            Quotation.objects.filter(lead=lead, client__isnull=True).update(client=client)
        audit.log('lead.won', lead, actor=actor, changes={'won_value': won_value, 'client': str(client) if client else ''})
        from crm.modules.payroll.services import accrue_commission_for_won
        accrue_commission_for_won(lead)
    return lead


def _apply_lost(lead: Lead, actor, data: dict):
    lead.lost_reason = data['reason']
    lead.lost_competitor = data.get('competitor', '')[:120]
    lead.lost_price = D(data['price']) if data.get('price') not in (None, '') else None
    lead.lost_notes = data.get('notes', '')
    lead.lost_at = timezone.now()
    company = CompanySettings.get()
    wb = data.get('win_back_date')
    from datetime import date
    lead.win_back_date = date.fromisoformat(wb) if isinstance(wb, str) and wb else (wb or (india.today() + timedelta(days=company.win_back_days)))
    lead.follow_ups.filter(status='pending').update(status='cancelled', completed_at=timezone.now(),
                                                    notes='Lead lost', completed_by=actor)
    _apply_stage(lead, STAGE_LOST, actor, f'{dict(Lead._meta.get_field("lost_reason").choices).get(data["reason"])}'
                 + (f': {data.get("notes")}' if data.get('notes') else ''))
    if data['reason'] == 'invalid':
        lead.is_invalid = True
    lead.next_follow_up_at = None
    lead.next_action = ''
    _save(lead, actor, ['lost_reason', 'lost_competitor', 'lost_price', 'lost_notes', 'lost_at', 'win_back_date',
                        'stage', 'stage_changed_at', 'probability', 'next_follow_up_at', 'next_action', 'is_invalid'])
    audit.log('lead.lost', lead, actor=actor, changes={'reason': data['reason'], 'competitor': data.get('competitor', '')})


def mark_lost(lead: Lead, actor, *, reason: str, competitor: str = '', price=None, notes: str = '',
              win_back_date=None) -> tuple[Lead, object]:
    """Returns (lead, approval_request_or_None). Above the value threshold a manager must approve."""
    if not reason:
        raise LeadError('Choose a lost reason.')
    if reason == 'other' and len(notes.strip()) < 5:
        raise LeadError('Add notes explaining the reason when you choose Other.')
    company = CompanySettings.get()
    data = {'reason': reason, 'competitor': competitor, 'price': str(price) if price not in (None, '') else '',
            'notes': notes, 'win_back_date': win_back_date.isoformat() if win_back_date else ''}
    with transaction.atomic():
        lead = _lock(lead)
        if not lead.is_open:
            raise LeadError('Only an open lead can be marked lost.')
        needs_approval = lead.value >= company.lost_approval_threshold and lead.value > 0 \
            and not permissions.has_perm(actor, 'leads', 'approve')
        if needs_approval:
            req = approvals.submit('lead_lost', lead, title=f'Mark lost: {lead} ({lead.value})', module='leads',
                                   requested_by=actor, level='manager', amount=lead.value,
                                   reasons=[f'Deal value is at or above the lost-approval threshold'],
                                   before={'stage': lead.get_stage_display()}, after={'stage': 'Lost', 'reason': reason},
                                   payload=data, message=notes)
            _activity(lead, 'system', 'Requested approval to mark lost.', actor=actor)
            return lead, req
        _apply_lost(lead, actor, data)
    return lead, None


def _approve_lost(req, user):
    lead = Lead.objects.select_for_update().get(pk=req.object_id)
    if lead.is_open:
        _apply_lost(lead, req.requested_by, req.payload)


approvals.register('lead_lost', _approve_lost)


def reopen(lead: Lead, actor, *, reason: str, next_due: datetime, next_kind: str = 'call') -> Lead:
    if len(reason.strip()) < 3:
        raise LeadError('Give a reason for re-opening.')
    if not next_due or next_due < timezone.now():
        raise LeadError('Schedule the next follow-up. A re-opened lead needs a dated next action.')
    with transaction.atomic():
        lead = _lock(lead)
        if lead.stage != STAGE_LOST or lead.archived_at:
            raise LeadError('Only a lost lead can be re-opened.')
        FollowUp.objects.create(lead=lead, kind=next_kind, due_at=next_due, assignee=lead.owner or actor,
                                purpose='Re-opened', created_by=actor)
        StageHistory.objects.create(lead=lead, from_stage=STAGE_LOST, to_stage=STAGE_CONTACTED, actor=actor,
                                    reason=f'Re-opened: {reason}', seconds_in_previous=int((timezone.now() - lead.stage_changed_at).total_seconds()))
        lead.stage = STAGE_CONTACTED
        lead.stage_changed_at = timezone.now()
        lead.probability = DEFAULT_PROBABILITY[STAGE_CONTACTED]
        lead.is_invalid = False
        lead.win_back_date = None
        sync_next_action(lead)
        _touch(lead)
        _activity(lead, 'stage', f'Re-opened. Reason: {reason}', actor=actor)
        _save(lead, actor, ['stage', 'stage_changed_at', 'probability', 'is_invalid', 'win_back_date',
                            'next_follow_up_at', 'next_action', 'last_activity_at', 'stale_alert_level'])
        audit.log('lead.reopened', lead, actor=actor, reason=reason)
    return lead


def archive_blockers(lead: Lead) -> list[str]:
    from crm.models import PaymentLink
    out = []
    if lead.quotations.filter(status__in=OPEN_QUOTATION_STATUSES).exists():
        out.append('an open quotation')
    if PaymentLink.objects.filter(quotation__lead=lead, status__in=['draft', 'pending_approval', 'active', 'partially_paid']).exists():
        out.append('an active payment link')
    return out


def archive(lead: Lead, actor, reason: str) -> Lead:
    if len(reason.strip()) < 10:
        raise LeadError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        lead = _lock(lead)
        if lead.archived_at:
            raise LeadError('This lead is already archived.')
        blockers = archive_blockers(lead)
        if blockers:
            raise LeadError('Cannot archive: this lead has ' + ' and '.join(blockers) + '.')
        lead.archived_at = timezone.now()
        lead.archived_by = actor
        lead.archive_reason = reason[:500]
        lead.follow_ups.filter(status='pending').update(status='cancelled', completed_at=timezone.now(),
                                                        notes='Lead archived', completed_by=actor)
        _activity(lead, 'system', f'Archived. Reason: {reason}', actor=actor)
        _save(lead, actor, ['archived_at', 'archived_by', 'archive_reason'])
        audit.log('lead.archived', lead, actor=actor, reason=reason)
    return lead


def log_update(lead: Lead, actor, before: dict) -> None:
    """Audit the fields an edit changed (call after saving, with a snapshot taken before)."""
    after = audit.snapshot(lead, before.keys())
    changes = audit.diff(before, after)
    if changes:
        audit.log('lead.updated', lead, actor=actor, changes=changes)


# ---------------------------------------------------------------- merge (Part 4.7)

MERGE_FIELDS = ['name', 'company', 'phone', 'email', 'website', 'city', 'state', 'owner', 'priority', 'value',
                'expected_close_date', 'decision_maker', 'notes']


def merge(primary: Lead, secondary: Lead, actor, choices: dict | None = None) -> Lead:
    """Move secondary's history into primary; secondary is archived with a pointer (never deleted)."""
    from crm.models import Quotation
    if primary.pk == secondary.pk:
        raise LeadError('Choose two different leads.')
    choices = choices or {}
    with transaction.atomic():
        a, b = sorted([primary.pk, secondary.pk])
        locked = {l.pk: l for l in Lead.objects.select_for_update().filter(pk__in=[a, b])}
        primary, secondary = locked[primary.pk], locked[secondary.pk]
        if secondary.archived_at or primary.archived_at:
            raise LeadError('Archived leads cannot be merged.')
        changed = {}
        for f in MERGE_FIELDS:
            if choices.get(f) == 'secondary':
                old = getattr(primary, f)
                new = getattr(secondary, f)
                if f == 'owner' and new is None:
                    continue
                setattr(primary, f, new)
                changed[f] = [str(old), str(new)]
        if not primary.alt_phone and secondary.phone and secondary.phone != primary.phone:
            primary.alt_phone = secondary.phone
        LeadActivity.objects.filter(lead=secondary).update(lead=primary)
        FollowUp.objects.filter(lead=secondary).update(lead=primary)
        StageHistory.objects.filter(lead=secondary).update(lead=primary)
        LeadAssignment.objects.filter(lead=secondary).update(lead=primary)
        LeadInbox.objects.filter(lead=secondary).update(lead=primary)
        Quotation.objects.filter(lead=secondary).update(lead=primary)
        for s in secondary.services.all():
            primary.services.add(s)
        for t in secondary.tags.all():
            primary.tags.add(t)
        secondary.merged_into = primary
        secondary.archived_at = timezone.now()
        secondary.archived_by = actor
        secondary.archive_reason = f'Merged into lead #{primary.pk}'
        _save(secondary, actor, ['merged_into', 'archived_at', 'archived_by', 'archive_reason'])
        sync_next_action(primary)
        if primary.is_open and primary.next_follow_up_at is None:
            primary.next_follow_up_at = india.next_working_day_at()
        primary.flagged_for_merge = False
        _save(primary, actor, MERGE_FIELDS + ['alt_phone', 'next_follow_up_at', 'next_action', 'flagged_for_merge'])
        _activity(primary, 'system', f'Merged lead #{secondary.pk} ({secondary.name}) into this lead.', actor=actor)
        DuplicateCandidate.objects.filter(Q(lead_a=primary, lead_b=secondary) | Q(lead_a=secondary, lead_b=primary)) \
            .update(status='merged', decided_by=actor, decided_at=timezone.now())
        audit.log('lead.merged', primary, actor=actor, changes={'secondary': secondary.pk, 'fields': changed})
    return primary


def _approve_merge(req, user):
    primary = Lead.objects.get(pk=req.object_id)
    secondary = Lead.objects.get(pk=req.payload['secondary'])
    merge(primary, secondary, req.requested_by, req.payload.get('choices', {}))


approvals.register('lead_merge', _approve_merge)


# ---------------------------------------------------------------- offboarding (Part 4.9)

def reassign_open_work(user, actor=None, reason: str = 'Owner deactivated or on leave') -> dict:
    """Reassign a user's open leads by rule; manager gets a summary with 24 h undo."""
    leads = list(Lead.objects.filter(owner=user, stage__in=OPEN_STAGES, archived_at__isnull=True))
    moved, unassigned = 0, 0
    for lead in leads:
        new_owner = pick_round_robin(exclude_ids=[user.pk])
        if new_owner is None:
            unassigned += 1
            continue
        assign(lead, new_owner, actor, rule='offboarding', reason=reason, override_availability=True, undo_hours=24)
        moved += 1
    FollowUp.objects.filter(assignee=user, status='pending', lead__stage__in=CLOSED_STAGES).update(
        status='cancelled', notes='Assignee offboarded')
    mgr = notify.manager_of(user)
    recipients = [mgr] if mgr else notify.owners()
    if leads:
        notify.notify(recipients, f'{len(leads)} open leads of {user.get_full_name() or user} were reassigned',
                      body=f'{moved} reassigned by rule, {unassigned} could not be assigned (no one available). '
                           f'Undo is available for 24 hours.', url=reverse('crm:lead_list') + '?quick=unassigned',
                      event_type='offboarding', level='warning', email=True)
    audit.log('lead.offboarding_reassigned', user, actor=actor, changes={'moved': moved, 'unassigned': unassigned})
    return {'moved': moved, 'unassigned': unassigned, 'total': len(leads)}


def undo_assignment(assignment: LeadAssignment, actor) -> Lead:
    if not assignment.undo_until or assignment.undo_until < timezone.now():
        raise LeadError('The undo window for this assignment has passed.')
    if assignment.from_user is None or not assignment.from_user.is_active:
        raise LeadError('The previous owner is not active, so this cannot be undone.')
    lead = assign(assignment.lead, assignment.from_user, actor, rule='manual', reason='Undo offboarding reassignment',
                  override_availability=True)
    assignment.undo_until = None
    assignment.save(update_fields=['undo_until'])
    return lead
