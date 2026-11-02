from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import BlockedBrand, Business, RunResult, SearchRun
from .services import extraction

DETAILS = {
    'p1': {'placeId': 'p1', 'name': 'Alpha Boutique', 'address': 'MG Road', 'nationalPhoneNumber': '0471 234 5678',
           'internationalPhoneNumber': '', 'rating': 4.5, 'userRatingCount': 40, 'website': '',
           'businessStatus': 'OPERATIONAL', 'primaryType': 'clothing_store', 'mapsUri': 'https://maps/p1'},
    'p2': {'placeId': 'p2', 'name': 'Beta Boutique', 'address': 'MG Road', 'nationalPhoneNumber': '0471 999 0000',
           'internationalPhoneNumber': '', 'rating': 4.0, 'userRatingCount': 12, 'website': '',
           'businessStatus': 'OPERATIONAL', 'primaryType': 'clothing_store', 'mapsUri': 'https://maps/p2'},
    # Same phone as p1 under a different place id
    'p3': {'placeId': 'p3', 'name': 'Alpha Boutique (2)', 'address': 'Other', 'nationalPhoneNumber': '04712345678',
           'internationalPhoneNumber': '', 'rating': 4.0, 'userRatingCount': 5, 'website': '',
           'businessStatus': 'OPERATIONAL', 'primaryType': 'clothing_store', 'mapsUri': ''},
}


def fake_page(query, page_token=''):
    return {'results': [{'placeId': k, 'name': v['name'], 'latitude': 8.5, 'longitude': 76.9, 'primaryType': ''}
                        for k, v in DETAILS.items()], 'nextPageToken': ''}


@mock.patch('leadfinder.services.extraction.time.sleep')
@mock.patch('leadfinder.services.google_places.search_text_page', side_effect=fake_page)
@mock.patch('leadfinder.services.google_places.get_place_details', side_effect=lambda pid: DETAILS[pid])
class LeadFinderTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser('boss', 'b@x.com', 'pw')
        self.staff = User.objects.create_user('staff', 's@x.com', 'pw')

    def _run(self, target=10):
        run = SearchRun.objects.create(location='TVM', category='Boutique', target_count=target)
        extraction._run(run.pk)
        run.refresh_from_db()
        return run

    def test_first_run_collects_and_dedups_by_phone(self, *_):
        run = self._run()
        self.assertEqual(run.status, 'completed')
        self.assertEqual((run.new_count, run.duplicate_count), (2, 1))
        self.assertEqual(Business.objects.count(), 2)  # p3 is the same phone as p1

    def test_second_run_finds_nothing_new(self, *_):
        self._run()
        run2 = self._run()
        self.assertEqual(run2.new_count, 0)
        self.assertEqual(Business.objects.count(), 2)
        self.assertTrue(RunResult.objects.filter(run=run2, outcome='duplicate').exists())
        self.assertGreater(Business.objects.get(google_place_id='p1').times_seen, 1)

    def test_target_stops_run(self, *_):
        run = self._run(target=1)
        self.assertEqual(run.new_count, 1)

    def test_admin_only(self, *_):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('leadfinder:lf_index')).status_code, 403)
        self.assertEqual(self.client.get(reverse('leadfinder:lf_history')).status_code, 403)

    def test_pages_and_exports(self, *_):
        run = self._run()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('leadfinder:lf_index')).status_code, 200)
        self.assertEqual(self.client.get(reverse('leadfinder:lf_history')).status_code, 200)
        st = self.client.get(reverse('leadfinder:lf_run_status', args=[run.pk]))
        self.assertEqual(len(st.json()['results']), RunResult.objects.filter(run=run).count())
        for url in (reverse('leadfinder:lf_export_run', args=[run.pk]), reverse('leadfinder:lf_export_history')):
            x = self.client.get(url + '?format=xlsx')
            self.assertEqual(x.status_code, 200)
            self.assertTrue(x.content.startswith(b'PK'))
            p = self.client.get(url + '?format=pdf')
            self.assertEqual(p.status_code, 200)
            self.assertTrue(p.content.startswith(b'%PDF'))

    def test_creates_no_crm_rows(self, *_):
        from crm.models import Lead
        before = Lead.objects.count()
        self._run()
        self.assertEqual(Lead.objects.count(), before)


