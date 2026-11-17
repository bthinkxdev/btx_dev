"""The rules for client photos: who may do what, every status move, and the timeline row each action leaves behind.

Views call these functions and nothing else changes a photo. Each one checks permission, runs in a transaction, writes the
timeline event (who, what, before, after, why) and tells the right people, so no path can skip the history."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from crm.core import audit, india, notify, numbering, permissions
from crm.models import ClientAccess, ImageAsset, ImageEvent, ImageSubmission, ImageVersion, ImageVoice
from crm.models.images import ACTIVE, CLIENT_LABELS, DONE, PRIORITIES, STATUS_LABELS, WORKING

from . import storage

logger = logging.getLogger('crm.images')

MODULE = 'images'
MAX_FILES_PER_SUBMISSION = 40
EDITOR_ROLE = 'image_editor'
HR_ROLE = 'hr_manager'


class ImageError(Exception):
    """A rule was not met. The message is written for the person to read."""


# ---- who can move a photo from one status to the next.
#   C = coordinator (a person with the Approve permission for this photo's client)
#   E = the editor the photo is assigned to (a coordinator may also act for them)
#   K = the client's own login (a coordinator may act for them, with a reason)
FLOW = {
    'new': {'clarification': 'C', 'ready': 'C', 'on_hold': 'C', 'cancelled': 'C'},
    'clarification': {'new': 'C', 'ready': 'C', 'on_hold': 'C', 'cancelled': 'C'},
    'ready': {'clarification': 'C', 'on_hold': 'C', 'cancelled': 'C'},                      # assigning moves it on
    'assigned': {'editing': 'EC', 'clarification': 'EC', 'ready': 'C', 'on_hold': 'C', 'cancelled': 'C'},
    'editing': {'internal_review': 'EC', 'clarification': 'EC', 'on_hold': 'C', 'cancelled': 'C'},
    'internal_review': {'client_review': 'C', 'changes_internal': 'C', 'on_hold': 'C', 'cancelled': 'C'},
    'changes_internal': {'editing': 'EC', 'clarification': 'EC', 'on_hold': 'C', 'cancelled': 'C'},
    'client_review': {'approved': 'KC', 'revision_requested': 'KC', 'on_hold': 'C', 'cancelled': 'C'},
    'revision_requested': {'revision_in_progress': 'EC', 'ready': 'C', 'on_hold': 'C', 'cancelled': 'C'},
    'revision_in_progress': {'internal_review': 'EC', 'clarification': 'EC', 'on_hold': 'C', 'cancelled': 'C'},
    'approved': {'delivered': 'C', 'revision_requested': 'KC'},
    'delivered': {},
    'on_hold': {},                                    # resume() sends it back to where it was
    'cancelled': {'new': 'C'},
}
REASON_NEEDED = {'clarification', 'on_hold', 'cancelled', 'changes_internal', 'revision_requested'}
# The client sees these moves in their own timeline. The others are internal steps.
CLIENT_SEES = {'clarification': 'We have a question about your photo.', 'editing': 'Editing has started.',
               'client_review': 'Your edited photo is ready for you to review.',
               'revision_requested': 'A revision was requested.', 'revision_in_progress': 'We started the changes you asked for.',
               'approved': 'The photo was approved.', 'delivered': 'The final photo was delivered.',
               'on_hold': 'The photo is on hold.', 'cancelled': 'The photo was cancelled.', 'new': 'We received your reply.'}


# ---------------------------------------------------------------- people

def name_of(user) -> str:
    return (user.get_full_name() or user.get_username()) if user else 'The system'


def role_of(user) -> str:
    if user is None:
        return 'System'
    if permissions.is_client(user):
        return 'Client'
    if permissions.is_super_admin(user):
        return 'Owner'
    profile = getattr(user, 'crm_profile', None)
    role = profile.roles.order_by('name').first() if profile else None
    return role.name if role else 'Staff'


def client_user_ids(client) -> list[int]:
    return list(ClientAccess.objects.filter(client=client, user__is_active=True).values_list('user_id', flat=True))


def client_users(client):
    return get_user_model().objects.filter(pk__in=client_user_ids(client))


def users_with_role(code: str):
    return get_user_model().objects.filter(is_active=True, crm_profile__roles__code=code).distinct()


def editors():
    """Who a photo can be assigned to: people in the Editing role."""
    return users_with_role(EDITOR_ROLE).exclude(image_access__isnull=False).order_by('first_name', 'username')


def coordinator_for(asset):
    """The sales person looking after this client's photos: the one named on the photo, else the client's owner."""
    for u in (asset.coordinator, asset.client.owner):
        if u is not None and u.is_active and not permissions.is_client(u):
            return u
    return None


def coordinators_for(asset) -> list:
    people = [coordinator_for(asset)]
    return [p for p in people if p is not None] or list(notify.owners())


def is_coordinator(user, asset) -> bool:
    if user is None or not user.is_authenticated or permissions.is_client(user):
        return False
    if permissions.is_super_admin(user):
        return True
    return permissions.has_perm(user, MODULE, 'approve') and permissions.in_scope(user, MODULE, 'approve', asset)


def is_editor_of(user, asset) -> bool:
    return bool(user and user.is_authenticated and asset.editor_id == user.pk and permissions.has_perm(user, MODULE, 'edit'))


def is_client_of(user, asset) -> bool:
    return bool(user and user.is_authenticated and permissions.is_client(user)
                and ClientAccess.objects.filter(user=user, client_id=asset.client_id).exists())


def actor_kinds(user, asset) -> set:
    kinds = set()
    if is_coordinator(user, asset):
        kinds.add('C')
    if is_editor_of(user, asset):
        kinds.add('E')
    if is_client_of(user, asset):
        kinds.add('K')
    return kinds


def can_view(user, asset) -> bool:
    return assets_for(user).filter(pk=asset.pk).exists()


def _need(user, asset, letters: str, what: str):
    kinds = actor_kinds(user, asset)
    if not (kinds & set(letters)):
        permissions.denied(user, MODULE, 'edit', detail=f'{what} on {asset}')


# ---------------------------------------------------------------- what a person may see

def assets_for(user):
    """Photos this person may open. Clients get only their own company's; staff get what their permission scope allows."""
    qs = ImageAsset.objects.filter(submission__submitted_at__isnull=False)
    qs = permissions.scoped(qs, user, MODULE, 'view')
    if permissions.is_client(user):
        qs = qs.filter(archived_at__isnull=True)
    return qs


