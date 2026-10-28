"""
Shared view infrastructure.

* `StaffRequiredMixin` / `staff_required`: signed-in, active user with a CRM profile.
* `PermissionMixin`: page-level permission (hidden in UI, re-checked here: Part 1.2 #5).
* `get_scoped`: object lookup inside the user's data scope; out-of-scope is a 404, never a hint.
* `ListPage`: the list standard (Part 2.7) - search, filters, quick filters, sorting,
  pagination 10-100, logged CSV/XLSX export with permission check.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date
from functools import wraps

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import EmptyPage, Paginator
from django.db import models
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from . import audit, permissions

PER_PAGE_CHOICES = (10, 25, 50, 100)


def ensure_profile(user):
    from crm.models import UserProfile
    try:
        return user.crm_profile
    except UserProfile.DoesNotExist:
        return UserProfile.objects.get_or_create(user=user)[0]


def staff_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        ensure_profile(request.user)
        return view(request, *args, **kwargs)
    return wrapped


def perm_required(module: str, action: str = 'view'):
    def deco(view):
        @wraps(view)
        @staff_required
        def wrapped(request, *args, **kwargs):
            permissions.require(request.user, module, action, request)
            return view(request, *args, **kwargs)
        return wrapped
    return deco


class StaffRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        ensure_profile(request.user)
        return super().dispatch(request, *args, **kwargs)


class PermissionMixin(StaffRequiredMixin):
    module: str = ''
    action: str = 'view'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and self.module:
            permissions.require(request.user, self.module, self.action, request)
        return super().dispatch(request, *args, **kwargs)


class CrmTemplateView(PermissionMixin, TemplateView):
    pass


def get_scoped(request, model, pk, module: str, action: str = 'view', owner_fields=None, extra_q: Q | None = None,
               queryset=None):
    """Fetch a record inside the user's scope. Not found and out of scope look identical (Part 2.4)."""
    permissions.require(request.user, module, action, request)
    qs = queryset if queryset is not None else model._default_manager.all()
    qs = permissions.scoped(qs, request.user, module, action, owner_fields, extra_q)
    try:
        return qs.get(pk=pk)
    except (model.DoesNotExist, ValueError, TypeError):
        raise Http404('Not found')


def wants_json(request) -> bool:
    return request.headers.get('x-requested-with') == 'XMLHttpRequest' or \
        'application/json' in request.headers.get('accept', '')


def json_error(message: str, status: int = 400, **extra):
    return JsonResponse({'ok': False, 'error': message, **extra}, status=status)


def back(request, fallback: str):
    """Redirect to a safe same-site referer, else fallback."""
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get('next') or request.GET.get('next') or request.META.get('HTTP_REFERER')
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(fallback)


def form_error_text(form) -> str:
    errs = []
    for field, items in form.errors.items():
        label = form.fields[field].label if field in form.fields else ''
        for e in items:
            errs.append(f'{label}: {e}' if label and field != '__all__' else str(e))
    return ' '.join(errs[:4]) or 'Please correct the highlighted fields.'


class Filter:
    """A list filter rendered as a select chip. `choices` may be a callable(request)."""

    def __init__(self, name, label, choices, lookup=None, apply=None):
        self.name, self.label, self._choices, self.lookup, self._apply = name, label, choices, lookup or name, apply

    def choices(self, request):
        c = self._choices(request) if callable(self._choices) else self._choices
        return [(str(k), v) for k, v in c]

    def apply(self, qs, value, request):
        if self._apply:
            return self._apply(qs, value, request)
        return qs.filter(**{self.lookup: value})


