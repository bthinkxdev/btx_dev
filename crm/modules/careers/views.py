"""Careers: post and manage the job openings shown on the public website, and work the applications."""
from __future__ import annotations

from urllib.parse import urlsplit

from django.contrib import messages
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from crm.core import audit, permissions
from crm.core.views import Filter, ListPage, staff_required
from pages.models import JobApplication, JobPosting

from .forms import JobPostingForm

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
        return JobApplication.objects.select_related('job')

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
    app = get_object_or_404(JobApplication.objects.select_related('job'), pk=pk)
    ctx = {'app': app, 'statuses': JobApplication.STATUS_CHOICES,
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
