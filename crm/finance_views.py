"""Finance module views — income tracking & dashboard."""

from __future__ import annotations

import json
from datetime import datetime
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .finance_forms import (
    ExpenseFilterForm,
    ExpenseForm,
    FinancePeriodFilterForm,
    FinanceReportFilterForm,
    IncomeFilterForm,
    IncomeForm,
)
from .models import Client, Expense, Income
from .rbac import can_access_finance, can_view_financial_data
from .services import finance as finance_service
from .services import finance_reports as reports_service
from .services.project import get_projects_for_user

INCOME_PER_PAGE = 25
EXPENSE_PER_PAGE = 25


def finance_required(view_fn):
    @wraps(view_fn)
    def _wrapped(request, *args, **kwargs):
        if not can_access_finance(request.user):
            return HttpResponseForbidden(
                'Finance is restricted to Admin, Support, and Finance roles.'
            )
        return view_fn(request, *args, **kwargs)

    return _wrapped


def _client_ip(request) -> str | None:
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR')


def _project_client_map_json(form: IncomeForm) -> str:
    return json.dumps({
        str(p.pk): p.client_id
        for p in form.fields['project'].queryset
    })


def _hx_redirect(request, url_name: str, *, pk: int | None = None, msg: str | None = None):
    """Redirect with HTMX support when triggered from finance pages."""
    if msg:
        messages.success(request, msg)
    url = reverse(url_name, kwargs={'pk': pk}) if pk is not None else reverse(url_name)
    if request.headers.get('HX-Request'):
        response = HttpResponse(status=200)
        response['HX-Redirect'] = url
        return response
    return redirect(url)


@login_required
@finance_required
def finance_dashboard(request):
    """Real-time management dashboard — live Income + Expense aggregates."""
    finance_service.ensure_default_categories()
    finance_service.ensure_default_expense_categories()
    data = finance_service.get_management_dashboard()

    # HTMX partial refresh of live body (auto-poll)
    if request.headers.get('HX-Request') and request.GET.get('partial') == '1':
        return render(
            request,
            'crm/finance/partials/management_body.html',
            {**data, 'page_title': 'Finance'},
        )

    return render(
        request,
        'crm/finance/dashboard.html',
        {
            **data,
            'page_title': 'Finance',
        },
    )


