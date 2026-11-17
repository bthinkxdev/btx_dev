"""Client photos: the client's own pages and the team's pages, on the one set of URLs. What a person sees and may do comes from
their permissions and their link to a client, never from a separate app."""
from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from crm.core import audit, india, permissions
from crm.core.views import Filter, ListPage, get_scoped, staff_required
from crm.models import Client, ClientAccess, ImageAsset, ImageSubmission, ImageVersion, ImageVoice
from crm.models.images import ACTIVE, CLIENT_LABELS, PRIORITIES, STATUSES, VERSION_KINDS

from . import services as svc
from . import storage

MODULE = 'images'


def _gone(request, what='This photo'):
    """A photo that does not exist, was removed, or belongs to someone else. Never a blank page or a hint which it was."""
    return render(request, 'crm/images/gone.html', {'what': what}, status=404)


def _asset(request, pk):
    return (svc.assets_for(request.user).select_related('client', 'client__owner', 'submission', 'editor', 'coordinator')
            .filter(pk=pk).first())


def _brief_voices(user, submission):
    qs = submission.voices.filter(asset__isnull=True)
    return qs.filter(visibility='client') if permissions.is_client(user) else qs


def _is_client(request) -> bool:
    return permissions.is_client(request.user)


def _my_client(user):
    access = ClientAccess.objects.select_related('client').filter(user=user).first()
    return access.client if access else None


def _clients_for(user):
    return permissions.scoped(Client.objects.filter(archived_at__isnull=True, merged_into__isnull=True), user, 'clients', 'view') \
        if permissions.has_perm(user, 'clients', 'view') else Client.objects.none()


def _staff_pick_clients(user):
    if permissions.is_super_admin(user):
        return Client.objects.filter(archived_at__isnull=True, merged_into__isnull=True).order_by('display_name')
    return _clients_for(user).order_by('display_name')


# ---------------------------------------------------------------- home

@staff_required
def home(request):
    permissions.require(request.user, MODULE, 'view', request)
    u = request.user
    counts = svc.counts_for(u)
    base = svc.with_thumbs(svc.assets_for(u).select_related('client', 'editor', 'coordinator', 'submission').filter(archived_at__isnull=True), u)
    ctx = {'counts': counts, 'is_client': _is_client(request), 'today': india.today()}
    if ctx['is_client']:
        ctx['waiting'] = list(base.filter(status='client_review').order_by('-status_changed_at')[:12])
        ctx['recent'] = list(base.order_by('-last_activity_at')[:12])
        ctx['client'] = _my_client(u)
        return render(request, 'crm/images/client_home.html', ctx)
    mine = permissions.has_perm(u, MODULE, 'assign') or permissions.has_perm(u, MODULE, 'approve')
    if mine:
        ctx['attention'] = list(base.filter(Q(status='internal_review') | Q(status='revision_requested') |
                                            Q(status__in=('new', 'ready'), editor__isnull=True) | Q(status='clarification') |
                                            Q(status__in=ACTIVE, deadline__lt=india.today())).order_by('deadline', '-priority', 'id')[:15])
    if permissions.has_perm(u, MODULE, 'edit') and not mine:
        ctx['queue'] = list(base.filter(editor=u, status__in=('assigned', 'editing', 'changes_internal', 'revision_requested', 'revision_in_progress'))
                            .order_by('deadline', '-priority', 'id')[:30])
    ctx['recent'] = list(base.order_by('-last_activity_at')[:10])
    ctx['can_upload'] = permissions.has_perm(u, MODULE, 'create')
    ctx['can_team'] = _can_see_team(u)
    return render(request, 'crm/images/home.html', ctx)


def _can_see_team(user) -> bool:
    return permissions.has_perm(user, MODULE, 'assign') or permissions.has_scope(user, MODULE, 'view', 'team')


# ---------------------------------------------------------------- the photo list

def _editor_choices(request):
    return [(u.pk, u.get_full_name() or u.get_username()) for u in svc.editors()]


def _coordinator_choices(request):
    from django.contrib.auth import get_user_model
    ids = set(ImageAsset.objects.exclude(coordinator__isnull=True).values_list('coordinator_id', flat=True))
    return [(u.pk, u.get_full_name() or u.get_username()) for u in get_user_model().objects.filter(pk__in=ids).order_by('first_name')]


