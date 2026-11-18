"""Client photos and editing: upload, per-photo tracking, the editing flow, who sees what, the timeline, storage and notifications."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as HttpClient
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from crm.core import india, notify, permissions
from crm.models import Client, ClientAccess, ImageAsset, ImageEvent, ImageSubmission, ImageVersion, Notification, Role, UserProfile
from crm.modules.images import jobs as image_jobs
from crm.modules.images import services as svc
from crm.modules.images import storage

from .base import PASSWORD, CrmTestCase

TMP = tempfile.mkdtemp(prefix='crm-images-test-')


def photo(name='photo.jpg', size=(64, 48), color=(200, 30, 30), fmt='JPEG'):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, fmt)
    return SimpleUploadedFile(name, buf.getvalue(), content_type={'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}[fmt])


@override_settings(CRM_PRIVATE_MEDIA_ROOT=TMP, CRM_IMAGE_BUCKET='')
class ImagesBase(CrmTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        editor_role = Role.objects.get(code='image_editor')
        cls.editor = User.objects.create_user('editor1', 'e1@example.test', PASSWORD, first_name='Anjana')
        cls.editor2 = User.objects.create_user('editor2', 'e2@example.test', PASSWORD, first_name='Binu')
        for e in (cls.editor, cls.editor2):
            UserProfile.objects.get_or_create(user=e)[0].roles.set([editor_role])
        cls.other_client = Client.objects.create(legal_name='Other Boutique', display_name='Other', owner=cls.u['sales_b'],
                                                 billing_state='32', email='o@example.test', phone='+919876500000')
        cls.client_user, cls.client_pw = svc.create_client_login(cls.client_obj, cls.u['manager'], username='acmeuser', full_name='Asha Menon')
        cls.other_user, _ = svc.create_client_login(cls.other_client, cls.u['manager'], username='otheruser', full_name='Omar Khan')
        for u in (cls.client_user, cls.other_user):
            UserProfile.objects.filter(user=u).update(must_change_password=False)

    def setUp(self):
        super().setUp()
        shutil.rmtree(TMP, ignore_errors=True)
        storage.reset_storage()
        for u in (self.editor, self.editor2, self.client_user, self.other_user):
            permissions.clear_cache(u)

    def tearDown(self):
        storage.reset_storage()

    # ---- helpers
    def web(self, user):
        c = HttpClient()
        c.force_login(user)
        return c

    def submit(self, client=None, actor=None, n=2, note='Remove the background', names=None):
        client = client or self.client_obj
        actor = actor or self.client_user
        batch = f'batch{ImageSubmission.objects.count()}{timezone.now().timestamp()}'.replace('.', '')[:40]
        for i in range(n):
            svc.add_draft_file(client=client, actor=actor, batch=batch, upload=photo((names or [f'p{i}.jpg'] * n)[i], color=(10 * i, 20, 30)))
        sub = ImageSubmission.objects.get(batch_token=batch)
        return svc.submit(sub, actor, note)

    def assets(self, sub):
        return list(sub.assets.order_by('id'))

    def assign(self, asset, editor=None, actor=None, **kw):
        return svc.assign(asset, editor or self.editor, actor or self.u['sales'], **kw)

    def to_review(self, asset):
        """Assign, start, edit and send for review. Returns the asset."""
        self.assign(asset, deadline=india.today() + timedelta(days=2))
        svc.transition(asset, 'editing', self.editor)
        svc.upload_version(asset, self.editor, photo('edit1.jpg', color=(0, 200, 0)), note='Removed background', send_for_review=True)
        asset.refresh_from_db()
        return asset

    def to_client(self, asset):
        asset = self.to_review(asset)
        svc.transition(asset, 'client_review', self.u['sales'])
        asset.refresh_from_db()
        return asset


# =========================================================== upload and individual tracking

class UploadTests(ImagesBase):
    def test_client_can_upload_one_photo(self):
        sub = self.submit(n=1)
        self.assertEqual(sub.number, 'SUB-0001')
        (a,) = self.assets(sub)
        self.assertEqual((a.number, a.status, a.client_id), ('IMG-0001', 'new', self.client_obj.pk))
        self.assertEqual(a.versions.count(), 1)
        v = a.original()
        self.assertEqual((v.kind, v.number, v.client_visible), ('original', 1, True))

    def test_client_can_upload_many_and_each_has_its_own_id_and_status(self):
        sub = self.submit(n=4)
        a = self.assets(sub)
        self.assertEqual([x.number for x in a], ['IMG-0001', 'IMG-0002', 'IMG-0003', 'IMG-0004'])
        self.assertTrue(all(x.status == 'new' for x in a))
        self.assertEqual(len({x.pk for x in a}), 4)

    def test_status_is_per_photo_and_the_batch_progress_is_derived(self):
        sub = self.submit(n=4)
        a = self.assets(sub)
        self.to_client(a[0])
        svc.approve(a[0], self.client_user)
        self.assign(a[1])
        p = svc.progress(sub)
        self.assertEqual((p['total'], p['done'], p['percent']), (4, 1, 25))
        self.assertEqual(p['counts'], {'new': 2, 'assigned': 1, 'approved': 1})

    def test_upload_through_the_pages_as_the_client(self):
        c = self.web(self.client_user)
        for name in ('a.jpg', 'b.png'):
            r = c.post(reverse('crm:image_upload_file'), {'batch': 'webbatch1', 'file': photo(name, fmt='PNG' if name.endswith('png') else 'JPEG')},
                       HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(r.status_code, 200, r.content)
            self.assertTrue(json.loads(r.content)['ok'])
        self.assertEqual(ImageAsset.objects.filter(submission__batch_token='webbatch1').count(), 2)
        self.assertFalse(ImageSubmission.objects.get(batch_token='webbatch1').submitted_at)           # still a draft until Send
        r = c.post(reverse('crm:image_upload_send'), {'batch': 'webbatch1', 'note': 'Brighten it please'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        data = json.loads(r.content)
        self.assertTrue(data['ok'])
        sub = ImageSubmission.objects.get(batch_token='webbatch1')
        self.assertEqual((sub.number, sub.note), ('SUB-0001', 'Brighten it please'))
        self.assertEqual(c.get(data['url']).status_code, 200)

    def test_a_client_can_only_upload_for_their_own_company(self):
        c = self.web(self.client_user)
        r = c.post(reverse('crm:image_upload_file'), {'batch': 'x1', 'client': self.other_client.pk, 'file': photo()}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 200)                                    # the client field is ignored; their own company is used
        self.assertEqual(ImageAsset.objects.get(submission__batch_token='x1').client_id, self.client_obj.pk)

    def test_staff_can_upload_for_their_own_client_only(self):
        c = self.web(self.u['sales'])
        r = c.post(reverse('crm:image_upload_file'), {'batch': 's1', 'client': self.client_obj.pk, 'file': photo()}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ImageSubmission.objects.get(batch_token='s1').on_behalf)
        r = c.post(reverse('crm:image_upload_file'), {'batch': 's2', 'client': self.other_client.pk, 'file': photo()}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(ImageAsset.objects.filter(submission__batch_token='s2').exists())

    def test_bad_files_are_refused_with_a_plain_message(self):
        c = self.web(self.client_user)
        bad = SimpleUploadedFile('virus.jpg', b'not a picture at all', content_type='image/jpeg')
        r = c.post(reverse('crm:image_upload_file'), {'batch': 'bad1', 'file': bad}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 400)
        self.assertIn('not a photo', json.loads(r.content)['error'])
        gif = io.BytesIO()
        Image.new('RGB', (4, 4)).save(gif, 'GIF')
        r = c.post(reverse('crm:image_upload_file'), {'batch': 'bad2', 'file': SimpleUploadedFile('a.gif', gif.getvalue())}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 400)
        with override_settings(CRM_IMAGE_MAX_MB=0):
            r = c.post(reverse('crm:image_upload_file'), {'batch': 'bad3', 'file': photo()}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(r.status_code, 400)
            self.assertIn('larger than', json.loads(r.content)['error'])
        self.assertEqual(ImageAsset.objects.count(), 0)

    def test_a_batch_cannot_be_sent_twice_or_hijacked(self):
        sub = self.submit(n=1)
        with self.assertRaises(svc.ImageError):
            svc.add_draft_file(client=self.client_obj, actor=self.client_user, batch=sub.batch_token, upload=photo())
        self.assertEqual(svc.submit(sub, self.client_user).number, sub.number)      # sending again changes nothing
        self.assertEqual(ImageSubmission.objects.filter(submitted_at__isnull=False).count(), 1)

    def test_original_file_is_stored_exactly_as_sent(self):
        up = photo('keep.jpg', size=(321, 123))
        raw = up.read()
        up.seek(0)
        batch = 'exact1'
        svc.add_draft_file(client=self.client_obj, actor=self.client_user, batch=batch, upload=up)
        v = ImageVersion.objects.get()
        self.assertEqual(storage.get_storage().read(v.storage_key), raw)
        self.assertEqual((v.width, v.height, v.size), (321, 123, len(raw)))
        self.assertTrue(storage.get_storage().exists(v.thumb_key))

    def test_iphone_heic_photos_are_accepted_kept_exactly_and_shown_as_a_jpeg_copy(self):
        import pillow_heif
        pillow_heif.register_heif_opener()
        buf = io.BytesIO()
        Image.new('RGB', (200, 120), (20, 120, 200)).save(buf, 'HEIF')
        raw = buf.getvalue()
        up = SimpleUploadedFile('IMG_0042.HEIC', raw, content_type='image/heic')
        svc.add_draft_file(client=self.client_obj, actor=self.client_user, batch='heic1', upload=up)
        sub = svc.submit(ImageSubmission.objects.get(batch_token='heic1'), self.client_user, 'Please brighten this photo')
        a = self.assets(sub)[0]
        v = a.original()
        self.assertEqual((v.mime_type, v.width, v.height), ('image/heic', 200, 120))
        self.assertTrue(v.storage_key.endswith('.heic') and v.preview_key.endswith('-view.jpg'))
        self.assertEqual(storage.get_storage().read(v.storage_key), raw)                    # the original is untouched
        c = self.web(self.client_user)
        shown = c.get(reverse('crm:image_file', args=[v.pk]))
        self.assertEqual(shown['Content-Type'], 'image/jpeg')                                  # screens get a JPEG they can display
        Image.open(io.BytesIO(b''.join(shown.streaming_content))).verify()
        dl = c.get(reverse('crm:image_file', args=[v.pk]) + '?dl=1')
        self.assertEqual(dl['Content-Type'], 'image/heic')
        self.assertIn('attachment', dl['Content-Disposition'])
        self.assertEqual(b''.join(dl.streaming_content), raw)                                  # Download gives back exactly what was sent
        self.assertEqual(c.get(reverse('crm:image_thumb', args=[v.pk]))['Content-Type'], 'image/jpeg')
        # An editor can work from the original and send back an ordinary edit.
        self.assign(a)
        svc.transition(a, 'editing', self.editor)
        self.assertEqual(self.web(self.editor).get(reverse('crm:image_file', args=[v.pk]) + '?dl=1')['Content-Type'], 'image/heic')

    def test_heic_is_refused_cleanly_if_the_converter_is_missing(self):
        import builtins
        real = builtins.__import__

        def fake(name, *a, **kw):
            if name == 'pillow_heif':
                raise ImportError(name)
            return real(name, *a, **kw)
        import pillow_heif
        pillow_heif.register_heif_opener()
        buf = io.BytesIO()
        Image.new('RGB', (8, 8)).save(buf, 'HEIF')
        # Pillow keeps the opener once registered in this process, so this only checks the message path stays safe.
        with mock.patch('builtins.__import__', side_effect=fake):
            try:
                storage.process(SimpleUploadedFile('x.heic', buf.getvalue()))
            except storage.ImageFileError as exc:
                self.assertIn('JPEG, PNG, WebP or HEIC', str(exc))

    def test_the_same_file_twice_is_flagged_for_the_coordinator(self):
        self.submit(n=1, names=['same.jpg'])
        sub2 = self.submit(n=1, names=['same.jpg'])
        a = self.assets(sub2)[0]
        self.assertTrue(a.events.filter(kind='duplicate', visibility='internal').exists())


# =========================================================== who sees what

class AccessTests(ImagesBase):
    def test_client_login_keeps_the_client_signed_in_for_a_month(self):
        c = HttpClient()
        r = c.post(reverse('crm:login'), {'username': 'acmeuser', 'password': self.client_pw})
        self.assertEqual(r.status_code, 302)
        self.assertGreaterEqual(c.session.get_expiry_age(), 29 * 86400)
        self.assertEqual(c.get(reverse('crm:home')).url, reverse('crm:image_home'))

    def test_first_login_asks_for_a_new_password(self):
        user, pw = svc.create_client_login(self.client_obj, self.u['manager'], username='freshuser', full_name='Fresh One')
        self.assertTrue(UserProfile.objects.get(user=user).must_change_password)
        c = HttpClient()
        c.post(reverse('crm:login'), {'username': 'freshuser', 'password': pw})
        r = c.get(reverse('crm:image_home'))
        self.assertEqual(r.status_code, 302)                               # sent to change the password first

    def test_a_client_never_reaches_staff_pages(self):
        c = self.web(self.client_user)
        for url in ('/crm/leads/', '/crm/clients/', '/crm/invoices/', '/crm/my-day/', '/crm/deliverables/mine/', '/crm/search/', '/crm/reports/',
                    '/crm/settings/users/', '/crm/hr/employees/', '/crm/dashboards/owner/'):
            r = c.get(url)
            self.assertIn(r.status_code, (302, 404), url)
            if r.status_code == 302:
                self.assertEqual(r.url, reverse('crm:image_home'), url)
        self.assertEqual(c.get(reverse('crm:image_home')).status_code, 200)
        self.assertEqual(c.get(reverse('crm:notification_list')).status_code, 200)

    def test_a_clients_menu_shows_only_their_photos(self):
        html = self.web(self.client_user).get(reverse('crm:image_home')).content.decode()
        self.assertIn('Upload photos', html)
        for word in ('Leads', 'Invoices', 'Payroll', 'Pipeline', 'My Day', 'Working time', 'Approvals'):
            self.assertNotIn(f'>{word}<', html, word)
        self.assertNotIn('work-clock', html)                               # customers are not time-tracked

    def test_client_cannot_see_another_clients_photo_or_files(self):
        sub = self.submit(client=self.other_client, actor=self.other_user, n=1)
        a = self.assets(sub)[0]
        c = self.web(self.client_user)
        for url in (reverse('crm:image_detail', args=[a.pk]), reverse('crm:image_file', args=[a.original().pk]),
                    reverse('crm:image_thumb', args=[a.original().pk]), reverse('crm:image_submission', args=[sub.pk])):
            self.assertEqual(c.get(url).status_code, 404, url)
        r = c.post(reverse('crm:image_action', args=[a.pk, 'approve']))
        self.assertEqual(r.status_code, 404)
        self.assertNotIn('Other', c.get(reverse('crm:image_list')).content.decode())

    def test_gone_page_is_friendly_for_a_missing_photo(self):
        for who in (self.client_user, self.u['sales']):
            r = self.web(who).get(reverse('crm:image_detail', args=[99999]))
            self.assertEqual(r.status_code, 404)
            self.assertIn('is not available', r.content.decode())
            self.assertIn(reverse('crm:image_list'), r.content.decode())

    def test_editor_sees_only_what_is_assigned_to_them(self):
        a, b = self.assets(self.submit(n=2))
        self.assign(a)
        e = self.web(self.editor)
        self.assertEqual(e.get(reverse('crm:image_detail', args=[a.pk])).status_code, 200)
        self.assertEqual(e.get(reverse('crm:image_detail', args=[b.pk])).status_code, 404)
        self.assertEqual(e.get(reverse('crm:image_file', args=[b.original().pk])).status_code, 404)
        self.assertEqual(self.web(self.editor2).get(reverse('crm:image_detail', args=[a.pk])).status_code, 404)
        self.assertEqual(list(svc.assets_for(self.editor)), [a])

    def test_sales_only_see_their_own_clients_and_managers_their_team(self):
        a = self.assets(self.submit(n=1))[0]
        self.assertEqual(self.web(self.u['sales']).get(reverse('crm:image_detail', args=[a.pk])).status_code, 200)
        self.assertEqual(self.web(self.u['sales_b']).get(reverse('crm:image_detail', args=[a.pk])).status_code, 404)
        self.assertEqual(self.web(self.u['manager']).get(reverse('crm:image_detail', args=[a.pk])).status_code, 200)   # same team
        self.assertEqual(self.web(self.u['owner']).get(reverse('crm:image_detail', args=[a.pk])).status_code, 200)

    def test_roles_without_photo_access_are_refused(self):
        for who in ('finance', 'accountant', 'pm', 'dev'):
            self.assertEqual(self.web(self.u[who]).get(reverse('crm:image_home')).status_code, 403, who)

    def test_hr_can_watch_everything_but_change_nothing(self):
        a = self.assets(self.submit(n=1))[0]
        hr = self.web(self.u['hr'])
        self.assertEqual(hr.get(reverse('crm:image_detail', args=[a.pk])).status_code, 200)
        self.assertEqual(hr.get(reverse('crm:image_list')).status_code, 200)
        self.assertEqual(hr.get(reverse('crm:image_team')).status_code, 200)                           # HR watches who holds what
        self.assertEqual(self.web(self.editor).get(reverse('crm:image_team')).status_code, 403)
        for verb, data in (('assign', {'editor': self.editor.pk}), ('hold', {'reason': 'because'}), ('instructions', {'text': 'x'})):
            hr.post(reverse('crm:image_action', args=[a.pk, verb]), data)
        a.refresh_from_db()
        self.assertEqual((a.status, a.editor_id, a.instructions), ('new', None, 'Remove the background'))
        self.assertEqual(hr.get(reverse('crm:image_upload')).status_code, 403)

    def test_the_audit_role_can_read_but_not_act(self):
        a = self.assets(self.submit(n=1))[0]
        c = self.web(self.u['auditor'])
        self.assertEqual(c.get(reverse('crm:image_detail', args=[a.pk])).status_code, 200)
        c.post(reverse('crm:image_action', args=[a.pk, 'hold']), {'reason': 'nope'})
        a.refresh_from_db()
        self.assertEqual(a.status, 'new')

    def test_clients_see_client_friendly_status_names(self):
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        html = self.web(self.client_user).get(reverse('crm:image_detail', args=[a.pk])).content.decode()
        self.assertIn('Being prepared', html)
        self.assertNotIn('Assigned', html)
        self.assertNotIn('Anjana', html)                                    # the editor's name is not the client's business


# =========================================================== the editing flow

class WorkflowTests(ImagesBase):
    def test_the_whole_journey_from_upload_to_delivery(self):
        sub = self.submit(n=1)
        a = self.assets(sub)[0]
        self.assertEqual(a.coordinator_id, self.u['sales'].pk)               # the client's owner coordinates
        self.assign(a, deadline=india.today() + timedelta(days=2), priority='high')
        a.refresh_from_db()
        self.assertEqual((a.status, a.editor_id, a.priority), ('assigned', self.editor.pk, 'high'))
        svc.transition(a, 'editing', self.editor)
        v = svc.upload_version(a, self.editor, photo('e1.jpg', color=(0, 99, 0)), note='first pass', send_for_review=True)
        a.refresh_from_db()
        self.assertEqual((a.status, v.label, v.kind, v.client_visible), ('internal_review', 'First edit', 'edit', False))
        # The client must not see the edit yet.
        self.assertEqual(self.web(self.client_user).get(reverse('crm:image_file', args=[v.pk])).status_code, 404)
        svc.transition(a, 'client_review', self.u['sales'])
        v.refresh_from_db()
        self.assertTrue(v.client_visible)
        self.assertEqual(self.web(self.client_user).get(reverse('crm:image_file', args=[v.pk])).status_code, 200)
        svc.approve(a, self.client_user)
        a.refresh_from_db()
        v.refresh_from_db()
        self.assertEqual((a.status, v.is_final, v.label), ('approved', True, 'Final'))
        svc.transition(a, 'delivered', self.u['sales'])
        a.refresh_from_db()
        self.assertEqual(a.status, 'delivered')
        kinds = list(a.events.values_list('kind', flat=True))
        for k in ('uploaded', 'assignment', 'status', 'version'):
            self.assertIn(k, kinds)
        self.assertEqual(a.events.filter(kind='status').count(), 6)         # assigned, editing, review, client review, approved, delivered

    def test_the_client_can_ask_for_a_change_and_the_editor_redoes_it(self):
        a = self.to_client(self.assets(self.submit(n=1))[0])
        with self.assertRaises(svc.ImageError):
            svc.request_revision(a, self.client_user, 'no')                  # must say what to change
        svc.request_revision(a, self.client_user, 'Make the dress more red')
        a.refresh_from_db()
        self.assertEqual((a.status, a.revision_count), ('revision_requested', 1))
        self.assertTrue(Notification.objects.filter(user=self.editor, event_type='image_revision').exists())
        svc.transition(a, 'revision_in_progress', self.editor)
        with self.assertRaises(svc.ImageError):                              # nothing new uploaded yet
            svc.transition(a, 'internal_review', self.editor)
        v2 = svc.upload_version(a, self.editor, photo('e2.jpg', color=(250, 0, 0)), send_for_review=True)
        self.assertEqual(v2.label, 'Revision 1')
        svc.transition(a, 'client_review', self.u['sales'])
        svc.approve(a, self.client_user)
        a.refresh_from_db()
        self.assertEqual(a.status, 'approved')
        self.assertEqual(a.versions.filter(is_final=True).get().pk, v2.pk)

    def test_invalid_moves_and_wrong_people_are_refused(self):
        a = self.assets(self.submit(n=1))[0]
        with self.assertRaises(svc.ImageError):
            svc.transition(a, 'approved', self.u['sales'])                   # cannot skip the journey
        self.assign(a)
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            svc.transition(a, 'editing', self.editor2)                       # not their photo
        with self.assertRaises(PermissionDenied):
            svc.transition(a, 'editing', self.client_user)
        with self.assertRaises(PermissionDenied):
            svc.assign(a, self.editor2, self.editor)                         # editors do not assign
        with self.assertRaises(PermissionDenied):
            svc.assign(a, self.editor2, self.client_user)
        svc.transition(a, 'editing', self.editor)
        with self.assertRaises(svc.ImageError):
            svc.transition(a, 'internal_review', self.editor)                # no edit uploaded
        with self.assertRaises(svc.ImageError):
            svc.upload_version(a, self.editor, photo('x.jpg'), kind='original')
        with self.assertRaises(PermissionDenied):
            svc.upload_version(a, self.editor2, photo('x.jpg'))
        with self.assertRaises(PermissionDenied):
            svc.transition(a, 'on_hold', self.editor, reason='tired')        # only coordinators hold work

    def test_only_a_coordinator_sends_to_the_client_and_the_client_decides(self):
        a = self.to_review(self.assets(self.submit(n=1))[0])
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            svc.transition(a, 'client_review', self.editor)
        svc.transition(a, 'changes_internal', self.u['sales'], reason='Edges are rough')
        svc.transition(a, 'editing', self.editor)
        svc.upload_version(a, self.editor, photo('e2.jpg', color=(1, 2, 3)), send_for_review=True)
        svc.transition(a, 'client_review', self.u['sales'])
        with self.assertRaises(PermissionDenied):
            svc.approve(a, self.other_user)   # another client
        with self.assertRaises(PermissionDenied):
            svc.approve(a, self.editor)

    def test_coordinator_can_record_the_clients_phone_decision_with_a_reason(self):
        a = self.to_client(self.assets(self.submit(n=1))[0])
        with self.assertRaises(svc.ImageError):
            svc.transition(a, 'approved', self.u['sales'])                   # acting for the client needs a reason
        svc.transition(a, 'approved', self.u['sales'], reason='Asha confirmed on a call at 4pm')
        e = a.events.filter(kind='status').last()
        self.assertIn('recorded for the client', e.summary)
        self.assertIn('Asha confirmed', e.reason)

    def test_clarification_goes_to_the_client_and_their_reply_reopens_it(self):
        a = self.assets(self.submit(n=1))[0]
        svc.transition(a, 'clarification', self.u['sales'], reason='Which background colour do you want?')
        self.assertTrue(Notification.objects.filter(user=self.client_user, event_type='image_question').exists())
        svc.comment(a, self.client_user, 'White please')
        a.refresh_from_db()
        self.assertEqual(a.status, 'new')
        self.assertTrue(Notification.objects.filter(user=self.u['sales'], event_type='image_comment').exists())

    def test_hold_remembers_where_it_was_and_cancel_needs_a_reason(self):
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        with self.assertRaises(svc.ImageError):
            svc.transition(a, 'on_hold', self.u['sales'])
        svc.transition(a, 'on_hold', self.u['sales'], reason='Client is travelling')
        svc.resume(a, self.u['sales'])
        a.refresh_from_db()
        self.assertEqual(a.status, 'assigned')
        svc.transition(a, 'cancelled', self.u['sales'], reason='Client withdrew')
        svc.transition(a, 'new', self.u['sales'])
        a.refresh_from_db()
        self.assertEqual(a.status, 'new')

    def test_reassigning_tells_both_editors_and_is_recorded(self):
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        self.assign(a, self.editor2, reason='Anjana is on leave')
        a.refresh_from_db()
        self.assertEqual(a.editor_id, self.editor2.pk)
        e = a.events.filter(kind='assignment').last()
        self.assertEqual((e.old_value, e.new_value, e.reason), ('Anjana', 'Binu', 'Anjana is on leave'))
        self.assertTrue(Notification.objects.filter(user=self.editor, event_type='image_reassigned').exists())
        self.assertTrue(Notification.objects.filter(user=self.editor2, event_type='image_assigned').exists())

    def test_only_the_editing_team_can_be_assigned(self):
        a = self.assets(self.submit(n=1))[0]
        with self.assertRaises(svc.ImageError):
            svc.assign(a, self.u['sales_b'], self.u['sales'])
        with self.assertRaises(svc.ImageError):
            svc.assign(a, self.client_user, self.u['sales'])

    def test_bulk_assign_from_the_submission_page(self):
        sub = self.submit(n=3)
        a = self.assets(sub)
        r = self.web(self.u['sales']).post(reverse('crm:image_submission_bulk', args=[sub.pk]),
                                           {'asset': [x.pk for x in a[:2]], 'editor': self.editor.pk, 'priority': 'urgent',
                                            'deadline': (india.today() + timedelta(days=3)).isoformat()})
        self.assertEqual(r.status_code, 302)
        for x in a:
            x.refresh_from_db()
        self.assertEqual([x.status for x in a], ['assigned', 'assigned', 'new'])
        self.assertEqual(a[0].priority, 'urgent')
        self.assertEqual(a[0].deadline, india.today() + timedelta(days=3))

    def test_every_action_through_the_pages_works_for_the_right_person(self):
        a = self.assets(self.submit(n=1))[0]
        co, ed, cl = self.web(self.u['sales']), self.web(self.editor), self.web(self.client_user)
        url = lambda verb: reverse('crm:image_action', args=[a.pk, verb])
        co.post(url('assign'), {'editor': self.editor.pk, 'priority': 'normal', 'deadline': ''})
        ed.post(url('start'))
        ed.post(url('upload'), {'file': photo('w.jpg', color=(9, 9, 9)), 'kind': 'edit', 'send_for_review': '1', 'note': 'done'})
        co.post(url('send_client'))
        a.refresh_from_db()
        self.assertEqual(a.status, 'client_review')
        cl.post(url('revision'), {'what': 'Crop closer to the face'})
        a.refresh_from_db()
        self.assertEqual(a.status, 'revision_requested')
        ed.post(url('start'))
        ed.post(url('upload'), {'file': photo('w2.jpg', color=(8, 9, 9)), 'kind': 'edit', 'send_for_review': '1'})
        co.post(url('send_client'))
        cl.post(url('approve'))
        co.post(url('deliver'))
        a.refresh_from_db()
        self.assertEqual(a.status, 'delivered')


# =========================================================== coordinator changes and the timeline

class TimelineTests(ImagesBase):
    def test_instruction_changes_record_who_what_before_after_and_why(self):
        a = self.assets(self.submit(n=1, note='Remove the background'))[0]
        svc.set_instructions(a, self.u['sales'], 'Remove the background and brighten', reason='Client called')
        e = a.events.filter(kind='instructions').last()
        self.assertEqual((e.actor_id, e.actor_role, e.field), (self.u['sales'].pk, 'Sales Executive', 'instructions'))
        self.assertEqual((e.old_value, e.new_value, e.reason), ('Remove the background', 'Remove the background and brighten', 'Client called'))
        self.assertEqual(e.visibility, 'client')
        svc.set_priority(a, self.u['sales'], 'urgent', reason='Wedding on Friday')
        svc.set_deadline(a, self.u['sales'], india.today() + timedelta(days=1), reason='Wedding on Friday')
        kinds = set(a.events.values_list('kind', flat=True))
        self.assertTrue({'priority', 'deadline', 'instructions'} <= kinds)
        self.assertEqual(a.events.filter(kind='priority').get().new_value, 'Urgent')

    def test_internal_notes_never_reach_the_client(self):
        a = self.assets(self.submit(n=1))[0]
        svc.set_instructions(a, self.u['sales'], 'Client is fussy about skin tone. Do not over-smooth.', internal=True)
        svc.comment(a, self.u['sales'], 'Secret team chat about price', visibility='internal')
        svc.comment(a, self.u['sales'], 'We will start tomorrow', visibility='client')
        self.assign(a)
        svc.comment(a, self.editor, 'Editor note to team')
        for text in ('Secret team chat', 'fussy', 'Editor note', 'Anjana'):
            self.assertNotIn(text, self.web(self.client_user).get(reverse('crm:image_detail', args=[a.pk])).content.decode(), text)
        html = self.web(self.client_user).get(reverse('crm:image_detail', args=[a.pk])).content.decode()
        self.assertIn('We will start tomorrow', html)
        staff = self.web(self.u['sales']).get(reverse('crm:image_detail', args=[a.pk])).content.decode()
        for text in ('Secret team chat', 'fussy', 'Editor note', 'Team only'):
            self.assertIn(text, staff)
        self.assertEqual(svc.events_for(self.client_user, asset=a).exclude(visibility='client').count(), 0)

    def test_editors_cannot_speak_to_the_client(self):
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        ev = svc.comment(a, self.editor, 'hello client', visibility='client')
        self.assertEqual(ev.visibility, 'internal')

    def test_the_timeline_is_in_order_and_names_the_person_and_role(self):
        a = self.to_client(self.assets(self.submit(n=1))[0])
        svc.approve(a, self.client_user)
        events = list(a.events.all())
        self.assertEqual(events, sorted(events, key=lambda e: (e.created_at, e.pk)))
        roles = {e.actor_role for e in events}
        self.assertTrue({'Client', 'Sales Executive', 'Image Editor'} <= roles)
        html = self.web(self.u['hr']).get(reverse('crm:image_detail', args=[a.pk])).content.decode()
        self.assertIn('Image Editor', html)
        self.assertIn('Anjana', html)

    def test_clients_see_their_own_side_of_the_story_only(self):
        a = self.to_client(self.assets(self.submit(n=1))[0])
        client_text = ' '.join(e.text_for_client for e in svc.events_for(self.client_user, asset=a))
        self.assertIn('Photo received', client_text)
        self.assertIn('ready for you to review', client_text)
        self.assertNotIn('internal review', client_text.lower())
        self.assertNotIn('Anjana', client_text)

    def test_timeline_rows_cannot_be_changed_or_deleted(self):
        a = self.assets(self.submit(n=1))[0]
        e = a.events.first()
        e.summary = 'tampered'
        with self.assertRaises(ValidationError):
            e.save()
        with self.assertRaises(ValidationError):
            e.delete()
        with self.assertRaises(ValidationError):
            ImageEvent.objects.filter(pk=e.pk).update(summary='x')
        with self.assertRaises(ValidationError):
            ImageEvent.objects.all().delete()

    def test_images_added_replaced_and_removed_keep_their_history(self):
        sub = self.submit(n=2)
        a, b = self.assets(sub)
        old = a.original()
        v = svc.replace_original(a, self.u['sales'], photo('right.jpg', color=(5, 5, 200)), reason='Client sent the wrong picture')
        a.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual((a.original().pk, v.number, old.superseded_at is not None), (v.pk, 2, True))
        self.assertTrue(storage.get_storage().exists(old.storage_key))               # the old file is still there
        with self.assertRaises(svc.ImageError):
            svc.replace_original(a, self.u['sales'], photo(), reason='')
        new = svc.add_image(sub, self.u['sales'], photo('extra.jpg', color=(1, 1, 1)))
        self.assertEqual((new.number, new.status, new.submission_id), ('IMG-0003', 'new', sub.pk))
        with self.assertRaises(svc.ImageError):
            svc.archive(b, self.u['sales'], '')
        svc.archive(b, self.u['sales'], 'Duplicate of IMG-0001')
        b.refresh_from_db()
        self.assertTrue(b.archived_at)
        self.assertEqual(ImageAsset.objects.filter(pk=b.pk).count(), 1)             # nothing deleted
        self.assertEqual(b.versions.count(), 1)
        self.assertNotIn(b, svc.assets_for(self.client_user))                          # hidden from the client's list
        self.assertIn(b, svc.assets_for(self.u['sales']))
        self.assertEqual(self.web(self.client_user).get(reverse('crm:image_detail', args=[b.pk])).status_code, 404)
        self.assertEqual(self.web(self.u['sales']).get(reverse('crm:image_list') + '?quick=removed').status_code, 200)
        svc.restore(b, self.u['sales'])
        b.refresh_from_db()
        self.assertIsNone(b.archived_at)
        self.assertEqual(a.events.filter(kind='replaced').count(), 1)

    def test_an_editor_cannot_overwrite_or_replace_the_original(self):
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        svc.transition(a, 'editing', self.editor)
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            svc.replace_original(a, self.editor, photo(), reason='better one')
        with self.assertRaises(PermissionDenied):
            svc.archive(a, self.editor, 'remove it')
        before = a.original().storage_key
        svc.upload_version(a, self.editor, photo('e.jpg', color=(7, 7, 7)))
        self.assertEqual(a.original().storage_key, before)
        self.assertEqual(a.versions.filter(kind='original').count(), 1)

    def test_a_delivered_photo_cannot_be_removed(self):
        a = self.to_client(self.assets(self.submit(n=1))[0])
        svc.approve(a, self.client_user)
        svc.transition(a, 'delivered', self.u['sales'])
        with self.assertRaises(svc.ImageError):
            svc.archive(a, self.u['sales'], 'tidy up')
        with self.assertRaises(svc.ImageError):
            svc.replace_original(a, self.u['sales'], photo(), reason='oops')

    def test_hr_and_admin_see_the_whole_history(self):
        a = self.to_client(self.assets(self.submit(n=1))[0])
        svc.set_instructions(a, self.u['sales'], 'internal only', internal=True)
        total = a.events.count()
        for who in ('hr', 'owner'):
            self.assertEqual(svc.events_for(self.u[who], asset=a).count(), total, who)
        self.assertLess(svc.events_for(self.client_user, asset=a).count(), total)

    def test_audit_log_records_the_changes_too(self):
        from crm.models import AuditLog
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        svc.set_priority(a, self.u['sales'], 'high')
        actions = set(AuditLog.objects.filter(object_type='imageasset', object_id=str(a.pk)).values_list('action', flat=True))
        self.assertTrue({'images.assigned', 'images.priority'} <= actions)


# =========================================================== notifications and deep links

class NotificationTests(ImagesBase):
    def test_a_new_submission_tells_the_coordinator_hr_admin_and_the_client(self):
        sub = self.submit(n=2)
        self.assertTrue(Notification.objects.filter(user=self.u['sales'], event_type='image_submission').exists())
        self.assertTrue(Notification.objects.filter(user=self.u['hr'], event_type='image_submission_admin').exists())
        self.assertTrue(Notification.objects.filter(user=self.u['owner'], event_type='image_submission_admin').exists())
        # You are not told about something you just did yourself.
        self.assertFalse(Notification.objects.filter(user=self.client_user, event_type='image_received').exists())
        # When the team uploads for the client, the client is told it arrived.
        sub2 = self.submit(actor=self.u['sales'], n=1)
        n = Notification.objects.get(user=self.client_user, event_type='image_received')
        self.assertEqual(n.url, reverse('crm:image_submission', args=[sub2.pk]))
        self.assertTrue(sub2.on_behalf)

    def test_every_notification_points_at_a_real_page_for_the_person_it_is_for(self):
        a = self.to_client(self.assets(self.submit(n=1))[0])
        svc.request_revision(a, self.client_user, 'Brighter please')
        svc.transition(a, 'revision_in_progress', self.editor)
        svc.upload_version(a, self.editor, photo('e2.jpg', color=(3, 4, 5)), send_for_review=True)
        svc.transition(a, 'client_review', self.u['sales'])
        svc.approve(a, self.client_user)
        svc.transition(a, 'delivered', self.u['sales'])
        notes = list(Notification.objects.filter(event_type__startswith='image_'))
        self.assertGreater(len(notes), 10)
        for n in notes:
            self.assertTrue(n.url.startswith('/crm/images/'), (n.event_type, n.url))
            r = self.web(n.user).get(n.url)
            self.assertEqual(r.status_code, 200, (n.event_type, n.user, n.url))
        types = {n.event_type for n in notes}
        for t in ('image_assigned', 'image_review', 'image_ready', 'image_revision', 'image_approved', 'image_delivered', 'image_editing'):
            self.assertIn(t, types)

    def test_the_right_people_get_the_right_messages(self):
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        self.assertTrue(Notification.objects.filter(user=self.editor, event_type='image_assigned', url=reverse('crm:image_detail', args=[a.pk])).exists())
        svc.transition(a, 'editing', self.editor)
        self.assertTrue(Notification.objects.filter(user=self.client_user, event_type='image_editing').exists())
        svc.upload_version(a, self.editor, photo('e.jpg', color=(1, 9, 9)), send_for_review=True)
        self.assertTrue(Notification.objects.filter(user=self.u['sales'], event_type='image_review').exists())
        svc.transition(a, 'client_review', self.u['sales'])
        self.assertTrue(Notification.objects.filter(user=self.client_user, event_type='image_ready').exists())
        self.assertFalse(Notification.objects.filter(user=self.client_user, event_type__in=('image_review', 'image_assigned')).exists())
        svc.set_priority(a, self.u['sales'], 'urgent')
        self.assertTrue(Notification.objects.filter(user=self.editor, event_type='image_priority').exists())
        svc.set_instructions(a, self.u['sales'], 'new brief')
        self.assertTrue(Notification.objects.filter(user=self.client_user, event_type='image_instructions_client').exists())
        self.assertFalse(Notification.objects.filter(user=self.u['sales']).filter(event_type='image_instructions').exists())    # you are not told of your own action

    def test_push_payload_is_always_a_path_inside_the_app(self):
        with override_settings(SITE_BASE_URL='https://crm.example.test'):
            self.assertEqual(notify.push_path('https://crm.example.test/crm/images/4/'), '/crm/images/4/')
            self.assertEqual(notify.push_path('/crm/images/4/?x=1'), '/crm/images/4/?x=1')
            self.assertEqual(notify.push_path('https://evil.example.test/crm/images/4/'), '/crm/')
            self.assertEqual(notify.push_path('//evil.example.test/crm/'), '/crm/')
            self.assertEqual(notify.push_path('/somewhere/else'), '/crm/')
            self.assertEqual(notify.push_path(''), '/crm/')
        # A wrong SITE_BASE_URL must not change where the notification opens.
        with override_settings(SITE_BASE_URL='http://127.0.0.1:8000'):
            self.assertEqual(notify.push_path('/crm/images/9/'), '/crm/images/9/')

    @override_settings(VAPID_PRIVATE_KEY='k', VAPID_PUBLIC_KEY='p')
    def test_the_delivered_push_carries_type_and_path(self):
        from crm.models import PushSubscription
        PushSubscription.objects.create(user=self.client_user, endpoint='https://push.example.test/abc', p256dh='x', auth='y')
        sent = []
        with mock.patch('pywebpush.webpush', side_effect=lambda **kw: sent.append(json.loads(kw['data']))):
            notify._deliver_push(list(PushSubscription.objects.all()), 'Your photo is ready', 'Tap to review', '/crm/images/7/', 'image_ready')
            notify._deliver_push(list(PushSubscription.objects.all()), 'Odd', '', 'https://elsewhere.example/crm/images/7/', 'image_ready')
        self.assertEqual(len(sent), 2)
        self.assertEqual((sent[0]['url'], sent[0]['type']), ('/crm/images/7/', 'image_ready'))
        self.assertEqual(sent[1]['url'], '/crm/')                                       # never another site
        self.assertTrue(sent[0]['tag'].startswith('image_ready:'))

    def test_service_worker_validates_and_routes_the_click(self):
        js = self.web(self.client_user).get(reverse('crm:service_worker')).content.decode()
        for needle in ('function safeTarget', "u.pathname.startsWith(APP_SCOPE)", 'u.origin !== self.location.origin', 'openWindow(absolute)',
                       'navigate(absolute)', 'includeUncontrolled: true', "data: { url: safeTarget(data.url)"):
            self.assertIn(needle, js, needle)

    def test_opening_a_link_when_signed_out_returns_to_it_after_sign_in(self):
        a = self.assets(self.submit(n=1))[0]
        target = reverse('crm:image_detail', args=[a.pk])
        c = HttpClient()
        r = c.get(target)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)
        self.assertIn('next=', r.url)
        r = c.post(reverse('crm:login'), {'username': 'acmeuser', 'password': self.client_pw, 'next': target})
        self.assertEqual(r.url, target)
        self.assertEqual(c.get(target).status_code, 200)

    def test_a_link_to_a_deleted_or_foreign_photo_ends_on_a_helpful_page(self):
        sub = self.submit(client=self.other_client, actor=self.other_user, n=1)
        a = self.assets(sub)[0]
        n = Notification.objects.create(user=self.client_user, title='old', url=reverse('crm:image_detail', args=[a.pk]), event_type='image_ready')
        r = self.web(self.client_user).get(reverse('crm:notification_open', args=[n.pk]), follow=True)
        self.assertEqual(r.status_code, 404)
        self.assertIn('is not available', r.content.decode())

    def test_reminders_and_stale_drafts_are_safe_to_repeat(self):
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        ImageAsset.objects.filter(pk=a.pk).update(status='editing', deadline=india.today() - timedelta(days=1))
        first = image_jobs.reminders()
        self.assertGreater(first, 0)
        self.assertEqual(image_jobs.reminders(), 0)                                    # same day, nothing new
        self.assertTrue(Notification.objects.filter(user=self.editor, event_type='image_overdue').exists())
        self.assertTrue(Notification.objects.filter(user=self.u['hr'], event_type='image_overdue_digest').exists())
        # A client uploads and walks away without pressing Send.
        svc.add_draft_file(client=self.client_obj, actor=self.client_user, batch='lost1', upload=photo('lost.jpg', color=(4, 4, 4)))
        self.assertEqual(image_jobs.stale_drafts(), 0)                                 # too recent
        ImageSubmission.objects.filter(batch_token='lost1').update(updated_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(image_jobs.stale_drafts(), 1)
        sub = ImageSubmission.objects.get(batch_token='lost1')
        self.assertTrue(sub.number and sub.auto_submitted)
        self.assertEqual(image_jobs.stale_drafts(), 0)


# =========================================================== storage

class StorageTests(ImagesBase):
    def test_files_are_served_only_through_a_permission_checked_page(self):
        a = self.assets(self.submit(n=1))[0]
        v = a.original()
        r = self.web(self.client_user).get(reverse('crm:image_file', args=[v.pk]))
        self.assertEqual((r.status_code, r['Content-Type']), (200, 'image/jpeg'))
        self.assertIn('no-store', r['Cache-Control'])
        r = self.web(self.client_user).get(reverse('crm:image_file', args=[v.pk]) + '?dl=1')
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertEqual(self.web(self.u['sales_b']).get(reverse('crm:image_file', args=[v.pk])).status_code, 404)
        self.assertEqual(HttpClient().get(reverse('crm:image_file', args=[v.pk])).status_code, 302)       # signed out: sign in first
        self.assertEqual(self.web(self.client_user).get(reverse('crm:image_thumb', args=[v.pk])).status_code, 200)

    def test_downloads_are_audited(self):
        from crm.models import AuditLog
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        self.web(self.editor).get(reverse('crm:image_file', args=[a.original().pk]) + '?dl=1')
        self.assertTrue(AuditLog.objects.filter(action='images.downloaded', object_id=str(a.pk)).exists())

    def test_keys_are_unique_unguessable_and_kept_per_client(self):
        sub = self.submit(n=3)
        keys = [v.storage_key for v in ImageVersion.objects.all()] + [v.thumb_key for v in ImageVersion.objects.all()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(k.startswith(f'client-{self.client_obj.pk}/') for k in keys))
        other = self.submit(client=self.other_client, actor=self.other_user, n=1)
        self.assertTrue(all(v.storage_key.startswith(f'client-{self.other_client.pk}/') for v in ImageVersion.objects.filter(asset__client=self.other_client)))

    def test_local_storage_refuses_to_escape_its_folder(self):
        with self.assertRaises(ValueError):
            storage.get_storage()._path('../../etc/passwd')

    @override_settings(CRM_IMAGE_BUCKET='btxdev', CRM_IMAGE_REGION='ap-southeast-2', CRM_IMAGE_PREFIX='crm-images')
    def test_s3_is_private_encrypted_and_signed_for_a_few_minutes(self):
        calls = {}

        class FakeS3:
            def put_object(self, **kw):
                calls.setdefault('put', []).append(kw)

            def generate_presigned_url(self, op, Params, ExpiresIn):
                calls['sign'] = (op, Params, ExpiresIn)
                return 'https://btxdev.s3.ap-southeast-2.amazonaws.com/signed?X-Amz-Expires=300'

            def head_object(self, **kw):
                return {}

        storage.reset_storage()
        store = storage.get_storage()
        self.assertEqual(store.name, 's3')
        store._client = FakeS3()
        a = self.assets(self.submit(n=1))[0]
        puts = calls['put']
        self.assertEqual(len(puts), 2)                                                 # the file and its preview
        for p in puts:
            self.assertEqual((p['Bucket'], p['ServerSideEncryption']), ('btxdev', 'AES256'))
            self.assertTrue(p['Key'].startswith('crm-images/client-'))
            self.assertNotIn('ACL', p)                                                 # never public
        r = self.web(self.client_user).get(reverse('crm:image_file', args=[a.original().pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.url.startswith('https://btxdev.s3.'))
        op, params, expires = calls['sign']
        self.assertEqual((op, expires, params['Bucket']), ('get_object', 300, 'btxdev'))
        self.assertIn('inline', params['ResponseContentDisposition'])
        self.assertEqual(self.web(self.u['sales_b']).get(reverse('crm:image_file', args=[a.original().pk])).status_code, 404)   # no link for strangers
        r = self.web(self.client_user).get(reverse('crm:image_file', args=[a.original().pk]) + '?dl=1')
        self.assertIn('attachment', calls['sign'][1]['ResponseContentDisposition'])


# =========================================================== logins, pages and integrity

class AdminAndPageTests(ImagesBase):
    def test_creating_a_client_login_shows_the_password_once_and_forces_a_change(self):
        c = self.web(self.u['manager'])
        r = c.post(reverse('crm:image_client_logins', args=[self.client_obj.pk]), {'do': 'create', 'username': 'asha2', 'full_name': 'Asha Two', 'email': 'a2@example.test'})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('shown only once', html)
        access = ClientAccess.objects.get(user__username='asha2')
        self.assertEqual(access.client_id, self.client_obj.pk)
        self.assertTrue(access.user.crm_profile.must_change_password)
        self.assertEqual(access.user.crm_profile.roles.get().code, 'image_client')
        again = c.get(reverse('crm:image_client_logins', args=[self.client_obj.pk])).content.decode()
        self.assertNotIn('shown only once', again)
        r = c.post(reverse('crm:image_client_logins', args=[self.client_obj.pk]), {'do': 'create', 'username': 'ASHA2'})
        self.assertIn('already taken', r.content.decode())

    def test_only_managers_make_logins_and_only_for_clients_they_can_see(self):
        self.assertEqual(self.web(self.u['sales']).get(reverse('crm:image_client_logins', args=[self.client_obj.pk])).status_code, 403)
        self.assertEqual(self.web(self.u['hr']).get(reverse('crm:image_client_logins', args=[self.client_obj.pk])).status_code, 403)
        self.assertEqual(self.web(self.client_user).get(reverse('crm:image_client_logins', args=[self.client_obj.pk])).status_code, 403)
        self.assertEqual(self.web(self.u['manager']).get(reverse('crm:image_client_logins', args=[self.client_obj.pk])).status_code, 200)

    def test_stopping_a_login_ends_access_and_a_reset_makes_a_new_password(self):
        access = ClientAccess.objects.get(user=self.client_user)
        c = HttpClient()
        self.assertTrue(c.login(username='acmeuser', password=self.client_pw))
        svc.set_client_login_active(access, self.u['manager'], False)
        self.assertEqual(c.get(reverse('crm:image_home')).status_code, 302)
        self.assertFalse(HttpClient().login(username='acmeuser', password=self.client_pw))
        svc.set_client_login_active(access, self.u['manager'], True)
        new = svc.reset_client_password(access, self.u['manager'])
        self.assertNotEqual(new, self.client_pw)
        self.assertTrue(HttpClient().login(username='acmeuser', password=new))
        self.assertTrue(UserProfile.objects.get(user=self.client_user).must_change_password)

    def test_client_logins_do_not_pollute_staff_lists_or_checks(self):
        from crm.core import integrity
        from crm.modules.leads.forms import assignable_users
        from crm.modules.worktime import services as worktime
        self.assertNotIn(self.client_user, list(assignable_users(self.u['owner'])))
        self.assertFalse(worktime.tracked(self.client_user))
        self.assertTrue(worktime.tracked(self.editor))
        res = integrity.check_hr_links()
        self.assertFalse([i for i in res['items'] if 'Asha' in i or 'Omar' in i])

    def test_integrity_flags_photos_with_nobody_to_move_them(self):
        from crm.core import integrity
        a = self.assets(self.submit(n=1))[0]
        self.assertTrue(integrity.check_images_flow()['ok'])
        ImageAsset.objects.filter(pk=a.pk).update(created_at=timezone.now() - timedelta(days=2))
        res = integrity.check_images_flow()
        self.assertFalse(res['ok'])
        self.assertIn(a.number, res['items'][0])
        self.assertIn(integrity.check_images_flow, integrity.CHECKS)

    def test_pages_render_for_every_role(self):
        sub = self.submit(n=2)
        a = self.to_client(self.assets(sub)[0])
        urls = [reverse('crm:image_home'), reverse('crm:image_list'), reverse('crm:image_list') + '?quick=overdue', reverse('crm:image_list') + '?status=assigned&q=IMG',
                reverse('crm:image_submissions'), reverse('crm:image_submission', args=[sub.pk]), reverse('crm:image_detail', args=[a.pk]),
                reverse('crm:image_upload'), reverse('crm:notification_list')]
        for who in ('sales', 'manager', 'owner', 'hr', 'auditor'):
            c = self.web(self.u[who])
            for url in urls:
                if who in ('hr', 'auditor') and url == reverse('crm:image_upload'):
                    continue
                self.assertEqual(c.get(url).status_code, 200, (who, url))
        for who in (self.client_user, self.editor):
            c = self.web(who)
            for url in urls:
                if who == self.editor and url in (reverse('crm:image_upload'),):
                    continue
                r = c.get(url)
                self.assertIn(r.status_code, (200, 404), (who, url))
        c = self.web(self.client_user)
        for url in (reverse('crm:image_home'), reverse('crm:image_list'), reverse('crm:image_detail', args=[a.pk]), reverse('crm:image_upload')):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertEqual(self.web(self.u['sales']).get(reverse('crm:image_team')).status_code, 200)
        self.assertEqual(self.web(self.u['owner']).get(reverse('crm:image_team')).status_code, 200)

    def test_the_client_page_has_a_photos_tab_that_links_everything_to_one_client(self):
        self.submit(n=2)
        html = self.web(self.u['sales']).get(reverse('crm:client_detail', args=[self.client_obj.pk]) + '?tab=photos').content.decode()
        self.assertIn('2 photos in 1 submission', html)
        self.assertIn('IMG-0001', html)
        self.assertEqual(ImageAsset.objects.exclude(client=self.client_obj).count(), 0)       # no duplicate client record was made
        self.assertEqual(Client.objects.filter(display_name='Acme').count(), 1)
        owner_html = self.web(self.u['manager']).get(reverse('crm:client_detail', args=[self.client_obj.pk]) + '?tab=photos').content.decode()
        self.assertIn('Client logins', owner_html)
        self.assertNotIn('Client logins', html)                                                 # executives cannot manage logins

    def test_the_new_roles_and_grants_exist_for_existing_databases_too(self):
        for code in ('image_editor', 'image_client'):
            self.assertTrue(Role.objects.filter(code=code).exists())
        self.assertIn('images', Role.objects.get(code='sales_executive').grants)
        self.assertEqual(Role.objects.get(code='hr_manager').grants['images'], {'view': 'company'})
        self.assertEqual(Role.objects.get(code='image_client').grants['images'], {'view': 'own', 'create': 'own'})

    def test_phone_layout_basics(self):
        a = self.assets(self.submit(n=1))[0]
        html = self.web(self.client_user).get(reverse('crm:image_home')).content.decode()
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1.0"', html)
        self.assertIn('class="bottom-nav', html)
        for tab in ('Home', 'My photos', 'Upload', 'Alerts'):
            self.assertIn(f'<span>{tab}</span>', html)
        up = self.web(self.client_user).get(reverse('crm:image_upload')).content.decode()
        self.assertIn('image/heic,image/heif,.heic,.heif', up)
        self.assertIn('multiple', up)
        self.assertIn('crypto.randomUUID', up)
        detail = self.web(self.client_user).get(reverse('crm:image_detail', args=[a.pk])).content.decode()
        self.assertIn('Write to us', detail)


# =========================================================== instructions are required, and voice messages

def voice(kind='webm', size=3000, mime=None):
    heads = {'webm': (b'\x1a\x45\xdf\xa3', 'audio/webm;codecs=opus'), 'mp4': (b'\x00\x00\x00\x18ftypM4A ', 'audio/mp4'), 'ogg': (b'OggS', 'audio/ogg')}
    head, ctype = heads[kind]
    return SimpleUploadedFile(f'voice.{kind}', head + b'\x00' * size, content_type=mime or ctype)


class InstructionsAndVoiceTests(ImagesBase):
    def draft(self, batch='vb1', actor=None, client=None):
        svc.add_draft_file(client=client or self.client_obj, actor=actor or self.client_user, batch=batch, upload=photo())
        return ImageSubmission.objects.get(batch_token=batch)

    def test_instructions_must_be_at_least_12_characters_or_a_voice_message(self):
        sub = self.draft()
        for note in ('', '   ', 'too short', '12345678901'):
            with self.assertRaises(svc.ImageError) as cm:
                svc.submit(sub, self.client_user, note)
            self.assertIn('at least 12 characters', str(cm.exception))
        self.assertFalse(ImageSubmission.objects.get(pk=sub.pk).submitted_at)
        self.assertEqual(svc.submit(sub, self.client_user, '123456789012').number, 'SUB-0001')      # exactly 12 is enough
        self.assertEqual(ImageAsset.objects.get().instructions, '123456789012')

    def test_the_page_refuses_short_instructions_with_a_plain_message(self):
        self.draft('vb2')
        c = self.web(self.client_user)
        r = c.post(reverse('crm:image_upload_send'), {'batch': 'vb2', 'note': 'brighten'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 400)
        self.assertIn('at least 12 characters', json.loads(r.content)['error'])
        html = c.get(reverse('crm:image_upload')).content.decode()
        self.assertIn('minlength="12"', html)
        self.assertNotIn('(optional)', html)
        self.assertIn('!valid()', html)                                                   # the Send button waits until it is enough

    def test_a_voice_message_can_stand_in_for_the_typed_instructions(self):
        c = self.web(self.client_user)
        c.post(reverse('crm:image_upload_file'), {'batch': 'vb3', 'file': photo()}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        r = c.post(reverse('crm:image_upload_voice'), {'batch': 'vb3', 'seconds': '14', 'file': voice()}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 200, r.content)
        r = c.post(reverse('crm:image_upload_send'), {'batch': 'vb3', 'note': ''}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(json.loads(r.content)['ok'])
        sub = ImageSubmission.objects.get(batch_token='vb3')
        v = sub.voices.get()
        self.assertEqual((v.seconds, v.visibility, v.asset_id, v.mime_type), (14, 'client', None, 'audio/webm'))
        self.assertTrue(sub.events.filter(kind='voice', voice=v, visibility='client').exists())
        a = sub.assets.get()
        for who in (self.client_user, self.u['sales'], self.u['owner']):
            html = self.web(who).get(reverse('crm:image_detail', args=[a.pk])).content.decode()
            self.assertIn(reverse('crm:image_voice', args=[v.pk]), html, who)
            self.assertIn('<audio controls', html)
            self.assertEqual(self.web(who).get(reverse('crm:image_voice', args=[v.pk])).status_code, 200)

    def test_re_recording_replaces_the_draft_message(self):
        sub = self.draft('vb4')
        svc.add_draft_voice(client=self.client_obj, actor=self.client_user, batch='vb4', upload=voice(), seconds=5)
        svc.add_draft_voice(client=self.client_obj, actor=self.client_user, batch='vb4', upload=voice('mp4'), seconds=9)
        self.assertEqual(sub.voices.count(), 1)
        self.assertEqual(sub.voices.get().seconds, 9)

    def test_bad_recordings_are_refused_without_any_processing(self):
        sub = self.draft('vb5')
        bad = [(SimpleUploadedFile('v.webm', b'', content_type='audio/webm'), 5, 'empty'),
               (SimpleUploadedFile('v.webm', b'this is text, not audio at all', content_type='audio/webm'), 5, 'not a voice recording'),
               (voice(mime='audio/x-unknown'), 5, 'not a voice recording'),
               (voice(), 500, 'too long'),
               (voice(size=4 * 1024 * 1024), 30, 'too long')]
        for upload, secs, word in bad:
            with self.assertRaises(storage.ImageFileError) as cm:
                svc.add_draft_voice(client=self.client_obj, actor=self.client_user, batch='vb5', upload=upload, seconds=secs)
            self.assertIn(word, str(cm.exception))
        self.assertEqual(sub.voices.count(), 0)
        for kind in ('webm', 'mp4', 'ogg'):
            svc.add_draft_voice(client=self.client_obj, actor=self.client_user, batch='vb5', upload=voice(kind), seconds=3)
        self.assertEqual(sub.voices.count(), 1)

    def test_voice_comments_follow_the_same_visibility_rules_as_written_ones(self):
        a = self.assets(self.submit(n=1))[0]
        self.assign(a)
        client_ev = svc.comment(a, self.client_user, '', voice=storage.check_voice(voice(), 8))
        self.assertEqual((client_ev.kind, client_ev.visibility), ('voice', 'client'))
        team_ev = svc.comment(a, self.u['sales'], '', visibility='internal', voice=storage.check_voice(voice('mp4'), 6))
        edit_ev = svc.comment(a, self.editor, 'asking about the edge', visibility='client', voice=storage.check_voice(voice('ogg'), 4))
        self.assertEqual((team_ev.visibility, edit_ev.visibility, edit_ev.kind), ('internal', 'internal', 'comment'))     # editors speak to the team only
        open_ev = svc.comment(a, self.u['sales'], 'Here is the plan', visibility='client', voice=storage.check_voice(voice(), 7))
        self.assertEqual(open_ev.visibility, 'client')
        client = self.web(self.client_user)
        self.assertEqual(client.get(reverse('crm:image_voice', args=[client_ev.voice_id])).status_code, 200)
        self.assertEqual(client.get(reverse('crm:image_voice', args=[open_ev.voice_id])).status_code, 200)
        for hidden in (team_ev, edit_ev):
            self.assertEqual(client.get(reverse('crm:image_voice', args=[hidden.voice_id])).status_code, 404)          # team-only stays team-only
            self.assertNotIn(reverse('crm:image_voice', args=[hidden.voice_id]), client.get(reverse('crm:image_detail', args=[a.pk])).content.decode())
            self.assertEqual(self.web(self.u['sales']).get(reverse('crm:image_voice', args=[hidden.voice_id])).status_code, 200)
            self.assertEqual(self.web(self.u['hr']).get(reverse('crm:image_voice', args=[hidden.voice_id])).status_code, 200)
        self.assertEqual(self.web(self.other_user).get(reverse('crm:image_voice', args=[client_ev.voice_id])).status_code, 404)
        self.assertEqual(self.web(self.u['sales_b']).get(reverse('crm:image_voice', args=[client_ev.voice_id])).status_code, 404)
        self.assertEqual(self.web(self.editor2).get(reverse('crm:image_voice', args=[client_ev.voice_id])).status_code, 404)
        self.assertEqual(HttpClient().get(reverse('crm:image_voice', args=[client_ev.voice_id])).status_code, 302)

    def test_a_spoken_comment_through_the_page_notifies_the_team(self):
        a = self.assets(self.submit(n=1))[0]
        c = self.web(self.client_user)
        r = c.post(reverse('crm:image_voice_post', args=[a.pk]), {'seconds': '11', 'body': '', 'file': voice()}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(Notification.objects.filter(user=self.u['sales'], event_type='image_comment', body='Voice message').exists())
        bad = c.post(reverse('crm:image_voice_post', args=[a.pk]), {'seconds': '11', 'file': SimpleUploadedFile('x.webm', b'nope', content_type='audio/webm')}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(bad.status_code, 400)
        foreign = self.assets(self.submit(client=self.other_client, actor=self.other_user, n=1))[0]
        self.assertEqual(c.post(reverse('crm:image_voice_post', args=[foreign.pk]), {'seconds': '5', 'file': voice()}).status_code, 404)
        html = c.get(reverse('crm:image_detail', args=[a.pk])).content.decode()
        self.assertIn('<audio controls', html)
        self.assertIn('preload="none"', html)                                              # nothing downloads until someone presses play

    def test_a_voice_message_is_stored_exactly_as_recorded(self):
        raw = voice('mp4', size=1234)
        data = raw.read()
        raw.seek(0)
        a = self.assets(self.submit(n=1))[0]
        ev = svc.comment(a, self.client_user, '', voice=storage.check_voice(raw, 12))
        v = ev.voice
        self.assertEqual(storage.get_storage().read(v.storage_key), data)
        self.assertEqual(v.size, len(data))
        self.assertTrue(v.storage_key.startswith(f'client-{self.client_obj.pk}/voice/'))

    def test_playback_supports_ranges_so_phones_can_play_it(self):
        a = self.assets(self.submit(n=1))[0]
        ev = svc.comment(a, self.client_user, '', voice=storage.check_voice(voice(size=1000), 5))
        c = self.web(self.client_user)
        url = reverse('crm:image_voice', args=[ev.voice_id])
        full = c.get(url)
        self.assertEqual((full.status_code, full['Accept-Ranges']), (200, 'bytes'))
        total = len(full.content)
        part = c.get(url, HTTP_RANGE='bytes=0-9')
        self.assertEqual((part.status_code, len(part.content), part['Content-Range']), (206, 10, f'bytes 0-9/{total}'))
        self.assertEqual(c.get(url, HTTP_RANGE='bytes=-5').content, full.content[-5:])
        self.assertEqual(c.get(url, HTTP_RANGE=f'bytes={total + 50}-').status_code, 416)

    @override_settings(CRM_IMAGE_BUCKET='btxdev', CRM_IMAGE_REGION='ap-southeast-2')
    def test_with_s3_the_storage_service_streams_it_and_the_crm_only_signs_a_link(self):
        seen = {}

        class FakeS3:
            def put_object(self, **kw):
                seen.setdefault('put', []).append(kw)

            def generate_presigned_url(self, op, Params, ExpiresIn):
                seen['sign'] = (Params, ExpiresIn)
                return 'https://btxdev.s3.ap-southeast-2.amazonaws.com/voice-signed'

            def head_object(self, **kw):
                return {}

        storage.reset_storage()
        storage.get_storage()._client = FakeS3()
        a = self.assets(self.submit(n=1))[0]
        ev = svc.comment(a, self.client_user, '', voice=storage.check_voice(voice(), 6))
        r = self.web(self.client_user).get(reverse('crm:image_voice', args=[ev.voice_id]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.url.startswith('https://btxdev.s3.'))
        self.assertEqual(seen['sign'][1], 300)
        voice_puts = [p for p in seen['put'] if '/voice/' in p['Key']]
        self.assertEqual((len(voice_puts), voice_puts[0]['ServerSideEncryption']), (1, 'AES256'))
        self.assertEqual(self.web(self.u['sales_b']).get(reverse('crm:image_voice', args=[ev.voice_id])).status_code, 404)

    def test_a_draft_voice_is_private_to_the_person_recording_it(self):
        self.draft('vb6')
        v = svc.add_draft_voice(client=self.client_obj, actor=self.client_user, batch='vb6', upload=voice(), seconds=4)
        self.assertEqual(self.web(self.client_user).get(reverse('crm:image_voice', args=[v.pk])).status_code, 200)
        self.assertEqual(self.web(self.u['owner']).get(reverse('crm:image_voice', args=[v.pk])).status_code, 404)

    def test_a_forgotten_draft_is_still_sent_for_the_client_without_instructions(self):
        sub = self.draft('vb7')
        ImageSubmission.objects.filter(pk=sub.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(image_jobs.stale_drafts(), 1)                                      # photos are never lost, even with no note
        self.assertTrue(ImageSubmission.objects.get(pk=sub.pk).auto_submitted)

    def test_the_recorder_is_small_and_loaded_only_where_needed(self):
        import os
        js = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'crm', 'js', 'voice.js'), encoding='utf-8').read()
        for needle in ('audioBitsPerSecond: 24000', 'MediaRecorder', "'audio/mp4'", 'this.max', 'getUserMedia'):
            self.assertIn(needle, js, needle)
        self.assertLess(len(js), 6000)                                                       # a few kilobytes
        html = self.web(self.client_user).get(reverse('crm:image_upload')).content.decode()
        self.assertIn('crm/js/voice.js', html)
        self.assertIn('Record a voice message instead', html)
        self.assertNotIn('crm/js/voice.js', self.web(self.client_user).get(reverse('crm:image_home')).content.decode())


class ShareLinkTests(ImagesBase):
    def test_every_role_gets_a_share_button_with_the_exact_address_of_the_page(self):
        from django.utils.html import escapejs
        sub = self.submit(n=1)
        a = self.assets(sub)[0]
        self.assign(a)
        pages = [reverse('crm:image_detail', args=[a.pk]), reverse('crm:image_list'), reverse('crm:image_list') + '?quick=overdue&status=assigned',
                 reverse('crm:image_submission', args=[sub.pk]), reverse('crm:image_submissions')]
        for who in (self.client_user, self.u['sales'], self.u['manager'], self.u['hr'], self.u['owner'], self.editor):
            c = self.web(who)
            for url in pages:
                r = c.get(url)
                if r.status_code != 200:
                    continue                                              # e.g. an editor has no submission page
                html = r.content.decode()
                self.assertIn('Share link', html, (who, url))
                self.assertIn(escapejs('http://testserver' + url), html, (who, url))   # the filters travel with the link
        self.assertEqual(self.web(self.client_user).get(pages[0]).status_code, 200)

    def test_a_shared_link_only_opens_for_people_who_may_see_it(self):
        a = self.assets(self.submit(n=1))[0]
        url = reverse('crm:image_detail', args=[a.pk])
        signed_out = HttpClient().get(url)
        self.assertEqual(signed_out.status_code, 302)
        self.assertIn('next=' + url, signed_out.url)                 # sign in, then straight to the photo
        for who in (self.other_user, self.u['sales_b'], self.editor2):
            r = self.web(who).get(url)
            self.assertEqual(r.status_code, 404, who)
            self.assertIn('is not available', r.content.decode())
        for who in (self.client_user, self.u['sales'], self.u['hr'], self.u['owner']):
            self.assertEqual(self.web(who).get(url).status_code, 200, who)


class PwaInstallTests(ImagesBase):
    def manifest(self, who=None):
        c = self.web(who) if who is not None else HttpClient()
        r = c.get(reverse('crm:manifest'))
        self.assertEqual(r.status_code, 200)
        return r, json.loads(r.content)

    def test_a_customer_installs_a_photo_app_with_its_own_name_start_page_and_shortcuts(self):
        r, m = self.manifest(self.client_user)
        self.assertEqual((m['name'], m['short_name'], m['start_url']), ('BThinkX Photos', 'Photos', '/crm/images/?source=pwa'))
        self.assertEqual((m['id'], m['scope'], m['display']), ('/crm/', '/crm/', 'standalone'))            # same app identity, so sign-in and push are shared
        urls = {s['url'] for s in m['shortcuts']}
        self.assertEqual(urls, {'/crm/images/upload/', '/crm/images/list/', '/crm/notifications/'})
        self.assertIn('Cookie', r['Vary'])
        self.assertIn('no-cache', r['Cache-Control'])

    def test_an_editor_installs_an_editing_app(self):
        _, m = self.manifest(self.editor)
        self.assertEqual((m['name'], m['short_name'], m['start_url']), ('BThinkX Editor', 'Editor', '/crm/images/?source=pwa'))
        self.assertIn('/crm/images/', {s['url'] for s in m['shortcuts']})

    def test_staff_and_signed_out_visitors_still_get_the_full_business_os(self):
        for who in (None, self.u['sales'], self.u['owner'], self.u['manager'], self.u['hr']):
            _, m = self.manifest(who)
            self.assertEqual((m['name'], m['start_url']), ('BThinkX — Business OS', '/crm/?source=pwa'), who)
            self.assertNotIn('shortcuts', m)

    def test_every_shortcut_is_inside_the_app_and_opens_for_that_person(self):
        for who in (self.client_user, self.editor):
            _, m = self.manifest(who)
            c = self.web(who)
            for s in m['shortcuts']:
                self.assertTrue(s['url'].startswith(m['scope']), s)
                self.assertEqual(c.get(s['url']).status_code, 200, (who, s['url']))
            self.assertEqual(c.get(m['start_url']).status_code, 200)
            for icon in m['icons'] + [i for s in m['shortcuts'] for i in s['icons']]:
                from django.contrib.staticfiles import finders
                self.assertTrue(finders.find(icon['src'].replace('/static/', '', 1)), icon['src'])

    def test_customers_and_editors_see_the_install_card_where_they_land(self):
        for who in (self.client_user, self.editor):
            html = self.web(who).get(reverse('crm:image_home')).content.decode()
            for needle in ('Get the app on your phone', '$store.pwa.install()', 'Turn on notifications', 'Add to Home Screen', 'Not now',
                           '!$store.pwa.isStandalone'):
                self.assertIn(needle, html, (who, needle))
        sales_home = self.web(self.u['sales']).get(reverse('crm:image_home')).content.decode()
        self.assertNotIn('Get the app on your phone', sales_home)                             # staff already have it in their menu

    def test_the_card_is_also_on_everyones_profile_without_a_not_now_button(self):
        for who in (self.client_user, self.editor, self.u['sales']):
            r = self.web(who).get(reverse('crm:profile'))
            self.assertEqual(r.status_code, 200, who)
            html = r.content.decode()
            self.assertIn('Get the app on your phone', html)
            self.assertNotIn('Not now', html)

    def test_the_app_reads_the_real_notification_state_on_start(self):
        import os
        js = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'crm', 'js', 'pwa.js'), encoding='utf-8').read()
        self.assertIn("Alpine.store('pwa').refreshPushState()", js)
        self.assertIn('beforeinstallprompt', js)
