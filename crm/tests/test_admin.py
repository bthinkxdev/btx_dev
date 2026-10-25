"""Administration, audit and integrity (Part 13.5-13.7, 14)."""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.utils import DatabaseError
from django.urls import reverse

from crm.core import integrity, permissions
from crm.modules.administration import services as svc

from .base import PASSWORD, CrmTestCase

ADMIN_PAGES = ['crm:settings_company', 'crm:settings_policy', 'crm:settings_tax', 'crm:settings_gateway', 'crm:user_list',
               'crm:role_list', 'crm:team_list', 'crm:template_list', 'crm:job_list', 'crm:audit_log', 'crm:security_events']


class AdminPagesTest(CrmTestCase):
    def test_owner_sees_every_admin_page_and_others_are_refused(self):
        self.login('owner')
        for name in ADMIN_PAGES:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse('crm:template_list') + '?tab=log').status_code, 200)
        self.assertEqual(self.client.get(reverse('crm:user_detail', args=[self.u['sales'].pk])).status_code, 200)
        from crm.models import MessageTemplate
        self.assertEqual(self.client.get(reverse('crm:template_edit', args=[MessageTemplate.objects.first().pk])).status_code, 200)
        for key in ('sales', 'manager', 'finance', 'hr', 'dev'):
            self.login(key)
            for name in ADMIN_PAGES:
                self.assertEqual(self.client.get(reverse(name)).status_code, 403, f'{key} {name}')
        self.login('auditor')
        self.assertEqual(self.client.get(reverse('crm:audit_log')).status_code, 200)
        self.assertEqual(self.client.get(reverse('crm:settings_company')).status_code, 403)

    def _company_post(self, **changes):
        from crm.models import CompanySettings
        c = CompanySettings.get()
        data = {f: getattr(c, f) for f in ['legal_name', 'display_name', 'gstin', 'pan', 'state_code', 'address_line1', 'address_line2',
                                           'city', 'pincode', 'phone', 'email', 'website', 'bank_name', 'bank_account_name',
                                           'bank_account_number', 'bank_ifsc', 'bank_branch', 'upi_id', 'signatory_name',
                                           'signatory_designation', 'quotation_validity_days', 'invoice_due_days', 'quotation_terms',
                                           'invoice_terms', 'refund_policy']}
        data['row_version'] = c.row_version
        data.update(changes)
        return self.client.post(reverse('crm:settings_company'), {k: ('' if v is None else v) for k, v in data.items()})

    def test_sensitive_company_change_needs_reason_and_is_audited(self):
        from crm.models import AuditLog, CompanySettings
        self.login('owner')
        r = self._company_post(bank_account_number='1234567890')
        self.assertEqual(r.status_code, 400)
        self.assertNotEqual(CompanySettings.get().bank_account_number, '1234567890')
        r = self._company_post(bank_account_number='1234567890', reason='New current account at SBI')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CompanySettings.get().bank_account_number, '1234567890')
        row = AuditLog.objects.filter(action='settings.changed').latest('id')
        self.assertIn('bank_account_number', row.changes)
        self.assertEqual(row.reason, 'New current account at SBI')
        # Non-sensitive changes need no reason.
        r = self._company_post(signatory_name='A. Partner')
        self.assertEqual(r.status_code, 302)

    def test_gstin_must_match_state_and_pan(self):
        self.login('owner')
        r = self._company_post(gstin='29ABCDE1234F1Z5', reason='Testing a wrong state')
        self.assertEqual(r.status_code, 400)

    def test_policy_validation(self):
        from crm.models import CompanySettings
        from crm.modules.administration.forms import PolicyForm
        c = CompanySettings.get()
        data = {f: getattr(c, f) for f in PolicyForm.Meta.fields}
        data.update(exec_discount_limit='30', manager_discount_limit='10', reason='Tighten limits for Q3', row_version=c.row_version)
        f = PolicyForm(data, instance=c)
        self.assertFalse(f.is_valid())
        self.assertIn('exec_discount_limit', f.errors)


