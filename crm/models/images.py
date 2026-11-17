"""Client photos and the editing work done on them.

One submission holds many images; every image has its own status, people, versions and timeline. Everything is linked to the
existing Client, so there is one customer history. Versions and timeline rows are append-only: an original is never
overwritten, a removed image is archived, and an event can never be edited or deleted."""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .base import USER, TimeStamped

# ---- the 14 statuses. Order is the normal journey of a photo.
STATUSES = [
    ('new', 'New submission'), ('clarification', 'Clarification required'), ('ready', 'Ready for editing'),
    ('assigned', 'Assigned'), ('editing', 'Editing in progress'), ('internal_review', 'Internal review'),
    ('changes_internal', 'Changes required (internal)'), ('client_review', 'Client review'),
    ('revision_requested', 'Revision requested (client)'), ('revision_in_progress', 'Revision in progress'),
    ('approved', 'Approved'), ('delivered', 'Delivered'), ('on_hold', 'On hold'), ('cancelled', 'Cancelled'),
]
STATUS_LABELS = dict(STATUSES)

# What a client sees. Clients never need to know our internal steps.
CLIENT_LABELS = {
    'new': 'Received', 'clarification': 'We have a question', 'ready': 'Being prepared', 'assigned': 'Being prepared',
    'editing': 'Being edited', 'internal_review': 'Being checked', 'changes_internal': 'Being checked',
    'client_review': 'Ready for your review', 'revision_requested': 'Revision requested',
    'revision_in_progress': 'Being revised', 'approved': 'Approved', 'delivered': 'Delivered', 'on_hold': 'On hold',
    'cancelled': 'Cancelled',
}

TONES = {
    'new': 'blue', 'clarification': 'amber', 'ready': 'blue', 'assigned': 'blue', 'editing': 'purple',
    'internal_review': 'amber', 'changes_internal': 'red', 'client_review': 'brand', 'revision_requested': 'red',
    'revision_in_progress': 'purple', 'approved': 'green', 'delivered': 'green', 'on_hold': 'grey', 'cancelled': 'grey',
}
CLIENT_TONES = {**TONES, 'ready': 'purple', 'assigned': 'purple', 'internal_review': 'purple', 'changes_internal': 'purple'}

# Statuses where work is still moving. Used for overdue, workload and "needs attention".
ACTIVE = ['new', 'clarification', 'ready', 'assigned', 'editing', 'internal_review', 'changes_internal', 'client_review',
          'revision_requested', 'revision_in_progress']
WORKING = ['assigned', 'editing', 'changes_internal', 'revision_requested', 'revision_in_progress']   # with an editor
DONE = ['approved', 'delivered', 'cancelled']

PRIORITIES = [('low', 'Low'), ('normal', 'Normal'), ('high', 'High'), ('urgent', 'Urgent')]
PRIORITY_RANK = {'low': 0, 'normal': 1, 'high': 2, 'urgent': 3}

VERSION_KINDS = [('original', 'Original photo'), ('reference', 'Reference'), ('edit', 'Edited'), ('final', 'Final')]


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Timeline rows cannot be changed.')

    def delete(self):
        raise ValidationError('Timeline rows cannot be deleted.')


class ClientAccess(TimeStamped):
    """Links a login to an existing client. A person with this link is a client user: they sign in at the same place as
    staff, and see only their own client's images, because the permission scope follows this link."""
    user = models.OneToOneField(USER, on_delete=models.PROTECT, related_name='image_access')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='image_access')

    class Meta:
        ordering = ['client__display_name', 'user__username']

    def __str__(self):
        return f'{self.user} for {self.client}'


class ImageSubmission(TimeStamped):
    """One batch of photos from a client. Created as a draft while files are uploading; the number is only given when it is
    submitted, so abandoned drafts never leave gaps."""
    number = models.CharField(max_length=20, unique=True, null=True, blank=True, editable=False)
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='image_submissions')
    submitted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='image_submissions')
    on_behalf = models.BooleanField(default=False, help_text='Staff uploaded these for the client.')
    batch_token = models.CharField(max_length=40, unique=True, editable=False)
    note = models.TextField(blank=True, help_text='What the client asked for, in their own words.')
    submitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    auto_submitted = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return self.number or f'Draft {self.pk}'

    @property
    def is_draft(self):
        return self.submitted_at is None