@login_required
@finance_required
def income_list(request):
    finance_service.ensure_default_categories()
    form = IncomeFilterForm(request.GET)
    qs = Income.objects.select_related(
        'client', 'project', 'project__package', 'category', 'created_by'
    )

    period = 'month'
    if form.is_valid():
        data = form.cleaned_data
        period = data.get('period') or 'month'
        q = (data.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(reference__icontains=q)
                | Q(notes__icontains=q)
                | Q(bank_account__icontains=q)
                | Q(client__business_name__icontains=q)
                | Q(category__name__icontains=q)
            )
        if data.get('category'):
            qs = qs.filter(category=data['category'])
        if data.get('payment_type'):
            qs = qs.filter(payment_type=data['payment_type'])
        if data.get('payment_status'):
            qs = qs.filter(payment_status=data['payment_status'])
        if period != 'all':
            start, end, _ = finance_service.resolve_period(
                period,
                date_from=data.get('date_from'),
                date_to=data.get('date_to'),
            )
            qs = qs.filter(payment_date__gte=start, payment_date__lte=end)
    else:
        start, end, _ = finance_service.resolve_period('month')
        qs = qs.filter(payment_date__gte=start, payment_date__lte=end)

    qs = qs.order_by('-payment_date', '-created_at')
    paginator = Paginator(qs, INCOME_PER_PAGE)
    page_raw = request.GET.get('page') or 1
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(
        request,
        'crm/finance/income_list.html',
        {
            'page_obj': page_obj,
            'filter_form': form,
            'period': period,
            'page_title': 'Income',
        },
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def income_create(request):
    finance_service.ensure_default_categories()
    if request.method == 'POST':
        form = IncomeForm(request.POST)
        if form.is_valid():
            income = finance_service.create_income(
                actor=request.user,
                ip_address=_client_ip(request),
                **form.cleaned_data,
            )
            return _hx_redirect(
                request,
                'crm:income_list',
                msg=f'Income of Rs. {income.amount} recorded.',
            )
    else:
        initial = {'payment_date': timezone.localdate()}
        project_id = request.GET.get('project')
        if project_id:
            try:
                initial['project'] = int(project_id)
                from .models import Project as ProjectModel
                proj = ProjectModel.objects.filter(pk=int(project_id)).select_related('client').first()
                if proj:
                    initial['client'] = proj.client_id
            except (TypeError, ValueError):
                pass
        form = IncomeForm(initial=initial)

    return render(
        request,
        'crm/finance/income_form.html',
        {
            'form': form,
            'is_edit': False,
            'page_title': 'Add Income',
            'project_client_map': _project_client_map_json(form),
        },
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def income_edit(request, pk):
    income = get_object_or_404(
        Income.objects.select_related('client', 'project', 'category'),
        pk=pk,
    )
    if request.method == 'POST':
        form = IncomeForm(request.POST, instance=income)
        if form.is_valid():
            finance_service.update_income(
                income,
                actor=request.user,
                ip_address=_client_ip(request),
                **form.cleaned_data,
            )
            return _hx_redirect(
                request,
                'crm:income_list',
                msg='Income updated.',
            )
    else:
        form = IncomeForm(instance=income)

    return render(
        request,
        'crm/finance/income_form.html',
        {
            'form': form,
            'income': income,
            'is_edit': True,
            'page_title': f'Edit Income #{income.pk}',
            'project_client_map': _project_client_map_json(form),
        },
    )


@login_required
@finance_required
@require_POST
def income_delete(request, pk):
    income = get_object_or_404(Income, pk=pk)
    finance_service.delete_income(
        income,
        actor=request.user,
        ip_address=_client_ip(request),
    )
    return _hx_redirect(
        request,
        'crm:income_list',
        msg='Income deleted.',
    )


def _parse_period_from_request(request):
    period = request.GET.get('period') or 'month'
    date_from = request.GET.get('date_from') or None
    date_to = request.GET.get('date_to') or None
    parsed_from = parsed_to = None
    if date_from:
        try:
            parsed_from = datetime.strptime(date_from, '%Y-%m-%d').date()
        except ValueError:
            parsed_from = None
    if date_to:
        try:
            parsed_to = datetime.strptime(date_to, '%Y-%m-%d').date()
        except ValueError:
            parsed_to = None
    start, end, period_label = finance_service.resolve_period(
        period,
        date_from=parsed_from,
        date_to=parsed_to,
    )
    return {
        'period': period if period in ('today', 'week', 'month', 'custom') else 'month',
        'period_label': period_label,
        'date_from': parsed_from.isoformat() if parsed_from else '',
        'date_to': parsed_to.isoformat() if parsed_to else '',
        'filter_start': start,
        'filter_end': end,
        'start': start,
        'end': end,
    }


@login_required
@finance_required
def expense_dashboard(request):
    finance_service.ensure_default_expense_categories()
    ctx = _parse_period_from_request(request)
    payload = finance_service.get_expense_dashboard_payload(ctx['start'], ctx['end'])
    return render(
        request,
        'crm/finance/expense_dashboard.html',
        {
            **ctx,
            **payload,
            'page_title': 'Expense Dashboard',
        },
    )


@login_required
@finance_required
def expense_list(request):
    finance_service.ensure_default_expense_categories()
    form = ExpenseFilterForm(request.GET)
    qs = Expense.objects.select_related(
        'category', 'project', 'project__client', 'employee', 'created_by'
    )

    period = 'month'
    if form.is_valid():
        data = form.cleaned_data
        period = data.get('period') or 'month'
        q = (data.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(vendor__icontains=q)
                | Q(notes__icontains=q)
                | Q(paid_from__icontains=q)
                | Q(category__name__icontains=q)
                | Q(project__client__business_name__icontains=q)
            )
        if data.get('category'):
            qs = qs.filter(category=data['category'])
        if data.get('payment_method'):
            qs = qs.filter(payment_method=data['payment_method'])
        if data.get('project'):
            qs = qs.filter(project=data['project'])
        if data.get('employee'):
            qs = qs.filter(employee=data['employee'])
        if period != 'all':
            start, end, _ = finance_service.resolve_period(
                period,
                date_from=data.get('date_from'),
                date_to=data.get('date_to'),
            )
            qs = qs.filter(expense_date__gte=start, expense_date__lte=end)
    else:
        start, end, _ = finance_service.resolve_period('month')
        qs = qs.filter(expense_date__gte=start, expense_date__lte=end)

    qs = qs.order_by('-expense_date', '-created_at')
    paginator = Paginator(qs, EXPENSE_PER_PAGE)
    page_raw = request.GET.get('page') or 1
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(
        request,
        'crm/finance/expense_list.html',
        {
            'page_obj': page_obj,
            'filter_form': form,
            'period': period,
            'page_title': 'Expenses',
        },
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def expense_create(request):
    finance_service.ensure_default_expense_categories()
    if request.method == 'POST':
        form = ExpenseForm(request.POST, request.FILES)
        if form.is_valid():
            expense = finance_service.create_expense(
                actor=request.user,
                ip_address=_client_ip(request),
                **form.cleaned_data,
            )
            return _hx_redirect(
                request,
                'crm:expense_list',
                msg=f'Expense of Rs. {expense.amount} recorded.',
            )
    else:
        initial = {'expense_date': timezone.localdate()}
        project_id = request.GET.get('project')
        if project_id:
            try:
                initial['project'] = int(project_id)
            except (TypeError, ValueError):
                pass
        form = ExpenseForm(initial=initial)

    return render(
        request,
        'crm/finance/expense_form.html',
        {
            'form': form,
            'is_edit': False,
            'page_title': 'Add Expense',
        },
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def expense_edit(request, pk):
    expense = get_object_or_404(
        Expense.objects.select_related('category', 'project', 'employee'),
        pk=pk,
    )
    if request.method == 'POST':
        form = ExpenseForm(request.POST, request.FILES, instance=expense)
        if form.is_valid():
            finance_service.update_expense(
                expense,
                actor=request.user,
                ip_address=_client_ip(request),
                **form.cleaned_data,
            )
            return _hx_redirect(
                request,
                'crm:expense_list',
                msg='Expense updated.',
            )
    else:
        form = ExpenseForm(instance=expense)

    return render(
        request,
        'crm/finance/expense_form.html',
        {
            'form': form,
            'expense': expense,
            'is_edit': True,
            'page_title': f'Edit Expense #{expense.pk}',
        },
    )


@login_required
@finance_required
@require_POST
def expense_delete(request, pk):
    expense = get_object_or_404(Expense, pk=pk)
    finance_service.delete_expense(
        expense,
        actor=request.user,
        ip_address=_client_ip(request),
    )
    return _hx_redirect(
        request,
        'crm:expense_list',
        msg='Expense deleted.',
    )


@login_required
@finance_required
@require_http_methods(['GET'])
def expense_receipt(request, pk):
    """Receipt preview page (image inline / PDF link)."""
    expense = get_object_or_404(
        Expense.objects.select_related('category', 'project', 'employee'),
        pk=pk,
    )
    if not expense.receipt:
        messages.warning(request, 'No receipt uploaded for this expense.')
        return redirect('crm:expense_edit', pk=pk)
    return render(
        request,
        'crm/finance/expense_receipt.html',
        {
            'expense': expense,
            'page_title': f'Receipt — Expense #{expense.pk}',
        },
    )


@login_required
@finance_required
def finance_reports(request):
    return render(
        request,
        'crm/finance/reports_hub.html',
        {
            'page_title': 'Finance Reports',
            'report_types': reports_service.REPORT_TYPES,
        },
    )


@login_required
@finance_required
def finance_executive(request):
    data = reports_service.get_executive_dashboard()
    return render(
        request,
        'crm/finance/executive.html',
        {**data, 'page_title': 'Executive Dashboard'},
    )


def _build_report_context(request, report_type: str):
    form = FinanceReportFilterForm(request.GET or None, report_type=report_type)
    if form.is_valid():
        filters = reports_service.filters_from_cleaned(form.cleaned_data)
    else:
        filters = reports_service.filters_from_cleaned({'period': 'month'})
    report = reports_service.run_report(report_type, filters)
    return form, filters, report


@login_required
@finance_required
def finance_report_run(request, report_type: str):
    report_type = (report_type or '').strip().lower().replace('-', '_')
    if report_type not in reports_service.REPORT_TYPES:
        messages.error(request, 'Unknown report.')
        return redirect('crm:finance_reports')

    form, filters, report = _build_report_context(request, report_type)
    export = (request.GET.get('export') or '').strip().lower()
    if export == 'csv':
        return reports_service.export_csv(report)
    if export in ('xlsx', 'excel'):
        return reports_service.export_xlsx(report)
    if export == 'pdf':
        return reports_service.export_pdf(report)

    template = (
        'crm/finance/report_print.html'
        if export == 'print'
        else 'crm/finance/report_runner.html'
    )
    return render(
        request,
        template,
        {
            'filter_form': form,
            'filters': filters,
            'page_title': report['report_title'],
            'is_print': export == 'print',
            **report,
        },
    )


@login_required
@finance_required
def report_monthly_expense(request):
    """Legacy alias → expense report (monthly)."""
    q = request.GET.copy()
    q['period'] = q.get('period') or 'year'
    return redirect(f"{reverse('crm:finance_report_run', kwargs={'report_type': 'expense'})}?{q.urlencode()}")


@login_required
@finance_required
def report_category(request):
    q = request.GET.urlencode()
    url = reverse('crm:finance_report_run', kwargs={'report_type': 'expense_category'})
    return redirect(f'{url}?{q}' if q else url)


@login_required
@finance_required
def report_vendor(request):
    # Vendor rolled into expense report with filters
    q = request.GET.urlencode()
    url = reverse('crm:finance_report_run', kwargs={'report_type': 'expense'})
    return redirect(f'{url}?{q}' if q else url)


@login_required
@finance_required
def report_project_expense(request):
    q = request.GET.urlencode()
    url = reverse('crm:finance_report_run', kwargs={'report_type': 'project_profitability'})
    return redirect(f'{url}?{q}' if q else url)


@login_required
def project_finance(request, pk):
    """Project Finance tab — revenue, expenses, profit from Income/Expense."""
    if not (can_access_finance(request.user) or can_view_financial_data(request.user)):
        return HttpResponseForbidden('Finance data is restricted for your role.')
    project = get_object_or_404(
        get_projects_for_user(request.user).select_related('client', 'package'),
        pk=pk,
    )
    data = finance_service.get_project_finance(project)
    return render(
        request,
        'crm/finance/project_finance.html',
        {
            'project': project,
            'client': project.client,
            **data,
            'page_title': f'Finance — Project #{project.pk}',
            'active_tab': 'finance',
        },
    )


@login_required
def client_finance(request, pk):
    """Client Finance summary across all projects."""
    if not (can_access_finance(request.user) or can_view_financial_data(request.user)):
        return HttpResponseForbidden('Finance data is restricted for your role.')
    visible_projects = get_projects_for_user(request.user).filter(client_id=pk)
    if not visible_projects.exists() and not can_access_finance(request.user):
        return HttpResponseForbidden('You do not have access to this client.')
    client = get_object_or_404(Client, pk=pk)
    # Scope to projects the user can see unless finance admin-level access
    data = finance_service.get_client_finance(client)
    if not can_access_finance(request.user):
        visible_ids = set(visible_projects.values_list('pk', flat=True))
        data['project_rows'] = [
            r for r in data['project_rows'] if r['project'].pk in visible_ids
        ]
    return render(
        request,
        'crm/finance/client_finance.html',
        {
            'client': client,
            **data,
            'page_title': f'Finance — {client.business_name}',
        },
    )
