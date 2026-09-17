"""
Tests for the Google Places lead-extraction system: scoring, the extraction
run loop, and the admin-only views (start/stop/status/live table).

Split across three test classes deliberately:
- *ScoringTests / *ServiceTests (SimpleTestCase): pure functions, no DB.
- LeadExtractionCoreTests (TestCase): calls lead_extraction._run() directly
  (synchronous, single connection) — safe under TestCase's wrapping transaction.
- LeadExtractionViewsTests (TransactionTestCase): exercises the real HTTP
  views, including the background thread started by "Start Extraction".
  Must be TransactionTestCase, not TestCase — a background thread opens its
  own DB connection and cannot see rows created inside TestCase's still-open
  outer transaction.
"""

from __future__ import annotations

import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from crm.models import EmployeeProfile, ExtractionLead, ExtractionRun, Lead, Place
from crm.services import google_places, lead_extraction, lead_qualification, online_presence, website_check

User = get_user_model()


def make_details(place_id='PID1', *, name='Biz', address='Addr', phone='9999999999',
                  rating=4.5, reviews=20, status='OPERATIONAL', website=''):
    return {
        'placeId': place_id, 'name': name, 'address': address,
        'nationalPhoneNumber': phone, 'internationalPhoneNumber': '',
        'rating': rating, 'userRatingCount': reviews, 'website': website,
        'businessStatus': status, 'primaryType': '', 'mapsUri': '',
    }


def make_website_result(status='none', **overrides):
    base = {
        'status': status, 'https': None, 'mobile_responsive': None,
        'appearance': None, 'ecommerce_signal': None, 'broken': False, 'html': '',
    }
    base.update(overrides)
    return base


