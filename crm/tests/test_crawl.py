"""Every page, every role: no request may ever end in a server error.

A realistic dataset is built through the real services (quotation, invoice, payments,
contract, project, expenses, HR, payroll, journals, bank lines), then every URL in the app
is opened with GET as each role. Pages a role may not use must answer 403/404/405/302,
never 500.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from crm import urls as crm_urls
from crm.core import approvals, india, posting
from crm.modules.reports import catalogue

from .base import CrmTestCase

SKIP_PARAMS = {'action', 'token', 'order', 'contact_id', 'rule_id', 'source_key', 'model_name', 'provider'}
SLUGS = {'key': ['owner'], 'slug': [r.slug for r in catalogue.REPORTS]}
# url-name prefix -> dataset key (first match wins)
PK_MAP = [
    ('notification', 'notification'), ('approval', 'approval'), ('attachment', 'attachment'), ('saved_filter', 'none'),
    ('lead_import', 'none'), ('assignment', 'none'), ('follow_up', 'followup'), ('lead', 'lead'), ('source', 'source'),
    ('campaign', 'none'), ('inbox', 'none'), ('duplicate', 'none'), ('client', 'client'), ('contact', 'client'),
    ('service', 'service'), ('quotation', 'quotation'), ('link', 'link'), ('receipt', 'payment'), ('allocate', 'payment'),
    ('payment', 'payment'), ('refund', 'none'), ('event', 'event'), ('invoice', 'invoice'), ('credit_note', 'credit_note'),
    ('tds', 'none'), ('account', 'account'), ('journal', 'journal'), ('period', 'period'), ('reconciliation', 'none'),
    ('contract', 'contract'), ('project', 'project'), ('task', 'task'), ('blocker', 'blocker'), ('ticket', 'ticket'),
    ('expense', 'expense'), ('vendor', 'vendor'), ('employee', 'employee'), ('payslip', 'payslip'), ('payroll', 'payroll'),
    ('report_schedule', 'none'), ('settings_series', 'series'), ('settings_category', 'category'), ('user', 'user'),
    ('role', 'role'), ('template', 'template'), ('delivery', 'none'), ('security', 'profile'),
]


@override_settings(DEBUG=False)
class CrawlTest(CrmTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls._build()

    @classmethod
    def _build(cls):
        from crm.models import (Account, ApprovalRequest, BankAccount, Blocker, ContractLine, Contract, Employee, Expense,
                                ExpenseCategory, FollowUp, GatewayEvent, Lead, LeadSource, MessageTemplate, Milestone, Notification,
                                Period, Role, SalaryComponent, SalaryStructure, SalaryStructureLine, SequenceSeries, Task,
                                UserProfile, Vendor)
        from crm.modules.accounting import banking
        from crm.modules.accounting import services as acc
        from crm.modules.billing import services as bs
        from crm.modules.contracts import services as cs
        from crm.modules.expenses import services as es
        from crm.modules.payments import services as pay
        from crm.modules.payroll import runs
        from crm.modules.projects import services as ps
        from crm.modules.quotations import services as qsvc
        from crm.models import Quotation, QuotationLine
        u = cls.u
        d = {}
        src = LeadSource.objects.first() or LeadSource.objects.create(name='Website', kind='website')
        d['source'] = src.pk
        lead = Lead.objects.create(name='Crawl lead', phone='+919800000001', source=src, owner=u['sales'], stage='qualified',
                                   value=Decimal('80000'), probability=40, next_follow_up_at=timezone.now() + timedelta(days=1),
                                   expected_close_date=india.today() + timedelta(days=20))
        d['lead'] = lead.pk
        d['followup'] = FollowUp.objects.create(lead=lead, kind='call', due_at=timezone.now() + timedelta(days=1),
                                                assignee=u['sales'], purpose='Call back', created_by=u['sales']).pk
        Lead.objects.create(name='Unassigned', email='new@example.test', source=src, stage='new',
                            next_follow_up_at=timezone.now() + timedelta(hours=1))
        # Quotation -> invoice -> manual payment (maker-checker)
        q = qsvc.prefill(client=cls.client_obj, owner=u['sales'])
        q.save()
        QuotationLine.objects.create(quotation=q, service=cls.service, description='Store', sac_code='998314', quantity=1,
                                     unit_price=Decimal('50000'), tax_rate=Decimal('18'))
        qsvc.after_save(q, u['sales'], created=True)
        qsvc.recalculate(q)
        qsvc.send(q, u['sales'], channel='link')
        q = Quotation.objects.get(pk=q.pk)
        qsvc.accept(q, u['sales'], accepted_by='Anu', evidence='email')
        d['quotation'] = q.pk
        link = pay.create_link(Quotation.objects.get(pk=q.pk), u['sales'], purpose='full', amount=Decimal('1000'), description='Advance',
                               expires_at=timezone.now() + timedelta(days=5))
        d['link'] = link.pk
        inv = bs.prefill(client=cls.client_obj, owner=u['sales'])
        inv.save()
        from crm.models import InvoiceLine
        InvoiceLine.objects.create(invoice=inv, service=cls.service, description='Store', sac_code='998314', quantity=1,
                                   unit_price=Decimal('20000'), tax_rate=Decimal('18'))
        bs.recalculate(inv)
        inv = bs.issue(inv, u['finance'])
        d['invoice'] = inv.pk
        p = pay.record_manual(u['finance'], client=cls.client_obj, invoice=inv, amount=Decimal('10000'), payment_date=india.today(),
                              method='upi', reference_number='UTR-CRAWL-1')
        pay.verify_manual(p, u['finance2'])
        d['payment'] = p.pk
        inv.refresh_from_db()
        cn = bs.create_credit_note(inv, u['finance'], taxable=Decimal('1000'), reason='discount', notes='Discount agreed after delivery')
        d['credit_note'] = cn.pk
        d['event'] = GatewayEvent.objects.create(provider='sandbox', event_id='crawl-1', event_type='payment', payload={},
                                                 status='failed').pk
        # Contract
        c = Contract.objects.create(client=cls.client_obj, owner=u['manager'], title='SEO retainer', start_date=india.today(),
                                    end_date=india.add_months(india.today(), 12) - timedelta(days=1))
        ContractLine.objects.create(contract=c, service=cls.service, description='SEO', unit_price=Decimal('12000'),
                                    billing_model='monthly', start_date=india.today(), sac_code='998365')
        d['contract'] = cs.activate(c, u['manager']).pk
        # Project, task, milestone, blocker, ticket, timesheet
        pr = ps.create_project(u['pm'], client=cls.client_obj, name='Store build', manager=u['pm'], start_date=india.today() - timedelta(days=10),
                               due_date=india.today() + timedelta(days=30))
        ps.add_member(pr, u['pm'], user=u['dev'], allocation_percent=50)
        d['project'] = pr.pk
        t = Task.objects.create(project=pr, title='Checkout page', assignee=u['dev'], due_date=india.today() - timedelta(days=1))
        d['task'] = t.pk
        Milestone.objects.create(project=pr, name='Design sign-off', due_date=india.today() + timedelta(days=2), amount=Decimal('10000'))
        d['blocker'] = Blocker.objects.create(task=t, description='Waiting for logo', owner=u['pm']).pk
        d['ticket'] = ps.create_ticket(u['pm'], client=cls.client_obj, subject='Login issue', assignee=u['dev'], project=pr).pk
        from crm.models import TimeEntry
        TimeEntry.objects.create(user=u['dev'], project=pr, task=t, entry_date=india.today(), hours=Decimal('6'))
        # Expense and vendor
        vendor = Vendor.objects.create(name='Paper Co', state='32')
        d['vendor'] = vendor.pk
        cat = ExpenseCategory.objects.get(name='Office and miscellaneous')
        d['category'] = cat.pk
        e = Expense(kind='vendor_bill', vendor=vendor, category=cat, description='Printer paper', bill_date=india.today(), bill_number='B-1',
                    amount_before_tax=Decimal('5000'), cgst=Decimal('0'), sgst=Decimal('0'), itc_eligibility='na', created_by=u['finance'])
        es.recompute(e)
        e.save()
        d['expense'] = e.pk
        # HR and payroll
        comps = {x.code: x for x in SalaryComponent.objects.all()}
        for key, code in (('dev', 'E1'), ('sales', 'E2')):
            emp = Employee.objects.create(user=u[key], code=code, name=u[key].first_name, work_email=f'{key}@co.test',
                                          joining_date=date(2025, 1, 1), status='active', bank_account_number='1234567890', bank_ifsc='HDFC0000001')
            s = SalaryStructure.objects.create(employee=emp, effective_from=date(2025, 1, 1))
            SalaryStructureLine.objects.create(structure=s, component=comps['basic'], value=Decimal('30000'))
            d.setdefault('employee', emp.pk)
        run = runs.create_run(india.add_months(india.month_start(india.today()), -1), u['hr'])
        d['payroll'] = run.pk
        d['payslip'] = run.payslips.first().pk
        # Accounting and banking
        je = acc.create_manual_journal(u['accountant'], entry_date=india.today(), narration='Capital introduced',
                                       lines=[posting.Line('BANK_PRIMARY', debit=Decimal('5000')), posting.Line(posting.OWNER_CAPITAL, credit=Decimal('5000'))])
        d['journal'] = je.pk
        d['account'] = Account.objects.first().pk
        d['period'] = acc.get_period(india.today().year, india.today().month).pk
        bank = BankAccount.objects.first()
        csv = ('Date,Narration,Ref No,Withdrawal,Deposit,Balance\n'
               f'{india.today():%d-%m-%Y},NEFT ACME UTR-CRAWL-1,UTR-CRAWL-1,,10000.00,10000.00\n')
        banking.import_statement(bank, SimpleUploadedFile('s.csv', csv.encode(), content_type='text/csv'), u['finance'])
        # Cross-cutting
        req = approvals.submit('lead_lost', Lead.objects.get(pk=lead.pk), title='Mark lost', module='leads', requested_by=u['sales'], level='manager')
        d['approval'] = req.pk
        d['notification'] = Notification.objects.create(user=u['owner'], event_type='info', title='Hello').pk
        d['user'] = u['sales'].pk
        d['role'] = Role.objects.first().pk
        d['template'] = MessageTemplate.objects.first().pk
        d['series'] = SequenceSeries.objects.first().pk
        d['profile'] = UserProfile.objects.get(user=u['sales']).pk
        d['client'] = cls.client_obj.pk
        d['service'] = cls.service.pk
        d['attachment'] = None
        d['none'] = None
        assert ApprovalRequest.objects.filter(pk=req.pk).exists()
        assert Period.objects.exists()
        cls.data = d

    def _urls(self):
        out = []
        for p in crm_urls.urlpatterns:
            conv = set(p.pattern.converters)
            if conv & SKIP_PARAMS:
                continue
            if not conv:
                out.append(reverse(f'crm:{p.name}'))
                continue
            kwargs_options = [{}]
            for name in conv:
                if name in SLUGS:
                    kwargs_options = [dict(k, **{name: v}) for k in kwargs_options for v in SLUGS[name]]
                elif name == 'pk':
                    key = next((v for prefix, v in PK_MAP if p.name.startswith(prefix)), 'none')
                    pk = self.data.get(key)
                    if pk is None:
                        kwargs_options = []
                        break
                    kwargs_options = [dict(k, pk=pk) for k in kwargs_options]
                else:
                    kwargs_options = []
                    break
            out += [reverse(f'crm:{p.name}', kwargs=k) for k in kwargs_options]
        return out

    EXTRA = ['?tab=tasks', '?tab=milestones', '?tab=time', '?tab=activity', '?tab=payroll', '?tab=attendance', '?tab=leave',
             '?export=csv', '?view=board', '?preset=fy', '?quick=overdue', '?tab=log', '?status=failed']

    def test_no_page_errors_for_any_role(self):
        urls = self._urls()
        self.assertGreater(len(urls), 150)
        failures = []
        self.client.raise_request_exception = False
        for key in self.u:
            self.login(key)
            for url in urls:
                variants = [url] + ([url + x for x in self.EXTRA] if key == 'owner' else [])
                for v in variants:
                    r = self.client.get(v)
                    if r.status_code >= 500:
                        failures.append(f'{key} {v} -> {r.status_code}')
        self.assertEqual(failures, [], '\n'.join(failures[:50]))

    def test_anonymous_is_sent_to_login(self):
        from crm.core.middleware import PUBLIC_PREFIXES
        self.client.raise_request_exception = False
        for url in self._urls():
            r = self.client.get(url)
            if url.startswith(PUBLIC_PREFIXES):
                self.assertLess(r.status_code, 500, url)
                continue
            self.assertIn(r.status_code, (302, 401, 403, 404, 405), url)
            if r.status_code == 302:
                self.assertIn('/login', r['Location'], url)
