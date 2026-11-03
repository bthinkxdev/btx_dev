"""Diwali Special: the master switch and the glitter / crackers switches in Site settings."""
import re
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import SiteSettings


@override_settings(ALLOWED_HOSTS=['testserver'])
class DiwaliSettingsTests(TestCase):
    def setUp(self):
        cache.clear()

    def home(self):
        return self.client.get(reverse('pages:index')).content.decode()

    def test_defaults_show_everything(self):
        html = self.home()
        self.assertIn('id="diwali-fx"', html)
        self.assertIn('data-glitter="1"', html)
        self.assertIn('data-crackers="1"', html)
        self.assertIn('assets/diwali.js', html)
        self.assertIn('nav-offer', html)
        self.assertIn('diwali-lights', html)
        self.assertIn('Light the diya', html)
        self.assertIn('<html lang="en" data-diwali="1">', html)
        self.assertIn("localStorage.getItem('btx_diwali_mode')", html)
        self.assertNotIn('diwaliGame', html)           # the crackers card is gone

    def test_crackers_off_keeps_glitter(self):
        SiteSettings.objects.create(diwali_crackers_enabled=False)
        html = self.home()
        self.assertIn('data-glitter="1"', html)
        self.assertIn('data-crackers="0"', html)

    def test_both_effects_off_drops_the_script_but_keeps_the_offer(self):
        SiteSettings.objects.create(diwali_crackers_enabled=False, diwali_glitter_enabled=False)
        html = self.home()
        self.assertNotIn('id="diwali-fx"', html)
        self.assertIn('assets/diwali.js', html)      # still needed by the lead card
        self.assertNotIn('class="cracker"', html)
        self.assertIn('Get the Diwali Offer', html)

    def test_master_switch_removes_everything(self):
        SiteSettings.objects.create(diwali_enabled=False)
        html = self.home()
        for needle in ('diwali-fx', 'diwali.js', 'nav-offer', 'diwali-note', 'diwali-lights', 'btx_diwali_mode',
                       'data-diwali', 'Get the Diwali Offer'):
            self.assertNotIn(needle, html, needle)

    def test_end_date_switches_it_off(self):
        SiteSettings.objects.create(diwali_ends_on=timezone.localdate() - timedelta(days=1))
        self.assertNotIn('diwali.js', self.home())
        self.assertNotIn('diwaliGame', self.home())

    def test_countdown(self):
        today = timezone.localdate()
        s = SiteSettings.objects.create(diwali_date=today + timedelta(days=5))
        self.assertIn('5 days to Diwali', self.home())
        s.diwali_date = today + timedelta(days=1)
        s.save()
        self.assertIn('Diwali is tomorrow', self.home())
        s.diwali_date = today
        s.save()
        self.assertIn('Diwali is today', self.home())
        s.diwali_date = today - timedelta(days=3)
        s.save()
        self.assertNotIn('class="diwali-count"', self.home())
        s.diwali_date = None
        s.save()
        self.assertNotIn('class="diwali-count"', self.home())


@override_settings(ALLOWED_HOSTS=['testserver'])
class NoEmojiTests(TestCase):
    """The site does not use emojis anywhere in the Diwali work."""

    def test_home_page_has_no_emoji(self):
        cache.clear()
        html = self.client.get(reverse('pages:index'), HTTP_HOST='testserver').content.decode()
        found = re.findall('[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2728]', html)
        self.assertEqual(found, [])

    def test_settings_row_is_cached_and_busted_on_save(self):
        cache.clear()
        self.assertTrue(SiteSettings.current().diwali_enabled)
        with self.assertNumQueries(0):
            SiteSettings.current()
        row = SiteSettings.objects.create(diwali_enabled=False)
        self.assertFalse(SiteSettings.current().diwali_enabled)
        row.diwali_enabled = True
        row.save()
        self.assertTrue(SiteSettings.current().diwali_enabled)


@override_settings(ALLOWED_HOSTS=['testserver'])
class FaviconTests(TestCase):
    """Google shows the site icon in search results from /favicon.ico or the <link rel=icon> tags."""

    def test_root_favicon_is_served(self):
        r = self.client.get('/favicon.ico')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'image/x-icon')
        self.assertEqual(b''.join(r.streaming_content)[:4], b'\x00\x00\x01\x00')     # ICO header

    def test_pages_link_square_png_and_ico_icons(self):
        cache.clear()
        html = self.client.get(reverse('pages:index')).content.decode()
        self.assertIn('<link rel="icon" href="/favicon.ico" sizes="any">', html)
        for size in ('48x48', '96x96', '192x192'):
            self.assertIn(f'sizes="{size}"', html)
        self.assertIn('apple-touch-icon', html)
        self.assertNotIn('logo.jpg" type="image/jpeg"', html)

    def test_icons_are_not_blocked_for_crawlers(self):
        robots = self.client.get('/robots.txt').content.decode()
        self.assertNotIn('Disallow: /static', robots)
        self.assertNotIn('Disallow: /favicon', robots)