def _d(pid, name, phone, reviews):
    return {'placeId': pid, 'name': name, 'address': 'x', 'nationalPhoneNumber': phone, 'internationalPhoneNumber': '',
            'rating': 4.2, 'userRatingCount': reviews, 'website': '', 'businessStatus': 'OPERATIONAL',
            'primaryType': '', 'mapsUri': ''}


class SizeScreeningTests(TestCase):
    def _run(self, details, **kw):
        def page(query, page_token=''):
            return {'results': [{'placeId': k, 'name': v['name']} for k, v in details.items()], 'nextPageToken': ''}
        fetch = mock.Mock(side_effect=lambda pid: details[pid])
        run = SearchRun.objects.create(location='TVM', category='Shop', target_count=50, **kw)
        with mock.patch('leadfinder.services.extraction.time.sleep'),                 mock.patch('leadfinder.services.google_places.search_text_page', side_effect=page),                 mock.patch('leadfinder.services.google_places.get_place_details', fetch):
            extraction._run(run.pk)
        run.refresh_from_db()
        return run, fetch

    def test_review_range(self):
        run, _ = self._run({'a': _d('a', 'Small Shop', '9447000001', 40), 'b': _d('b', 'Giant Mart', '9447000002', 4200),
                            'c': _d('c', 'Ghost Shop', '9447000003', 1)})
        self.assertEqual((run.new_count, run.rejected_count), (1, 2))
        self.assertIn('Too large', RunResult.objects.get(run=run, name='Giant Mart').reason)
        self.assertIn('Too few', RunResult.objects.get(run=run, name='Ghost Shop').reason)

    def test_no_ceiling_when_max_is_none(self):
        run, _ = self._run({'b': _d('b', 'Giant Mart', '9447000002', 4200)}, max_reviews=None)
        self.assertEqual(run.new_count, 1)

    def test_blocked_brand_skips_details_call(self):
        BlockedBrand.objects.create(name='Reliance', name_key='reliance')
        run, fetch = self._run({'a': _d('a', 'Reliance Trends - Kowdiar', '9447000001', 40),
                                'b': _d('b', 'Reliancer Tailors', '9447000002', 40)})
        fetch.assert_called_once_with('b')   # whole-word match: "Reliancer" is not "Reliance"
        self.assertEqual(run.new_count, 1)
        self.assertIn('Excluded brand', RunResult.objects.get(run=run, name='Reliance Trends - Kowdiar').reason)

    def test_chain_detected_at_third_location(self):
        run, fetch = self._run({'a': _d('a', 'Zudio - Kowdiar', '9447000001', 40), 'b': _d('b', 'Zudio - Pattom', '9447000002', 40),
                                'c': _d('c', 'Zudio (Technopark)', '9447000003', 40), 'd': _d('d', 'Zudio, Vellayambalam', '9447000004', 40)})
        self.assertEqual(run.new_count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(Business.objects.filter(name_key='zudio').count(), 4)
        self.assertEqual(RunResult.objects.filter(run=run, reason__startswith='Chain').count(), 2)

    def test_name_key(self):
        self.assertEqual(extraction.name_key('The Pothys - Kowdiar Branch'), 'pothys')
        self.assertEqual(extraction.name_key('Lulu Mall, Edappally'), 'lulu mall')

    def test_brand_admin_views(self):
        User = get_user_model()
        admin = User.objects.create_superuser('boss2', 'b2@x.com', 'pw')
        self.client.force_login(admin)
        self.client.post(reverse('leadfinder:lf_brand_add'), {'name': 'Lulu Mall'})
        brand = BlockedBrand.objects.get()
        self.assertEqual(brand.name_key, 'lulu mall')
        self.client.post(reverse('leadfinder:lf_brand_add'), {'name': ' Reliance ,Pothys, , lulu mall,Pothys'})
        self.assertEqual(sorted(BlockedBrand.objects.values_list('name_key', flat=True)), ['lulu mall', 'pothys', 'reliance'])
        BlockedBrand.objects.exclude(pk=brand.pk).delete()
        self.client.post(reverse('leadfinder:lf_brand_remove', args=[brand.pk]))
        self.assertFalse(BlockedBrand.objects.exists())
        staff = User.objects.create_user('st2', 's2@x.com', 'pw')
        self.client.force_login(staff)
        self.assertEqual(self.client.post(reverse('leadfinder:lf_brand_add'), {'name': 'X'}).status_code, 403)