class UserAdminTest(CrmTestCase):
    def test_create_user_forces_password_change(self):
        from crm.models import Role
        self.login('owner')
        r = self.client.post(reverse('crm:user_list'), {
            'new-first_name': 'Neha', 'new-username': 'neha', 'new-email': 'neha@example.test',
            'new-roles': [Role.objects.get(code='sales_executive').pk]})
        self.assertEqual(r.status_code, 302)
        page = self.client.get(r['Location'])
        temp = page.context['temp_password']
        self.assertTrue(temp)
        self.assertEqual(self.client.get(r['Location']).context['temp_password'], '')  # shown once
        self.client.logout()
        self.assertTrue(self.client.login(username='neha', password=temp))
        r = self.client.get(reverse('crm:my_day'))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('crm:profile'), r['Location'])
        r = self.client.post(reverse('crm:profile'), {'action': 'password', 'old_password': temp,
                                                      'new_password1': 'A-Much-Better-Pass-2026', 'new_password2': 'A-Much-Better-Pass-2026'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse('crm:my_day')).status_code, 200)

    def test_cannot_grant_above_own_access(self):
        from crm.models import Role
        with self.assertRaises(ValidationError):
            svc.update_access(self.u['sales'], self.u['manager'], roles=[Role.objects.get(code='owner')], team=None, department=None,
                              manager=None, designation='', receives_leads=False, open_lead_cap=None, reason='Promote to owner now')

    def test_role_change_needs_reason_and_is_audited(self):
        from crm.models import AuditLog, Role, SecurityEvent
        role = Role.objects.get(code='finance_executive')
        with self.assertRaises(ValidationError):
            svc.update_access(self.u['sales'], self.u['owner'], roles=[role], team=None, department=None, manager=None,
                              designation='', receives_leads=False, open_lead_cap=None, reason='')
        svc.update_access(self.u['sales'], self.u['owner'], roles=[role], team=None, department=None, manager=None,
                          designation='', receives_leads=False, open_lead_cap=None, reason='Moved to the finance team')
        row = AuditLog.objects.filter(action='user.access_changed').latest('id')
        self.assertEqual(row.changes['roles'], [['sales_executive'], ['finance_executive']])
        self.assertTrue(SecurityEvent.objects.filter(kind='permission_change', user=self.u['sales']).exists())
        permissions.clear_cache(self.u['sales'])
        self.assertTrue(permissions.has_perm(self.u['sales'], 'payments', 'approve'))

    def test_last_owner_is_protected(self):
        with self.assertRaises(ValidationError):
            svc.deactivate(self.u['owner'], self.u['owner'], reason='Leaving the company now')
        from crm.models import Role, UserProfile
        other = self.u['auditor']
        p = UserProfile.objects.get(user=other)
        p.roles.add(Role.objects.get(code='owner'))
        with self.assertRaises(ValidationError):
            svc.update_access(self.u['owner'], self.u['owner'], roles=[], team=None, department=None, manager=None,
                              designation='', receives_leads=False, open_lead_cap=None, reason='Removing myself as owner')
        with self.assertRaises(ValidationError):
            svc.update_access(other, other, roles=[], team=None, department=None, manager=None, designation='',
                              receives_leads=False, open_lead_cap=None, reason='Self change is not allowed')

    def test_deactivate_signs_out_and_reassigns(self):
        from django.utils import timezone
        from crm.models import Lead, LeadSource, UserSession
        src = LeadSource.objects.first() or LeadSource.objects.create(name='Web', kind='website')
        lead = Lead.objects.create(name='X', phone='+919876500001', source=src, owner=self.u['sales'], stage='contacted',
                                   next_follow_up_at=timezone.now())
        p = self.u['sales_b'].crm_profile
        p.receives_leads = True
        p.save()
        self.client.force_login(self.u['sales'])
        UserSession.objects.create(user=self.u['sales'], session_key=self.client.session.session_key)
        self.login('owner')
        r = self.client.post(reverse('crm:user_action', args=[self.u['sales'].pk, 'deactivate']),
                             {'r-reason': 'Resigned, last day today', 'reassign': '1'})
        self.assertEqual(r.status_code, 302)
        self.u['sales'].refresh_from_db()
        self.assertFalse(self.u['sales'].is_active)
        lead.refresh_from_db()
        self.assertEqual(lead.owner, self.u['sales_b'])
        self.assertFalse(UserSession.objects.filter(user=self.u['sales'], ended_at__isnull=True).exists())

    def test_reset_access_unlocks_and_forces_change(self):
        from django.utils import timezone
        p = self.u['sales'].crm_profile
        p.locked_until = timezone.now() + timezone.timedelta(minutes=10)
        p.failed_logins = 5
        p.save()
        pw = svc.reset_access(self.u['sales'], self.u['owner'], reset_two_factor=True, reason='Locked out after holiday')
        p.refresh_from_db()
        self.assertIsNone(p.locked_until)
        self.assertTrue(p.must_change_password)
        self.assertTrue(self.client.login(username='sales', password=pw))
        self.assertFalse(self.client.login(username='sales', password=PASSWORD))


