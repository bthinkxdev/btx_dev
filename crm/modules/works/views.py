"""Our Works: add and manage the portfolio shown on the website's Our Work page and homepage."""
from __future__ import annotations

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from crm.core import audit, permissions
from crm.core.views import Filter, ListPage, staff_required
from pages.models import Project

from .forms import WorkForm

MODULE = 'settings'
ACTION = 'configure'


class WorkList(ListPage):
    model = Project
    module = MODULE
    action = ACTION
    template_name = 'crm/works/list.html'
    title = 'Our works'
    search_fields = ('title__icontains', 'short_description__icontains', 'stack__name__icontains')
    default_sort = 'sort_order'
    quick_filters = {'featured': ('Featured', lambda qs, r: qs.filter(is_featured=True))}
    filters = [
        Filter('state', 'Status', [('live', 'Live on website'), ('hidden', 'Hidden')],
               apply=lambda qs, v, r: qs.filter(is_published=v == 'live')),
        Filter('category', 'Category', Project.CATEGORY_CHOICES),
    ]

    def base_queryset(self):
        return Project.objects.prefetch_related('stack').distinct()

    def extra_context(self, qs):
        everything = Project.objects.aggregate(total=Count('pk'))['total']
        live = Project.objects.filter(is_published=True).exclude(image='').count()
        return {'module_code': MODULE, 'search_placeholder': 'Search works or technologies…',
                'stats': {'total': everything, 'live': live, 'hidden': everything - live,
                          'featured': Project.objects.filter(is_featured=True).count()}}


@staff_required
def work_form(request, pk=None):
    permissions.require(request.user, MODULE, ACTION, request)
    work = get_object_or_404(Project, pk=pk) if pk else None
    form = WorkForm(request.POST or None, request.FILES or None, instance=work)
    if request.method == 'POST' and form.is_valid():
        created = work is None
        work = form.save()
        audit.log('work.created' if created else 'work.updated', work, actor=request.user,
                  changes={'title': work.title, 'published': work.is_published})
        messages.success(request, f'"{work.title}" saved' + (' and live on the website.' if work.is_published
                                                          else ' but hidden from the website.'))
        return redirect('crm:work_list')
    return render(request, 'crm/works/form.html', {'form': form, 'obj': work})


@staff_required
@require_POST
def work_toggle(request, pk):
    permissions.require(request.user, MODULE, ACTION, request)
    work = get_object_or_404(Project, pk=pk)
    work.is_published = not work.is_published
    work.save(update_fields=['is_published'])
    audit.log('work.published' if work.is_published else 'work.unpublished', work, actor=request.user, changes={})
    messages.success(request, f'"{work.title}" is now {"live on the website" if work.is_published else "hidden from the website"}.')
    return redirect('crm:work_list')


@staff_required
@require_POST
def work_delete(request, pk):
    permissions.require(request.user, MODULE, ACTION, request)
    work = get_object_or_404(Project, pk=pk)
    title = work.title
    audit.log('work.deleted', work, actor=request.user, changes={'title': title})
    work.delete()
    messages.success(request, f'"{title}" deleted.')
    return redirect('crm:work_list')
