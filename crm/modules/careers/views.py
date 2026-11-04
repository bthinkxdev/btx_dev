"""Careers: post and manage the job openings shown on the public website, and work the applications."""
from __future__ import annotations

from urllib.parse import urlsplit

from django.contrib import messages
from django.db.models import Count, Min, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from crm.core import audit, permissions
from crm.core.views import Filter, ListPage, staff_required
from pages.models import Interview, JobApplication, JobPosting

from . import services
from .forms import ApplicationActionForm, InterviewForm, InterviewOutcomeForm, JobPostingForm

MODULE = 'employees'


class CareerList(ListPage):
    model = JobPosting
    module = MODULE
    template_name = 'crm/careers/list.html'
    title = 'Job openings'
    search_fields = ('title__icontains', 'department__icontains', 'location__icontains')
    default_sort = 'sort_order'
    filters = [
        Filter('state', 'Status', [('live', 'Live on website'), ('draft', 'Draft')],
               apply=lambda qs, v, r: qs.filter(is_published=v == 'live')),
        Filter('type', 'Type', JobPosting.EMPLOYMENT_CHOICES, lookup='employment_type'),
    ]

    def base_queryset(self):
        return JobPosting.objects.annotate(
            app_count=Count('applications'),
            new_count=Count('applications', filter=Q(applications__status=JobApplication.STATUS_NEW)))

    def extra_context(self, qs):
        jobs = JobPosting.objects.aggregate(live=Count('pk', filter=Q(is_published=True)),
                                            draft=Count('pk', filter=Q(is_published=False)))
        applicants = JobApplication.objects.aggregate(total=Count('pk'),
                                                      new=Count('pk', filter=Q(status=JobApplication.STATUS_NEW)))
        return {'module_code': MODULE, 'can_edit': permissions.has_perm(self.request.user, MODULE, 'edit'),
                'search_placeholder': 'Search roles…', 'stats': {**jobs, **applicants}}


@staff_required
def career_form(request, pk=None):
    permissions.require(request.user, MODULE, 'edit', request)
    job = get_object_or_404(JobPosting, pk=pk) if pk else None
    form = JobPostingForm(request.POST or None, instance=job)
    if request.method == 'POST' and form.is_valid():
        created = job is None
        job = form.save()
        audit.log('career.created' if created else 'career.updated', job, actor=request.user,
                  changes={'title': job.title, 'published': job.is_published})
        messages.success(request, f'"{job.title}" saved' + (' and live on the website.' if job.is_published else ' as a draft.'))
        return redirect('crm:career_list')
    return render(request, 'crm/careers/form.html', {'form': form, 'obj': job})


@staff_required
@require_POST
def career_toggle(request, pk):
    permissions.require(request.user, MODULE, 'edit', request)
    job = get_object_or_404(JobPosting, pk=pk)
    job.is_published = not job.is_published
    job.save(update_fields=['is_published', 'updated_at'])
    audit.log('career.published' if job.is_published else 'career.unpublished', job, actor=request.user, changes={})
    messages.success(request, f'"{job.title}" is now {"live on the website" if job.is_published else "hidden (draft)"}.')
    return redirect('crm:career_list')


@staff_required
@require_POST
def career_delete(request, pk):
    permissions.require(request.user, MODULE, 'archive', request)
    job = get_object_or_404(JobPosting, pk=pk)
    title = job.title
    audit.log('career.deleted', job, actor=request.user, changes={'title': title})
    job.delete()
    messages.success(request, f'"{title}" deleted. Its applications were kept as general applications.')
    return redirect('crm:career_list')