class RoleMatrixTest(CrmTestCase):
    def _post(self, role, grants, reason='Give sales read access to projects'):
        data = {'reason': reason}
        for m, acts in grants.items():
            for a, scope in acts.items():
                data[f'g__{m}__{a}'] = scope
        for g in role.field_groups:
            data.setdefault('field_groups', []).append(g)
        return self.client.post(reverse('crm:role_save', args=[role.pk]), data)

    def test_permission_change_is_audited_and_effective_next_request(self):
        from crm.models import AuditLog, Role
        role = Role.objects.get(code='sales_executive')
        self.client.force_login(self.u['sales'])
        self.assertEqual(self.client.get(reverse('crm:project_list')).status_code, 403)
        self.login('owner')
        grants = {m: dict(a) for m, a in role.grants.items()}
        grants['projects'] = {'view': 'own'}
        self._post(role, grants)
        row = AuditLog.objects.filter(action='role.permissions_changed').latest('id')
        self.assertIn('grants', row.changes)
        before, after = row.changes['grants']
        self.assertNotIn('projects', before)
        self.assertEqual(after['projects'], {'view': 'own'})
        self.client.force_login(self.u['sales'])
        self.assertEqual(self.client.get(reverse('crm:project_list')).status_code, 200)

    def test_owner_role_keeps_settings(self):
        from crm.models import Role
        role = Role.objects.get(code='owner')
        with self.assertRaises(ValidationError):
            svc.save_role_grants(role, self.u['owner'], grants={'leads': {'view': 'company'}}, field_groups=[], denied_groups=[],
                                 reason='Strip the owner role down')

    def test_matrix_compare_and_export(self):
        from crm.models import Role
        self.login('owner')
        a, b = Role.objects.get(code='sales_executive'), Role.objects.get(code='sales_manager')
        r = self.client.get(reverse('crm:role_list') + f'?role={a.pk}&compare={b.pk}')
        self.assertTrue(r.context['comparison'])
        r = self.client.get(reverse('crm:role_list') + f'?role={a.pk}&export=csv')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Leads', r.content)

    def test_create_role_from_copy(self):
        from crm.models import Role
        self.login('owner')
        src = Role.objects.get(code='telecaller')
        r = self.client.post(reverse('crm:role_create'), {'nr-name': 'Night shift', 'nr-code': 'night_shift', 'nr-copy_from': src.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Role.objects.get(code='night_shift').grants, src.grants)


class GatewaySettingsTest(CrmTestCase):
    def test_secret_is_write_only(self):
        from crm.models import AuditLog, CompanySettings
        self.login('owner')
        secret = 'cfsk_ma_test_super_secret_value_123'
        r = self.client.post(reverse('crm:settings_gateway'), {'action': 'save', 'provider': 'cashfree', 'app_id': 'TEST1234',
                                                              'secret': secret, 'methods': ['upi', 'card']})
        self.assertEqual(r.status_code, 302)
        c = CompanySettings.get()
        self.assertTrue(c.gateway_secret_enc)
        self.assertNotIn(secret, c.gateway_secret_enc)
        page = self.client.get(reverse('crm:settings_gateway')).content.decode()
        self.assertNotIn(secret, page)
        for row in AuditLog.objects.all():
            self.assertNotIn(secret, str(row.changes) + row.reason)

    def test_only_owner_can_go_live_and_sandbox_cannot(self):
        from crm.models import CompanySettings
        with self.assertRaises(ValidationError):
            svc.save_gateway(CompanySettings.get(), self.u['owner'], provider='sandbox', app_id='', methods=[], live=True,
                             reason='Going live with the sandbox')
        c = CompanySettings.get()
        c.gateway_provider = 'cashfree'
        c.save()
        with self.assertRaises(ValidationError):
            svc.save_gateway(CompanySettings.get(), self.u['accountant'], provider='cashfree', app_id='', methods=[], live=True,
                             reason='Not allowed for accountants')
        svc.save_gateway(CompanySettings.get(), self.u['owner'], provider='cashfree', app_id='', methods=[], live=True,
                         reason='Launch day, keys verified')
        self.assertTrue(CompanySettings.get().gateway_live)

    def test_switch_live_through_the_page(self):
        from crm.models import CompanySettings
        c = CompanySettings.get()
        c.gateway_provider = 'cashfree'
        c.save()
        self.login('owner')
        self.client.post(reverse('crm:settings_gateway'), {'action': 'live', 'provider': 'cashfree', 'app_id': '', 'methods': ['upi'],
                                                          'reason': 'Launch day, keys verified'})
        self.assertTrue(CompanySettings.get().gateway_live)


class NumberingAndTemplatesTest(CrmTestCase):
    def test_series_change_after_issue_starts_next_year(self):
        from crm.models import DocumentSequence, SequenceSeries
        inv = self.make_invoice()
        s = SequenceSeries.objects.get(doc_type='INV')
        self.login('owner')
        self.client.post(reverse('crm:settings_series', args=[s.pk]), {f's{s.pk}-prefix': 'BTX', f's{s.pk}-padding': '5'})
        seq = DocumentSequence.objects.get(doc_type='INV', fiscal_year=inv.number.split('/')[1])
        self.assertEqual(seq.prefix, 'INV')  # issued series unchanged
        self.assertEqual(SequenceSeries.objects.get(pk=s.pk).prefix, 'BTX')
        second = self.make_invoice()
        self.assertTrue(second.number.startswith('INV/'))

    def test_template_edit_creates_live_version(self):
        from crm.models import MessageTemplate
        tpl = MessageTemplate.objects.get(code='invoice_send', version=1)
        self.login('owner')
        r = self.client.post(reverse('crm:template_edit', args=[tpl.pk]), {'tp-name': tpl.name, 'tp-subject': 'Your invoice {{ number }}',
                                                                          'tp-body': 'Hello {{ name }}', 'tp-reason': 'Shorter wording', 'action': 'save'})
        self.assertEqual(r.status_code, 302)
        live = MessageTemplate.objects.filter(code='invoice_send', is_active=True)
        self.assertEqual(live.count(), 1)
        self.assertEqual(live.first().version, 2)
        r = self.client.post(reverse('crm:template_edit', args=[tpl.pk]), {'tp-name': tpl.name, 'tp-subject': 'x', 'tp-body': '{% load static %}',
                                                                          'tp-reason': 'Trying a tag load', 'action': 'save'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(MessageTemplate.objects.filter(code='invoice_send').count(), 2)
        r = self.client.post(reverse('crm:template_edit', args=[tpl.pk]), {'tp-name': tpl.name, 'tp-subject': 'Hi {{ name }}', 'tp-body': 'Pay {{ amount }}',
                                                                          'tp-reason': 'Preview only please', 'action': 'preview'})
        self.assertIn('Anu Thomas', r.context['preview']['subject'])


class AuditAndIntegrityTest(CrmTestCase):
    def test_audit_rows_cannot_be_changed_in_the_database(self):
        from crm.core import audit
        from crm.models import AuditLog
        audit.log('test.event', actor=self.u['owner'], object_type='x', object_id='1')
        row = AuditLog.objects.latest('id')
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute('UPDATE crm_auditlog SET action = %s WHERE id = %s', ['tampered', row.pk])
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                AuditLog.objects.filter(pk=row.pk).delete()
        self.assertEqual(AuditLog.objects.get(pk=row.pk).action, 'test.event')

    def test_viewing_and_exporting_the_log_is_logged(self):
        from crm.models import AuditLog, ExportLog
        self.login('auditor')
        self.client.get(reverse('crm:audit_log') + '?action=settings')
        self.assertTrue(AuditLog.objects.filter(action='audit.viewed', actor_id=self.u['auditor'].pk).exists())
        r = self.client.get(reverse('crm:audit_log') + '?export=csv')
        b''.join(r.streaming_content)
        self.assertTrue(ExportLog.objects.filter(module='audit', user=self.u['auditor']).exists())

    def test_integrity_passes_then_detects_tampering(self):
        from crm.models import Invoice
        inv = self.make_invoice()
        result = integrity.run_checks()
        self.assertEqual(result['failed'], 0, [c for c in result['checks'] if not c['ok']])
        Invoice.objects.filter(pk=inv.pk).update(balance=inv.balance - Decimal('1'))
        result = integrity.run_checks()
        failed = {c['code'] for c in result['checks'] if not c['ok']}
        self.assertIn('invoice_balances', failed)
        self.assertIn('ar_tie_out', failed)

    def test_failed_job_retry_and_resolve(self):
        from crm.core import jobs
        from crm.models import JobRun, Invoice
        inv = self.make_invoice()
        Invoice.objects.filter(pk=inv.pk).update(balance=Decimal('1'))
        r = jobs.run_job('integrity.nightly', force=True)
        self.assertEqual(r['status'], 'failed')
        run = JobRun.objects.filter(job='integrity.nightly').latest('id')
        self.assertTrue(run.result['value']['checks'])
        self.login('owner')
        self.assertEqual(self.client.get(reverse('crm:job_list')).status_code, 200)
        self.client.post(reverse('crm:job_action', args=['resolve']), {'pk': run.pk, 'reason': 'Known test data, fixed by hand'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'resolved')

    def test_job_run_now(self):
        self.login('owner')
        r = self.client.post(reverse('crm:job_action', args=['run']), {'job': 'housekeeping.idempotency'})
        self.assertEqual(r.status_code, 302)
        from crm.models import JobRun
        self.assertEqual(JobRun.objects.filter(job='housekeeping.idempotency').latest('id').status, 'succeeded')
        self.login('sales')
        self.assertEqual(self.client.post(reverse('crm:job_action', args=['run']), {'job': 'housekeeping.idempotency'}).status_code, 403)

    def test_security_events_unlock(self):
        from django.utils import timezone
        p = self.u['sales'].crm_profile
        p.locked_until = timezone.now() + timezone.timedelta(minutes=10)
        p.save()
        self.login('owner')
        r = self.client.get(reverse('crm:security_events'))
        self.assertEqual(len(r.context['locked']), 1)
        self.client.post(reverse('crm:security_unlock', args=[p.pk]))
        p.refresh_from_db()
        self.assertIsNone(p.locked_until)
