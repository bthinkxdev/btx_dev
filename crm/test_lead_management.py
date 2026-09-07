"""Duplicate-phone prevention on manual lead creation (crm:lead_create)."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.models import EmployeeProfile, Lead

User = get_user_model()


class LeadDuplicatePreventionTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('lm_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.client.force_login(self.rep)

    def _post(self, name, phone):
        return self.client.post(
            '/crm/leads/create/',
            {
                'name': name, 'phone': phone, 'email': '', 'source': '',
                'status': 'new', 'package': '', 'deal_value': '0', 'notes': '',
            },
            HTTP_HX_REQUEST='true',
        )

    def test_duplicate_phone_blocked(self):
        self._post('Alice', '9998887777')
        self.assertEqual(Lead.objects.filter(phone='9998887777').count(), 1)

        self._post('Alice Again', '9998887777')
        self.assertEqual(Lead.objects.filter(phone='9998887777').count(), 1)

    def test_distinct_phone_not_blocked(self):
        self._post('Alice', '9998887777')
        self._post('Bob', '9998887778')
        self.assertEqual(Lead.objects.filter(employee=self.rep).count(), 2)

    def test_blank_phone_never_flagged_as_duplicate(self):
        self._post('Carol', '')
        self._post('Dave', '')
        self.assertEqual(Lead.objects.filter(employee=self.rep, phone='').count(), 2)

    def test_duplicate_check_is_global_across_employees(self):
        other_rep = User.objects.create_user('lm_rep2', password='x')
        EmployeeProfile.objects.filter(user=other_rep).update(crm_role='sales')

        self._post('Alice', '9998887777')
        self.client.force_login(other_rep)
        self._post('Alice Copy', '9998887777')

        self.assertEqual(Lead.objects.filter(phone='9998887777').count(), 1)
        self.assertEqual(Lead.objects.filter(employee=other_rep).count(), 0)