def with_thumbs(qs, user):
    """Adds `thumb_id`: the picture to show for each photo. The newest edit when there is one (a client only counts edits that
    were sent to them), otherwise the original."""
    from django.db.models import OuterRef, Subquery
    from django.db.models.functions import Coalesce
    edits = ImageVersion.objects.filter(asset=OuterRef('pk'), kind='edit')
    if permissions.is_client(user):
        edits = edits.filter(client_visible=True)
    orig = ImageVersion.objects.filter(asset=OuterRef('pk'), kind='original', superseded_at__isnull=True)
    return qs.annotate(thumb_id=Coalesce(Subquery(edits.order_by('-number').values('pk')[:1]),
                                         Subquery(orig.order_by('-number').values('pk')[:1])))


def submissions_for(user):
    qs = ImageSubmission.objects.filter(submitted_at__isnull=False, pk__in=assets_for(user).values('submission_id'))
    return qs


def events_for(user, *, asset=None, submission=None):
    qs = ImageEvent.objects.select_related('actor', 'voice')
    if asset is not None:
        qs = qs.filter(asset=asset)
    if submission is not None:
        qs = qs.filter(submission=submission)
    if permissions.is_client(user):
        qs = qs.filter(visibility='client')
    return qs


def versions_for(user, asset):
    qs = asset.versions.all()
    if permissions.is_client(user):
        qs = qs.filter(client_visible=True)
    return qs


def version_visible_to(user, version) -> bool:
    if not can_view(user, version.asset):
        return False
    if permissions.is_client(user):
        return version.client_visible
    return True


# ---------------------------------------------------------------- the timeline

def _event(submission, kind, actor, summary, *, asset=None, client_summary='', visibility='internal', field='', old='',
           new='', reason='', version=None, at=None, voice=None) -> ImageEvent:
    ev = ImageEvent.objects.create(
        submission=submission, asset=asset, version=version, voice=voice, kind=kind, actor=actor, actor_name=name_of(actor) if actor else 'The system',
        actor_role=role_of(actor), summary=summary, client_summary=client_summary, field=field, old_value=str(old)[:2000],
        new_value=str(new)[:2000], reason=reason[:2000], visibility=visibility, created_at=at or timezone.now())
    if asset is not None:
        ImageAsset.objects.filter(pk=asset.pk).update(last_activity_at=ev.created_at)
    return ev


def url_for(asset) -> str:
    return reverse('crm:image_detail', args=[asset.pk])


def _tell(users: Iterable, actor, title: str, body: str, asset, event, event_type: str, *, level='info', url=None):
    people = [u for u in users if u is not None and (actor is None or u.pk != actor.pk)]
    if people:
        notify.notify(people, title, body=body, url=url or url_for(asset), event_type=event_type, level=level,
                      dedupe_key=f'img:{event.pk}:{event_type}')


def _lock(asset) -> ImageAsset:
    return ImageAsset.objects.select_for_update().select_related('client', 'client__owner', 'submission', 'editor', 'coordinator').get(pk=asset.pk)


# ---------------------------------------------------------------- storing files

def _store(asset, processed, *, kind, label, actor, client_visible, note='') -> ImageVersion:
    last = asset.versions.order_by('-number').first()
    number = (last.number if last else 0) + 1
    key = storage.new_key(asset.pk, asset.client_id, number, processed.ext)
    thumb_key = storage.new_key(asset.pk, asset.client_id, number, 'jpg', thumb=True)
    store = storage.get_storage()
    store.put(key, processed.data, processed.mime)
    store.put(thumb_key, processed.thumb, 'image/jpeg')
    preview_key = ''
    if processed.preview:
        preview_key = storage.new_key(asset.pk, asset.client_id, number, 'jpg', preview=True)
        store.put(preview_key, processed.preview, 'image/jpeg')
    return ImageVersion.objects.create(
        asset=asset, kind=kind, number=number, label=label, storage_key=key, thumb_key=thumb_key, preview_key=preview_key, original_name=asset_name(asset),
        mime_type=processed.mime, size=len(processed.data), width=processed.width, height=processed.height,
        sha256=processed.sha256, note=note[:500], client_visible=client_visible, uploaded_by=actor,
        shared_at=timezone.now() if client_visible and kind == 'edit' else None)


def asset_name(asset) -> str:
    return asset.title[:255]


# ---------------------------------------------------------------- uploading a batch (the client's flow)

def can_upload_for(user, client) -> bool:
    if user is None or not user.is_authenticated or not permissions.has_perm(user, MODULE, 'create'):
        return False
    if permissions.is_client(user):
        return ClientAccess.objects.filter(user=user, client=client).exists()
    from crm.models import Client
    return permissions.scoped(Client.objects.filter(pk=client.pk), user, 'clients', 'view').exists() or permissions.is_super_admin(user)


def add_draft_file(*, client, actor, batch: str, upload) -> ImageAsset:
    """One file of a batch. Files are sent one by one so a bad signal loses one photo, not all of them."""
    if not batch or len(batch) > 40:
        raise ImageError('This upload is not valid. Reload the page and try again.')
    if not can_upload_for(actor, client):
        permissions.denied(actor, MODULE, 'create', detail=f'upload for client {client.pk}')
    processed = storage.process(upload)
    with transaction.atomic():
        sub, _ = ImageSubmission.objects.get_or_create(
            batch_token=batch, defaults={'client': client, 'submitted_by': actor, 'created_by': actor,
                                         'on_behalf': not permissions.is_client(actor)})
        if sub.submitted_at is not None:
            raise ImageError('These photos were already sent. Start a new upload to send more.')
        if sub.client_id != client.pk or sub.submitted_by_id != actor.pk:
            permissions.denied(actor, MODULE, 'create', detail='batch belongs to someone else')
        if sub.assets.count() >= MAX_FILES_PER_SUBMISSION:
            raise ImageError(f'One submission can hold {MAX_FILES_PER_SUBMISSION} photos. Send these, then start another.')
        asset = ImageAsset.objects.create(submission=sub, client=client, title=(getattr(upload, 'name', '') or 'photo')[:255], created_by=actor)
        _store(asset, processed, kind='original', label='Original', actor=actor, client_visible=True)
    return asset