def _client_choices(request):
    return [(c.pk, c.display_name) for c in _staff_pick_clients(request.user)[:400]]


class ImageList(ListPage):
    model = ImageAsset
    module = MODULE
    template_name = 'crm/images/list.html'
    title = 'Photos'
    search_fields = ('number__icontains', 'title__icontains', 'client__display_name__icontains', 'submission__number__icontains')
    select_related = ('client', 'editor', 'coordinator', 'submission')
    default_sort = '-last_activity_at'
    sort_fields = {'updated': 'last_activity_at', 'deadline': 'deadline', 'client': 'client__display_name', 'created': 'created_at'}
    staff_filters = [
        Filter('status', 'Status', STATUSES),
        Filter('priority', 'Priority', PRIORITIES),
        Filter('client', 'Client', _client_choices, lookup='client_id'),
        Filter('editor', 'Editor', _editor_choices, lookup='editor_id'),
        Filter('coordinator', 'Coordinator', _coordinator_choices, lookup='coordinator_id'),
    ]
    client_filters = [Filter('status', 'Status', [(k, v) for k, v in CLIENT_LABELS.items()], apply=lambda qs, v, r: qs.filter(status=v))]
    all_quick = {
        'attention': ('Needs my action', lambda qs, r: qs.filter(Q(status='internal_review') | Q(status='revision_requested') | Q(status='clarification') |
                                                                 Q(status__in=('new', 'ready'), editor__isnull=True))),
        'mine': ('Mine', lambda qs, r: qs.filter(Q(editor=r.user) | Q(coordinator=r.user) | Q(client__owner=r.user))),
        'overdue': ('Overdue', lambda qs, r: qs.filter(status__in=ACTIVE, deadline__lt=india.today())),
        'unassigned': ('No editor', lambda qs, r: qs.filter(status__in=('new', 'ready', 'clarification'), editor__isnull=True)),
        'review': ('Internal review', lambda qs, r: qs.filter(status='internal_review')),
        'client': ('With the client', lambda qs, r: qs.filter(status='client_review')),
        'revisions': ('Revisions', lambda qs, r: qs.filter(status__in=('revision_requested', 'revision_in_progress'))),
        'done': ('Approved or delivered', lambda qs, r: qs.filter(status__in=('approved', 'delivered'))),
        'removed': ('Removed', lambda qs, r: qs),
    }
    client_quick = ('client', 'revisions', 'done')
    export_name = 'photos'
    export_columns = [('Photo', 'number'), ('Submission', lambda a: a.submission.number), ('Client', lambda a: a.client.display_name),
                      ('File', 'title'), ('Status', lambda a: a.get_status_display()), ('Priority', lambda a: a.get_priority_display()),
                      ('Editor', lambda a: svc.name_of(a.editor) if a.editor else ''), ('Coordinator', lambda a: svc.name_of(a.coordinator) if a.coordinator else ''),
                      ('Deadline', 'deadline'), ('Revisions', 'revision_count'), ('Sent', 'created_at')]

    @property
    def filters(self):
        return self.client_filters if _is_client(self.request) else self.staff_filters

    @property
    def quick_filters(self):
        if _is_client(self.request):
            return {k: v for k, v in self.all_quick.items() if k in self.client_quick}
        return self.all_quick

    def base_queryset(self):
        qs = svc.with_thumbs(svc.assets_for(self.request.user), self.request.user)
        qs = qs.select_related(*self.select_related)
        if self.request.GET.get('quick') == 'removed' and not _is_client(self.request):
            return qs.filter(archived_at__isnull=False)
        return qs.filter(archived_at__isnull=True)

    def extra_context(self, qs):
        u = self.request.user
        return {'is_client': _is_client(self.request), 'can_upload': permissions.has_perm(u, MODULE, 'create'),
                'search_placeholder': 'Search photo number, file or client', 'statuses': STATUSES}


# ---------------------------------------------------------------- uploading

@staff_required
def upload(request):
    permissions.require(request.user, MODULE, 'create', request)
    u = request.user
    if _is_client(request):
        client = _my_client(u)
        clients = [client] if client else []
    else:
        clients = list(_staff_pick_clients(u)[:500])
        wanted = request.GET.get('client', '')
        client = next((c for c in clients if str(c.pk) == wanted), None) or (clients[0] if len(clients) == 1 else None)
    return render(request, 'crm/images/upload.html', {
        'is_client': _is_client(request), 'clients': clients, 'client': client,
        'max_mb': int(storage.settings.CRM_IMAGE_MAX_MB), 'voice_max': storage.max_voice_seconds(), 'min_note': storage.MIN_NOTE})