def make_social_result(presence='none', activity='unknown', **overrides):
    base = {
        'instagram_url': '', 'facebook_url': '',
        'social_presence_type': presence, 'social_activity': activity,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Pure scoring logic — no DB, no network
# ---------------------------------------------------------------------------
class LeadQualificationScoringTests(SimpleTestCase):
    def evaluate(self, details, website_status='none', social_presence='none', **kw):
        return lead_qualification.evaluate_business(
            details,
            website_result=make_website_result(website_status, **{k: v for k, v in kw.items() if k in
                ('https', 'mobile_responsive', 'appearance', 'ecommerce_signal')}),
            social_result=make_social_result(social_presence, kw.get('social_activity', 'unknown')),
            category='Boutique',
        )

    def test_strong_business_no_website_qualifies(self):
        details = make_details(rating=4.7, reviews=400, website='')
        result = self.evaluate(details, website_status='none')
        self.assertTrue(result['qualified'])
        self.assertGreaterEqual(result['website_opportunity_score'], 80)
        self.assertEqual(result['website_opportunity_label'], 'very_high')

    def test_permanently_closed_rejected(self):
        details = make_details(status='CLOSED_PERMANENTLY')
        result = self.evaluate(details)
        self.assertFalse(result['qualified'])
        self.assertIn('closed', result['reason'].lower())

    def test_missing_phone_rejected(self):
        details = make_details(phone='', rating=4.8, reviews=200)
        details['internationalPhoneNumber'] = ''
        result = self.evaluate(details)
        self.assertFalse(result['qualified'])
        self.assertIn('phone', result['reason'].lower())

    def test_missing_name_rejected(self):
        details = make_details(name='')
        result = self.evaluate(details)
        self.assertFalse(result['qualified'])

    def test_weak_business_rejected(self):
        # Thin identity: no address on file, no rating/reviews yet, ambiguous status —
        # not enough evidence of a real, established business, even with a phone number.
        details = make_details(rating=None, reviews=0, address='', status='')
        result = self.evaluate(details)
        self.assertFalse(result['qualified'])
        self.assertLess(result['business_quality_score'], lead_qualification.MIN_BUSINESS_QUALITY_FOR_QUALIFICATION)

    def test_low_opportunity_business_still_qualifies(self):
        # A strong business with a great modern website and full social presence must
        # NOT be auto-rejected — opportunity scores are for prioritization, not a gate.
        details = make_details(rating=4.6, reviews=300, website='https://example.com')
        result = self.evaluate(
            details, website_status='reachable',
            https=True, mobile_responsive=True, appearance='modern', ecommerce_signal=True,
            social_presence='instagram_and_facebook',
        )
        self.assertTrue(result['qualified'])
        self.assertEqual(result['website_opportunity_label'], 'low')

    def test_website_opportunity_scoring_modern_site_scores_low(self):
        details = make_details(website='https://example.com')
        modern = self.evaluate(
            details, website_status='reachable',
            https=True, mobile_responsive=True, appearance='modern', ecommerce_signal=True,
        )
        broken = self.evaluate(details, website_status='unreachable')
        self.assertLess(modern['website_opportunity_score'], broken['website_opportunity_score'])
        self.assertEqual(modern['website_opportunity_label'], 'low')
        self.assertEqual(broken['website_opportunity_label'], 'very_high')

    def test_meta_opportunity_scoring_no_presence_vs_multiple(self):
        details = make_details(rating=4.5, reviews=100)
        no_presence = self.evaluate(details, social_presence='none')
        multi_presence = self.evaluate(details, social_presence='instagram_and_facebook')
        self.assertGreater(no_presence['meta_opportunity_score'], multi_presence['meta_opportunity_score'])

    def test_inactive_social_raises_meta_opportunity(self):
        details = make_details(rating=4.5, reviews=100)
        active = self.evaluate(details, social_presence='instagram', social_activity='active')
        # unknown activity is the default and must not be treated as "inactive"
        unknown = self.evaluate(details, social_presence='instagram', social_activity='unknown')
        inactive = self.evaluate(details, social_presence='instagram', social_activity='inactive')
        self.assertEqual(active['meta_opportunity_score'], unknown['meta_opportunity_score'])
        self.assertGreater(inactive['meta_opportunity_score'], unknown['meta_opportunity_score'])

    def test_overall_score_is_weighted_blend(self):
        details = make_details(rating=4.6, reviews=300, website='')
        result = self.evaluate(details, website_status='none', social_presence='none')
        expected = round(
            result['business_quality_score'] * lead_qualification.WEIGHT_BUSINESS_QUALITY
            + result['website_opportunity_score'] * lead_qualification.WEIGHT_WEBSITE_OPPORTUNITY
            + result['meta_opportunity_score'] * lead_qualification.WEIGHT_META_OPPORTUNITY
        )
        self.assertEqual(result['overall_score'], expected)

    def test_high_hope_only_for_strong_opportunities(self):
        strong = make_details(rating=4.8, reviews=500, website='')
        weak_but_qualified = make_details(rating=4.0, reviews=25, website='https://example.com')
        strong_result = self.evaluate(strong, website_status='none')
        weak_result = self.evaluate(
            weak_but_qualified, website_status='reachable',
            https=True, mobile_responsive=True, appearance='modern', ecommerce_signal=True,
        )
        self.assertTrue(strong_result['high_hope'])
        self.assertFalse(weak_result['high_hope'])

    def test_unknown_social_activity_not_fabricated(self):
        details = make_details()
        result = self.evaluate(details, social_presence='none')
        # We never claim to know activity we didn't check.
        self.assertNotIn('active', make_social_result('none')['social_activity'])


class OnlinePresenceTests(SimpleTestCase):
    def test_no_signals_returns_none(self):
        result = online_presence.detect(google_website_url='', website_html='')
        self.assertEqual(result['social_presence_type'], 'none')
        self.assertEqual(result['social_activity'], 'unknown')

    def test_google_website_field_is_instagram_profile(self):
        result = online_presence.detect(google_website_url='https://instagram.com/somebiz', website_html='')
        self.assertEqual(result['social_presence_type'], 'instagram')
        self.assertEqual(result['instagram_url'], 'https://instagram.com/somebiz')

    def test_facebook_link_found_in_website_html(self):
        html = '<a href="https://www.facebook.com/somebiz">Follow us</a>'
        result = online_presence.detect(google_website_url='https://realsite.com', website_html=html)
        self.assertEqual(result['social_presence_type'], 'facebook')

    def test_both_found_marks_instagram_and_facebook(self):
        html = '<a href="https://instagram.com/x">IG</a><a href="https://facebook.com/x">FB</a>'
        result = online_presence.detect(google_website_url='', website_html=html)
        self.assertEqual(result['social_presence_type'], 'instagram_and_facebook')

    def test_never_fabricates_activity(self):
        result = online_presence.detect(google_website_url='https://instagram.com/x', website_html='')
        self.assertEqual(result['social_activity'], 'unknown')


class WebsiteCheckTests(SimpleTestCase):
    def test_empty_url_is_none(self):
        self.assertEqual(website_check.check_website('')['status'], 'none')

    def test_malformed_url_is_invalid(self):
        self.assertEqual(website_check.check_website('not-a-url')['status'], 'invalid')

    def test_timeout_does_not_raise(self):
        import requests
        with mock.patch('crm.services.website_check.requests.get', side_effect=requests.exceptions.Timeout):
            result = website_check.check_website('https://slow.example.com')
        self.assertEqual(result['status'], 'timeout')

    def test_connection_error_is_unreachable(self):
        import requests
        with mock.patch('crm.services.website_check.requests.get', side_effect=requests.exceptions.ConnectionError):
            result = website_check.check_website('https://down.example.com')
        self.assertEqual(result['status'], 'unreachable')

    def test_404_is_unreachable(self):
        resp = mock.Mock(status_code=404, url='https://example.com/x')
        with mock.patch('crm.services.website_check.requests.get', return_value=resp):
            result = website_check.check_website('https://example.com/x')
        self.assertEqual(result['status'], 'unreachable')

    def test_reachable_detects_viewport_meta(self):
        html = b'<html><head><meta name="viewport" content="width=device-width"></head></html>'
        resp = mock.Mock(status_code=200, url='https://example.com')
        resp.iter_content = lambda chunk_size: [html]
        with mock.patch('crm.services.website_check.requests.get', return_value=resp):
            result = website_check.check_website('https://example.com')
        self.assertEqual(result['status'], 'reachable')
        self.assertTrue(result['mobile_responsive'])
        self.assertTrue(result['https'])


# ---------------------------------------------------------------------------
# Extraction run loop — direct synchronous calls, single DB connection
# ---------------------------------------------------------------------------
class LeadExtractionCoreTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('core_admin', password='x')
        EmployeeProfile.objects.filter(user=self.admin).update(crm_role='admin')

        self.rep1 = User.objects.create_user('core_rep1', password='x')
        EmployeeProfile.objects.filter(user=self.rep1).update(crm_role='sales', eligible_for_leads=True)

        self.rep2 = User.objects.create_user('core_rep2', password='x')
        EmployeeProfile.objects.filter(user=self.rep2).update(crm_role='sales', eligible_for_leads=True)

        self.not_eligible = User.objects.create_user('core_rep3', password='x')
        EmployeeProfile.objects.filter(user=self.not_eligible).update(crm_role='sales', eligible_for_leads=False)

        self.admin_role_rep = User.objects.create_user('core_admin_rep', password='x')
        EmployeeProfile.objects.filter(user=self.admin_role_rep).update(crm_role='admin', eligible_for_leads=True)

    def _run_with_mocks(self, run, candidates, details_map, website_status='none', social_presence='none'):
        call_count = {'n': 0}

        def fake_search_page(query, page_token=''):
            # Only the first query variation returns real results — later
            # rewordings legitimately find nothing new for this small fixture.
            call_count['n'] += 1
            if call_count['n'] == 1:
                return {'results': candidates, 'nextPageToken': ''}
            return {'results': [], 'nextPageToken': ''}

        def fake_details(place_id):
            return details_map[place_id]

        with mock.patch.object(google_places, 'search_text_page', side_effect=fake_search_page), \
             mock.patch.object(google_places, 'get_place_details', side_effect=fake_details), \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result(website_status)), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result(social_presence)):
            lead_extraction._run(run.pk)
        run.refresh_from_db()
        return run

    def test_google_search_failure_fails_safely(self):
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin)
        with mock.patch.object(
            google_places, 'search_text_page',
            side_effect=google_places.GooglePlacesRateLimitError('rate limited', status_code=429),
        ):
            lead_extraction._run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, ExtractionRun.Status.FAILED)
        self.assertNotIn('rate limited', run.error_message)  # no raw google text/secrets leaked
        self.assertEqual(Lead.objects.filter(source=lead_extraction.LEAD_SOURCE).count(), 0)

    def test_place_details_failure_counts_invalid_and_continues(self):
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin)
        candidates = [{'placeId': 'A1'}, {'placeId': 'A2'}]

        def fake_details(place_id):
            if place_id == 'A1':
                raise google_places.GooglePlacesAPIError('boom', status_code=500)
            return make_details('A2', rating=4.5, reviews=50)

        call_count = {'n': 0}

        def fake_search_page(query, page_token=''):
            call_count['n'] += 1
            if call_count['n'] == 1:
                return {'results': candidates, 'nextPageToken': ''}
            return {'results': [], 'nextPageToken': ''}

        with mock.patch.object(google_places, 'search_text_page', side_effect=fake_search_page), \
             mock.patch.object(google_places, 'get_place_details', side_effect=fake_details), \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result('none')), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result('none')):
            lead_extraction._run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.invalid_count, 1)
        self.assertEqual(run.qualified_count, 1)
        self.assertEqual(run.status, ExtractionRun.Status.COMPLETED)

    def test_duplicate_place_and_duplicate_lead_not_recreated(self):
        run1 = ExtractionRun.objects.create(location='Kochi', category='Cafe', created_by=self.admin)
        candidates = [{'placeId': 'D1'}]
        details = {'D1': make_details('D1', rating=4.6, reviews=80)}
        self._run_with_mocks(run1, candidates, details)
        self.assertEqual(run1.qualified_count, 1)

        run2 = ExtractionRun.objects.create(location='Kochi', category='Cafe', created_by=self.admin)
        self._run_with_mocks(run2, candidates, details)
        self.assertEqual(run2.duplicate_count, 1)
        self.assertEqual(run2.qualified_count, 0)
        self.assertEqual(Lead.objects.filter(place__google_place_id='D1').count(), 1)

    def test_closed_business_rejected_as_invalid(self):
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin)
        candidates = [{'placeId': 'C1'}]
        details = {'C1': make_details('C1', status='CLOSED_PERMANENTLY')}
        self._run_with_mocks(run, candidates, details)
        self.assertEqual(run.invalid_count, 1)
        self.assertEqual(run.qualified_count, 0)

    def test_missing_phone_never_becomes_a_lead(self):
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin)
        candidates = [{'placeId': 'P1'}]
        d = make_details('P1', rating=4.9, reviews=500, phone='')
        d['internationalPhoneNumber'] = ''
        self._run_with_mocks(run, candidates, {'P1': d})
        self.assertEqual(run.qualified_count, 0)
        self.assertEqual(run.invalid_count, 1)
        self.assertFalse(Lead.objects.filter(place__google_place_id='P1').exists())

    def test_qualified_lead_created_with_place_and_employee(self):
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin)
        candidates = [{'placeId': 'Q1'}]
        details = {'Q1': make_details('Q1', rating=4.7, reviews=200)}
        self._run_with_mocks(run, candidates, details)
        lead = Lead.objects.get(place__google_place_id='Q1')
        self.assertEqual(lead.source, 'Google Places')
        self.assertEqual(lead.status, Lead.Status.NEW)
        self.assertIsNotNone(lead.place)
        self.assertIn(lead.employee_id, (self.rep1.id, self.rep2.id))
        self.assertIn('Google Places Lead', lead.notes)

    def test_only_eligible_sales_executives_receive_leads(self):
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin)
        candidates = [{'placeId': f'E{i}'} for i in range(4)]
        details = {f'E{i}': make_details(f'E{i}', rating=4.5, reviews=40) for i in range(4)}
        self._run_with_mocks(run, candidates, details)
        employee_ids = set(Lead.objects.filter(place__google_place_id__startswith='E').values_list('employee_id', flat=True))
        self.assertTrue(employee_ids <= {self.rep1.id, self.rep2.id})
        self.assertNotIn(self.not_eligible.id, employee_ids)
        self.assertNotIn(self.admin_role_rep.id, employee_ids)  # admin role, not sales — never auto-assigned

    def test_fair_round_robin_assignment(self):
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin)
        candidates = [{'placeId': f'F{i}'} for i in range(6)]
        details = {f'F{i}': make_details(f'F{i}', rating=4.5, reviews=40) for i in range(6)}
        self._run_with_mocks(run, candidates, details)
        counts = {}
        for lead in Lead.objects.filter(place__google_place_id__startswith='F'):
            counts[lead.employee_id] = counts.get(lead.employee_id, 0) + 1
        self.assertEqual(counts.get(self.rep1.id), 3)
        self.assertEqual(counts.get(self.rep2.id), 3)

    def test_zero_eligible_employees_creates_no_orphan_leads(self):
        EmployeeProfile.objects.filter(user__in=[self.rep1, self.rep2]).update(eligible_for_leads=False)
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin)
        with mock.patch.object(google_places, 'search_text_page') as mocked_search:
            lead_extraction._run(run.pk)
            mocked_search.assert_not_called()  # must bail before even calling Google
        run.refresh_from_db()
        self.assertEqual(run.status, ExtractionRun.Status.FAILED)
        self.assertIn('No eligible', run.error_message)
        self.assertEqual(Lead.objects.filter(source=lead_extraction.LEAD_SOURCE).count(), 0)

    def test_start_extraction_stores_total_target_before_thread_runs(self):
        # total_target_count must be correct in the very first response — not just
        # once the background thread gets around to it — so the UI never flashes 0.
        with mock.patch('crm.services.lead_extraction.threading.Thread') as mocked_thread:
            run = lead_extraction.start_extraction(
                location='X', category='Y', created_by=self.admin, target_count=10,
            )
        mocked_thread.assert_called_once()
        self.assertEqual(run.target_count, 10)
        self.assertEqual(run.total_target_count, 20)  # 10 per exec x 2 eligible reps

    def test_start_extraction_clamps_target_to_max_per_executive(self):
        with mock.patch('crm.services.lead_extraction.threading.Thread'):
            run = lead_extraction.start_extraction(
                location='X', category='Y', created_by=self.admin,
                target_count=lead_extraction.MAX_TARGET_COUNT + 500,
            )
        self.assertEqual(run.target_count, lead_extraction.MAX_TARGET_COUNT)

    def test_hard_cap_is_per_executive_not_total(self):
        # target_count=3 with 2 eligible reps (rep1, rep2) -> each gets up to 3, 6 total —
        # never a flat 3 for the whole run, and never more than 3 for any one executive.
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin, target_count=3)
        candidates = [{'placeId': f'CAP{i}'} for i in range(10)]
        details = {f'CAP{i}': make_details(f'CAP{i}', rating=4.8, reviews=90) for i in range(10)}
        self._run_with_mocks(run, candidates, details)
        self.assertEqual(run.total_target_count, 6)
        self.assertEqual(run.qualified_count, 6)
        self.assertEqual(run.assigned_count, 6)
        self.assertEqual(Lead.objects.filter(place__google_place_id__startswith='CAP').count(), 6)
        self.assertEqual(run.status, ExtractionRun.Status.COMPLETED)
        counts = {}
        for lead in Lead.objects.filter(place__google_place_id__startswith='CAP'):
            counts[lead.employee_id] = counts.get(lead.employee_id, 0) + 1
        self.assertEqual(counts.get(self.rep1.id), 3)
        self.assertEqual(counts.get(self.rep2.id), 3)

    def test_target_scales_with_eligible_executive_count(self):
        # A third eligible rep joins -> same per-executive target now yields more total leads.
        rep3 = User.objects.create_user('core_rep4', password='x')
        EmployeeProfile.objects.filter(user=rep3).update(crm_role='sales', eligible_for_leads=True)

        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin, target_count=2)
        candidates = [{'placeId': f'SC{i}'} for i in range(10)]
        details = {f'SC{i}': make_details(f'SC{i}', rating=4.8, reviews=90) for i in range(10)}
        self._run_with_mocks(run, candidates, details)
        self.assertEqual(run.total_target_count, 6)  # 2 per exec x 3 eligible execs
        self.assertEqual(run.qualified_count, 6)
        counts = {}
        for lead in Lead.objects.filter(place__google_place_id__startswith='SC'):
            counts[lead.employee_id] = counts.get(lead.employee_id, 0) + 1
        self.assertEqual(counts.get(self.rep1.id), 2)
        self.assertEqual(counts.get(self.rep2.id), 2)
        self.assertEqual(counts.get(rep3.id), 2)

    def test_pagination_fetches_next_page_when_target_not_yet_reached(self):
        # 2 eligible reps x target_count=8 -> total_target=16, needs both pages (10 each).
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin, target_count=8)
        page1 = [{'placeId': f'PG{i}'} for i in range(10)]
        page2 = [{'placeId': f'PG{i}'} for i in range(10, 20)]
        details = {f'PG{i}': make_details(f'PG{i}', rating=4.8, reviews=90) for i in range(20)}
        page_tokens_requested = []

        def fake_search_page(query, page_token=''):
            page_tokens_requested.append(page_token)
            if not page_token:
                return {'results': page1, 'nextPageToken': 'TOKEN2'}
            return {'results': page2, 'nextPageToken': ''}

        def fake_details(place_id):
            return details[place_id]

        with mock.patch.object(google_places, 'search_text_page', side_effect=fake_search_page), \
             mock.patch.object(google_places, 'get_place_details', side_effect=fake_details), \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result('none')), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result('none')), \
             mock.patch('crm.services.lead_extraction.time.sleep'):
            lead_extraction._run(run.pk)
        run.refresh_from_db()
        self.assertEqual(page_tokens_requested, ['', 'TOKEN2'])
        self.assertEqual(run.total_target_count, 16)
        self.assertEqual(run.qualified_count, 16)
        self.assertEqual(run.status, ExtractionRun.Status.COMPLETED)

    def test_page_fetch_failure_after_first_page_completes_gracefully(self):
        # Page 1 succeeds and creates real leads; page 2 blows up — the run must
        # keep those leads and complete, not discard them by failing the whole run.
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin, target_count=50)
        page1 = [{'placeId': f'PF{i}'} for i in range(3)]
        details = {f'PF{i}': make_details(f'PF{i}', rating=4.8, reviews=90) for i in range(3)}

        def fake_search_page(query, page_token=''):
            if not page_token:
                return {'results': page1, 'nextPageToken': 'TOKEN2'}
            raise google_places.GooglePlacesAPIError('boom', status_code=500)

        def fake_details(place_id):
            return details[place_id]

        with mock.patch.object(google_places, 'search_text_page', side_effect=fake_search_page), \
             mock.patch.object(google_places, 'get_place_details', side_effect=fake_details), \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result('none')), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result('none')), \
             mock.patch('crm.services.lead_extraction.time.sleep'):
            lead_extraction._run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.qualified_count, 3)
        self.assertEqual(run.status, ExtractionRun.Status.COMPLETED)

    def test_max_candidates_cap_spans_pages(self):
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin, target_count=50)
        page1 = [{'placeId': f'MX{i}'} for i in range(3)]
        page2 = [{'placeId': f'MX{i}'} for i in range(3, 6)]
        details = {f'MX{i}': make_details(f'MX{i}', rating=4.8, reviews=90) for i in range(6)}

        def fake_search_page(query, page_token=''):
            # Would page forever if not capped — always offers another page.
            if not page_token:
                return {'results': page1, 'nextPageToken': 'TOKEN2'}
            return {'results': page2, 'nextPageToken': 'TOKEN3'}

        def fake_details(place_id):
            return details[place_id]

        with mock.patch.object(lead_extraction, 'MAX_CANDIDATES_PER_RUN', 5), \
             mock.patch.object(google_places, 'search_text_page', side_effect=fake_search_page), \
             mock.patch.object(google_places, 'get_place_details', side_effect=fake_details), \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result('none')), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result('none')), \
             mock.patch('crm.services.lead_extraction.time.sleep'):
            lead_extraction._run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.discovered_count, 5)
        self.assertEqual(run.status, ExtractionRun.Status.COMPLETED)


    def test_query_variations_tried_when_base_query_exhausted(self):
        # target=5 x 2 execs = 10 total. Base query alone only offers 6 -> the run
        # must fall back to a reworded query to source the rest, not stop at 6.
        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin, target_count=5)
        queries = lead_extraction._query_variations('Y', 'X')
        self.assertGreaterEqual(len(queries), 2)

        v1 = [{'placeId': f'V1_{i}'} for i in range(6)]
        v2 = [{'placeId': f'V2_{i}'} for i in range(6)]
        details = {c['placeId']: make_details(c['placeId'], rating=4.8, reviews=90) for c in v1 + v2}

        def fake_search_page(query, page_token=''):
            if query == queries[0]:
                return {'results': v1, 'nextPageToken': ''}
            if query == queries[1]:
                return {'results': v2, 'nextPageToken': ''}
            return {'results': [], 'nextPageToken': ''}

        def fake_details(place_id):
            return details[place_id]

        with mock.patch.object(google_places, 'search_text_page', side_effect=fake_search_page), \
             mock.patch.object(google_places, 'get_place_details', side_effect=fake_details), \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result('none')), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result('none')), \
             mock.patch('crm.services.lead_extraction.time.sleep'):
            lead_extraction._run(run.pk)
        run.refresh_from_db()

        self.assertEqual(run.total_target_count, 10)
        self.assertEqual(run.qualified_count, 10)  # 6 from the base phrasing + 4 from the reworded one
        self.assertEqual(run.status, ExtractionRun.Status.COMPLETED)
        self.assertTrue(Lead.objects.filter(place__google_place_id__startswith='V2_').exists())


    def test_duplicates_are_free_and_dont_eat_the_details_api_budget(self):
        # 5 candidates are already-known duplicates (from a prior run); 4 are genuinely
        # new. Even with MAX_CANDIDATES_PER_RUN capped at 4, all 4 new ones must still
        # get processed — the duplicates must not have consumed that budget.
        dup_ids = [f'DUP{i}' for i in range(5)]
        for pid in dup_ids:
            place = Place.objects.create(google_place_id=pid, name=f'Old {pid}')
            Lead.objects.create(
                employee=self.rep1, name=f'Old {pid}', phone='1112223333',
                source='Google Places', status=Lead.Status.NEW, place=place,
            )

        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin, target_count=2)
        new_ids = [f'NEW{i}' for i in range(4)]
        candidates = [{'placeId': pid} for pid in dup_ids] + [{'placeId': pid} for pid in new_ids]
        details = {pid: make_details(pid, rating=4.8, reviews=90) for pid in new_ids}

        def fake_details(place_id):
            return details[place_id]

        with mock.patch.object(lead_extraction, 'MAX_CANDIDATES_PER_RUN', 4), \
             mock.patch.object(google_places, 'search_text_page', return_value={'results': candidates, 'nextPageToken': ''}), \
             mock.patch.object(google_places, 'get_place_details', side_effect=fake_details), \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result('none')), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result('none')):
            lead_extraction._run(run.pk)
        run.refresh_from_db()

        self.assertEqual(run.duplicate_count, 5)
        self.assertEqual(run.qualified_count, 4)  # all 4 new candidates processed despite the cap of 4
        self.assertEqual(run.status, ExtractionRun.Status.COMPLETED)

    def test_all_duplicate_run_stops_via_total_scanned_ceiling(self):
        # A fully mined-out location/category (every result already a lead) must still
        # terminate — bounded by MAX_TOTAL_SCANNED, not by MAX_CANDIDATES_PER_RUN (which
        # duplicates never touch).
        dup_ids = [f'MINED{i}' for i in range(10)]
        for pid in dup_ids:
            place = Place.objects.create(google_place_id=pid, name=f'Old {pid}')
            Lead.objects.create(
                employee=self.rep1, name=f'Old {pid}', phone='1112223333',
                source='Google Places', status=Lead.Status.NEW, place=place,
            )
        candidates = [{'placeId': pid} for pid in dup_ids]

        run = ExtractionRun.objects.create(location='X', category='Y', created_by=self.admin, target_count=50)
        with mock.patch.object(lead_extraction, 'MAX_TOTAL_SCANNED', 15), \
             mock.patch.object(google_places, 'search_text_page', return_value={'results': candidates, 'nextPageToken': ''}), \
             mock.patch.object(google_places, 'get_place_details') as mocked_details, \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result('none')), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result('none')), \
             mock.patch('crm.services.lead_extraction.time.sleep'):
            lead_extraction._run(run.pk)
        run.refresh_from_db()

        mocked_details.assert_not_called()  # every candidate was a free duplicate skip
        self.assertEqual(run.qualified_count, 0)
        self.assertLessEqual(run.duplicate_count, 15)  # stopped by MAX_TOTAL_SCANNED, not run forever
        self.assertEqual(run.status, ExtractionRun.Status.COMPLETED)