def _store_voice(client, submission, asset, actor, voice, visibility) -> ImageVoice:
    data, mime, ext, seconds = voice
    key = storage.voice_key(client.pk, ext)
    storage.get_storage().put(key, data, mime)
    return ImageVoice.objects.create(submission=submission, asset=asset, author=actor, storage_key=key, mime_type=mime,
                                     size=len(data), seconds=seconds, visibility=visibility)


def add_draft_voice(*, client, actor, batch: str, upload, seconds) -> ImageVoice:
    """The client's spoken brief for a batch, sent before they press Send. One short message, kept as recorded."""
    if not batch or len(batch) > 40:
        raise ImageError('This upload is not valid. Reload the page and try again.')
    if not can_upload_for(actor, client):
        permissions.denied(actor, MODULE, 'create', detail=f'voice for client {client.pk}')
    voice = storage.check_voice(upload, seconds)
    with transaction.atomic():
        sub, _ = ImageSubmission.objects.get_or_create(
            batch_token=batch, defaults={'client': client, 'submitted_by': actor, 'created_by': actor,
                                         'on_behalf': not permissions.is_client(actor)})
        if sub.submitted_at is not None:
            raise ImageError('These photos were already sent. Start a new upload to send more.')
        if sub.client_id != client.pk or sub.submitted_by_id != actor.pk:
            permissions.denied(actor, MODULE, 'create', detail='batch belongs to someone else')
        sub.voices.filter(asset__isnull=True).delete()          # re-recording replaces the draft message
        return _store_voice(client, sub, None, actor, voice, 'client')


def submit(submission, actor, note: str = '', *, auto: bool = False) -> ImageSubmission:
    """Send the batch. Numbers are given now (so abandoned drafts leave no gaps), every photo gets its own record and
    timeline, the client's person is told, and so are HR and the owners."""
    with transaction.atomic():
        sub = ImageSubmission.objects.select_for_update().select_related('client', 'client__owner').get(pk=submission.pk)
        if sub.submitted_at is not None:
            return sub
        if not auto and sub.submitted_by_id != actor.pk and not permissions.is_super_admin(actor):
            permissions.denied(actor, MODULE, 'create', detail='submit someone else\'s batch')
        assets = list(sub.assets.order_by('id'))
        if not assets:
            raise ImageError('Add at least one photo before sending.')
        voices = list(sub.voices.filter(asset__isnull=True))
        if not auto and len(note.strip()) < storage.MIN_NOTE and not voices:
            raise ImageError(f'Tell us what to do: write at least {storage.MIN_NOTE} characters, or record a voice message.')
        now = timezone.now()
        sub.number = numbering.allocate_simple('SUB')
        sub.note = note.strip()[:4000]
        sub.submitted_at = now
        sub.auto_submitted = auto
        sub.save(update_fields=['number', 'note', 'submitted_at', 'auto_submitted', 'updated_at'])
        owner = sub.client.owner if sub.client.owner.is_active else None
        by = sub.submitted_by
        who = name_of(by)
        for asset in assets:
            asset.number = numbering.allocate_simple('IMG')
            asset.coordinator = owner
            asset.instructions = sub.note
            asset.status_changed_at = now
            asset.last_activity_at = now
            asset.save(update_fields=['number', 'coordinator', 'instructions', 'status_changed_at', 'last_activity_at', 'updated_at'])
            original = asset.versions.order_by('number').first()
            _event(sub, 'uploaded', by, f'{who} uploaded {asset.title} ({asset.number}).', asset=asset, visibility='client',
                   client_summary='Photo received.', version=original, at=asset.created_at)
            if sub.note:
                _event(sub, 'instructions', by, f'{who} added instructions.', asset=asset, visibility='client', field='instructions',
                       new=sub.note)
            same = (ImageVersion.objects.filter(asset__client=sub.client, kind='original', sha256=original.sha256)
                    .exclude(asset=asset).select_related('asset').order_by('-id').first()) if original else None
            if same is not None and same.asset.number:
                _event(sub, 'duplicate', None, f'This looks like the same file as {same.asset.number}. Check it is not a duplicate.',
                       asset=asset, visibility='internal')
        for v in voices:
            _event(sub, 'voice', by, f'{who} left a voice message ({v.seconds}s).', visibility='client', client_summary='Voice message received.',
                   voice=v, at=v.created_at)
        ev = _event(sub, 'submitted', by, f'{who} sent {len(assets)} photo{"s" if len(assets) != 1 else ""} ({sub.number}).',
                    visibility='client', client_summary=f'Your {len(assets)} photo{"s were" if len(assets) != 1 else " was"} received.')
        audit.log('images.submitted', sub, actor=by, changes={'number': sub.number, 'photos': len(assets), 'client': str(sub.client)})
        first = assets[0]
        title = f'New photos from {sub.client.display_name}'
        body = f'{len(assets)} photo{"s" if len(assets) != 1 else ""} ({sub.number}) sent by {who}.'
        link = reverse('crm:image_submission', args=[sub.pk])
        _tell(coordinators_for(first), by, title, body, first, ev, 'image_submission', url=link)
        extra = list(users_with_role(HR_ROLE)) + list(notify.owners())
        _tell([u for u in extra if u.pk not in {c.pk for c in coordinators_for(first)}], by, title, body, first, ev, 'image_submission_admin', url=link)
        _tell(client_users(sub.client), by, 'We received your photos', f'{len(assets)} photo{"s" if len(assets) != 1 else ""} received ({sub.number}). We will keep you updated.',
              first, ev, 'image_received', url=link)
    return sub