class ListPage(PermissionMixin, View):
    model = None
    template_name = ''
    module = ''
    action = 'view'
    owner_fields: tuple | None = None
    search_fields: tuple = ()
    filters: list = []
    quick_filters: dict = {}          # key -> (label, callable(qs, request))
    sort_fields: dict = {}            # param -> order_by expression
    default_sort = '-id'
    select_related: tuple = ()
    prefetch_related: tuple = ()
    export_columns: list = []         # [(header, callable(obj) or attr path)]
    export_name = 'export'
    title = ''
    # Optional date range (?from=YYYY-MM-DD&to=YYYY-MM-DD&on=<key>); first key is the default field.
    date_fields: dict = {}

    def base_queryset(self):
        qs = self.model._default_manager.all()
        if self.select_related:
            qs = qs.select_related(*self.select_related)
        if self.prefetch_related:
            qs = qs.prefetch_related(*self.prefetch_related)
        return permissions.scoped(qs, self.request.user, self.module, self.action, self.owner_fields,
                                  self.extra_scope_q())

    def extra_scope_q(self):
        return None

    def apply_filters(self, qs):
        request = self.request
        self.active_filters = {}
        quick = request.GET.get('quick', '')
        if quick and quick in self.quick_filters:
            qs = self.quick_filters[quick][1](qs, request)
        term = request.GET.get('q', '').strip()
        if term and self.search_fields:
            # Every word must match somewhere ("john acme" finds John at Acme); a phone field
            # also matches the digits alone, so "+91 98765-43210" finds "9876543210".
            phone_like = all(ch.isdigit() or ch in '+-() ' for ch in term)
            words = [term] if phone_like else term.split()[:6]
            for word in words:
                digits = ''.join(ch for ch in word if ch.isdigit())
                q = Q()
                for f in self.search_fields:
                    q |= Q(**{f: word})
                    if 'phone' in f and len(digits) >= 3 and digits != word:
                        q |= Q(**{f: digits}) | Q(**{f: digits[-10:]})
                qs = qs.filter(q)
        qs = self.apply_date_range(qs)
        for flt in self.filters:
            value = request.GET.get(flt.name, '').strip()
            if value:
                try:
                    qs = flt.apply(qs, value, request)
                    self.active_filters[flt.name] = value
                except (ValueError, TypeError):
                    pass
        return qs

    def apply_date_range(self, qs):
        self.date_range = None
        if not self.date_fields:
            return qs
        request = self.request
        key = request.GET.get('on', '')
        if key not in self.date_fields:
            key = next(iter(self.date_fields))
        field_name = self.date_fields[key]
        try:
            start = date.fromisoformat(request.GET['from']) if request.GET.get('from') else None
            end = date.fromisoformat(request.GET['to']) if request.GET.get('to') else None
        except ValueError:
            return qs
        if not start and not end:
            return qs
        field = self.model._meta.get_field(field_name) if '__' not in field_name else None
        suffix = '__date' if isinstance(field, models.DateTimeField) else ''
        if start:
            qs = qs.filter(**{f'{field_name}{suffix}__gte': start})
        if end:
            qs = qs.filter(**{f'{field_name}{suffix}__lte': end})
        self.date_range = {'start': start, 'end': end, 'on': key,
                           'label': str(self.model._meta.get_field(field_name).verbose_name) if field else key}
        return qs

    def apply_sort(self, qs):
        sort = self.request.GET.get('sort', '')
        key = sort.lstrip('-')
        if key in self.sort_fields:
            expr = self.sort_fields[key]
            return qs.order_by(('-' if sort.startswith('-') else '') + expr, '-pk')
        return qs.order_by(self.default_sort, '-pk') if self.default_sort else qs

    def get_filtered(self):
        return self.apply_sort(self.apply_filters(self.base_queryset()))

    def extra_context(self, qs) -> dict:
        return {}

    def get(self, request, *args, **kwargs):
        qs = self.get_filtered()
        fmt = request.GET.get('export')
        if fmt in ('csv', 'xlsx'):
            return self.export(qs, fmt)
        try:
            per_page = int(request.GET.get('per_page', 25))
        except ValueError:
            per_page = 25
        per_page = per_page if per_page in PER_PAGE_CHOICES else 25
        paginator = Paginator(qs, per_page)
        try:
            page = paginator.page(request.GET.get('page', 1))
        except (EmptyPage, ValueError, TypeError):
            page = paginator.page(paginator.num_pages if request.GET.get('page') else 1)
        from django.shortcuts import render
        ctx = {
            'page_obj': page, 'object_list': page.object_list, 'paginator': paginator, 'total': paginator.count,
            'filters': [(f, f.choices(request)) for f in self.filters], 'active_filters': self.active_filters,
            'quick_filters': [(k, v[0]) for k, v in self.quick_filters.items()], 'per_page': per_page,
            'per_page_choices': PER_PAGE_CHOICES, 'q': request.GET.get('q', ''),
            'any_filter': bool(self.active_filters or request.GET.get('q') or request.GET.get('quick') or self.date_range),
            'date_range': self.date_range,
            'can_export': permissions.has_perm(request.user, self.module, 'export') and bool(self.export_columns),
            'title': self.title, 'saved_filters': self.saved_filters(),
        }
        ctx.update(self.extra_context(qs))
        return render(request, self.template_name, ctx)

    def saved_filters(self):
        from crm.models import SavedFilter
        return SavedFilter.objects.filter(user=self.request.user, module=self.module)

    # ------------------------------------------------------------- export (Part 2.7, 3.5)
    def check_export(self, count: int):
        """Override to require approval (e.g. lead export above 100 rows for executives)."""
        return None

    def export(self, qs, fmt):
        request = self.request
        permissions.require(request.user, self.module, 'export', request)
        count = qs.count()
        blocked = self.check_export(count)
        if blocked is not None:
            return blocked
        headers = [h for h, _ in self.export_columns]
        from crm.models import ExportLog
        ExportLog.objects.create(user=request.user, module=self.module, filters=dict(request.GET.lists()),
                                 columns=headers, row_count=count, file_format=fmt)
        audit.log('export.created', actor=request.user, object_type=self.module, object_id='',
                  changes={'rows': count, 'format': fmt, 'filters': dict(request.GET.lists())})

        def rows():
            for obj in qs.iterator(chunk_size=500):
                yield [_cell(obj, acc) for _, acc in self.export_columns]

        stamp = timezone.localtime().strftime('%Y%m%d-%H%M')
        if fmt == 'xlsx':
            from openpyxl import Workbook
            wb = Workbook(write_only=True)
            ws = wb.create_sheet(self.export_name[:30])
            ws.append(headers)
            for r in rows():
                ws.append([_xlsx_safe(v) for v in r])
            buf = io.BytesIO()
            wb.save(buf)
            resp = HttpResponse(buf.getvalue(),
                                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            resp['Content-Disposition'] = f'attachment; filename="{self.export_name}-{stamp}.xlsx"'
            return resp

        class Echo:
            def write(self, value):
                return value

        writer = csv.writer(Echo())

        def stream():
            yield '﻿'
            yield writer.writerow(headers)
            for r in rows():
                yield writer.writerow([_csv_safe(v) for v in r])

        resp = StreamingHttpResponse(stream(), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{self.export_name}-{stamp}.csv"'
        return resp


def _cell(obj, accessor):
    if callable(accessor):
        value = accessor(obj)
    else:
        value = obj
        for part in accessor.split('.'):
            value = getattr(value, part, '') if value is not None else ''
            if callable(value) and not hasattr(value, 'all'):
                value = value()
    if value is None:
        return ''
    if hasattr(value, 'strftime'):
        if hasattr(value, 'hour'):
            value = timezone.localtime(value) if timezone.is_aware(value) else value
            return value.strftime('%d-%b-%Y %H:%M')
        return value.strftime('%d-%b-%Y')
    return str(value)


def _csv_safe(value):
    """Neutralise spreadsheet formula injection."""
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r') and not _looks_numeric(value):
        return "'" + value
    return value


def _xlsx_safe(value):
    return _csv_safe(value)


def _looks_numeric(value: str) -> bool:
    try:
        float(value.replace(',', ''))
        return True
    except ValueError:
        return False


def save_filter_view(request):
    """POST: save current list filter for the user (Part 2.7 'Saved filters per user')."""
    from django.db import IntegrityError
    from crm.models import SavedFilter
    module = request.POST.get('module', '')[:40]
    name = request.POST.get('name', '').strip()[:60]
    query = request.POST.get('querystring', '')[:1000].lstrip('?')
    if not name:
        messages.error(request, 'Give the filter a name.')
    else:
        try:
            SavedFilter.objects.create(user=request.user, module=module, name=name, querystring=query)
            messages.success(request, f'Saved filter "{name}".')
        except IntegrityError:
            messages.error(request, 'A saved filter with that name already exists.')
    return back(request, '/crm/')


def delete_filter_view(request, pk):
    from crm.models import SavedFilter
    SavedFilter.objects.filter(user=request.user, pk=pk).delete()
    messages.success(request, 'Saved filter removed.')
    return back(request, '/crm/')


def json_body(request) -> dict:
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return {}


def is_owner_user(user) -> bool:
    return user.is_superuser or permissions.has_role(user, 'owner')
