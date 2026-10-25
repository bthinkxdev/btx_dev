"""Leads tools: wrap crm.modules.leads.services directly - no extraction needed, that
module already exposes clean, view-independent functions."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from crm.core import permissions
from crm.mcp import context, helpers, serialize
from crm.modules.leads import services as svc

FIELDS = ('name', 'phone', 'email', 'company', 'city', 'state', 'source', 'owner', 'priority', 'stage', 'value',
          'expected_close_date', 'next_follow_up_at', 'created_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_leads(stage: str = '', limit: int = 50) -> list[dict]:
        """List leads, optionally filtered by stage (new/contacted/nurturing/qualified/quotation_prepared/
        quotation_sent/negotiation/won/lost). Company-wide for the Business Manager role."""
        from crm.models import Lead
        user = context.current_user()
        qs = permissions.scoped(Lead.objects.all(), user, 'leads', 'view').order_by('-id')
        if stage:
            qs = qs.filter(stage=stage)
        return serialize.rows(qs, FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def get_lead(lead_id: int) -> dict:
        """Fetch one lead by id."""
        from crm.models import Lead
        user = context.current_user()
        lead = helpers.get_scoped(user, Lead, lead_id, 'leads', 'view')
        return serialize.row(lead, FIELDS)

    @helpers.sync_tool(mcp)
    def create_lead(name: str, phone: str = '', email: str = '', company: str = '', city: str = '', state: str = '',
                    source_name: str = 'Manual entry', priority: str = 'warm', value: str = '',
                    notes: str = '') -> dict:
        """Create a new lead. Provide a phone or an email (at least one). `source_name` must
        match an existing lead source name (e.g. "Manual entry", "Website form", "Referral")."""
        from crm.models import LeadSource
        user = context.current_user()
        permissions.require(user, 'leads', 'create')
        try:
            source = LeadSource.objects.get(name=source_name)
        except LeadSource.DoesNotExist:
            raise ValueError(f'Unknown lead source "{source_name}". Call list_lead_sources first.')
        lead = svc.create_lead(svc.LeadInput(
            name=name, phone=phone, email=email, company=company, city=city, state=state, notes=notes,
            source=source, priority=priority, value=Decimal(value) if value else None,
            next_follow_up_at=timezone.now() + timezone.timedelta(hours=24)), user)
        return serialize.row(lead, FIELDS)

    @helpers.sync_tool(mcp)
    def list_lead_sources() -> list[dict]:
        """List valid lead source names for create_lead()."""
        from crm.models import LeadSource
        return serialize.rows(LeadSource.objects.filter(is_active=True), ('name', 'channel'), limit=100)

    @helpers.sync_tool(mcp)
    def log_lead_activity(lead_id: int, kind: str, summary: str = '') -> dict:
        """Log a call/meeting/email/note against a lead (kind: call/meeting/email/whatsapp/note)."""
        from crm.models import Lead
        user = context.current_user()
        lead = helpers.get_scoped(user, Lead, lead_id, 'leads', 'edit')
        svc.log_activity(lead, kind=kind, summary=summary, actor=user)
        return {'ok': True}

    @helpers.sync_tool(mcp)
    def schedule_lead_follow_up(lead_id: int, due_at_iso: str, kind: str = 'call', purpose: str = '') -> dict:
        """Schedule the next follow-up for a lead. due_at_iso example: 2026-11-01T10:30:00."""
        from crm.models import Lead
        user = context.current_user()
        lead = helpers.get_scoped(user, Lead, lead_id, 'leads', 'edit')
        due = datetime.fromisoformat(due_at_iso)
        if timezone.is_naive(due):
            due = timezone.make_aware(due)
        fu = svc.schedule_follow_up(lead, kind=kind, due_at=due, assignee=lead.owner or user, purpose=purpose)
        return {'ok': True, 'follow_up_id': fu.pk}

    @helpers.sync_tool(mcp)
    def change_lead_stage(lead_id: int, to_stage: str, reason: str = '') -> dict:
        """Move a lead to a new pipeline stage (not for won/lost - use mark_lead_won/mark_lead_lost)."""
        from crm.models import Lead
        user = context.current_user()
        lead = helpers.get_scoped(user, Lead, lead_id, 'leads', 'edit')
        try:
            lead = svc.change_stage(lead, to_stage, user, reason=reason)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        return serialize.row(lead, FIELDS)

    @helpers.sync_tool(mcp)
    def mark_lead_won(lead_id: int, won_value: str, note: str = '') -> dict:
        """Mark a lead won. Needs an accepted quotation already on the lead, or a confirmed-order
        note of at least 10 characters. Creates/links a client automatically."""
        from crm.models import Lead
        user = context.current_user()
        lead = helpers.get_scoped(user, Lead, lead_id, 'leads', 'edit')
        try:
            lead = svc.mark_won(lead, user, won_value=Decimal(won_value), note=note)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        return serialize.row(lead, FIELDS)

    @helpers.sync_tool(mcp)
    def mark_lead_lost(lead_id: int, reason: str, notes: str = '', competitor: str = '') -> dict:
        """Mark a lead lost with a reason code (see list_lead_lost_reasons)."""
        from crm.models import Lead
        user = context.current_user()
        lead = helpers.get_scoped(user, Lead, lead_id, 'leads', 'edit')
        try:
            lead, _approval = svc.mark_lost(lead, user, reason=reason, competitor=competitor, notes=notes)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        return serialize.row(lead, FIELDS)