# ---------------------------------------------------------------- status moves

def transition(asset, to: str, actor, *, reason: str = '', note: str = '', _quiet: bool = False) -> ImageAsset:
    reason = (reason or '').strip()
    with transaction.atomic():
        a = _lock(asset)
        frm = a.status
        allowed = FLOW.get(frm, {}).get(to)
        if not allowed:
            raise ImageError(f'A photo that is "{STATUS_LABELS[frm]}" cannot be moved to "{STATUS_LABELS[to]}".')
        if a.archived_at:
            raise ImageError('This photo was removed. Restore it first.')
        kinds = actor_kinds(actor, a)
        if not (kinds & set(allowed)):
            permissions.denied(actor, MODULE, 'edit', detail=f'{a} {frm}->{to}')
        acting_for_client = 'K' in allowed and 'K' not in kinds
        if to in REASON_NEEDED and len(reason) < 3:
            raise ImageError('Say why, in a few words, so everyone can see.')
        if acting_for_client and len(reason) < 5:
            raise ImageError('You are acting for the client. Say how they told you (a call, WhatsApp, in person).')
        now = timezone.now()
        shared = None
        if to == 'internal_review':
            if not a.versions.filter(kind='edit', uploaded_at__gte=a.status_changed_at).exists():
                raise ImageError('Upload the edited photo before sending it for review.')
        if to == 'client_review':
            shared = a.latest_edit()
            if shared is None:
                raise ImageError('There is no edited photo to send to the client yet.')
            ImageVersion.objects.filter(pk=shared.pk).update(client_visible=True, shared_at=now)
            a.client_wait_since = now
        if to == 'approved':
            final = a.versions.filter(kind='edit', client_visible=True).order_by('-number').first()
            if final is not None:
                ImageVersion.objects.filter(asset=a, is_final=True).update(is_final=False)
                ImageVersion.objects.filter(pk=final.pk).update(is_final=True, label='Final')
            a.client_wait_since = None
        if to == 'revision_requested':
            a.revision_count += 1
            a.client_wait_since = None
        if to == 'on_hold':
            a.resume_status = frm
        a.status = to
        a.status_changed_at = now
        a.save(update_fields=['status', 'status_changed_at', 'resume_status', 'revision_count', 'client_wait_since', 'updated_at'])
        client_visible = to in CLIENT_SEES
        who = name_of(actor)
        text = f'{who} moved it from "{STATUS_LABELS[frm]}" to "{STATUS_LABELS[to]}".'
        if acting_for_client:
            text = f'{who} recorded for the client: "{STATUS_LABELS[to]}".'
        ev = _event(a.submission, 'status', actor, text, asset=a, visibility='client' if client_visible else 'internal',
                    client_summary=(CLIENT_SEES.get(to, '') + (f' {reason}' if reason and to in ('clarification', 'revision_requested') else '')).strip(),
                    field='status', old=frm, new=to, reason=reason or note, version=shared)
        audit.log('images.status', a, actor=actor, changes={'from': frm, 'to': to, 'reason': reason})
        if not _quiet:
            _notify_status(a, to, actor, ev, reason)
    return a


def _notify_status(a, to, actor, ev, reason):
    coords = coordinators_for(a)
    editor = [a.editor] if a.editor_id else []
    clients = list(client_users(a.client))
    name = a.number
    if to == 'clarification':
        _tell(clients, actor, f'A question about {name}', reason or 'We need a little more information.', a, ev, 'image_question')
    elif to == 'editing':
        _tell(clients, actor, f'Editing started on {name}', 'Our editor has started working on your photo.', a, ev, 'image_editing')
    elif to == 'internal_review':
        _tell(coords, actor, f'{name} is ready for internal review', f'{name_of(a.editor)} finished the edit.', a, ev, 'image_review', level='warning')
    elif to == 'changes_internal':
        _tell(editor, actor, f'Changes needed on {name}', reason, a, ev, 'image_changes')
    elif to == 'client_review':
        _tell(clients, actor, 'Your edited photo is ready', f'{name} is ready for you to review. Approve it or ask for a change.', a, ev, 'image_ready')
    elif to == 'revision_requested':
        _tell(coords + editor, actor, f'{a.client.display_name} asked for a revision on {name}', reason, a, ev, 'image_revision', level='warning')
        _tell(clients, actor, 'Revision requested', f'We have your request for {name}.', a, ev, 'image_revision_client')
    elif to == 'revision_in_progress':
        _tell(clients, actor, f'Revision started on {name}', 'We are making the changes you asked for.', a, ev, 'image_editing')
    elif to == 'approved':
        _tell(coords + editor, actor, f'{name} approved by {a.client.display_name}', '', a, ev, 'image_approved')
        _tell(clients, actor, 'Final photo approved', f'{name} is approved.', a, ev, 'image_approved_client')
    elif to == 'delivered':
        _tell(clients, actor, 'Your final photo is ready to download', f'{name} has been delivered.', a, ev, 'image_delivered')
        _tell(editor, actor, f'{name} delivered', '', a, ev, 'image_delivered_team')
    elif to in ('on_hold', 'cancelled'):
        _tell(editor + clients, actor, f'{name} is {STATUS_LABELS[to].lower()}', reason, a, ev, f'image_{to}')


def resume(asset, actor, reason: str = '') -> ImageAsset:
    """Take a photo off hold and put it back where it was."""
    with transaction.atomic():
        a = _lock(asset)
        if a.status != 'on_hold':
            raise ImageError('Only a photo on hold can be resumed.')
        if not is_coordinator(actor, a):
            permissions.denied(actor, MODULE, 'edit', detail=f'resume {a}')
        to = a.resume_status if a.resume_status in STATUS_LABELS and a.resume_status != 'on_hold' else 'ready'
        now = timezone.now()
        a.status, a.resume_status, a.status_changed_at = to, '', now
        a.save(update_fields=['status', 'resume_status', 'status_changed_at', 'updated_at'])
        ev = _event(a.submission, 'status', actor, f'{name_of(actor)} resumed it: back to "{STATUS_LABELS[to]}".', asset=a,
                    visibility='client', client_summary='The photo is moving again.', field='status', old='on_hold', new=to, reason=reason)
        audit.log('images.status', a, actor=actor, changes={'from': 'on_hold', 'to': to})
        _tell([a.editor] if a.editor_id else [], actor, f'{a.number} is back in progress', reason, a, ev, 'image_resumed')
    return a


