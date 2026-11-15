"""Guards that keep the system deployable: every scheduled job runs, migrations are complete, and the app shell stays a phone app."""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.urls import reverse

from crm.core import jobs

from .base import PASSWORD, CrmTestCase


class DeployGuardTests(CrmTestCase):
    def test_every_scheduled_job_runs_without_failing(self):
        """A job that raises on an ordinary database would show as failed on the Jobs page every night."""
        failed = {}
        for name in jobs.JOBS:
            result = jobs.run_job(name, force=True)
            if result['status'] not in ('succeeded', 'skipped'):
                failed[name] = result
        self.assertEqual(failed, {})

    def test_the_job_runner_command_runs_all_groups(self):
        out = StringIO()
        call_command('crm_run_jobs', '--group', 'all', stdout=out)
        self.assertNotIn('failed', out.getvalue().lower())

    def test_no_model_change_is_missing_a_migration(self):
        out = StringIO()
        try:
            call_command('makemigrations', '--check', '--dry-run', stdout=out)
        except SystemExit:
            self.fail(f'Models changed without a migration: {out.getvalue()}')

    def test_every_job_has_a_valid_group_and_a_real_function(self):
        from importlib import import_module
        for name, (path, group, minutes) in jobs.JOBS.items():
            module, _, fn = path.rpartition('.')
            self.assertTrue(callable(getattr(import_module(module), fn)), name)
            self.assertIn(group, ('frequent', 'daily'), name)
            self.assertGreater(minutes, 0, name)


class PhoneAppShellTests(CrmTestCase):
    """The CRM is used on phones like an app: installable, with a bottom tab bar, and nothing that forces sideways scrolling."""

    def page(self, who='sales'):
        from django.test import Client
        c = Client()
        c.post(reverse('crm:login'), {'username': who, 'password': PASSWORD})
        c.post(reverse('crm:work_notice_ack'))
        return c.get(reverse('crm:my_day')).content.decode()

    def test_it_is_an_installable_phone_app(self):
        html = self.page()
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1.0"', html)
        self.assertIn('rel="manifest"', html)
        self.assertIn('name="theme-color"', html)
        self.assertIn('apple-mobile-web-app-capable', html)
        self.assertIn('class="bottom-nav', html)                       # the tab bar at the bottom of a phone screen
        self.assertIn('Open full menu', html)

    def test_the_manifest_and_service_worker_are_served(self):
        from django.test import Client
        c = Client()
        c.post(reverse('crm:login'), {'username': 'sales', 'password': PASSWORD})
        m = c.get(reverse('crm:manifest'))
        self.assertEqual(m.status_code, 200)
        self.assertIn('"display"', m.content.decode())
        self.assertEqual(c.get(reverse('crm:service_worker')).status_code, 200)

    def test_the_work_clock_never_crowds_the_phone_header(self):
        """On a phone the clock sits in its own strip under the header; the header keeps room for the bell and profile."""
        html = self.page()
        self.assertIn('hidden sm:block', html)                         # in the header only from tablet width up
        self.assertIn('sm:hidden', html)                               # a slim strip on phones
        self.assertIn('work-clock-m', html)
        self.assertEqual(html.count('class="work-clock'), 2)

    def test_administrators_get_no_clock_strip(self):
        self.assertNotIn('work-clock', self.page('owner'))

    def test_the_stylesheet_gives_phones_proper_touch_targets(self):
        import os
        css = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'crm', 'css', 'crm.css'), encoding='utf-8').read()
        self.assertIn('2.75rem', css)                                   # 44px controls on phones
        self.assertIn('pointer:coarse', css.replace(' ', ''))

    def test_a_break_takes_over_the_whole_screen_with_one_button(self):
        from django.test import Client
        c = Client()
        c.post(reverse('crm:login'), {'username': 'sales', 'password': PASSWORD})
        c.post(reverse('crm:break_start'))
        html = c.get(reverse('crm:on_break')).content.decode()
        self.assertIn('fixed inset-0', html)
        self.assertEqual(html.count('<button'), 1)
        self.assertIn('width=device-width', html)
