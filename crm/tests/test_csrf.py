from django.test import Client, TestCase
from django.urls import reverse


class CsrfRecoveryTests(TestCase):
    def test_login_page_is_never_cached_and_sets_cookie(self):
        r = self.client.get(reverse('crm:login'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('no-store', r['Cache-Control'])
        self.assertIn('csrftoken', r.cookies)

    def test_html_pages_are_no_store(self):
        r = self.client.get(reverse('crm:login'))
        self.assertIn('no-store', r['Cache-Control'])

    def test_missing_cookie_redirects_to_login_not_403(self):
        c = Client(enforce_csrf_checks=True)
        r = c.post(reverse('crm:login'), {'username': 'x', 'password': 'y'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('crm:login'))
        self.assertIn('csrftoken', r.cookies)
        page = c.get(r['Location'])
        self.assertContains(page, 'expired')

    def test_retry_after_failure_works(self):
        c = Client(enforce_csrf_checks=True)
        c.post(reverse('crm:login'), {'username': 'x', 'password': 'y'})
        token = c.get(reverse('crm:login')).cookies['csrftoken'].value
        r = c.post(reverse('crm:login'), {'username': 'x', 'password': 'y', 'csrfmiddlewaretoken': token})
        self.assertEqual(r.status_code, 401)  # reached the view: wrong password, not a CSRF block