# ---------------------------------------------------------------- coordinator controls: people, dates, instructions

def _changed(a, actor, kind, field, label, old, new, reason, *, visibility='internal', client_text=''):
    return _event(a.submission, kind, actor, f'{name_of(actor)} changed {label} from "{old or "nothing"}" to "{new or "nothing"}".',
                  asset=a, visibility=visibility, client_summary=client_text, field=field, old=old, new=new, reason=reason)


def assign(asset, editor, actor, *, deadline=None, priority: str | None = None, reason: str = '') -> ImageAsset:
    """Give a photo to an editor (or move it to another). Priority and deadline can be set at the same time."""
    with transaction.atomic():
        a = _lock(asset)
        if not is_coordinator(actor, a):
            permissions.denied(actor, MODULE, 'assign', detail=f'assign {a}')
        if not permissions.has_perm(actor, MODULE, 'assign'):
            permissions.denied(actor, MODULE, 'assign')
        if a.archived_at or a.status in DONE or a.status == 'on_hold':
            raise ImageError('This photo cannot be assigned right now.')
        if editor is None or not editors().filter(pk=editor.pk).exists():
            raise ImageError('Choose someone from the editing team.')
        previous = a.editor
        first = previous is None
        if previous is not None and previous.pk == editor.pk and deadline is None and priority is None:
            raise ImageError(f'{name_of(editor)} already has this photo.')
        now = timezone.now()
        a.editor = editor
        if a.status in ('new', 'ready', 'clarification'):
            frm = a.status
            a.status, a.status_changed_at = 'assigned', now
        else:
            frm = None
        a.save(update_fields=['editor', 'status', 'status_changed_at', 'updated_at'])
        ev = _changed(a, actor, 'assignment', 'editor', 'the editor', name_of(previous) if previous else '', name_of(editor), reason)
        if frm:
            _event(a.submission, 'status', actor, f'{name_of(actor)} moved it from "{STATUS_LABELS[frm]}" to "Assigned".', asset=a,
                   field='status', old=frm, new='assigned', reason=reason)
        audit.log('images.assigned', a, actor=actor, changes={'editor': str(editor), 'previous': str(previous) if previous else '', 'reason': reason})
    if priority and priority != a.priority:
        set_priority(a, actor, priority, reason=reason, _notify=False)
    if deadline is not None and deadline != a.deadline:
        set_deadline(a, actor, deadline, reason=reason, _notify=False)
    a.refresh_from_db()
    title = f'New photo assigned: {a.number}' if first else f'{a.number} was assigned to you'
    body = f'{a.client.display_name}. Due {a.deadline:%d %b}.' if a.deadline else a.client.display_name
    _tell([editor], actor, title, body, a, ev, 'image_assigned')
    if previous is not None and previous.pk != editor.pk:
        _tell([previous], actor, f'{a.number} was moved to {name_of(editor)}', reason, a, ev, 'image_reassigned')
    return a


def assign_many(assets, editor, actor, *, deadline=None, priority=None, reason='') -> int:
    n = 0
    for asset in assets:
        try:
            assign(asset, editor, actor, deadline=deadline, priority=priority, reason=reason)
            n += 1
        except (ImageError, PermissionDenied):
            continue
    return n


def set_priority(asset, actor, priority: str, *, reason: str = '', _notify: bool = True) -> ImageAsset:
    if priority not in dict(PRIORITIES):
        raise ImageError('Choose a priority.')
    with transaction.atomic():
        a = _lock(asset)
        _need(actor, a, 'C', 'priority')
        if a.priority == priority:
            return a
        old = a.priority
        a.priority = priority
        a.save(update_fields=['priority', 'updated_at'])
        ev = _changed(a, actor, 'priority', 'priority', 'the priority', dict(PRIORITIES)[old], dict(PRIORITIES)[priority], reason)
        audit.log('images.priority', a, actor=actor, changes={'from': old, 'to': priority})
    if _notify and a.editor_id:
        _tell([a.editor], actor, f'Priority changed on {a.number}', f'Now {dict(PRIORITIES)[priority].lower()}.', a, ev, 'image_priority')
    return a


def set_deadline(asset, actor, deadline, *, reason: str = '', _notify: bool = True) -> ImageAsset:
    with transaction.atomic():
        a = _lock(asset)
        _need(actor, a, 'C', 'deadline')
        if deadline == a.deadline:
            return a
        if deadline is not None and deadline < india.today() and a.deadline != deadline:
            raise ImageError('Choose a deadline that is today or later.')
        old = a.deadline
        a.deadline = deadline
        a.save(update_fields=['deadline', 'updated_at'])
        ev = _changed(a, actor, 'deadline', 'deadline', 'the deadline', f'{old:%d %b %Y}' if old else '', f'{deadline:%d %b %Y}' if deadline else '',
                      reason, visibility='client', client_text=(f'Expected by {deadline:%d %b %Y}.' if deadline else 'The expected date was cleared.'))
        audit.log('images.deadline', a, actor=actor, changes={'from': str(old or ''), 'to': str(deadline or '')})
    if _notify and a.editor_id:
        _tell([a.editor], actor, f'Deadline changed on {a.number}', f'Now {deadline:%d %b %Y}.' if deadline else 'Deadline removed.', a, ev, 'image_deadline')
    return a


