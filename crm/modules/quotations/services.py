"""
Quotation service layer (Part 5.4-5.10). Status change functions here are the only way to
change a quotation's status. Rules:

* A sent version is immutable. Editing a sent quotation creates a new version; the old one is
  marked Superseded only when the new version is sent.
* The number is allocated at approval or send (never at draft), gap-free per FY, shared by versions.
* Every send stores the exact PDF sent; resending uses the stored file.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, crypto, documents, gst, india, notify, numbering, pdf, permissions
from crm.core.money import D, ZERO, q2
from crm.models import CompanySettings, FollowUp, LeadActivity, Quotation
from crm.models.quotations import (Q_ACCEPTED, Q_APPROVED, Q_AWAITING, Q_CONVERTED, Q_DRAFT, Q_EXPIRED, Q_REJECTED,
                                   Q_SENT, Q_SUPERSEDED, Q_VIEWED, Q_VOID)


class QuotationError(ValidationError):
    pass


def q_url(q) -> str:
    return reverse('crm:quotation_detail', args=[q.pk])


def _lock(q) -> Quotation:
    return Quotation.objects.select_for_update().get(pk=q.pk)


def _lead_activity(q, summary, actor=None):
    if q.lead_id:
        LeadActivity.objects.create(lead_id=q.lead_id, kind='quotation', summary=summary, actor=actor,
                                    metadata={'quotation': q.pk})


# ---------------------------------------------------------------- calculation

def recalculate(q: Quotation, save: bool = True) -> gst.TaxResult:
    company = CompanySettings.get()
    lines = list(q.lines.all())
    result = documents.compute(q, lines, company.round_off_quotation)
    documents.apply(q, lines, result, save_lines=save)
    q.advance_amount = q2(q.grand_total * D(q.advance_percent) / 100) if q.advance_percent else ZERO
    if save:
        q.save(update_fields=['place_of_supply', 'supply_type', 'subtotal', 'discount_total', 'taxable_total',
                              'cgst_total', 'sgst_total', 'igst_total', 'tax_total', 'round_off', 'grand_total',
                              'tax_snapshot', 'advance_amount'])
    return result


def approval_reasons(q: Quotation, user) -> tuple[list[str], str]:
    """Why this quotation needs approval, and at which level (Part 3.4)."""
    c = CompanySettings.get()
    reasons, level = [], 'manager'
    disc = documents.effective_discount_percent(q)
    can_approve = permissions.has_perm(user, 'quotations', 'approve')
    if disc > c.exec_discount_limit and not (can_approve and disc <= c.manager_discount_limit):
        reasons.append(f'Discount of {disc}% is above the {c.exec_discount_limit}% limit')
        if disc > c.manager_discount_limit:
            level = 'owner'
            reasons[-1] += f' (and above the manager limit of {c.manager_discount_limit}%)'
    if q.payment_terms_days > c.special_terms_days:
        reasons.append(f'Payment terms of {q.payment_terms_days} days are beyond {c.special_terms_days} days')
    if q.terms_modified:
        reasons.append('Standard terms were changed')
    below_floor = [l.description for l in q.lines.all() if l.service_id and l.service.floor_price
                   and l.unit_price < l.service.floor_price]
    if below_floor:
        reasons.append('Priced below the floor price: ' + ', '.join(below_floor)[:200])
    if q.gst_overridden:
        reasons.append('GST treatment was overridden')
        level = 'owner' if level == 'owner' else 'accountant'
    if reasons and can_approve and level == 'manager' and not q.terms_modified and not q.gst_overridden \
            and q.payment_terms_days <= c.special_terms_days and not below_floor:
        # A manager's own quotation within their discount limit does not need a second approver.
        return [], level
    return reasons, level


def send_blockers(q: Quotation) -> list[str]:
    """Validation summary at send (Part 5.6)."""
    errs = []
    if not (q.contact_email or q.contact_phone):
        errs.append('Add a client contact with an email or phone')
    lines = list(q.lines.all())
    if not lines:
        errs.append('Add at least one line item to send')
    if any(l.quantity <= 0 for l in lines):
        errs.append('Every line needs a quantity above 0')
    if any(l.unit_price < 0 for l in lines):
        errs.append('Prices cannot be negative')
    if not (q.place_of_supply or q.customer_state or q.customer_type == 'overseas'):
        errs.append('Add customer state to determine tax')
    if q.customer_type == 'registered' and not q.customer_gstin:
        errs.append('A registered (B2B) customer needs a GSTIN')
    if q.valid_until < india.today():
        errs.append('The validity date is in the past')
    ms = list(q.milestones.all())
    if ms and sum((m.percent for m in ms), Decimal('0')) != Decimal('100'):
        errs.append('Payment milestone percentages must add up to 100')
    if not q.terms.strip():
        errs.append('Select or write the terms')
    if q.client_id and q.client.is_restricted:
        errs.append('The client is restricted: new quotations cannot be sent')
    return errs


# ---------------------------------------------------------------- creation

def prefill(*, lead=None, client=None, owner) -> Quotation:
    c = CompanySettings.get()
    today = india.today()
    q = Quotation(owner=owner, lead=lead, client=client, quotation_date=today,
                  valid_until=today + timedelta(days=c.quotation_validity_days), terms=c.quotation_terms,
                  signatory_name=c.signatory_name, payment_terms_days=c.invoice_due_days)
    if client is None and lead is not None and lead.client_id:
        client = q.client = lead.client
    if client is not None:
        contact = client.billing_contact
        q.customer_name = client.legal_name
        q.customer_gstin = client.gstin
        q.customer_state = client.billing_state
        q.customer_type = client.customer_type
        q.billing_address = client.billing_address
        q.contact_name = contact.name if contact else ''
        q.contact_email = (contact.email if contact else '') or client.email
        q.contact_phone = (contact.phone if contact else '') or client.phone
        q.payment_terms_days = client.payment_terms_days
    elif lead is not None:
        q.customer_name = lead.company or lead.name
        q.customer_state = lead.state
        q.contact_name = lead.name
        q.contact_email = lead.email
        q.contact_phone = lead.phone
    q.place_of_supply = q.customer_state
    return q


def after_save(q: Quotation, actor, created: bool):
    recalculate(q)
    if created:
        audit.log('quotation.created', q, actor=actor, changes={'lead': q.lead_id, 'client': q.client_id})
        _lead_activity(q, f'Quotation draft {q.display_number} created', actor)
        if q.lead_id:
            from crm.modules.leads.services import system_stage
            if q.lead.stage == 'qualified':
                system_stage(q.lead, 'quotation_prepared', f'Quotation {q.display_number} drafted', actor)


# ---------------------------------------------------------------- approval

def submit_for_approval(q: Quotation, actor, message: str = ''):
    with transaction.atomic():
        q = _lock(q)
        if q.status != Q_DRAFT:
            raise QuotationError('Only a draft can be sent for approval.')
        recalculate(q)
        reasons, level = approval_reasons(q, actor)
        if not reasons:
            raise QuotationError('This quotation does not need approval. You can send it.')
        blockers = [b for b in send_blockers(q) if 'past' not in b]
        if blockers:
            raise QuotationError('Fix these first: ' + '; '.join(blockers) + '.')
        q.status = Q_AWAITING
        q.approval_reasons = reasons
        q.row_version += 1
        q.save(update_fields=['status', 'approval_reasons', 'row_version'])
        req = approvals.submit('quotation', q, title=f'Quotation {q.display_number} for {q.customer_display}',
                               module='quotations', requested_by=actor, level=level, amount=q.grand_total,
                               reasons=reasons,
                               before={'discount': '0%', 'payment terms': f'{CompanySettings.get().invoice_due_days} days'},
                               after={'discount': f'{documents.effective_discount_percent(q)}%',
                                      'payment terms': f'{q.payment_terms_days} days', 'total': str(q.grand_total)},
                               message=message)
        audit.log('quotation.approval_requested', q, actor=actor, changes={'reasons': reasons, 'level': level})
    return req


def _approved(req, user):
    q = Quotation.objects.select_for_update().get(pk=req.object_id)
    if q.status != Q_AWAITING:
        return
    if not q.number:
        q.number = _family_number(q)
        q.issued_at = timezone.now()
    q.status = Q_APPROVED
    q.save(update_fields=['status', 'number', 'issued_at'])
    _lead_activity(q, f'Quotation {q.display_number} approved by {user.get_full_name() or user}', user)
    audit.log('quotation.approved', q, actor=user)


def _rejected(req, user):
    q = Quotation.objects.select_for_update().get(pk=req.object_id)
    if q.status == Q_AWAITING:
        q.status = Q_DRAFT
        q.save(update_fields=['status'])
        audit.log('quotation.returned_to_draft', q, actor=user, reason=req.comment)


approvals.register('quotation', _approved, _rejected)


def cancel_approval(q: Quotation, actor):
    req = approvals.pending_for(q, 'quotation')
    if req:
        approvals.cancel(req, actor, 'Requester reopened the draft to edit it')
    else:
        with transaction.atomic():
            q = _lock(q)
            if q.status == Q_AWAITING:
                q.status = Q_DRAFT
                q.save(update_fields=['status'])


def _family_number(q: Quotation) -> str:
    root = q.root
    if root.number:
        return root.number
    for v in q.all_versions():
        if v.number:
            return v.number
    return numbering.allocate('QT', q.quotation_date)


# ---------------------------------------------------------------- share link

def share_token(q: Quotation) -> str:
    if not q.share_nonce:
        raise QuotationError('This quotation has no share link yet.')
    return crypto.token_from_nonce('quotation', q.share_nonce)


def share_url(q: Quotation, request=None) -> str:
    from django.conf import settings
    path = reverse('crm:public_quotation', args=[share_token(q)])
    return request.build_absolute_uri(path) if request is not None else settings.SITE_BASE_URL + path


def _ensure_share(q: Quotation):
    if not q.share_nonce:
        q.share_nonce = crypto.new_nonce()
        q.share_token_hash = crypto.token_hash(crypto.token_from_nonce('quotation', q.share_nonce))


def by_token(token: str):
    return Quotation.objects.filter(share_token_hash=crypto.token_hash(token)).select_related('client', 'lead').first()


# ---------------------------------------------------------------- send (5.6 / 5.8)

def _content_hash(q: Quotation) -> str:
    data = {
        'number': q.number, 'version': q.version, 'total': str(q.grand_total), 'snapshot': q.tax_snapshot,
        'lines': [(l.description, str(l.quantity), str(l.unit_price), str(l.tax_rate), str(l.total)) for l in q.lines.all()],
        'terms': q.terms, 'valid_until': q.valid_until.isoformat(),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def send(q: Quotation, actor, *, to: str = '', cc: list[str] | None = None, subject: str = '', message: str = '',
         channel: str = 'email', request=None) -> Quotation:
    with transaction.atomic():
        q = _lock(q)
        if q.status == Q_AWAITING:
            raise QuotationError('Awaiting approval — it can be sent once approved.')
        if q.status not in (Q_DRAFT, Q_APPROVED):
            raise QuotationError('This version was already sent. Create a new version to change it.')
        recalculate(q)
        blockers = send_blockers(q)
        if blockers:
            raise QuotationError('Before sending: ' + '; '.join(blockers) + '.')
        if q.status == Q_DRAFT:
            reasons, _ = approval_reasons(q, actor)
            if reasons:
                raise QuotationError('Needs approval first: ' + '; '.join(reasons) + '.')
        if channel == 'email' and not to:
            raise QuotationError('Enter the email address to send to.')
        if not q.number:
            q.number = _family_number(q)
            q.issued_at = timezone.now()
        _ensure_share(q)
        q.status = Q_SENT
        q.sent_at = timezone.now()
        q.sent_to = (to or channel)[:300]
        q.content_hash = _content_hash(q)
        data = pdf.quotation_pdf(q)
        q.pdf_sha256 = pdf.sha256(data)
        q.pdf_file.save(f'{q.number.replace("/", "-")}-v{q.version}.pdf', ContentFile(data), save=False)
        q.row_version += 1
        q.save()
        # Older versions become Superseded only now that the new version is sent (5.4).
        for v in q.all_versions().exclude(pk=q.pk).filter(status__in=[Q_DRAFT, Q_APPROVED, Q_SENT, Q_VIEWED, Q_EXPIRED, Q_REJECTED]):
            v.status = Q_SUPERSEDED
            v.save(update_fields=['status'])
        link = share_url(q, request)
        if channel == 'email':
            from crm.models import MessageTemplate
            tpl = MessageTemplate.objects.filter(code='quotation_send', is_active=True).order_by('-version').first()
            company = CompanySettings.get()
            ctx = {'name': q.contact_name or q.customer_display, 'number': q.display_number,
                   'total': pdf._money(q.grand_total), 'link': link, 'valid_until': q.valid_until.strftime('%d-%b-%Y'),
                   'sender': actor.get_full_name() or actor.username, 'company': company.legal_name}
            subj = subject or (notify.render(tpl.subject, ctx) if tpl else f'Quotation {q.display_number}')
            body = (message + '\n\n' if message else '') + (notify.render(tpl.body, ctx) if tpl else link)
            notify.send_email(to, subj, body, cc=cc, sent_by=actor, obj=q, template_code='quotation_send',
                              attachments=[(f'{q.number.replace("/", "-")}.pdf', data, 'application/pdf')])
        _lead_activity(q, f'Quotation {q.display_number} sent ({channel}) for {pdf._money(q.grand_total)}', actor)
        audit.log('quotation.sent', q, actor=actor, changes={'to': to, 'channel': channel, 'pdf_sha256': q.pdf_sha256})
        if q.lead_id:
            from crm.modules.leads.services import system_stage
            system_stage(q.lead, 'quotation_sent', f'Quotation {q.display_number} sent', actor)
            # Follow-up tasks at +1 day and +3 days (5.8).
            owner = q.lead.owner or q.owner
            for days in (1, 3):
                FollowUp.objects.create(lead_id=q.lead_id, kind='call', due_at=india.next_working_day_at(11, 0, timezone.now() + timedelta(days=days - 1)),
                                        assignee=owner, purpose=f'Chase quotation {q.display_number} (day {days})', created_by=actor)
            from crm.modules.leads.services import sync_next_action
            lead = q.lead
            sync_next_action(lead)
            lead.save(update_fields=['next_follow_up_at', 'next_action'])
            q.lead.value = q.taxable_total
            q.lead.save(update_fields=['value'])
    return q


def resend(q: Quotation, actor, to: str, cc=None, message: str = ''):
    """Resending uses the stored PDF (5.9 rule 5)."""
    if q.status not in (Q_SENT, Q_VIEWED, Q_ACCEPTED) or not q.pdf_file:
        raise QuotationError('Only a sent quotation can be resent.')
    q.pdf_file.open('rb')
    data = q.pdf_file.read()
    q.pdf_file.close()
    if pdf.sha256(data) != q.pdf_sha256:
        raise QuotationError('The stored PDF does not match its fingerprint. Contact the administrator.')
    notify.send_email(to, f'Quotation {q.display_number}', (message + '\n\n' if message else '') + share_url(q), cc=cc,
                      sent_by=actor, obj=q, template_code='quotation_send',
                      attachments=[(f'{q.number.replace("/", "-")}.pdf', data, 'application/pdf')])
    audit.log('quotation.resent', q, actor=actor, changes={'to': to})


# ---------------------------------------------------------------- customer view (5.8)

def record_view(q: Quotation, ip: str | None, user_agent: str):
    from crm.models import QuotationView
    with transaction.atomic():
        q = _lock(q)
        QuotationView.objects.create(quotation=q, ip_hash=crypto.hash_ip(ip), user_agent=user_agent[:300])
        first = q.first_viewed_at is None
        q.view_count += 1
        q.last_viewed_at = timezone.now()
        if first:
            q.first_viewed_at = q.last_viewed_at
        if q.status == Q_SENT:
            q.status = Q_VIEWED
        q.save(update_fields=['view_count', 'last_viewed_at', 'first_viewed_at', 'status'])
    if first:
        _lead_activity(q, f'Customer viewed {q.display_number}')
        notify.notify([q.owner], f'{q.contact_name or q.customer_display} viewed {q.display_number}',
                      body='Call within 1 hour while it is fresh.', url=q_url(q), event_type='quotation_viewed', level='success')
        if q.lead_id:
            FollowUp.objects.create(lead_id=q.lead_id, kind='call', due_at=timezone.now() + timedelta(hours=1),
                                    assignee=q.lead.owner or q.owner, purpose=f'Customer viewed {q.display_number}: call now')
            from crm.modules.leads.services import sync_next_action
            sync_next_action(q.lead)
            q.lead.save(update_fields=['next_follow_up_at', 'next_action'])
    if q.view_count >= 3 and q.hot_flagged_at is None and q.status == Q_VIEWED:
        Quotation.objects.filter(pk=q.pk).update(hot_flagged_at=timezone.now())
        if q.lead_id:
            from crm.models import Lead
            Lead.objects.filter(pk=q.lead_id).update(priority='hot')
        notify.notify([q.owner, notify.manager_of(q.owner)], f'Hot: {q.display_number} viewed {q.view_count} times without a reply',
                      url=q_url(q), event_type='quotation_hot', level='warning')


# ---------------------------------------------------------------- decisions

def accept(q: Quotation, actor, *, accepted_by: str, evidence: str, note: str = '', online: bool = False):
    if evidence == 'verbal' and len(note.strip()) < 10:
        raise QuotationError('Describe the verbal acceptance (who, when) in at least 10 characters.')
    if not accepted_by.strip():
        raise QuotationError('Enter who accepted the quotation.')
    with transaction.atomic():
        q = _lock(q)
        if q.status not in (Q_SENT, Q_VIEWED, Q_APPROVED):
            raise QuotationError('Only a sent or viewed quotation can be accepted.')
        if q.is_expired_by_date:
            raise QuotationError('This quotation has expired. Create a new version first.')
        q.status = Q_ACCEPTED
        q.accepted_at = timezone.now()
        q.accepted_by_name = accepted_by[:120]
        q.acceptance_evidence = evidence
        q.acceptance_note = note
        q.save(update_fields=['status', 'accepted_at', 'accepted_by_name', 'acceptance_evidence', 'acceptance_note'])
        _lead_activity(q, f'Quotation {q.display_number} accepted by {accepted_by} ({evidence})', actor)
        audit.log('quotation.accepted', q, actor=actor, actor_type='customer' if online else '',
                  changes={'by': accepted_by, 'evidence': evidence})
    notify.notify([q.owner], f'Accepted: {q.display_number} ({pdf._money(q.grand_total)})',
                  body='Mark the lead won and collect the advance.', url=q_url(q), event_type='quotation_accepted', level='success')
    return q


def reject(q: Quotation, actor, *, reason: str, competitor: str = '', notes: str = ''):
    if not reason:
        raise QuotationError('Choose a rejection reason.')
    with transaction.atomic():
        q = _lock(q)
        if q.status not in (Q_SENT, Q_VIEWED, Q_APPROVED):
            raise QuotationError('Only a sent quotation can be rejected.')
        q.status = Q_REJECTED
        q.rejected_at = timezone.now()
        q.rejection_reason = reason
        q.rejection_notes = (notes + (f' Competitor: {competitor}' if competitor else '')).strip()
        q.save(update_fields=['status', 'rejected_at', 'rejection_reason', 'rejection_notes'])
        _lead_activity(q, f'Quotation {q.display_number} rejected: {q.get_rejection_reason_display()}', actor)
        audit.log('quotation.rejected', q, actor=actor, changes={'reason': reason})
    return q


def void(q: Quotation, actor, reason: str):
    if len(reason.strip()) < 10:
        raise QuotationError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        q = _lock(q)
        if q.status not in (Q_DRAFT, Q_APPROVED, Q_AWAITING):
            raise QuotationError('Only an unsent quotation can be voided.')
        if q.status == Q_AWAITING:
            req = approvals.pending_for(q, 'quotation')
            if req:
                approvals.cancel(req, actor, 'Quotation voided', run_reject_handler=False)
        q.status = Q_VOID
        q.voided_at = timezone.now()
        q.void_reason = reason
        q.save(update_fields=['status', 'voided_at', 'void_reason'])
        audit.log('quotation.voided', q, actor=actor, reason=reason)
    return q


def _copy_into(src: Quotation, dst: Quotation):
    for ln in src.lines.all():
        ln.pk = None
        ln.quotation = dst
        ln.save()
    for m in src.milestones.all():
        m.pk = None
        m.quotation = dst
        m.save()


COPY_FIELDS = ['lead', 'client', 'contact_name', 'contact_email', 'contact_phone', 'owner', 'title', 'reference',
               'customer_name', 'customer_gstin', 'customer_state', 'customer_type', 'billing_address', 'place_of_supply',
               'price_mode', 'discount_mode', 'discount_value', 'discount_reason', 'advance_percent', 'balance_terms',
               'payment_terms_days', 'late_fee_text', 'deliverables', 'exclusions', 'assumptions', 'timeline',
               'revision_limits', 'terms', 'terms_modified', 'signatory_name', 'internal_notes', 'cost_estimate']


def new_version(q: Quotation, actor) -> Quotation:
    with transaction.atomic():
        q = _lock(q)
        root = q.root
        latest = q.all_versions().order_by('-version').first()
        if latest.status in (Q_DRAFT, Q_AWAITING):
            raise QuotationError(f'Version {latest.version} is still a draft. Edit that instead.')
        if q.status in (Q_CONVERTED, Q_VOID):
            raise QuotationError('A converted or void quotation cannot get a new version.')
        c = CompanySettings.get()
        today = india.today()
        nv = Quotation(family=root, version=latest.version + 1, number=root.number or q.number, status=Q_DRAFT,
                       quotation_date=today, valid_until=today + timedelta(days=c.quotation_validity_days),
                       created_by=actor, updated_by=actor)
        for f in COPY_FIELDS:
            setattr(nv, f, getattr(q, f))
        nv.save()
        _copy_into(q, nv)
        recalculate(nv)
        audit.log('quotation.version_created', nv, actor=actor, changes={'from_version': q.version})
        if q.lead_id and q.lead.stage == 'quotation_sent':
            from crm.modules.leads.services import system_stage
            system_stage(q.lead, 'negotiation', f'Revising quotation (v{nv.version})', actor)
    return nv


def duplicate(q: Quotation, actor) -> Quotation:
    c = CompanySettings.get()
    today = india.today()
    with transaction.atomic():
        nq = Quotation(status=Q_DRAFT, quotation_date=today, valid_until=today + timedelta(days=c.quotation_validity_days),
                       created_by=actor, updated_by=actor)
        for f in COPY_FIELDS:
            setattr(nq, f, getattr(q, f))
        nq.owner = actor if not permissions.has_perm(actor, 'quotations', 'approve') else q.owner
        nq.save()
        _copy_into(q, nq)
        recalculate(nq)
        audit.log('quotation.duplicated', nq, actor=actor, changes={'from': q.pk})
    return nq


def mark_converted(q: Quotation, actor, target: str):
    with transaction.atomic():
        q = _lock(q)
        if q.status != Q_ACCEPTED:
            return q
        q.status = Q_CONVERTED
        q.converted_at = timezone.now()
        q.save(update_fields=['status', 'converted_at'])
        audit.log('quotation.converted', q, actor=actor, changes={'target': target})
    return q


def compare(a: Quotation, b: Quotation) -> dict:
    def lines(q):
        return [(l.description, l.quantity, l.unit_price, l.tax_rate, l.total) for l in q.lines.all()]
    la, lb = lines(a), lines(b)
    rows = []
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else None
        y = lb[i] if i < len(lb) else None
        rows.append({'a': x, 'b': y, 'changed': x != y})
    fields = []
    for f, label in (('grand_total', 'Grand total'), ('discount_total', 'Discount'), ('valid_until', 'Valid until'),
                     ('advance_percent', 'Advance %'), ('payment_terms_days', 'Payment terms (days)'), ('terms', 'Terms')):
        va, vb = getattr(a, f), getattr(b, f)
        fields.append({'label': label, 'a': va, 'b': vb, 'changed': va != vb})
    return {'lines': rows, 'fields': fields}


# ---------------------------------------------------------------- GST override (6.3)

def request_gst_override(q: Quotation, actor, *, taxability: str, rate: Decimal, reason: str):
    """Non-default tax treatment needs a reason and Accountant/Owner approval."""
    if not permissions.has_perm(actor, 'quotations', 'override') and not permissions.has_perm(actor, 'invoices', 'override'):
        permissions.denied(actor, 'quotations', 'override')
    if len(reason.strip()) < 10:
        raise QuotationError('Give a reason of at least 10 characters for the override.')
    with transaction.atomic():
        q = _lock(q)
        if q.status != Q_DRAFT:
            raise QuotationError('Tax treatment can only change on a draft.')
        q.gst_engine_snapshot = q.tax_snapshot
        for ln in q.lines.all():
            ln.taxability = taxability
            ln.tax_rate = D(rate)
            ln.save(update_fields=['taxability', 'tax_rate'])
        q.gst_overridden = True
        q.gst_override_reason = reason
        q.save(update_fields=['gst_engine_snapshot', 'gst_overridden', 'gst_override_reason'])
        recalculate(q)
        audit.log('quotation.gst_override', q, actor=actor,
                  changes={'engine': q.gst_engine_snapshot.get('tax_total'), 'final': str(q.tax_total),
                           'taxability': taxability, 'rate': str(rate)}, reason=reason)
    return q
