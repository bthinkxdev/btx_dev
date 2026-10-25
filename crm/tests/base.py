"""Shared fixtures for CRM tests: one user per role, a service, a client and an accepted quotation."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from crm.core import bootstrap, india, permissions

PASSWORD = 'Test-Password-2026!'

ROLE_USERS = {
    'owner': 'owner', 'manager': 'sales_manager', 'sales': 'sales_executive', 'sales_b': 'sales_executive',
    'finance': 'finance_executive', 'finance2': 'finance_executive', 'accountant': 'accountant', 'hr': 'hr_manager',
    'pm': 'project_manager', 'dev': 'team_member', 'auditor': 'auditor',
}


@override_settings(CRM_SYNC_EMAIL=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   ALLOWED_HOSTS=['testserver'])
class CrmTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        from crm.models import Client, CompanySettings, Contact, Role, Service, Team, UserProfile
        bootstrap.run()
        User = get_user_model()
        cls.u = {}
        team = Team.objects.create(name='Sales Test')
        for key, role in ROLE_USERS.items():
            user = User.objects.create_user(key, f'{key}@example.test', PASSWORD, first_name=key.capitalize())
            p, _ = UserProfile.objects.get_or_create(user=user)
            p.roles.set(Role.objects.filter(code=role))
            if role in ('sales_executive', 'sales_manager'):
                p.team = team
            p.save()
            cls.u[key] = user
        team.manager = cls.u['manager']
        team.save()
        c = CompanySettings.get()
        c.gateway_provider = 'sandbox'
        c.gateway_live = False
        c.manual_payment_proof_above = Decimal('10000000')
        c.gstin = '32ABCDE1234F1Z5' if india.validate_gstin('32ABCDE1234F1Z5') is None else ''
        c.save()
        cls.service = Service.objects.create(name='Online Store', code='online-store', category='Web', description='x',
                                             deliverables='x', default_price=Decimal('10000'), sac_code='998314',
                                             floor_price=Decimal('8000'))
        cls.client_obj = Client.objects.create(legal_name='Acme Kerala Pvt Ltd', display_name='Acme', owner=cls.u['sales'],
                                               billing_state='32', email='acme@example.test', phone='+919876543210')
        Contact.objects.create(client=cls.client_obj, name='Anu', email='anu@example.test', phone='+919876543210',
                               is_primary=True, is_billing=True)

    def setUp(self):
        for user in self.u.values():
            permissions.clear_cache(user)

    def login(self, key):
        self.client.force_login(self.u[key])
        return self.u[key]

    # ------------------------------------------------------------ builders
    def make_quotation(self, price=Decimal('10000'), accepted=True, advance=0):
        from crm.models import Quotation, QuotationLine
        from crm.modules.quotations import services as qs
        owner = self.u['sales']
        q = qs.prefill(client=self.client_obj, owner=owner)
        q.advance_percent = advance
        q.save()
        QuotationLine.objects.create(quotation=q, service=self.service, description='Store', sac_code='998314', quantity=1,
                                     unit_price=price, tax_rate=Decimal('18'))
        qs.after_save(q, owner, created=True)
        qs.recalculate(q)
        with self.captureOnCommitCallbacks(execute=True):
            qs.send(q, owner, channel='link')
        q.refresh_from_db()
        if accepted:
            qs.accept(q, owner, accepted_by='Anu', evidence='email')
            q.refresh_from_db()
        return Quotation.objects.get(pk=q.pk)

    def make_invoice(self, price=Decimal('10000'), issue=True, quotation=None):
        from crm.models import InvoiceLine
        from crm.modules.billing import services as bs
        inv = bs.prefill(client=self.client_obj, owner=self.u['sales'], quotation=quotation)
        inv.save()
        InvoiceLine.objects.create(invoice=inv, service=self.service, description='Store', sac_code='998314', quantity=1,
                                   unit_price=price, tax_rate=Decimal('18'))
        bs.recalculate(inv)
        if issue:
            with self.captureOnCommitCallbacks(execute=True):
                inv = bs.issue(inv, self.u['finance'])
        inv.refresh_from_db()
        return inv

    def journal_balanced(self):
        from django.db.models import Sum
        from crm.models import JournalLine
        agg = JournalLine.objects.filter(posted=True).aggregate(d=Sum('debit'), c=Sum('credit'))
        return (agg['d'] or 0) == (agg['c'] or 0)

    def balance(self, code, **dims):
        from crm.core import posting
        return posting.balance(posting.account(code), **dims)


def days(n):
    return timedelta(days=n)