def set_instructions(asset, actor, text: str, *, internal: bool = False, reason: str = '') -> ImageAsset:
    text = (text or '').strip()[:4000]
    with transaction.atomic():
        a = _lock(asset)
        _need(actor, a, 'C', 'instructions')
        field = 'internal_instructions' if internal else 'instructions'
        old = getattr(a, field)
        if old == text:
            return a
        setattr(a, field, text)
        a.save(update_fields=[field, 'updated_at'])
        label = 'the team-only instructions' if internal else 'the editing instructions'
        ev = _changed(a, actor, 'instructions', field, label, old, text, reason, visibility='internal' if internal else 'client',
                      client_text='' if internal else 'The editing instructions were updated.')
        audit.log('images.instructions', a, actor=actor, changes={'field': field, 'reason': reason})
    if a.editor_id:
        _tell([a.editor], actor, f'New instructions on {a.number}', text[:200], a, ev, 'image_instructions')
    if not internal:
        _tell(client_users(a.client), actor, f'Instructions updated for {a.number}', 'We updated what we will do to your photo.', a, ev, 'image_instructions_client')
    return a


def set_coordinator(asset, user, actor, reason: str = '') -> ImageAsset:
    with transaction.atomic():
        a = _lock(asset)
        _need(actor, a, 'C', 'coordinator')
        old = a.coordinator
        if user is not None and (not user.is_active or permissions.is_client(user)):
            raise ImageError('Choose someone from the sales team.')
        a.coordinator = user
        a.save(update_fields=['coordinator', 'updated_at'])
        ev = _changed(a, actor, 'coordinator', 'coordinator', 'the coordinator', name_of(old) if old else '', name_of(user) if user else '', reason)
    _tell([user] if user else [], actor, f'{a.number} is now yours to coordinate', a.client.display_name, a, ev, 'image_coordinator')
    return a


# ---------------------------------------------------------------- comments

def comment(asset, actor, body: str = '', *, visibility: str = 'internal', voice=None) -> ImageEvent:
    """A written comment, or a spoken one (`voice` is the checked recording from storage.check_voice), or both."""
    body = (body or '').strip()
    if voice is None and len(body) < 2:
        raise ImageError('Write your comment first.')
    with transaction.atomic():
        a = _lock(asset)
        kinds = actor_kinds(actor, a)
        if not kinds and not (assets_for(actor).filter(pk=a.pk).exists() and permissions.has_perm(actor, MODULE, 'view')):
            permissions.denied(actor, MODULE, 'view', detail=f'comment on {a}')
        is_client = 'K' in kinds
        if is_client:
            visibility = 'client'
        elif 'C' not in kinds:
            visibility = 'internal'          # editors and other staff speak to the team, not to the client
        v = _store_voice(a.client, a.submission, a, actor, voice, visibility) if voice is not None else None
        spoken = f'{name_of(actor)} left a voice message ({v.seconds}s).' if v else ''
        text = f'{name_of(actor)}: {body}' if body else spoken
        if body and v:
            text = f'{text} (with a voice message, {v.seconds}s)'
        ev = _event(a.submission, 'voice' if (v and not body) else 'comment', actor, text, asset=a, visibility=visibility,
                    new=body[:2000], voice=v, client_summary=text if visibility == 'client' else '')
        if is_client and a.status == 'clarification':
            old = a.status
            a.status, a.status_changed_at = 'new', timezone.now()
            a.save(update_fields=['status', 'status_changed_at', 'updated_at'])
            _event(a.submission, 'status', actor, f'{name_of(actor)} replied, so it moved from "Clarification required" to "New submission".',
                   asset=a, visibility='client', client_summary='We received your reply.', field='status', old=old, new='new')
        audit.log('images.comment', a, actor=actor, changes={'visibility': visibility, 'voice': bool(v)})
    preview = body[:200] or 'Voice message'
    if is_client:
        _tell(coordinators_for(a) + ([a.editor] if a.editor_id else []), actor, f'{a.client.display_name} commented on {a.number}', preview, a, ev, 'image_comment', level='warning')
    elif visibility == 'client':
        _tell(client_users(a.client), actor, f'A message about {a.number}', preview, a, ev, 'image_comment_client')
    else:
        _tell(([a.editor] if a.editor_id else []) + coordinators_for(a), actor, f'Note on {a.number}', preview, a, ev, 'image_note')
    return ev


def voice_visible_to(user, voice) -> bool:
    """Who may hear a voice message: the people who may see the photo or batch it belongs to. A client never hears team-only ones,
    and a message still in a draft is only for the person recording it."""
    sub = voice.submission
    if sub.submitted_at is None:
        return voice.author_id == user.pk
    if permissions.is_client(user) and voice.visibility != 'client':
        return False
    return submissions_for(user).filter(pk=sub.pk).exists() if voice.asset_id is None else assets_for(user).filter(pk=voice.asset_id).exists()


# ---------------------------------------------------------------- versions

def upload_version(asset, actor, upload, *, kind: str = 'edit', note: str = '', send_for_review: bool = False) -> ImageVersion:
    """An edited version (editor or coordinator) or a reference picture (coordinator). Never touches the original."""
    if kind not in ('edit', 'reference'):
        raise ImageError('Choose what kind of file this is.')
    processed = storage.process(upload)
    with transaction.atomic():
        a = _lock(asset)
        kinds = actor_kinds(actor, a)
        if a.archived_at:
            raise ImageError('This photo was removed. Restore it first.')
        if kind == 'edit':
            if not kinds & {'E', 'C'}:
                permissions.denied(actor, MODULE, 'edit', detail=f'upload edit on {a}')
            if a.status not in ('editing', 'revision_in_progress') and not ('C' in kinds and a.status in ('internal_review', 'changes_internal')):
                raise ImageError('Start editing first. Edited photos can only be added while the photo is being edited.')
            edits = a.versions.filter(kind='edit').count()
            label = 'First edit' if edits == 0 else f'Revision {edits}'
        else:
            if 'C' not in kinds:
                permissions.denied(actor, MODULE, 'edit', detail=f'upload reference on {a}')
            label = 'Reference'
        v = _store(asset=a, processed=processed, kind=kind, label=label, actor=actor, client_visible=False, note=note)
        who = name_of(actor)
        ev = _event(a.submission, 'version', actor, f'{who} uploaded {label.lower()} ({a.number}, version {v.number}).' + (f' Note: {note}' if note else ''),
                    asset=a, visibility='internal', version=v, new=label, reason=note)
        audit.log('images.version', a, actor=actor, changes={'version': v.number, 'kind': kind, 'size': v.size})
    if kind == 'reference' and a.editor_id:
        _tell([a.editor], actor, f'Reference added to {a.number}', note, a, ev, 'image_reference')
    if send_for_review and kind == 'edit':
        transition(a, 'internal_review', actor)
    return v


