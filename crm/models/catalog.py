"""Service catalogue with effective-dated tax rules (Part 9.3, 14.6)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import models
from django.db.models import Q

from crm.core.gst import PRICE_MODE_CHOICES, TAXABILITY_CHOICES

from .base import MoneyField, RateField, TimeStamped, Versioned

BILLING_MODELS = [
    ('one_time', 'One-time'), ('milestone', 'Milestone'), ('monthly', 'Recurring monthly'),
    ('quarterly', 'Recurring quarterly'), ('annual', 'Recurring annual'), ('amc', 'AMC'),
    ('usage', 'Usage-based'),
]
RECURRING_MODELS = {'monthly': 1, 'quarterly': 3, 'annual': 12, 'amc': 12}
REVENUE_RECOGNITION = [('immediate', 'Immediate'), ('over_time', 'Over time (period)'), ('milestone', 'On milestone')]


class Service(TimeStamped, Versioned):
    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=40, unique=True)
    category = models.CharField(max_length=80)
    description = models.TextField()
    deliverables = models.TextField()
    sac_code = models.CharField('SAC or HSN code', max_length=8, blank=True)
    taxability = models.CharField(max_length=20, choices=TAXABILITY_CHOICES, default='taxable')
    tax_rate = RateField(default=Decimal('18.00'), help_text='Fallback when no effective-dated rule matches.')
    default_price = MoneyField()
    price_mode = models.CharField(max_length=12, choices=PRICE_MODE_CHOICES, default='exclusive')
    floor_price = MoneyField(help_text='Warning when quoted below this.')
    cost_estimate = MoneyField(help_text='Internal: estimated delivery cost per unit (cost_price field group).')
    unit = models.CharField(max_length=20, default='Nos')
    billing_model = models.CharField(max_length=20, choices=BILLING_MODELS, default='one_time')
    revenue_recognition = models.CharField(max_length=20, choices=REVENUE_RECOGNITION, default='immediate')
    estimated_hours = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal('0'))
    task_template = models.TextField(blank=True, help_text='One task title per line, used when creating a project.')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name

    def tax_for(self, on: date | None = None) -> tuple[str, Decimal]:
        """Effective-dated lookup (Part 9.3): returns (taxability, rate)."""
        from crm.core.india import today
        on = on or today()
        rule = (self.tax_rules.filter(effective_from__lte=on)
                .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on))
                .order_by('-effective_from').first())
        if rule:
            return rule.taxability, rule.rate
        return self.taxability, self.tax_rate


class ServiceTaxRule(models.Model):
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name='tax_rules')
    taxability = models.CharField(max_length=20, choices=TAXABILITY_CHOICES, default='taxable')
    rate = RateField(default=Decimal('18.00'))
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-effective_from']
        constraints = [
            models.CheckConstraint(check=Q(effective_to__isnull=True) | Q(effective_to__gte=models.F('effective_from')),
                                   name='crm_taxrule_dates'),
        ]
