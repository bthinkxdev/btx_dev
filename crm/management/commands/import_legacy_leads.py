"""Import leads from the legacy CRM Excel export (crm_export.xlsx).

    python manage.py import_legacy_leads crm_export.xlsx                 # dry run, writes nothing
    python manage.py import_legacy_leads crm_export.xlsx --commit        # really import

Only leads owned by the chosen executives are imported (default: lois, jenni). Old statuses are
mapped to the new pipeline stages and each lead keeps its executive as owner.

Maximum-detail import: besides the lead itself it brings across the package, the Google Places
details (address, rating, maps link, ...), the lead-extraction scores, follow-ups, completed
activities and the full activity log (as the lead timeline). Leads whose phone already exists are
imported too but flagged for merge. Only leads with neither phone nor email cannot be imported
(the new CRM forbids it) and are listed in the skipped report.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.core import india
from crm.core.india import STATE_NAMES
from crm.models import FollowUp, Lead, LeadActivity, LeadSource, LeadTag
from crm.models.leads import CLOSED_STAGES, DEFAULT_PROBABILITY

# old status -> new stage. Edit here if a mapping should change.
STAGE_MAP = {
    'new': 'new',
    'contacted': 'contacted',
    'call_connected': 'contacted',
    'whatsapp_connected': 'contacted',
    'failed_retry': 'nurturing',
    'qualified': 'qualified',
    'demo': 'qualified',
    'proposal': 'quotation_prepared',
    'proposal_sent': 'quotation_sent',
    'closing_ongoing': 'negotiation',
    'negotiation': 'negotiation',
    'advance_received_project_started': 'won',
    'project_handed': 'won',
    'won': 'won',
    'closed': 'won',  # in the old CRM "closed" meant the deal was won
    'lost': 'lost',
}

# old source text (lower case) -> (new source name, channel)
SOURCE_MAP = {
    'meta': ('Meta', 'meta'),
    'google': ('Google', 'google'),
    'google places': ('Google Places', 'google'),
    'whatsapp ads': ('WhatsApp Ads', 'whatsapp'),
    'inbound': ('Inbound', 'other'),
    'reference': ('Referral', 'referral'),
    'friend': ('Referral', 'referral'),
}

# old activity-log action -> new LeadActivity kind
LOG_KIND = {
    'created': 'system', 'notes_updated': 'note', 'status_change': 'stage', 'contact_updated': 'system',
    'whatsapp': 'whatsapp', 'follow_up_scheduled': 'follow_up', 'follow_up_done': 'follow_up',
    'follow_up_rescheduled': 'follow_up', 'call': 'call', 'package_change': 'system',
    'project_created': 'system', 'pipeline_outcome': 'system', 'lead_converted_to_project': 'system',
    'task_updated': 'system', 'note': 'note',
}
FOLLOW_UP_KINDS = ('call', 'meeting', 'email', 'whatsapp')


def _dt(value):
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and timezone.is_naive(value):
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


def _alt_phone(raw, primary):
    try:
        p = india.normalize_phone(raw or '')
    except ValueError:
        return ''
    return p if p and p != primary and len(p) <= 20 else ''


def _lost_reason(text):
    t = (text or '').lower()
    if 'price' in t or 'cost' in t:
        return 'price'
    if 'respon' in t:
        return 'no_response'
    if 'wrong' in t:
        return 'invalid'
    if 'financ' in t or 'budget' in t:
        return 'no_budget'
    return 'other'


def _state_city(address):
    """Best-effort (state code, city) from a Google address like '..., Kochi, Kerala 682001, India'."""
    if not address:
        return '', ''
    parts = [p.strip() for p in address.split(',')]
    code = next((c for c, n in STATE_NAMES.items() if n.lower() in address.lower()), '')
    city = parts[-3] if len(parts) >= 3 else ''
    return code, city[:80]


def _group(wb, sheet, key='lead_id'):
    out = {}
    if sheet not in wb.sheetnames:
        return out
    it = wb[sheet].iter_rows(values_only=True)
    header = list(next(it))
    for row in it:
        d = dict(zip(header, row))
        out.setdefault(d.get(key), []).append(d)
    return out


def _legacy_block(r, old_status, extraction):
    lines = [f'[Legacy CRM #{r["id"]} | status: {old_status} | executive: {r["executive_username"]}]']
    if r.get('package_name'):
        lines.append(f'Package: {r["package_name"]} (Rs {r.get("package_price") or 0})')
    if r.get('nurture_reason') or r.get('nurture_next_review'):
        lines.append(f'Nurture: {r.get("nurture_reason") or ""} (review {r.get("nurture_next_review") or "-"})')
    if r.get('place_name'):
        lines.append('--- Google Places ---')
        for label, key in [('Business', 'place_name'), ('Type', 'place_primary_type'), ('Address', 'place_address'),
                           ('Phone', 'place_phone'), ('Website', 'place_website'), ('Maps', 'place_maps_uri'),
                           ('Rating', 'place_rating'), ('Reviews', 'place_review_count'),
                           ('Business status', 'place_business_status'), ('Latitude', 'place_latitude'),
                           ('Longitude', 'place_longitude'), ('Google place id', 'place_google_place_id')]:
            if r.get(key) not in (None, ''):
                lines.append(f'{label}: {r[key]}')
    for e in extraction:
        lines.append('--- Lead extraction ---')
        for label, key in [('Qualification', 'qualification_status'), ('Website status', 'website_status'),
                           ('Instagram', 'instagram_url'), ('Facebook', 'facebook_url'),
                           ('Social presence', 'social_presence_type'), ('Social activity', 'social_activity'),
                           ('Business quality score', 'business_quality_score'),
                           ('Website opportunity', 'website_opportunity_score'),
                           ('Meta opportunity', 'meta_opportunity_score'), ('Overall score', 'overall_score'),
                           ('Reason', 'reason')]:
            if e.get(key) not in (None, ''):
                lines.append(f'{label}: {e[key]}')
    if r.get('notes'):
        lines.append('--- Notes ---')
        lines.append(r['notes'].strip())
    return '\n'.join(lines)


class _Rollback(Exception):
    pass


class Command(BaseCommand):
    help = 'Import legacy CRM leads (owned by selected executives) with full history from crm_export.xlsx.'

    def add_arguments(self, parser):
        parser.add_argument('xlsx', help='Path to crm_export.xlsx')
        parser.add_argument('--commit', action='store_true', help='Write to the database (default: dry run).')
        parser.add_argument('--owners', default='lois,jenni', help='Old executive usernames to import (comma list).')
        parser.add_argument('--map', action='append', default=[], metavar='OLD=NEW',
                            help='Map an old username to a different new username, e.g. --map jenni=Jenni.')
        parser.add_argument('--skipped-report', default='legacy_leads_skipped.csv',
                            help='CSV listing skipped leads.')

    def handle(self, *args, **o):
        import openpyxl

        owners = [x.strip().lower() for x in o['owners'].split(',') if x.strip()]
        overrides = {}
        for m in o['map']:
            old, _, new = m.partition('=')
            overrides[old.strip().lower()] = new.strip()

        User = get_user_model()
        user_by_old = {}
        for old in owners:
            uname = overrides.get(old, old)
            user = User.objects.filter(username__iexact=uname).first()
            if not user:
                raise CommandError(f'No user "{uname}" for old executive "{old}". Create it or use --map {old}=<username>.')
            user_by_old[old] = user

        wb = openpyxl.load_workbook(o['xlsx'], read_only=True, data_only=True)
        if 'Leads' not in wb.sheetnames:
            raise CommandError('Sheet "Leads" not found.')
        it = wb['Leads'].iter_rows(values_only=True)
        header = list(next(it))
        rows = [dict(zip(header, r)) for r in it]
        rows = [r for r in rows if (r.get('executive_username') or '').lower() in user_by_old]
        self.stdout.write(f'{len(rows)} legacy leads belong to {", ".join(owners)}.')

        ext_by_lead = _group(wb, 'extractionlead')
        fu_by_lead = _group(wb, 'followup')
        act_by_lead = _group(wb, 'activity')
        log_by_lead = _group(wb, 'activitylog')
        all_users = {u.username.lower(): u for u in User.objects.all()}

        def user_of(username, default):
            return all_users.get((username or '').lower(), default)

        created, skipped, per_stage, per_owner = 0, [], {}, {}
        n_fu = n_act = n_dup = 0
        followups, activities = [], []
        seen_phones = set(Lead.objects.exclude(phone='').values_list('phone', flat=True))
        sources = {}
        now = timezone.now()

        try:
            with transaction.atomic():
                legacy_tag, _ = LeadTag.objects.get_or_create(name='legacy-import', defaults={'color': 'grey'})
                hot_tag, _ = LeadTag.objects.get_or_create(name='high-hope', defaults={'color': 'red'})
                for r in rows:
                    old_status = (r.get('status') or 'new').strip()
                    stage = STAGE_MAP.get(old_status)
                    if stage is None:
                        skipped.append((r, f'unknown status "{old_status}"'))
                        continue
                    try:
                        phone = india.normalize_phone(r.get('phone') or '')
                    except ValueError:
                        phone = ''
                    if len(phone) > 20:
                        phone = ''
                    email = (r.get('email') or '').strip().lower()
                    if not phone and not email:
                        skipped.append((r, 'no valid phone or email'))
                        continue
                    is_dup = bool(phone and phone in seen_phones)
                    if phone:
                        seen_phones.add(phone)

                    src_text = (r.get('source') or '').strip()
                    name, channel = SOURCE_MAP.get(src_text.lower(), (src_text or 'Legacy import', 'manual'))
                    if name not in sources:
                        sources[name] = (LeadSource.objects.filter(name__iexact=name).first()
                                         or LeadSource.objects.create(name=name, channel=channel))
                    owner = user_by_old[r['executive_username'].lower()]

                    updated = _dt(r.get('updated_at')) or now
                    created_at = _dt(r.get('created_at')) or now
                    value = Decimal(str(r.get('deal_value') or 0))
                    extraction = ext_by_lead.get(r['id'], [])
                    state, city = _state_city(r.get('place_address'))

                    lead = Lead(
                        name=(r.get('name') or 'Unknown').strip()[:100], phone=phone, email=email,
                        source=sources[name], owner=owner, stage=stage, stage_changed_at=updated,
                        priority='hot' if r.get('high_hope') else 'warm',
                        value=value, probability=DEFAULT_PROBABILITY[stage],
                        notes=_legacy_block(r, old_status, extraction), created_at=created_at,
                        last_activity_at=updated, assigned_at=created_at, consent_basis='legacy_import',
                        company=(r.get('place_name') or '')[:150],
                        website=(r.get('place_website') or (extraction[0].get('website') if extraction else '') or '')[:200],
                        city=city, state=state, alt_phone=_alt_phone(r.get('place_phone'), phone),
                        flagged_for_merge=is_dup,
                        duplicate_override_reason='Legacy import: phone already exists' if is_dup else '',
                    )
                    if stage not in CLOSED_STAGES:
                        lead.next_follow_up_at = _dt(r.get('next_followup')) or (now + timedelta(days=1))
                    if stage == 'won':
                        lead.won_at = _dt(r.get('won_at')) or updated
                        lead.won_value = value
                    if stage == 'lost':
                        lead.lost_at = _dt(r.get('lost_at')) or updated
                        text = (r.get('lost_reason') or '').strip()
                        lead.lost_reason = _lost_reason(text)
                        lead.lost_notes = ' | '.join(x for x in [text, (r.get('lost_note') or '').strip(),
                                                                f'Legacy status: {old_status}'] if x)
                    lead.save()
                    lead.tags.add(legacy_tag)
                    if r.get('high_hope'):
                        lead.tags.add(hot_tag)
                    n_dup += is_dup

                    for f in fu_by_lead.get(r['id'], []):
                        due = _dt(f.get('datetime')) or created_at
                        done = bool(f.get('is_done'))
                        who = user_of(f.get('employee_id'), owner)
                        note = f.get('note') or ''
                        followups.append(FollowUp(
                            lead=lead, kind='call', due_at=due, assignee=who, purpose=note[:200],
                            notes=note if len(note) > 200 else '', status='done' if done else 'pending',
                            completed_at=due if done else None, completed_by=who if done else None,
                            reminded_at=_dt(f.get('reminder_sent_at')),
                            escalation_level=1 if f.get('escalated_at') else 0, created_at=created_at))
                    for a in act_by_lead.get(r['id'], []):
                        st = a.get('status')
                        atype = a.get('activity_type')
                        if st == 'completed':
                            activities.append(LeadActivity(
                                lead=lead, kind=atype if atype in ('call', 'whatsapp', 'meeting', 'email') else 'note',
                                outcome=(a.get('outcome') or '')[:20],
                                summary=a.get('notes') or f'{atype} ({a.get("outcome") or "completed"})',
                                occurred_at=_dt(a.get('completed_at')) or _dt(a.get('scheduled_at')) or created_at,
                                actor=user_of(a.get('completed_by_id'), owner), source_label='legacy',
                                metadata={'legacy_activity_id': a['id'], 'source': a.get('source')},
                                created_at=_dt(a.get('created_at')) or created_at))
                        else:
                            followups.append(FollowUp(
                                lead=lead, kind=atype if atype in FOLLOW_UP_KINDS else 'call',
                                due_at=_dt(a.get('scheduled_at')) or created_at,
                                assignee=user_of(a.get('owner_id'), owner), purpose='Legacy scheduled activity',
                                notes=a.get('notes') or '', status='cancelled' if st == 'cancelled' else 'pending',
                                outcome=(a.get('outcome') or '')[:200],
                                reminded_at=_dt(a.get('reminder_sent_at')),
                                escalation_level=1 if a.get('escalated_at') else 0, created_at=created_at))
                    for g in log_by_lead.get(r['id'], []):
                        action = g.get('action') or ''
                        activities.append(LeadActivity(
                            lead=lead, kind=LOG_KIND.get(action, 'system'),
                            summary=f'{action}: {g["note"]}' if g.get('note') else action,
                            occurred_at=_dt(g.get('created_at')) or created_at, source_label='legacy',
                            metadata={'legacy_log_id': g['id'], 'legacy_action': action},
                            created_at=_dt(g.get('created_at')) or created_at))

                    created += 1
                    per_stage[stage] = per_stage.get(stage, 0) + 1
                    per_owner[owner.username] = per_owner.get(owner.username, 0) + 1

                FollowUp.objects.bulk_create(followups, batch_size=500)
                LeadActivity.objects.bulk_create(activities, batch_size=500)
                n_fu, n_act = len(followups), len(activities)

                if not o['commit']:
                    raise _Rollback
        except _Rollback:
            pass

        if skipped:
            with open(o['skipped_report'], 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['legacy_id', 'name', 'phone', 'email', 'executive', 'status', 'reason'])
                for r, why in skipped:
                    w.writerow([r['id'], r.get('name'), r.get('phone'), r.get('email'),
                                r.get('executive_username'), r.get('status'), why])

        verb = 'Imported' if o['commit'] else 'Would import (dry run, nothing written)'
        self.stdout.write(self.style.SUCCESS(f'{verb}: {created} leads.'))
        self.stdout.write(f'By owner: {per_owner}')
        self.stdout.write(f'By stage: {per_stage}')
        self.stdout.write(f'Follow-ups: {n_fu}; timeline activities: {n_act}; flagged duplicates: {n_dup}')
        self.stdout.write(f'Skipped: {len(skipped)}' + (f' (see {o["skipped_report"]})' if skipped else ''))
        if not o['commit']:
            self.stdout.write('Re-run with --commit to write.')