def replace_original(asset, actor, upload, *, reason: str) -> ImageVersion:
    """The client sent the wrong picture, or a duplicate. The old file stays in the history; the new one becomes the original."""
    reason = (reason or '').strip()
    if len(reason) < 3:
        raise ImageError('Say why the photo is being replaced.')
    processed = storage.process(upload)
    with transaction.atomic():
        a = _lock(asset)
        _need(actor, a, 'C', 'replace')
        if a.status in ('approved', 'delivered', 'cancelled'):
            raise ImageError('A photo that is already finished cannot be replaced. Add it as a new photo instead.')
        previous = a.original()
        v = _store(asset=a, processed=processed, kind='original', label='Original (replaced)', actor=actor, client_visible=True, note=reason)
        if previous is not None:
            ImageVersion.objects.filter(pk=previous.pk).update(superseded_at=timezone.now())
        ev = _event(a.submission, 'replaced', actor, f'{name_of(actor)} replaced the original photo. Reason: {reason}', asset=a, visibility='client',
                    client_summary=f'The original photo was replaced. {reason}', version=v, field='original',
                    old=previous.original_name if previous else '', new=a.title, reason=reason)
        audit.log('images.replaced', a, actor=actor, changes={'reason': reason, 'version': v.number})
    _tell(([a.editor] if a.editor_id else []) + list(client_users(a.client)), actor, f'The photo for {a.number} was replaced', reason, a, ev, 'image_replaced')
    return v


def add_image(submission, actor, upload) -> ImageAsset:
    """Add a photo to a batch that was already sent (for example one the client sent over WhatsApp)."""
    processed = storage.process(upload)
    with transaction.atomic():
        sub = ImageSubmission.objects.select_for_update().select_related('client', 'client__owner').get(pk=submission.pk)
        if sub.submitted_at is None:
            raise ImageError('This batch has not been sent yet.')
        if not permissions.has_perm(actor, MODULE, 'approve') or permissions.is_client(actor):
            permissions.denied(actor, MODULE, 'approve', detail=f'add image to {sub}')
        if not submissions_for(actor).filter(pk=sub.pk).exists():
            permissions.denied(actor, MODULE, 'approve', detail=f'add image to {sub}')
        if sub.assets.count() >= MAX_FILES_PER_SUBMISSION:
            raise ImageError(f'One submission can hold {MAX_FILES_PER_SUBMISSION} photos.')
        now = timezone.now()
        asset = ImageAsset.objects.create(submission=sub, client=sub.client, title=(getattr(upload, 'name', '') or 'photo')[:255],
                                          number=numbering.allocate_simple('IMG'), created_by=actor,
                                          coordinator=sub.client.owner if sub.client.owner.is_active else None,
                                          instructions=sub.note, status_changed_at=now, last_activity_at=now)
        v = _store(asset, processed, kind='original', label='Original', actor=actor, client_visible=True)
        _event(sub, 'image_added', actor, f'{name_of(actor)} added {asset.title} ({asset.number}) to {sub.number}.', asset=asset,
               visibility='client', client_summary='A photo was added to your submission.', version=v)
        audit.log('images.added', asset, actor=actor, changes={'submission': sub.number})
    return asset


def archive(asset, actor, reason: str) -> ImageAsset:
    """Remove a photo from the working lists without deleting anything. Its files and timeline stay."""
    reason = (reason or '').strip()
    if len(reason) < 3:
        raise ImageError('Say why this photo is being removed.')
    with transaction.atomic():
        a = _lock(asset)
        _need(actor, a, 'C', 'remove')
        if a.archived_at:
            return a
        if a.status == 'delivered':
            raise ImageError('A delivered photo is part of the client record and cannot be removed.')
        a.archived_at, a.archived_by, a.archive_reason = timezone.now(), actor, reason[:300]
        a.save(update_fields=['archived_at', 'archived_by', 'archive_reason', 'updated_at'])
        ev = _event(a.submission, 'archived', actor, f'{name_of(actor)} removed the photo. Reason: {reason}', asset=a, visibility='client',
                    client_summary=f'The photo was removed from your list. {reason}', reason=reason)
        audit.log('images.archived', a, actor=actor, changes={'reason': reason})
    _tell(([a.editor] if a.editor_id else []), actor, f'{a.number} was removed', reason, a, ev, 'image_archived')
    return a


def restore(asset, actor, reason: str = '') -> ImageAsset:
    with transaction.atomic():
        a = _lock(asset)
        _need(actor, a, 'C', 'restore')
        if not a.archived_at:
            return a
        a.archived_at, a.archived_by, a.archive_reason = None, None, ''
        a.save(update_fields=['archived_at', 'archived_by', 'archive_reason', 'updated_at'])
        _event(a.submission, 'restored', actor, f'{name_of(actor)} restored the photo.', asset=a, visibility='client',
               client_summary='The photo is back on your list.', reason=reason)
        audit.log('images.restored', a, actor=actor, changes={})
    return a


# ---------------------------------------------------------------- the client's two decisions

def approve(asset, actor, reason: str = '') -> ImageAsset:
    return transition(asset, 'approved', actor, reason=reason)


def request_revision(asset, actor, what: str) -> ImageAsset:
    what = (what or '').strip()
    if len(what) < 5:
        raise ImageError('Tell us what to change, in a few words.')
    return transition(asset, 'revision_requested', actor, reason=what)


# ---------------------------------------------------------------- reading it back