class ApplicationList(ListPage):
    model = JobApplication
    module = MODULE
    template_name = 'crm/careers/applications.html'
    title = 'Job applications'
    search_fields = ('full_name__icontains', 'email__icontains', 'phone__icontains', 'job__title__icontains')
    default_sort = '-created_at'
    quick_filters = {'new': ('New', lambda qs, r: qs.filter(status='new'))}
    filters = [
        Filter('status', 'Status', JobApplication.STATUS_CHOICES),
        Filter('job', 'Role', lambda r: [(j.pk, j.title) for j in JobPosting.objects.all()]),
    ]
    date_fields = {'created': 'created_at'}

    def base_queryset(self):
        return JobApplication.objects.select_related('job').annotate(
            next_interview=Min('interviews__scheduled_at', filter=Q(interviews__status='scheduled',
                                                                    interviews__scheduled_at__gte=timezone.now())))

    def extra_context(self, qs):
        counts = dict(JobApplication.objects.values_list('status').annotate(n=Count('pk')))
        pipeline = [(v, label, counts.get(v, 0)) for v, label in JobApplication.STATUS_CHOICES
                    if v not in ('rejected',)]
        return {'module_code': MODULE, 'can_edit': permissions.has_perm(self.request.user, MODULE, 'edit'),
                'statuses': JobApplication.STATUS_CHOICES, 'search_placeholder': 'Search candidates…',
                'pipeline': pipeline, 'total_applications': sum(counts.values())}


