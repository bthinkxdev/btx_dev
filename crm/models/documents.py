"""Abstract tax document + line used by quotations and invoices so the GST snapshot is identical."""
from __future__ import annotations

from decimal import Decimal

from django.db import models

from crm.core.gst import (CUSTOMER_TYPE_CHOICES, DISCOUNT_MODE_CHOICES, PRICE_MODE_CHOICES,
                          TAXABILITY_CHOICES)
from crm.core.india import STATE_CHOICES

from .base import MoneyField, RateField


class TaxDocument(models.Model):
    customer_name = models.CharField(max_length=200, blank=True)
    customer_gstin = models.CharField('Customer GSTIN', max_length=15, blank=True)
    customer_state = models.CharField(max_length=2, choices=STATE_CHOICES, blank=True)
    customer_type = models.CharField(max_length=20, choices=CUSTOMER_TYPE_CHOICES, default='unregistered')
    billing_address = models.TextField(blank=True)
    place_of_supply = models.CharField(max_length=2, choices=STATE_CHOICES, blank=True)
    supply_type = models.CharField(max_length=30, blank=True, editable=False)
    price_mode = models.CharField(max_length=12, choices=PRICE_MODE_CHOICES, default='exclusive')
    discount_mode = models.CharField(max_length=10, choices=DISCOUNT_MODE_CHOICES, blank=True)
    discount_value = MoneyField()
    discount_reason = models.CharField(max_length=300, blank=True)

    subtotal = MoneyField(editable=False)
    discount_total = MoneyField(editable=False)
    taxable_total = MoneyField(editable=False)
    cgst_total = MoneyField(editable=False)
    sgst_total = MoneyField(editable=False)
    igst_total = MoneyField(editable=False)
    tax_total = MoneyField(editable=False)
    round_off = MoneyField(editable=False)
    grand_total = MoneyField(editable=False)
    tax_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    gst_overridden = models.BooleanField(default=False, editable=False)
    gst_override_reason = models.CharField(max_length=300, blank=True, editable=False)
    gst_engine_snapshot = models.JSONField(default=dict, blank=True, editable=False,
                                           help_text='Engine output before an approved override.')

    class Meta:
        abstract = True

    @property
    def is_intra_state(self) -> bool:
        return self.supply_type == 'intra'


class TaxLine(models.Model):
    sort = models.PositiveSmallIntegerField(default=0)
    service = models.ForeignKey('crm.Service', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    description = models.CharField(max_length=500)
    sac_code = models.CharField('SAC/HSN', max_length=8, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('1'))
    unit = models.CharField(max_length=20, default='Nos')
    unit_price = MoneyField()
    discount_mode = models.CharField(max_length=10, choices=DISCOUNT_MODE_CHOICES, blank=True)
    discount_value = MoneyField()
    taxability = models.CharField(max_length=20, choices=TAXABILITY_CHOICES, default='taxable')
    tax_rate = RateField(default=Decimal('18.00'))
    catalogue_price = MoneyField(null=True, blank=True, default=None, editable=False)

    gross = MoneyField(editable=False)
    discount_amount = MoneyField(editable=False)
    taxable_value = MoneyField(editable=False)
    cgst = MoneyField(editable=False)
    sgst = MoneyField(editable=False)
    igst = MoneyField(editable=False)
    total = MoneyField(editable=False)

    class Meta:
        abstract = True
        ordering = ['sort', 'id']

    @property
    def price_changed(self) -> bool:
        return self.catalogue_price is not None and self.catalogue_price != self.unit_price

    @property
    def tax(self):
        return self.cgst + self.sgst + self.igst