def progress(submission) -> dict:
    """The overall state of a batch, worked out from its photos. It is never stored, so it can never disagree with them."""
    counts = {s: 0 for s in STATUS_LABELS}
    rows = submission.assets.filter(archived_at__isnull=True).values('status').annotate(n=Count('id'))
    total = 0
    for r in rows:
        counts[r['status']] = r['n']
        total += r['n']
    finished = counts['approved'] + counts['delivered']
    cancelled = counts['cancelled']
    live = total - cancelled
    return {'total': total, 'done': finished, 'cancelled': cancelled, 'counts': {k: v for k, v in counts.items() if v},
            'percent': round(100 * finished / live) if live else 0,
            'waiting_client': counts['client_review'], 'editing': sum(counts[s] for s in ('assigned', 'editing', 'revision_in_progress', 'changes_internal')),
            'new': counts['new'] + counts['ready'] + counts['clarification'], 'revision': counts['revision_requested'],
            'review': counts['internal_review']}


def counts_for(user) -> dict:
    qs = assets_for(user).filter(archived_at__isnull=True)
    today = india.today()
    by = {r['status']: r['n'] for r in qs.values('status').annotate(n=Count('id'))}
    c = {
        'total': sum(by.values()), 'new': by.get('new', 0), 'clarification': by.get('clarification', 0), 'ready': by.get('ready', 0),
        'assigned': by.get('assigned', 0), 'editing': by.get('editing', 0) + by.get('revision_in_progress', 0) + by.get('changes_internal', 0),
        'internal_review': by.get('internal_review', 0), 'client_review': by.get('client_review', 0),
        'revision': by.get('revision_requested', 0), 'approved': by.get('approved', 0), 'delivered': by.get('delivered', 0),
        'on_hold': by.get('on_hold', 0), 'overdue': qs.filter(status__in=ACTIVE, deadline__lt=today).count(),
        'unassigned': qs.filter(status__in=('new', 'ready'), editor__isnull=True).count(),
        'submissions': submissions_for(user).count(),
    }
    c['being_edited'] = c['assigned'] + c['editing']
    c['finished'] = c['approved'] + c['delivered']
    return c


def workload(user) -> list[dict]:
    """Per person: what they hold, what is late. For coordinators and HR. People are only the ones whose photos this user may see."""
    qs = assets_for(user).filter(archived_at__isnull=True)
    today = india.today()
    rows = []
    for field, label in (('editor', 'Editor'), ('coordinator', 'Coordinator')):
        grouped = (qs.exclude(**{f'{field}__isnull': True}).values(field, f'{field}__first_name', f'{field}__username')
                   .annotate(total=Count('id'), active=Count('id', filter=Q(status__in=ACTIVE)),
                             working=Count('id', filter=Q(status__in=WORKING)),
                             late=Count('id', filter=Q(status__in=ACTIVE, deadline__lt=today)),
                             waiting=Count('id', filter=Q(status='client_review')),
                             done=Count('id', filter=Q(status__in=('approved', 'delivered'))),
                             revisions=Count('id', filter=Q(revision_count__gt=0))))
        for r in grouped:
            rows.append({'role': label, 'user_id': r[field], 'name': r[f'{field}__first_name'] or r[f'{field}__username'], **{k: r[k] for k in ('total', 'active', 'working', 'late', 'waiting', 'done', 'revisions')}})
    rows.sort(key=lambda r: (r['role'], -r['late'], -r['active']))
    return rows


# ---------------------------------------------------------------- client logins

def create_client_login(client, actor, *, username: str, full_name: str = '', email: str = '') -> tuple:
    """A login for someone at the client's company. The same sign-in page as staff; what they can see follows from the link
    to the client and the Client role. Returns (user, temporary password); the password is shown once and must be changed."""
    import secrets
    from crm.models import Role, UserProfile
    if not permissions.has_perm(actor, MODULE, 'configure'):
        permissions.denied(actor, MODULE, 'configure', detail='create client login')
    username = (username or '').strip()
    if len(username) < 3:
        raise ImageError('Choose a username of at least 3 characters.')
    User = get_user_model()
    if User.objects.filter(username__iexact=username).exists():
        raise ImageError('That username is already taken. Choose another.')
    if email and User.objects.filter(email__iexact=email).exists():
        raise ImageError('That email already belongs to another login.')
    role = Role.objects.filter(code='image_client').first()
    if role is None:
        raise ImageError('The Client role is missing. Ask an administrator to run the database update.')
    password = secrets.token_urlsafe(12)
    first, _, last = (full_name or '').strip().partition(' ')
    with transaction.atomic():
        user = User.objects.create_user(username=username, email=email.strip(), password=password, first_name=first[:150], last_name=last[:150])
        ClientAccess.objects.create(user=user, client=client, created_by=actor)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.roles.set([role])
        profile.must_change_password = True
        profile.save(update_fields=['must_change_password'])
        audit.log('images.client_login_created', client, actor=actor, changes={'username': username})
    return user, password


def reset_client_password(access, actor) -> str:
    import secrets
    if not permissions.has_perm(actor, MODULE, 'configure'):
        permissions.denied(actor, MODULE, 'configure', detail='reset client password')
    password = secrets.token_urlsafe(12)
    with transaction.atomic():
        access.user.set_password(password)
        access.user.save(update_fields=['password'])
        profile = access.user.crm_profile
        profile.must_change_password = True
        profile.failed_logins, profile.locked_until = 0, None
        profile.save(update_fields=['must_change_password', 'failed_logins', 'locked_until'])
        audit.log('images.client_password_reset', access.client, actor=actor, changes={'username': access.user.get_username()})
    from crm.modules.auth.views import end_sessions
    end_sessions(access.user)
    return password


def set_client_login_active(access, actor, active: bool) -> None:
    if not permissions.has_perm(actor, MODULE, 'configure'):
        permissions.denied(actor, MODULE, 'configure', detail='change client login')
    access.user.is_active = active
    access.user.save(update_fields=['is_active'])
    audit.log('images.client_login_active' if active else 'images.client_login_stopped', access.client, actor=actor,
              changes={'username': access.user.get_username()})
    if not active:
        from crm.modules.auth.views import end_sessions
        end_sessions(access.user)
