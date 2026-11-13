"""Webinar follow-up (CRM side).

Structure, from the public site inwards:

    pages.Webinar              the event                       (what is on sale)
    pages.WebinarRegistration  one person's registration       (public record: identity, payment, ad source)
        |  one-to-one
    crm.WebinarLead            the CRM's follow-up record      (stage, owner, history) -- shown only in the Webinar
        |                                                       section, never in the main Leads list
        |  optional foreign keys, set when someone is qualified and moved on
        +--> crm.Lead          the sales pipeline  (quotation, follow-ups, won)
        +--> crm.Client        the customer        (billing, contracts, onboarding projects)

Keeping the public registration separate from the CRM record means payments stay untouched by sales work, and a
registrant becomes a Lead or Client only on purpose."""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from .base import USER, TimeStamped

STAGE_REGISTERED = 'registered'
STAGE_CONTACTED = 'contacted'
STAGE_INTERESTED = 'interested'
STAGE_QUALIFIED = 'qualified'
STAGE_NOT_INTERESTED = 'not_interested'
STAGE_CONVERTED = 'converted'
WEBINAR_STAGES = [
    (STAGE_REGISTERED, 'Registered'), (STAGE_CONTACTED, 'Contacted'), (STAGE_INTERESTED, 'Interested'),
    (STAGE_QUALIFIED, 'Qualified'), (STAGE_NOT_INTERESTED, 'Not interested'), (STAGE_CONVERTED, 'Moved to CRM'),
]


class WebinarLead(TimeStamped):
    registration = models.OneToOneField('pages.WebinarRegistration', on_delete=models.PROTECT, related_name='crm')
    stage = models.CharField(max_length=16, choices=WEBINAR_STAGES, default=STAGE_REGISTERED, db_index=True)
    owner = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='webinar_leads')
    next_follow_up_at = models.DateTimeField(null=True, blank=True, db_index=True)
    qualified_at = models.DateTimeField(null=True, blank=True)
    # Set when the person is moved into the main CRM. Both stay empty for most registrants.
    lead = models.ForeignKey('crm.Lead', null=True, blank=True, on_delete=models.PROTECT, related_name='webinar_leads')
    client = models.ForeignKey('crm.Client', null=True, blank=True, on_delete=models.PROTECT, related_name='webinar_leads')
    converted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['owner', 'stage']), models.Index(fields=['stage', 'next_follow_up_at'])]

    def __str__(self) -> str:
        return f'{self.registration.name} ({self.get_stage_display()})'

    @property
    def is_converted(self) -> bool:
        return bool(self.lead_id or self.client_id)


class WebinarLeadActivity(models.Model):
    """Append-only history of what happened to a registrant: notes, calls, stage changes, hand-offs."""
    KINDS = [('note', 'Note'), ('call', 'Call'), ('whatsapp', 'WhatsApp'), ('email', 'Email'), ('stage', 'Stage change'),
             ('assigned', 'Assigned'), ('paid', 'Payment'), ('converted', 'Moved to CRM')]
    webinar_lead = models.ForeignKey(WebinarLead, on_delete=models.CASCADE, related_name='activities')
    kind = models.CharField(max_length=12, choices=KINDS, default='note')
    text = models.TextField(blank=True)
    actor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self) -> str:
        return f'{self.get_kind_display()}: {self.text[:40]}'