class ImageAsset(TimeStamped):
    """One photo, tracked on its own from upload to delivery."""
    number = models.CharField(max_length=20, unique=True, null=True, blank=True, editable=False)
    submission = models.ForeignKey(ImageSubmission, on_delete=models.PROTECT, related_name='assets')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='image_assets')
    title = models.CharField(max_length=255, help_text='The file name the client sent.')
    status = models.CharField(max_length=24, choices=STATUSES, default='new', db_index=True)
    resume_status = models.CharField(max_length=24, blank=True, help_text='Where it goes back to when taken off hold.')
    priority = models.CharField(max_length=8, choices=PRIORITIES, default='normal', db_index=True)
    deadline = models.DateField(null=True, blank=True, db_index=True)
    coordinator = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='coordinated_images')
    editor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='edited_images')
    instructions = models.TextField(blank=True, help_text='Visible to the client.')
    internal_instructions = models.TextField(blank=True, help_text='For the team only. Never shown to the client.')
    status_changed_at = models.DateTimeField(default=timezone.now)
    last_activity_at = models.DateTimeField(default=timezone.now, db_index=True)
    revision_count = models.PositiveSmallIntegerField(default=0)
    client_wait_since = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    archived_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    archive_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['editor', 'status']), models.Index(fields=['client', 'status'])]

    def __str__(self):
        return self.number or f'Image {self.pk}'

    @property
    def tone(self):
        return TONES.get(self.status, 'grey')

    @property
    def client_tone(self):
        return CLIENT_TONES.get(self.status, 'grey')

    @property
    def client_label(self):
        return CLIENT_LABELS.get(self.status, self.get_status_display())

    @property
    def is_overdue(self):
        from crm.core import india
        return bool(self.deadline and self.status in ACTIVE and self.deadline < india.today() and not self.archived_at)

    @property
    def is_active(self):
        return self.status in ACTIVE and not self.archived_at

    def original(self):
        return self.versions.filter(kind='original', superseded_at__isnull=True).order_by('-number').first()

    def latest_edit(self):
        return self.versions.filter(kind='edit').order_by('-number').first()


class ImageVersion(models.Model):
    """One stored file of an image. Rows are only ever added; a replaced original is marked superseded, never removed."""
    asset = models.ForeignKey(ImageAsset, on_delete=models.PROTECT, related_name='versions')
    kind = models.CharField(max_length=10, choices=VERSION_KINDS, db_index=True)
    number = models.PositiveIntegerField(help_text='1, 2, 3... within the image')
    label = models.CharField(max_length=60, blank=True, help_text='Original, First edit, Revision 1, Final')
    storage_key = models.CharField(max_length=400)
    thumb_key = models.CharField(max_length=400, blank=True)
    preview_key = models.CharField(max_length=400, blank=True, help_text='A JPEG copy for screens, for formats a browser cannot show.')
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=60)
    size = models.PositiveIntegerField()
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    note = models.CharField(max_length=500, blank=True)
    client_visible = models.BooleanField(default=False, help_text='Edits become visible to the client when sent for review.')
    shared_at = models.DateTimeField(null=True, blank=True)
    is_final = models.BooleanField(default=False)
    superseded_at = models.DateTimeField(null=True, blank=True)
    uploaded_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['number']
        constraints = [models.UniqueConstraint(fields=['asset', 'number'], name='crm_image_version_once')]

    def __str__(self):
        return f'{self.asset} v{self.number}'


class ImageVoice(models.Model):
    """A short voice message, kept exactly as the phone recorded it. The server never converts or analyses it, so it costs
    almost nothing: a minute is roughly 100 to 200 KB."""
    submission = models.ForeignKey(ImageSubmission, on_delete=models.PROTECT, related_name='voices')
    asset = models.ForeignKey(ImageAsset, null=True, blank=True, on_delete=models.PROTECT, related_name='voices',
                              help_text='Empty means it is the client brief for the whole submission.')
    author = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    storage_key = models.CharField(max_length=400)
    mime_type = models.CharField(max_length=60)
    size = models.PositiveIntegerField()
    seconds = models.PositiveSmallIntegerField(default=0)
    visibility = models.CharField(max_length=10, default='client', choices=[('client', 'Client and team'), ('internal', 'Team only')])
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'Voice {self.pk} ({self.seconds}s)'


class ImageEvent(models.Model):
    """The timeline. Every action on a photo or a submission writes one row here: who, what, before and after, and when.
    Rows can be added and never changed or removed."""
    VISIBILITY = [('client', 'Client and team'), ('internal', 'Team only')]
    submission = models.ForeignKey(ImageSubmission, on_delete=models.PROTECT, related_name='events')
    asset = models.ForeignKey(ImageAsset, null=True, blank=True, on_delete=models.PROTECT, related_name='events')
    version = models.ForeignKey(ImageVersion, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    voice = models.ForeignKey(ImageVoice, null=True, blank=True, on_delete=models.PROTECT, related_name='events')
    kind = models.CharField(max_length=30, db_index=True)
    actor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    actor_name = models.CharField(max_length=150, blank=True)
    actor_role = models.CharField(max_length=60, blank=True)
    summary = models.TextField(help_text='What happened, as the team reads it.')
    client_summary = models.TextField(blank=True, help_text='How the client reads it. Blank means the same as summary.')
    field = models.CharField(max_length=40, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    reason = models.TextField(blank=True)
    visibility = models.CharField(max_length=10, choices=VISIBILITY, default='internal', db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [models.Index(fields=['asset', 'created_at']), models.Index(fields=['visibility'])]

    def __str__(self):
        return f'{self.kind}: {self.summary[:60]}'

    def save(self, *args, **kwargs):
        if self.pk and ImageEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError('Timeline rows cannot be changed.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Timeline rows cannot be deleted.')

    @property
    def text_for_client(self):
        return self.client_summary or self.summary