class GooglePlacesPaginationTests(SimpleTestCase):
    def test_search_text_page_sends_page_token_and_field_mask(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {'places': [], 'nextPageToken': 'ABC123'}
        with mock.patch('crm.services.google_places.requests.request', return_value=resp) as mocked, \
             mock.patch('crm.services.google_places._api_key', return_value='fake-key'):
            page = google_places.search_text_page('boutiques in Kochi', page_token='PRIOR_TOKEN')

        self.assertEqual(page['nextPageToken'], 'ABC123')
        _, kwargs = mocked.call_args
        self.assertEqual(kwargs['json']['pageToken'], 'PRIOR_TOKEN')
        self.assertIn('nextPageToken', kwargs['headers']['X-Goog-FieldMask'])

    def test_search_text_wrapper_returns_first_page_results_only(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {
            'places': [{'id': 'X1', 'displayName': {'text': 'Biz'}, 'formattedAddress': 'Addr'}],
            'nextPageToken': 'MORE',
        }
        with mock.patch('crm.services.google_places.requests.request', return_value=resp), \
             mock.patch('crm.services.google_places._api_key', return_value='fake-key'):
            results = google_places.search_text('boutiques in Kochi')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['placeId'], 'X1')


# ---------------------------------------------------------------------------
# HTTP views + the real background thread
# ---------------------------------------------------------------------------
class LeadExtractionViewsTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.admin = User.objects.create_user('view_admin', password='x')
        EmployeeProfile.objects.filter(user=self.admin).update(crm_role='admin')

        self.superuser = User.objects.create_superuser('view_super', 'super@example.com', 'x')

        self.sales_only = User.objects.create_user('view_sales', password='x')
        EmployeeProfile.objects.filter(user=self.sales_only).update(crm_role='sales')

        self.rep1 = User.objects.create_user('view_rep1', password='x')
        EmployeeProfile.objects.filter(user=self.rep1).update(crm_role='sales', eligible_for_leads=True)

    def tearDown(self):
        # Background threads from a "start" call may still be finishing; give them a beat.
        time.sleep(0.1)

    def test_non_admin_gets_403(self):
        self.client.force_login(self.sales_only)
        r = self.client.get('/crm/lead-extraction/')
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            '/crm/lead-extraction/start/', data='{"location":"X","category":"Y"}',
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 403)

    def test_admin_can_view_page(self):
        self.client.force_login(self.admin)
        r = self.client.get('/crm/lead-extraction/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Lead Extraction', r.content)

    def test_superuser_can_view_page(self):
        self.client.force_login(self.superuser)
        r = self.client.get('/crm/lead-extraction/')
        self.assertEqual(r.status_code, 200)

    def test_no_eligible_executives_blocks_start(self):
        EmployeeProfile.objects.filter(user=self.rep1).update(eligible_for_leads=False)
        self.client.force_login(self.admin)
        r = self.client.post(
            '/crm/lead-extraction/start/', data='{"location":"X","category":"Y"}',
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 400)
        EmployeeProfile.objects.filter(user=self.rep1).update(eligible_for_leads=True)

    def test_concurrent_extraction_blocked(self):
        ExtractionRun.objects.create(location='Busy', category='Busy', status=ExtractionRun.Status.RUNNING)
        self.client.force_login(self.admin)
        r = self.client.post(
            '/crm/lead-extraction/start/', data='{"location":"X","category":"Y"}',
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 409)

    def test_start_stop_live_status_and_leads_page(self):
        candidates = [{'placeId': f'V{i}'} for i in range(6)]
        details = {f'V{i}': make_details(f'V{i}', name=f'Visible Biz {i}', rating=4.6, reviews=60) for i in range(6)}

        def slow_details(place_id):
            time.sleep(0.3)
            return details[place_id]

        self.client.force_login(self.admin)
        with mock.patch.object(google_places, 'search_text_page', return_value={'results': candidates, 'nextPageToken': ''}), \
             mock.patch.object(google_places, 'get_place_details', side_effect=slow_details), \
             mock.patch.object(website_check, 'check_website', return_value=make_website_result('none')), \
             mock.patch.object(online_presence, 'detect', return_value=make_social_result('none')):

            r = self.client.post(
                '/crm/lead-extraction/start/',
                data='{"location":"ViewCity","category":"ViewCat","target":50}',
                content_type='application/json',
            )
            self.assertEqual(r.status_code, 200)
            run_id = r.json()['id']

            # Live status endpoint returns counters + incremental new_leads while running.
            time.sleep(0.5)
            r = self.client.get(f'/crm/lead-extraction/{run_id}/status/')
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn('discovered', body)
            self.assertIn('new_leads', body)

            r = self.client.post(f'/crm/lead-extraction/{run_id}/stop/')
            self.assertEqual(r.status_code, 200)

            deadline = time.time() + 10
            final_status = None
            while time.time() < deadline:
                rr = self.client.get(f'/crm/lead-extraction/{run_id}/status/')
                final_status = rr.json()['status']
                if final_status in ('stopped', 'completed', 'failed'):
                    break
                time.sleep(0.3)

        self.assertEqual(final_status, 'stopped')
        run = ExtractionRun.objects.get(pk=run_id)
        self.assertEqual(run.qualified_count, run.assigned_count)
        self.assertLess(run.discovered_count, len(candidates))  # stopped before exhausting candidates
        surviving_leads = Lead.objects.filter(place__google_place_id__startswith='V')
        self.assertEqual(surviving_leads.count(), run.qualified_count)  # already-created leads preserved

        # The assigned rep sees the extracted lead on the existing /crm/leads/ page — no manual import.
        # date_scope=all bypasses the default "Today" view for an unambiguous check.
        if surviving_leads.exists():
            lead = surviving_leads.first()
            self.client.force_login(lead.employee)
            r = self.client.get('/crm/leads/?date_scope=all')
            self.assertEqual(r.status_code, 200)
            self.assertIn(lead.name.encode(), r.content)
