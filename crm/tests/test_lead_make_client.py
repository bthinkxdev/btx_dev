"""Making a won lead a client by hand: the ⋮ menu action, the warning on the lead, the quick filter, and the rules around it."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from crm.core import integrity
from crm.models import AuditLog, Client, Contact, Lead, LeadActivity, LeadSource, Quotation
from crm.modules.leads import services as leads

from .base import CrmTestCase


class MakeClientTests(CrmTestCase):
    def won_lead(self, phone='+919811100001', company='Brand New Boutique', name='Meera', owner=None, create_client=False):
        owner = owner or self.u['sales']
        lead = Lead.objects.create(name=name, company=company, phone=phone, email='meera@boutique.test', city='Kochi', source=LeadSource.objects.first(),
                                   owner=owner, stage='negotiation', created_by=owner, assigned_at=timezone.now(), value=Decimal('30000'),
                                   next_follow_up_at=timezone.now() + timedelta(days=2))
        leads.mark_won(lead, owner, won_value=Decimal('30000'), note='Confirmed on a call, paying advance', create_client=create_client)
        lead.refresh_from_db()
        return lead

    def post(self, who, lead, choice='new', **extra):
        self.login(who)
        return self.client.post(reverse('crm:lead_action', args=[lead.pk, 'make_client']), {'make_client-client_choice': choice, **extra})

    # ------------------------------------------------------------------ what the person sees
    def test_a_won_lead_without_a_client_shows_the_banner_the_menu_item_and_the_filter(self):
        lead = self.won_lead()
        self.assertIsNone(lead.client_id)
        self.login('sales')
        html = self.client.get(reverse('crm:lead_detail', args=[lead.pk])).content.decode()
        self.assertIn('no client record yet', html)
        self.assertEqual(html.count('Make client'), 3)                      # banner button, menu item, dialog button
        self.assertIn("$store.modal.show('make_client')", html)
        listing = self.client.get(reverse('crm:lead_list') + '?quick=won_no_client').content.decode()
        self.assertIn('Brand New Boutique', listing)
        self.assertIn('Won, no client yet', listing)

    def test_leads_that_already_have_a_client_or_are_not_won_do_not_offer_it(self):
        with_client = self.won_lead(phone='+919811100002', company='Has Client Co', create_client=True)
        self.assertIsNotNone(with_client.client_id)
        open_lead = Lead.objects.create(name='Open one', phone='+919811100003', source=LeadSource.objects.first(), owner=self.u['sales'],
                                        stage='contacted', created_by=self.u['sales'], assigned_at=timezone.now(), value=Decimal('1000'),
                                        next_follow_up_at=timezone.now() + timedelta(days=1))
        self.login('sales')
        for lead in (with_client, open_lead):
            html = self.client.get(reverse('crm:lead_detail', args=[lead.pk])).content.decode()
            self.assertNotIn('Make client', html, lead.name)
            self.assertNotIn('no client record yet', html)
        self.assertIn('Open client', self.client.get(reverse('crm:lead_detail', args=[with_client.pk])).content.decode())
        self.assertNotIn('Has Client Co', self.client.get(reverse('crm:lead_list') + '?quick=won_no_client').content.decode())

    # ------------------------------------------------------------------ creating
    def test_make_client_creates_a_linked_client_and_leaves_the_win_alone(self):
        lead = self.won_lead()
        before = Client.objects.count()
        r = self.post('sales', lead)
        self.assertEqual(r.status_code, 302)
        lead.refresh_from_db()
        self.assertEqual(Client.objects.count(), before + 1)
        c = lead.client
        self.assertEqual((c.display_name, c.phone, c.email, c.billing_city, c.owner_id, c.source_lead_id),
                         ('Brand New Boutique', '+919811100001', 'meera@boutique.test', 'Kochi', self.u['sales'].pk, lead.pk))
        self.assertTrue(Contact.objects.filter(client=c, name='Meera', is_primary=True).exists())
        self.assertEqual((lead.stage, lead.won_value), ('won', Decimal('30000.00')))                     # the win itself is untouched
        self.assertTrue(LeadActivity.objects.filter(lead=lead, kind='system', summary__contains='created the client record').exists())
        self.assertTrue(AuditLog.objects.filter(action='lead.client_created', object_id=str(lead.pk)).exists())
        html = self.client.get(reverse('crm:lead_detail', args=[lead.pk])).content.decode()
        self.assertNotIn('no client record yet', html)
        self.assertIn('Open client', html)
        self.assertTrue(self.client.get(reverse('crm:client_detail', args=[c.pk])).status_code == 200)

    def test_the_nightly_check_stops_flagging_it(self):
        lead = self.won_lead()
        self.assertTrue(any('won without a customer record' in row for row in integrity.check_won_flow()['items']))
        self.post('sales', lead)
        self.assertFalse(any(str(lead) in row and 'without a customer record' in row for row in integrity.check_won_flow()['items']))

    def test_quotations_of_the_lead_follow_the_new_client(self):
        lead = self.won_lead()
        today = timezone.localdate()
        q = Quotation.objects.create(lead=lead, owner=self.u['sales'], created_by=self.u['sales'], status='accepted', quotation_date=today,
                                     valid_until=today + timedelta(days=30))
        self.assertIsNone(q.client_id)
        self.post('sales', lead)
        lead.refresh_from_db()
        q.refresh_from_db()
        self.assertEqual(q.client_id, lead.client_id)

    # ------------------------------------------------------------------ linking
    def test_it_offers_and_links_an_existing_matching_client_instead_of_a_duplicate(self):
        lead = self.won_lead(phone=self.client_obj.phone, company='Acme Kerala Pvt Ltd')
        self.login('manager')
        html = self.client.get(reverse('crm:lead_detail', args=[lead.pk])).content.decode()
        self.assertIn('Link to existing client: Acme', html)
        self.assertIn('Create a new client from this lead', html)
        before = Client.objects.count()
        r = self.post('manager', lead, choice=str(self.client_obj.pk))
        self.assertEqual(r.status_code, 302)
        lead.refresh_from_db()
        self.assertEqual((lead.client_id, Client.objects.count()), (self.client_obj.pk, before))
        self.assertTrue(AuditLog.objects.filter(action='lead.client_linked', object_id=str(lead.pk)).exists())
        self.assertTrue(LeadActivity.objects.filter(lead=lead, summary__contains='linked this lead to the existing client').exists())

    def test_you_cannot_link_a_lead_to_a_client_that_is_outside_your_reach(self):
        lead = self.won_lead(phone='+919811100009', owner=self.u['sales_b'])
        other = Client.objects.create(legal_name='Private Co', display_name='Private Co', owner=self.u['finance'], billing_state='32')
        # sales_b is in the same team as sales, so use a client owned by someone outside that team
        with self.assertRaises(leads.LeadError):
            leads.make_client(lead, self.u['sales_b'], existing=other)
        lead.refresh_from_db()
        self.assertIsNone(lead.client_id)

    # ------------------------------------------------------------------ rules
    def test_only_won_leads_and_only_once(self):
        open_lead = Lead.objects.create(name='Open one', phone='+919811100004', source=LeadSource.objects.first(), owner=self.u['sales'],
                                        stage='qualified', created_by=self.u['sales'], assigned_at=timezone.now(), value=Decimal('1000'),
                                        next_follow_up_at=timezone.now() + timedelta(days=1))
        r = self.post('sales', open_lead)
        self.assertEqual(r.status_code, 400)
        self.assertIn('Only a won lead', r.content.decode())
        open_lead.refresh_from_db()
        self.assertIsNone(open_lead.client_id)
        lead = self.won_lead(phone='+919811100005')
        self.assertEqual(self.post('sales', lead).status_code, 302)
        count = Client.objects.count()
        again = self.post('sales', lead)
        self.assertEqual(again.status_code, 400)
        self.assertIn('already has a client', again.content.decode())
        self.assertEqual(Client.objects.count(), count)                      # a second tap never makes a second client

    def test_who_may_do_it(self):
        lead = self.won_lead(phone='+919811100006')
        for who, ok in (('finance', False), ('hr', False), ('dev', False)):
            self.assertIn(self.post(who, lead).status_code, (403, 404), who)
        lead.refresh_from_db()
        self.assertIsNone(lead.client_id)
        self.assertIn(self.post('sales_b', lead).status_code, (403, 404))      # another executive's lead
        lead.refresh_from_db()
        self.assertIsNone(lead.client_id)
        self.assertEqual(self.post('owner', lead).status_code, 302)           # the owner may
        lead.refresh_from_db()
        self.assertIsNotNone(lead.client_id)

    def test_the_service_checks_permission_itself(self):
        lead = self.won_lead(phone='+919811100007')
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            leads.make_client(lead, self.u['hr'])
        client = leads.make_client(lead, self.u['sales'])
        self.assertEqual(client.owner_id, self.u['sales'].pk)
