"""Clients and contacts: one central record across every module (Part 9.1)."""
from __future__ import annotations

from django.db import models
from django.db.models import Q

from crm.core.gst import CUSTOMER_TYPE_CHOICES
from crm.core.india import STATE_CHOICES, state_name

from .base import USER, ActiveQuerySet, Archivable, MoneyField, TimeStamped, Versioned

CLIENT_TYPES = [('company', 'Company'), ('proprietor', 'Proprietor'), ('individual', 'Individual'),
                ('government', 'Government')]
CLIENT_STATUSES = [('active', 'Active'), ('on_hold', 'On hold'), ('restricted', 'Restricted'), ('archived', 'Archived')]
TDS_SECTIONS = [('', 'Not applicable'), ('194C', '194C - Contractors'), ('194J', '194J - Professional / technical'),
                ('194H', '194H - Commission'), ('194I', '194I - Rent'), ('194Q', '194Q - Purchase of goods'),
                ('194O', '194O - E-commerce')]


class Client(TimeStamped, Versioned, Archivable):
    legal_name = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200)
    client_type = models.CharField(max_length=20, choices=CLIENT_TYPES, default='company')
    industry = models.CharField(max_length=100, blank=True)
    website = models.URLField(blank=True)
    phone = models.CharField(max_length=20, blank=True, db_index=True)
    email = models.EmailField(blank=True, db_index=True)
    normalized_name = models.CharField(max_length=200, db_index=True, editable=False)

    gstin = models.CharField('GSTIN', max_length=15, blank=True, db_index=True)
    pan = models.CharField('PAN', max_length=10, blank=True)
    customer_type = models.CharField(max_length=20, choices=CUSTOMER_TYPE_CHOICES, default='unregistered')
    tds_applicable = models.BooleanField('Customer deducts TDS', default=False)
    tds_section = models.CharField(max_length=10, choices=TDS_SECTIONS, blank=True)

    billing_line1 = models.CharField('Billing address', max_length=200, blank=True)
    billing_line2 = models.CharField(max_length=200, blank=True)
    billing_city = models.CharField(max_length=80, blank=True)
    billing_state = models.CharField(max_length=2, choices=STATE_CHOICES, blank=True)
    billing_pincode = models.CharField(max_length=10, blank=True)
    billing_country = models.CharField(max_length=60, default='India')
    shipping_same = models.BooleanField('Shipping address same as billing', default=True)
    shipping_line1 = models.CharField('Shipping address', max_length=200, blank=True)
    shipping_line2 = models.CharField(max_length=200, blank=True)
    shipping_city = models.CharField(max_length=80, blank=True)
    shipping_state = models.CharField(max_length=2, choices=STATE_CHOICES, blank=True)
    shipping_pincode = models.CharField(max_length=10, blank=True)

    owner = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_clients')
    credit_limit = MoneyField(null=True, blank=True, default=None)
    payment_terms_days = models.PositiveSmallIntegerField(default=15)
    dunning_paused = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=CLIENT_STATUSES, default='active', db_index=True)
    status_reason = models.CharField(max_length=300, blank=True)
    source_lead = models.ForeignKey('crm.Lead', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    merged_into = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='merged_clients')
    notes = models.TextField('Internal notes', blank=True)

    objects = ActiveQuerySet.as_manager()

    class Meta:
        ordering = ['display_name']
        constraints = [
            models.UniqueConstraint(fields=['gstin'], condition=~Q(gstin='') & Q(archived_at__isnull=True),
                                    name='crm_client_gstin_active_unique'),
        ]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        import re
        self.normalized_name = re.sub(r'[^a-z0-9]', '', (self.legal_name or '').lower())[:200]
        if self.gstin:
            self.gstin = self.gstin.strip().upper()
            if not self.billing_state:
                self.billing_state = self.gstin[:2]
        if self.pan:
            self.pan = self.pan.strip().upper()
        super().save(*args, **kwargs)

    @property
    def billing_address(self) -> str:
        parts = [self.billing_line1, self.billing_line2, f'{self.billing_city} {self.billing_pincode}'.strip(),
                 state_name(self.billing_state)]
        if self.billing_country and self.billing_country != 'India':
            parts.append(self.billing_country)
        return ', '.join(p for p in parts if p)

    @property
    def shipping_address(self) -> str:
        if self.shipping_same:
            return self.billing_address
        parts = [self.shipping_line1, self.shipping_line2, f'{self.shipping_city} {self.shipping_pincode}'.strip(),
                 state_name(self.shipping_state)]
        return ', '.join(p for p in parts if p)

    @property
    def primary_contact(self):
        return self.contacts.filter(archived_at__isnull=True).order_by('-is_primary', 'id').first()

    @property
    def billing_contact(self):
        return (self.contacts.filter(archived_at__isnull=True, is_billing=True).first()
                or self.primary_contact)

    @property
    def is_restricted(self) -> bool:
        return self.status == 'restricted'


class Contact(TimeStamped, Archivable):
    CHANNELS = [('phone', 'Phone'), ('email', 'Email'), ('whatsapp', 'WhatsApp')]
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name='contacts')
    name = models.CharField(max_length=120)
    designation = models.CharField('Role', max_length=120, blank=True)
    phone = models.CharField(max_length=20, blank=True, db_index=True)
    email = models.EmailField(blank=True, db_index=True)
    is_primary = models.BooleanField(default=False)
    is_billing = models.BooleanField('Billing contact', default=False)
    is_decision_maker = models.BooleanField(default=False)
    preferred_channel = models.CharField(max_length=10, choices=CHANNELS, default='phone')

    objects = ActiveQuerySet.as_manager()

    class Meta:
        ordering = ['-is_primary', 'name']

    def __str__(self):
        return self.name