@staff_required
def application_detail(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    from crm.models import DeliveryLog
    app = get_object_or_404(JobApplication.objects.select_related('job'), pk=pk)
    ctx = {'app': app, 'statuses': JobApplication.STATUS_CHOICES,
           'interviews': app.interviews.prefetch_related('interviewers').order_by('-scheduled_at'),
           'emails': DeliveryLog.objects.filter(object_type='jobapplication', object_id=str(app.pk))[:20],
           'can_edit': permissions.has_perm(request.user, MODULE, 'edit'),
           'resume_ext': app.resume.name.rsplit('.', 1)[-1].lower() if app.resume else '',
           'wa_number': ''.join(c for c in app.phone if c.isdigit())}
    return render(request, 'crm/careers/application_detail.html', ctx)


@staff_required
@require_POST
def application_update(request, pk):
    permissions.require(request.user, MODULE, 'edit', request)
    app = get_object_or_404(JobApplication, pk=pk)
    old_status = app.status
    status = request.POST.get('status', '')
    if status in dict(JobApplication.STATUS_CHOICES):
        app.status = status
    if 'internal_notes' in request.POST:
        app.internal_notes = request.POST['internal_notes'].strip()[:2000]
    app.save(update_fields=['status', 'internal_notes'])
    audit.log('career.application_updated', app, actor=request.user,
              changes={'status': app.status, 'from': old_status})
    if 'internal_notes' in request.POST and 'status' not in request.POST:
        messages.success(request, f'Notes saved for {app.full_name}.')
    elif app.status in ('shortlisted', 'rejected') and app.status != old_status:
        messages.success(request, f'{app.full_name}: {app.get_status_display()}. No email was sent. '
                                  'Open the application to email the candidate.')
    else:
        messages.success(request, f'{app.full_name}: {app.get_status_display()}.')
    back = request.META.get('HTTP_REFERER', '')
    if back and url_has_allowed_host_and_scheme(back, {request.get_host()}, request.is_secure()):
        parts = urlsplit(back)
        return redirect(parts.path + (f'?{parts.query}' if parts.query else ''))
    return redirect('crm:career_applications')


@staff_required
def application_resume(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    app = get_object_or_404(JobApplication, pk=pk)
    if not app.resume:
        raise Http404('No resume')
    try:
        fh = app.resume.open('rb')
    except (FileNotFoundError, ValueError):
        raise Http404('Resume file is missing')
    ext = app.resume.name.rsplit('.', 1)[-1].lower()
    name = ''.join(c if c.isalnum() else '-' for c in app.full_name).strip('-') or 'candidate'
    inline = request.GET.get('inline') == '1' and ext == 'pdf'
    return FileResponse(fh, as_attachment=not inline, filename=f'{name}-resume.{ext}')


# ---------------------------------------------------------------- candidate actions and interviews

@staff_required
@require_POST
def application_action(request, pk):
    """Review / shortlist / reject / hire from the application page, with an optional email to the candidate."""
    permissions.require(request.user, MODULE, 'edit', request)
    app = get_object_or_404(JobApplication.objects.select_related('job'), pk=pk)
    form = ApplicationActionForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'That action is not available.')
        return redirect('crm:career_application_detail', pk=app.pk)
    d = form.cleaned_data
    result = services.apply_action(app, d['action'], actor=request.user, send_email=d['send_email'], note=d['note'])
    label = app.get_status_display()
    wanted_email = d['send_email'] and d['action'] in ('shortlist', 'reject')
    if wanted_email:
        text = f'{app.full_name}: {label}. ' + ('The candidate was emailed.' if result['emailed']
                                                else 'The email could not be queued, please check the delivery log.')
    else:
        text = f'{app.full_name}: {label}.'
    (messages.success if result['emailed'] or not wanted_email else messages.warning)(request, text)
    return redirect('crm:career_application_detail', pk=app.pk)


@staff_required
def interview_form(request, app_pk=None, pk=None):
    """Schedule a new interview for an application, or change an existing one."""
    permissions.require(request.user, MODULE, 'edit', request)
    interview = (get_object_or_404(Interview.objects.select_related('application', 'application__job'), pk=pk)
                 if pk else None)
    app = interview.application if interview else get_object_or_404(
        JobApplication.objects.select_related('job'), pk=app_pk)
    if interview and interview.status != Interview.STATUS_SCHEDULED:
        messages.error(request, 'Only a scheduled interview can be changed.')
        return redirect('crm:career_application_detail', pk=app.pk)
    if not interview and app.status in ('rejected', 'hired'):
        messages.error(request, f'{app.full_name} is already {app.get_status_display().lower()}. '
                                'Change the status first to schedule an interview.')
        return redirect('crm:career_application_detail', pk=app.pk)
    form = InterviewForm(request.POST or None, instance=interview)
    if request.method == 'POST' and form.is_valid():
        iv = services.save_interview(form, app, actor=request.user)
        verb = 'scheduled' if interview is None else 'updated'
        extra = (' The candidate was emailed.' if getattr(iv, '_emailed', False)
                 else ' No email was sent to the candidate.')
        messages.success(request, f'Interview {verb} for {app.full_name}.' + extra)
        return redirect('crm:career_application_detail', pk=app.pk)
    return render(request, 'crm/careers/interview_form.html', {'form': form, 'app': app, 'obj': interview})


@staff_required
@require_POST
def interview_outcome(request, pk):
    """Mark an interview completed / no-show, or cancel it (optionally telling the candidate)."""
    permissions.require(request.user, MODULE, 'edit', request)
    iv = get_object_or_404(Interview.objects.select_related('application', 'application__job'), pk=pk)
    form = InterviewOutcomeForm(request.POST)
    if not form.is_valid() or iv.status != Interview.STATUS_SCHEDULED:
        messages.error(request, 'That interview can no longer be changed.')
        return redirect('crm:career_application_detail', pk=iv.application_id)
    d = form.cleaned_data
    emailed = services.close_interview(iv, d['action'], actor=request.user, note=d['note'], send_email=d['send_email'])
    done = {'complete': 'marked as completed', 'no_show': 'marked as a no-show', 'cancel': 'cancelled'}[d['action']]
    messages.success(request, f'Interview {done}.' + (' The candidate was emailed.' if emailed else ''))
    return redirect('crm:career_application_detail', pk=iv.application_id)


class InterviewList(ListPage):
    model = Interview
    module = MODULE
    template_name = 'crm/careers/interviews.html'
    title = 'Interviews'
    search_fields = ('application__full_name__icontains', 'application__email__icontains',
                     'application__job__title__icontains', 'round_name__icontains')
    default_sort = 'scheduled_at'
    quick_filters = {
        'upcoming': ('Upcoming', lambda qs, r: qs.filter(status='scheduled', scheduled_at__gte=timezone.now())),
        'today': ('Today', lambda qs, r: qs.filter(status='scheduled', scheduled_at__date=timezone.localdate())),
    }
    filters = [Filter('status', 'Status', Interview.STATUS_CHOICES),
               Filter('mode', 'Format', Interview.MODE_CHOICES)]

    def base_queryset(self):
        return Interview.objects.select_related('application', 'application__job').prefetch_related('interviewers')

    def extra_context(self, qs):
        now = timezone.now()
        return {'module_code': MODULE, 'can_edit': permissions.has_perm(self.request.user, MODULE, 'edit'),
                'search_placeholder': 'Search candidates or roles...', 'now': now,
                'upcoming_count': Interview.objects.filter(status='scheduled', scheduled_at__gte=now).count()}