@staff_required
@require_POST
def upload_file(request):
    """One photo of a batch. JSON in and out so the page can show progress and retry a single file."""
    u = request.user
    try:
        permissions.require(u, MODULE, 'create', request)
        if _is_client(request):
            client = _my_client(u)
        else:
            client = get_object_or_404(_staff_pick_clients(u), pk=request.POST.get('client') or 0)
        if client is None:
            raise svc.ImageError('Your login is not linked to a client yet. Ask us to fix it.')
        file = request.FILES.get('file')
        if file is None:
            raise svc.ImageError('No photo came through. Try again.')
        asset = svc.add_draft_file(client=client, actor=u, batch=request.POST.get('batch', ''), upload=file)
        return JsonResponse({'ok': True, 'id': asset.pk, 'name': asset.title})
    except (svc.ImageError, storage.ImageFileError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except Http404:
        return JsonResponse({'ok': False, 'error': 'Choose the client first.'}, status=400)


@staff_required
@require_POST
def upload_send(request):
    u = request.user
    sub = ImageSubmission.objects.filter(batch_token=request.POST.get('batch', ''), submitted_by=u).first()
    if sub is None:
        return JsonResponse({'ok': False, 'error': 'Nothing was uploaded yet. Add photos first.'}, status=400)
    try:
        sub = svc.submit(sub, u, request.POST.get('note', ''))
    except svc.ImageError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    return JsonResponse({'ok': True, 'url': reverse('crm:image_submission', args=[sub.pk]), 'number': sub.number})


@staff_required
@require_POST
def upload_voice(request):
    """The client's spoken brief for the batch. One small file, stored as recorded."""
    u = request.user
    try:
        permissions.require(u, MODULE, 'create', request)
        if _is_client(request):
            client = _my_client(u)
        else:
            client = get_object_or_404(_staff_pick_clients(u), pk=request.POST.get('client') or 0)
        if client is None:
            raise svc.ImageError('Your login is not linked to a client yet. Ask us to fix it.')
        file = request.FILES.get('file')
        if file is None:
            raise svc.ImageError('No recording came through. Try again.')
        v = svc.add_draft_voice(client=client, actor=u, batch=request.POST.get('batch', ''), upload=file, seconds=request.POST.get('seconds'))
        return JsonResponse({'ok': True, 'id': v.pk, 'seconds': v.seconds})
    except (svc.ImageError, storage.ImageFileError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except Http404:
        return JsonResponse({'ok': False, 'error': 'Choose the client first.'}, status=400)


@staff_required
@require_POST
def voice_post(request, pk):
    """A spoken comment on one photo."""
    asset = _asset(request, pk)
    if asset is None:
        return JsonResponse({'ok': False, 'error': 'This photo is not available.'}, status=404)
    try:
        file = request.FILES.get('file')
        if file is None:
            raise svc.ImageError('No recording came through. Try again.')
        voice = storage.check_voice(file, request.POST.get('seconds'))
        svc.comment(asset, request.user, request.POST.get('body', ''), visibility=request.POST.get('visibility', 'internal'), voice=voice)
        return JsonResponse({'ok': True})
    except (svc.ImageError, storage.ImageFileError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)


def _ranged(request, data: bytes, mime: str):
    """Serve bytes with Range support. Safari will not play audio from a server that ignores Range."""
    import re
    size = len(data)
    m = re.match(r'bytes=(\d*)-(\d*)$', request.headers.get('Range', '').strip())
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:
            start, end = max(0, size - int(m.group(2))), size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            resp = HttpResponse(status=416)
            resp['Content-Range'] = f'bytes */{size}'
            return resp
        resp = HttpResponse(data[start:end + 1], status=206, content_type=mime)
        resp['Content-Range'] = f'bytes {start}-{end}/{size}'
    else:
        resp = HttpResponse(data, content_type=mime)
    resp['Accept-Ranges'] = 'bytes'
    resp['Cache-Control'] = 'private, no-store'
    return resp


@staff_required
@require_GET
def voice_file(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    v = ImageVoice.objects.select_related('submission', 'asset').filter(pk=pk).first()
    if v is None or not svc.voice_visible_to(request.user, v):
        raise Http404
    store = storage.get_storage()
    url = store.signed_url(v.storage_key, filename=f'voice-{v.pk}', content_type=v.mime_type)
    if url:
        resp = redirect(url)                 # the storage service streams it, with Range, and the CRM does no work
        resp['Cache-Control'] = 'private, no-store'
        return resp
    if not store.exists(v.storage_key):
        raise Http404
    return _ranged(request, store.read(v.storage_key), v.mime_type)


# ---------------------------------------------------------------- submissions

@staff_required
def submission_list(request):
    permissions.require(request.user, MODULE, 'view', request)
    subs = (svc.submissions_for(request.user).select_related('client', 'submitted_by').order_by('-submitted_at'))
    term = request.GET.get('q', '').strip()
    if term:
        subs = subs.filter(Q(number__icontains=term) | Q(client__display_name__icontains=term))
    rows = []
    for s in subs[:60]:
        rows.append({'sub': s, 'progress': svc.progress(s)})
    return render(request, 'crm/images/submissions.html', {'rows': rows, 'q': term, 'is_client': _is_client(request)})


@staff_required
def submission_detail(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    sub = svc.submissions_for(request.user).select_related('client', 'submitted_by').filter(pk=pk).first()
    if sub is None:
        return _gone(request, 'This submission')
    assets = list(svc.with_thumbs(svc.assets_for(request.user).filter(submission=sub).select_related('editor', 'coordinator'), request.user).order_by('id'))
    u = request.user
    can_coord = not _is_client(request) and permissions.has_perm(u, MODULE, 'approve')
    return render(request, 'crm/images/submission.html', {
        'sub': sub, 'assets': assets, 'progress': svc.progress(sub), 'is_client': _is_client(request),
        'voice_max': storage.max_voice_seconds(), 'can_coord': can_coord, 'can_assign': can_coord and permissions.has_perm(u, MODULE, 'assign'),
        'editors': svc.editors() if can_coord else [], 'priorities': PRIORITIES,
        'events': svc.events_for(u, submission=sub).filter(asset__isnull=True),
        'brief_voices': _brief_voices(u, sub), 'today': india.today()})


@staff_required
@require_POST
def submission_add(request, pk):
    sub = svc.submissions_for(request.user).filter(pk=pk).first()
    if sub is None:
        return _gone(request, 'This submission')
    added = 0
    try:
        for f in request.FILES.getlist('files'):
            svc.add_image(sub, request.user, f)
            added += 1
        messages.success(request, f'{added} photo{"s" if added != 1 else ""} added.') if added else messages.error(request, 'Choose a photo to add.')
    except (svc.ImageError, storage.ImageFileError) as exc:
        messages.error(request, (f'{added} added. ' if added else '') + str(exc))
    return redirect('crm:image_submission', pk=sub.pk)


@staff_required
@require_POST
def submission_bulk(request, pk):
    sub = svc.submissions_for(request.user).filter(pk=pk).first()
    if sub is None:
        return _gone(request, 'This submission')
    ids = [int(i) for i in request.POST.getlist('asset') if i.isdigit()]
    assets = list(svc.assets_for(request.user).filter(submission=sub, pk__in=ids))
    if not assets:
        messages.error(request, 'Tick the photos first.')
        return redirect('crm:image_submission', pk=sub.pk)
    editor = svc.editors().filter(pk=request.POST.get('editor') or 0).first()
    deadline = _date(request.POST.get('deadline'))
    priority = request.POST.get('priority') or None
    if editor is None:
        messages.error(request, 'Choose an editor.')
    else:
        n = svc.assign_many(assets, editor, request.user, deadline=deadline, priority=priority, reason=request.POST.get('reason', ''))
        messages.success(request, f'{n} photo{"s" if n != 1 else ""} assigned to {svc.name_of(editor)}.') if n else messages.error(request, 'Nothing was assigned. The photos may already be with an editor or finished.')
    return redirect('crm:image_submission', pk=sub.pk)


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------- one photo

@staff_required
def detail(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    asset = _asset(request, pk)
    if asset is None:
        return _gone(request)
    u = request.user
    client_view = _is_client(request)
    kinds = svc.actor_kinds(u, asset)
    versions = list(svc.versions_for(u, asset).select_related('uploaded_by'))
    original = next((v for v in reversed(versions) if v.kind == 'original' and v.superseded_at is None), None) or (versions[0] if versions else None)
    edits = [v for v in versions if v.kind == 'edit']
    latest_edit = edits[-1] if edits else None
    steps = _steps(asset, kinds, latest_edit)
    ctx = {
        'asset': asset, 'is_client': client_view, 'kinds': kinds, 'versions': versions, 'original': original, 'latest_edit': latest_edit,
        'events': list(svc.events_for(u, asset=asset)), 'steps': steps,
        'can_coord': 'C' in kinds, 'is_editor': 'E' in kinds, 'is_owner_client': 'K' in kinds,
        'can_assign': 'C' in kinds and permissions.has_perm(u, MODULE, 'assign'),
        'editors': svc.editors() if 'C' in kinds else [], 'priorities': PRIORITIES, 'version_kinds': VERSION_KINDS,
        'label': asset.client_label if client_view else asset.get_status_display(),
        'tone': asset.client_tone if client_view else asset.tone,
        'siblings': list(svc.assets_for(u).filter(submission=asset.submission).exclude(pk=asset.pk).values('pk', 'number', 'status')[:30]) if not client_view else [],
        'today': india.today(), 'progress': svc.progress(asset.submission),
        'can_comment': True, 'quick': request.GET.get('quick', ''), 'brief_voices': _brief_voices(u, asset.submission),
        'voice_max': storage.max_voice_seconds(),
    }
    return render(request, 'crm/images/detail.html', ctx)


def _steps(asset, kinds, latest_edit) -> list:
    """The one or two things this person should do next, as buttons. The rest of the controls sit under 'More'."""
    s = asset.status
    steps = []
    if asset.archived_at:
        return steps
    if 'K' in kinds and s == 'client_review':
        steps += [{'verb': 'approve', 'label': 'Approve', 'style': 'btn-primary', 'confirm': 'Approve this photo as final?'},
                  {'verb': 'revision', 'label': 'Ask for a change', 'style': 'btn-secondary', 'modal': 'revision'}]
    if 'E' in kinds or 'C' in kinds:
        if s == 'assigned' and asset.editor_id:
            steps.append({'verb': 'start', 'label': 'Start editing', 'style': 'btn-primary'})
        if s == 'revision_requested' and asset.editor_id:
            steps.append({'verb': 'start', 'label': 'Start the revision', 'style': 'btn-primary'})
        if s in ('editing', 'revision_in_progress', 'changes_internal'):
            steps.append({'verb': 'upload', 'label': 'Upload edited photo', 'style': 'btn-primary', 'modal': 'upload'})
            if s == 'changes_internal':
                steps.append({'verb': 'start', 'label': 'Back to editing', 'style': 'btn-secondary'})
    if 'C' in kinds:
        if s in ('new', 'ready') and not asset.editor_id:
            steps.append({'verb': 'assign', 'label': 'Assign to an editor', 'style': 'btn-primary', 'modal': 'assign'})
        if s == 'clarification':
            steps.append({'verb': 'ready', 'label': 'Question answered: ready', 'style': 'btn-primary'})
        if s == 'internal_review':
            steps += [{'verb': 'send_client', 'label': 'Send to client', 'style': 'btn-primary', 'confirm': 'Send this edited photo to the client now?'},
                      {'verb': 'changes', 'label': 'Ask editor for changes', 'style': 'btn-secondary', 'modal': 'changes'}]
        if s == 'approved':
            steps.append({'verb': 'deliver', 'label': 'Mark delivered', 'style': 'btn-primary'})
        if s == 'on_hold':
            steps.append({'verb': 'resume', 'label': 'Resume', 'style': 'btn-primary'})
        if s == 'client_review':
            steps.append({'verb': 'client_for', 'label': 'Record client decision', 'style': 'btn-secondary', 'modal': 'client_for'})
    return steps


def _upload_of(request, name='file'):
    f = request.FILES.get(name)
    if f is None:
        raise svc.ImageError('Choose a photo to upload.')
    return f


def _act_start(request, a):
    to = 'revision_in_progress' if a.status == 'revision_requested' else 'editing'
    svc.transition(a, to, request.user)
    return 'Editing started.'


def _act_simple(to, message, reason_field=None):
    def run(request, a):
        svc.transition(a, to, request.user, reason=request.POST.get(reason_field or 'reason', ''))
        return message
    return run


def _act_upload(request, a):
    kind = request.POST.get('kind', 'edit')
    note = request.POST.get('note', '')
    svc.upload_version(a, request.user, _upload_of(request), kind=kind, note=note,
                       send_for_review=bool(request.POST.get('send_for_review')) and kind == 'edit')
    return 'Uploaded and sent for review.' if request.POST.get('send_for_review') and kind == 'edit' else 'Uploaded.'


def _act_assign(request, a):
    editor = svc.editors().filter(pk=request.POST.get('editor') or 0).first()
    svc.assign(a, editor, request.user, deadline=_date(request.POST.get('deadline')), priority=request.POST.get('priority') or None,
               reason=request.POST.get('reason', ''))
    return f'Assigned to {svc.name_of(editor)}.'


def _act_priority(request, a):
    svc.set_priority(a, request.user, request.POST.get('priority', ''), reason=request.POST.get('reason', ''))
    return 'Priority updated.'


def _act_deadline(request, a):
    svc.set_deadline(a, request.user, _date(request.POST.get('deadline')), reason=request.POST.get('reason', ''))
    return 'Deadline updated.'


def _act_instructions(request, a):
    svc.set_instructions(a, request.user, request.POST.get('text', ''), internal=False, reason=request.POST.get('reason', ''))
    return 'Instructions updated. The editor and the client were told.'


def _act_internal(request, a):
    svc.set_instructions(a, request.user, request.POST.get('text', ''), internal=True, reason=request.POST.get('reason', ''))
    return 'Team instructions updated.'


def _act_coordinator(request, a):
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=request.POST.get('user') or 0, is_active=True).first()
    svc.set_coordinator(a, user, request.user, reason=request.POST.get('reason', ''))
    return 'Coordinator updated.'


def _act_comment(request, a):
    svc.comment(a, request.user, request.POST.get('body', ''), visibility=request.POST.get('visibility', 'internal'))
    return 'Comment added.'


def _act_replace(request, a):
    svc.replace_original(a, request.user, _upload_of(request), reason=request.POST.get('reason', ''))
    return 'The photo was replaced. The old file stays in the history.'


def _act_archive(request, a):
    svc.archive(a, request.user, request.POST.get('reason', ''))
    return 'The photo was removed from the lists. Nothing was deleted.'


def _act_restore(request, a):
    svc.restore(a, request.user, request.POST.get('reason', ''))
    return 'The photo was restored.'


def _act_client_for(request, a):
    decision = request.POST.get('decision')
    reason = request.POST.get('reason', '')
    if decision == 'approved':
        svc.transition(a, 'approved', request.user, reason=reason)
        return 'Recorded as approved by the client.'
    svc.transition(a, 'revision_requested', request.user, reason=reason)
    return 'Recorded as a revision request from the client.'


def _act_resume(request, a):
    svc.resume(a, request.user, request.POST.get('reason', ''))
    return 'Resumed.'


ACTIONS = {
    'start': _act_start,
    'upload': _act_upload,
    'assign': _act_assign,
    'priority': _act_priority,
    'deadline': _act_deadline,
    'instructions': _act_instructions,
    'internal': _act_internal,
    'coordinator': _act_coordinator,
    'comment': _act_comment,
    'replace': _act_replace,
    'archive': _act_archive,
    'restore': _act_restore,
    'client_for': _act_client_for,
    'resume': _act_resume,
    'send_review': _act_simple('internal_review', 'Sent for internal review.'),
    'send_client': _act_simple('client_review', 'Sent to the client for review.'),
    'changes': _act_simple('changes_internal', 'Sent back to the editor.'),
    'approve': _act_simple('approved', 'Approved. Thank you.'),
    'revision': _act_simple('revision_requested', 'We have your request.', 'what'),
    'clarify': _act_simple('clarification', 'The client was asked.'),
    'ready': _act_simple('ready', 'Marked ready for editing.'),
    'hold': _act_simple('on_hold', 'Put on hold.'),
    'cancel': _act_simple('cancelled', 'Cancelled.'),
    'reopen': _act_simple('new', 'Reopened.'),
    'deliver': _act_simple('delivered', 'Marked delivered. The client was told.'),
}


@staff_required
@require_POST
def action(request, pk, verb):
    permissions.require(request.user, MODULE, 'view', request)
    asset = _asset(request, pk)          # a client never gets a removed photo here; staff do, so they can restore it
    if asset is None:
        return _gone(request)
    fn = ACTIONS.get(verb)
    if fn is None:
        raise Http404
    try:
        messages.success(request, fn(request, asset))
    except (svc.ImageError, storage.ImageFileError) as exc:
        messages.error(request, str(exc))
    return redirect(reverse('crm:image_detail', args=[asset.pk]) + '#timeline')


# ---------------------------------------------------------------- files

def _serve(request, version, key, *, download=False, cache=0, mime=None):
    store = storage.get_storage()
    name = version.original_name
    mime = mime or (version.mime_type if key == version.storage_key else 'image/jpeg')
    url = store.signed_url(key, filename=name, content_type=mime, download=download)
    if url:
        resp = redirect(url)
    else:
        if not store.exists(key):
            return _gone(request, 'This file')
        resp = FileResponse(store.open(key), content_type=mime, as_attachment=download, filename=name)
    resp['Cache-Control'] = f'private, max-age={cache}' if cache else 'private, no-store'
    return resp


def _version(request, pk):
    v = ImageVersion.objects.select_related('asset', 'asset__client').filter(pk=pk).first()
    if v is None or not svc.version_visible_to(request.user, v):
        return None
    return v


@staff_required
@require_GET
def file(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    v = _version(request, pk)
    if v is None:
        return _gone(request, 'This file')
    download = request.GET.get('dl') == '1'
    if download:
        audit.log('images.downloaded', v.asset, actor=request.user, changes={'version': v.number, 'kind': v.kind})
        return _serve(request, v, v.storage_key, download=True)
    if v.preview_key:                 # a HEIC photo is shown as its JPEG copy; Download gives the untouched original
        return _serve(request, v, v.preview_key, mime='image/jpeg')
    return _serve(request, v, v.storage_key)


@staff_required
@require_GET
def thumb(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    v = _version(request, pk)
    if v is None:
        raise Http404
    return _serve(request, v, v.thumb_key or v.storage_key, cache=300)


# ---------------------------------------------------------------- the team's view

@staff_required
def team(request):
    if not _can_see_team(request.user) or _is_client(request):
        permissions.denied(request.user, MODULE, 'assign', request)
    rows = svc.workload(request.user)
    return render(request, 'crm/images/team.html', {'rows': rows, 'counts': svc.counts_for(request.user)})


# ---------------------------------------------------------------- client logins (on the client's page)

@staff_required
def client_logins(request, pk):
    permissions.require(request.user, MODULE, 'configure', request)
    client = get_scoped(request, Client, pk, 'clients', 'view')
    ctx = {'client': client}
    if request.method == 'POST':
        verb = request.POST.get('do', '')
        try:
            if verb == 'create':
                user, password = svc.create_client_login(client, request.user, username=request.POST.get('username', ''),
                                                         full_name=request.POST.get('full_name', ''), email=request.POST.get('email', ''))
                ctx['credentials'] = {'username': user.get_username(), 'password': password, 'name': svc.name_of(user)}
            else:
                access = get_object_or_404(ClientAccess, pk=request.POST.get('access') or 0, client=client)
                if verb == 'reset':
                    ctx['credentials'] = {'username': access.user.get_username(), 'password': svc.reset_client_password(access, request.user),
                                          'name': svc.name_of(access.user)}
                elif verb in ('stop', 'start'):
                    svc.set_client_login_active(access, request.user, verb == 'start')
                    messages.success(request, f'Login {"re-enabled" if verb == "start" else "stopped"}.')
                else:
                    raise Http404
        except svc.ImageError as exc:
            messages.error(request, str(exc))
    ctx['accesses'] = list(ClientAccess.objects.filter(client=client).select_related('user').order_by('user__username'))
    ctx['suggest'] = ''.join(ch for ch in client.display_name.lower() if ch.isalnum())[:18]
    return render(request, 'crm/images/client_logins.html', ctx)
