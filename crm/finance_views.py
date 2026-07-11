"""Finance module views — income tracking & dashboard."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .finance_forms import (
    AllocationBucketFormSet,
    AllocationDashboardFilterForm,
    ExpenseFilterForm,
    ExpenseForm,
    FinanceReportFilterForm,
    FinanceStatementFilterForm,
    FounderFormSet,
    FounderWithdrawalForm,
    FundPeriodFilterForm,
    FundTransferForm,
    IncomeFilterForm,
    IncomeForm,
)
from .models import (
    Client,
    Expense,
    ExpenseCategory,
    Founder,
    FundTransfer,
    FundUsage,
    Income,
    IncomeAllocation,
    IncomeCategory,
    RevenueAllocationBucket,
)
from .rbac import can_access_billing, can_access_finance, can_view_financial_data
from .services import finance as finance_service
from .services import finance_reports as reports_service
from .services import finance_statement as statement_service
from .services import fund_management as fund_service
from .services import revenue_allocation as alloc_service
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


def finance_admin_required(view_fn):
    """Settings hub — admin only (same gate as Billing)."""
    @wraps(view_fn)
    def _wrapped(request, *args, **kwargs):
        if not can_access_billing(request.user):
            return HttpResponseForbidden(
                'Finance settings are restricted to Admin.'
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


def _hx_redirect(
    request,
    url_name: str,
    *,
    pk: int | None = None,
    msg: str | None = None,
    query: str | None = None,
    url_kwargs: dict | None = None,
):
    """Redirect with HTMX support when triggered from finance pages."""
    if msg:
        messages.success(request, msg)
    if url_kwargs is not None:
        url = reverse(url_name, kwargs=url_kwargs)
    elif pk is not None:
        url = reverse(url_name, kwargs={'pk': pk})
    else:
        url = reverse(url_name)
    if query:
        url = f'{url}?{query}'
    if request.headers.get('HX-Request'):
        response = HttpResponse(status=200)
        response['HX-Redirect'] = url
        return response
    return redirect(url)


FUND_SLUG_MAP = {
    'operations': 'operations',
    'company-savings': 'company_savings',
    'company_savings': 'company_savings',
    'founder-pool': 'founder_pool',
    'founder_pool': 'founder_pool',
    'sales-commission': 'sales_commission',
    'sales_commission': 'sales_commission',
}

FUND_CODE_TO_SLUG = {
    'operations': 'operations',
    'company_savings': 'company-savings',
    'founder_pool': 'founder-pool',
    'sales_commission': 'sales-commission',
}


@login_required
@finance_required
def finance_dashboard(request):
    """Single Finance Dashboard — live cashbook + fund summary (UI composition only)."""
    finance_service.ensure_default_categories()
    finance_service.ensure_default_expense_categories()
    data = finance_service.get_management_dashboard()

    # Compose existing service outputs for unified dashboard (no new calculations)
    fund_service.ensure_bucket_codes()
    fund_balances = fund_service.all_fund_balances()
    available_balance = sum((r['remaining'] for r in fund_balances), Decimal('0'))
    founder_dash = fund_service.get_founder_dashboard(
        fund_service.filters_from_cleaned({'period': 'month'})
    )

    data.update({
        'fund_balances': fund_balances,
        'available_balance': available_balance,
        'founder_cards': founder_dash.get('cards') or [],
        'founder_total_remaining': founder_dash.get('total_remaining'),
        'show_quick_actions': True,
    })

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
    return _render_income_list(request)


def _render_income_list(request, **modal_overrides):
    finance_service.ensure_default_categories()
    filter_form = IncomeFilterForm(request.GET)
    qs = Income.objects.select_related(
        'client', 'project', 'project__package', 'category', 'created_by'
    )

    period = 'month'
    if filter_form.is_valid():
        data = filter_form.cleaned_data
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

    income_form = modal_overrides.get('income_form')
    income_edit_pk = modal_overrides.get('income_edit_pk')
    income_allocations = modal_overrides.get('income_allocations') or []
    open_tx_modal = modal_overrides.get('open_tx_modal') or ''

    if income_form is None:
        edit_raw = (request.GET.get('edit') or '').strip()
        modal = (request.GET.get('modal') or '').strip().lower()
        if modal == 'income' and edit_raw:
            try:
                income = Income.objects.select_related(
                    'client', 'project', 'category'
                ).get(pk=int(edit_raw))
            except (Income.DoesNotExist, TypeError, ValueError):
                income = None
            if income:
                income_form = IncomeForm(instance=income, prefix='inc')
                income_edit_pk = income.pk
                income_allocations = list(
                    income.allocations.select_related('bucket').order_by(
                        'bucket__display_order'
                    )
                )
                open_tx_modal = 'income'
        if income_form is None:
            initial = {'payment_date': timezone.localdate()}
            project_id = request.GET.get('project')
            if project_id:
                try:
                    initial['project'] = int(project_id)
                    from .models import Project as ProjectModel
                    proj = (
                        ProjectModel.objects.filter(pk=int(project_id))
                        .select_related('client')
                        .first()
                    )
                    if proj:
                        initial['client'] = proj.client_id
                except (TypeError, ValueError):
                    pass
            income_form = IncomeForm(initial=initial, prefix='inc')
        if not open_tx_modal and (request.GET.get('modal') or '').strip().lower() == 'income':
            open_tx_modal = 'income'

    return render(
        request,
        'crm/finance/income_list.html',
        {
            'page_obj': page_obj,
            'filter_form': filter_form,
            'period': period,
            'page_title': 'Income',
            'income_form': income_form,
            'income_edit_pk': income_edit_pk,
            'income_allocations': income_allocations,
            'open_tx_modal': open_tx_modal,
            'project_client_map': _project_client_map_json(income_form),
        },
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def income_create(request):
    finance_service.ensure_default_categories()
    if request.method == 'GET':
        from urllib.parse import urlencode
        q = {'modal': 'income'}
        if request.GET.get('project'):
            q['project'] = request.GET['project']
        return redirect(f"{reverse('crm:income_list')}?{urlencode(q)}")

    form = IncomeForm(request.POST, prefix='inc')
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
            query='saved=1',
        )
    return _render_income_list(
        request,
        income_form=form,
        open_tx_modal='income',
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def income_edit(request, pk):
    income = get_object_or_404(
        Income.objects.select_related('client', 'project', 'category'),
        pk=pk,
    )
    if request.method == 'GET':
        from urllib.parse import urlencode
        return redirect(
            f"{reverse('crm:income_list')}?{urlencode({'modal': 'income', 'edit': pk})}"
        )

    form = IncomeForm(request.POST, instance=income, prefix='inc')
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
    return _render_income_list(
        request,
        income_form=form,
        income_edit_pk=income.pk,
        income_allocations=list(
            income.allocations.select_related('bucket').order_by(
                'bucket__display_order'
            )
        ),
        open_tx_modal='income',
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


@login_required
@finance_required
def expense_dashboard(request):
    """Legacy URL — merged into the single Finance Dashboard."""
    return redirect('crm:finance_dashboard')


@login_required
@finance_required
def expense_list(request):
    return _render_expense_list(request)


def _render_expense_list(request, **modal_overrides):
    finance_service.ensure_default_expense_categories()
    filter_form = ExpenseFilterForm(request.GET)
    qs = Expense.objects.select_related(
        'category', 'project', 'project__client', 'employee', 'created_by'
    )

    period = 'month'
    if filter_form.is_valid():
        data = filter_form.cleaned_data
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

    expense_form = modal_overrides.get('expense_form')
    expense_edit_pk = modal_overrides.get('expense_edit_pk')
    expense_obj = modal_overrides.get('expense_obj')
    open_tx_modal = modal_overrides.get('open_tx_modal') or ''

    if expense_form is None:
        edit_raw = (request.GET.get('edit') or '').strip()
        modal = (request.GET.get('modal') or '').strip().lower()
        if modal == 'expense' and edit_raw:
            try:
                expense = Expense.objects.select_related(
                    'category', 'project', 'employee'
                ).get(pk=int(edit_raw))
            except (Expense.DoesNotExist, TypeError, ValueError):
                expense = None
            if expense:
                expense_form = ExpenseForm(instance=expense, prefix='exp')
                expense_edit_pk = expense.pk
                expense_obj = expense
                open_tx_modal = 'expense'
        if expense_form is None:
            initial = {'expense_date': timezone.localdate()}
            project_id = request.GET.get('project')
            if project_id:
                try:
                    initial['project'] = int(project_id)
                except (TypeError, ValueError):
                    pass
            funding_id = request.GET.get('funding') or request.GET.get('bucket')
            if funding_id:
                try:
                    initial['funding_bucket'] = int(funding_id)
                except (TypeError, ValueError):
                    pass
            expense_form = ExpenseForm(initial=initial, prefix='exp')
        if not open_tx_modal and (request.GET.get('modal') or '').strip().lower() == 'expense':
            open_tx_modal = 'expense'

    return render(
        request,
        'crm/finance/expense_list.html',
        {
            'page_obj': page_obj,
            'filter_form': filter_form,
            'period': period,
            'page_title': 'Expenses',
            'expense_form': expense_form,
            'expense_edit_pk': expense_edit_pk,
            'expense_obj': expense_obj,
            'open_tx_modal': open_tx_modal,
            'fund_balance_json': _fund_balance_json(),
            'fund_balance_map': {
                str(row['bucket'].pk): f"{float(row['remaining']):.2f}"
                for row in fund_service.all_fund_balances()
            },
        },
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def expense_create(request):
    finance_service.ensure_default_expense_categories()
    if request.method == 'GET':
        from urllib.parse import urlencode
        q = {'modal': 'expense'}
        if request.GET.get('project'):
            q['project'] = request.GET['project']
        return redirect(f"{reverse('crm:expense_list')}?{urlencode(q)}")

    form = ExpenseForm(request.POST, request.FILES, prefix='exp')
    if form.is_valid():
        try:
            expense = finance_service.create_expense(
                actor=request.user,
                ip_address=_client_ip(request),
                **form.cleaned_data,
            )
        except ValidationError as exc:
            form.add_error(None, '; '.join(getattr(exc, 'messages', [str(exc)])))
        else:
            return _hx_redirect(
                request,
                'crm:expense_list',
                msg=f'Expense of Rs. {expense.amount} recorded.',
                query='saved=1',
            )
    return _render_expense_list(
        request,
        expense_form=form,
        open_tx_modal='expense',
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def expense_edit(request, pk):
    expense = get_object_or_404(
        Expense.objects.select_related('category', 'project', 'employee'),
        pk=pk,
    )
    if request.method == 'GET':
        from urllib.parse import urlencode
        return redirect(
            f"{reverse('crm:expense_list')}?{urlencode({'modal': 'expense', 'edit': pk})}"
        )

    form = ExpenseForm(request.POST, request.FILES, instance=expense, prefix='exp')
    if form.is_valid():
        try:
            finance_service.update_expense(
                expense,
                actor=request.user,
                ip_address=_client_ip(request),
                **form.cleaned_data,
            )
        except ValidationError as exc:
            form.add_error(None, '; '.join(getattr(exc, 'messages', [str(exc)])))
        else:
            return _hx_redirect(
                request,
                'crm:expense_list',
                msg='Expense updated.',
            )
    return _render_expense_list(
        request,
        expense_form=form,
        expense_edit_pk=expense.pk,
        expense_obj=expense,
        open_tx_modal='expense',
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
        from urllib.parse import urlencode
        return redirect(
            f"{reverse('crm:expense_list')}?{urlencode({'modal': 'expense', 'edit': pk})}"
        )
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
def finance_statement(request):
    """Bank-statement style Income + Expense ledger with running balance."""
    form = FinanceStatementFilterForm(request.GET or None)
    if form.is_valid():
        filters = statement_service.filters_from_cleaned(form.cleaned_data)
    else:
        filters = statement_service.filters_from_cleaned({'period': 'month', 'sort': 'newest'})

    export = (request.GET.get('export') or '').strip().lower()
    if export in ('csv', 'xlsx', 'excel', 'pdf', 'print'):
        ctx = statement_service.build_statement(filters, for_export=True)
        if export == 'csv':
            return statement_service.export_statement_csv(ctx)
        if export in ('xlsx', 'excel'):
            return statement_service.export_statement_xlsx(ctx)
        if export == 'pdf':
            return statement_service.export_statement_pdf(ctx)
        return render(
            request,
            'crm/finance/statement_print.html',
            {
                'filter_form': form,
                'page_title': 'Finance Statement',
                **ctx,
            },
        )

    data = statement_service.build_statement(
        filters,
        page=request.GET.get('page') or 1,
    )
    return render(
        request,
        'crm/finance/statement.html',
        {
            'filter_form': form,
            'page_title': 'Finance Statement',
            **data,
        },
    )


@login_required
@finance_required
def allocation_dashboard(request):
    """Legacy URL — Funds Overview is the new landing page. Exports still work here."""
    export = (request.GET.get('export') or '').strip().lower()
    if export:
        alloc_service.ensure_default_buckets()
        form = AllocationDashboardFilterForm(request.GET or None)
        if form.is_valid():
            filters = alloc_service.filters_from_cleaned(form.cleaned_data)
        else:
            filters = alloc_service.filters_from_cleaned({'period': 'month'})
        data = alloc_service.get_allocation_dashboard(filters)
        if export == 'csv':
            return alloc_service.export_allocation_csv(data)
        if export in ('xlsx', 'excel'):
            return alloc_service.export_allocation_xlsx(data)
        if export == 'pdf':
            return alloc_service.export_allocation_pdf(data)
    return redirect('crm:funds_overview')


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def allocation_settings(request):
    """Configure allocation bucket percentages (must total 100% when active)."""
    alloc_service.ensure_default_buckets()
    qs = RevenueAllocationBucket.objects.order_by('display_order', 'name')

    if request.method == 'POST':
        formset = AllocationBucketFormSet(request.POST, queryset=qs)
        if formset.is_valid():
            with transaction.atomic():
                formset.save()
            ok, total = alloc_service.validate_active_percentages()
            if not ok:
                messages.warning(
                    request,
                    f'Active percentages currently total {total}% — they must equal 100%. '
                    'Fix before new income will split correctly (splits are scaled until fixed).',
                )
            else:
                messages.success(request, 'Allocation settings saved. Active funds total 100%.')
                # Recalculate recent incomes without forcing full table scan — optional backfill
                n = alloc_service.backfill_missing_allocations(
                    actor=request.user, limit=500
                )
                if n:
                    messages.info(request, f'Allocated {n} income entr{"y" if n == 1 else "ies"} that had no split yet.')
            return _hx_redirect(request, 'crm:allocation_settings')
    else:
        formset = AllocationBucketFormSet(queryset=qs)

    ok, total = alloc_service.validate_active_percentages()
    return render(
        request,
        'crm/finance/allocation_settings.html',
        {
            'formset': formset,
            'settings_ok': ok,
            'settings_pct_total': total,
            'page_title': 'Allocation Settings',
        },
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def fund_usage_create(request):
    """Legacy Record Usage — redirected to Expense + Funding Source."""
    from urllib.parse import urlencode

    messages.info(
        request,
        'Record Usage was removed. Create an Expense and set Funding Source '
        'to draw from a fund.',
    )
    q = {'modal': 'expense'}
    bucket = request.GET.get('bucket') or request.POST.get('usage-bucket') or request.POST.get('bucket')
    if bucket:
        q['funding'] = bucket
    url = f"{reverse('crm:expense_list')}?{urlencode(q)}"
    if request.headers.get('HX-Request'):
        resp = HttpResponse(status=204)
        resp['HX-Redirect'] = url
        return resp
    return redirect(url)


@login_required
@finance_required
@require_POST
def allocation_recalculate_all(request):
    """Force re-sync allocations for incomes in a date window (admin ops)."""
    alloc_service.ensure_default_buckets()
    ok, total = alloc_service.validate_active_percentages()
    if not ok:
        messages.error(
            request,
            f'Cannot recalculate while active percentages total {total}% (need 100%).',
        )
        return _hx_redirect(request, 'crm:allocation_settings')

    # Recalculate last 90 days of income (bounded for performance)
    since = timezone.localdate() - timedelta(days=90)
    incomes = Income.objects.filter(payment_date__gte=since).order_by('id')[:5000]
    n = 0
    for income in incomes.iterator(chunk_size=200):
        alloc_service.sync_income_allocations(
            income,
            actor=request.user,
            ip_address=_client_ip(request),
            note='Bulk recalculate from settings',
        )
        n += 1
    messages.success(request, f'Recalculated allocations for {n} income entries (last 90 days).')
    return _hx_redirect(request, 'crm:funds_overview')


@login_required
@finance_required
def finance_reports(request):
    """Single reports hub — cashbook, analysis, and fund exports."""
    return render(
        request,
        'crm/finance/reports_hub.html',
        {
            'page_title': 'Reports',
            'cashbook_reports': {
                'income': 'Income Report',
                'expense': 'Expense Report',
                'profit': 'Profit Report',
                'cash_flow': 'Cash Flow by Method',
            },
            'analysis_reports': {
                'project_profitability': 'Project Profitability',
                'client_revenue': 'Client Revenue',
                'expense_category': 'Expense by Category',
                'payment_method': 'Expense by Payment Method',
            },
        },
    )


@login_required
@finance_required
def finance_executive(request):
    """Legacy URL — month KPIs live on the single Finance Dashboard."""
    return redirect('crm:finance_dashboard')


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
    # Legacy / duplicate report slugs → canonical report
    redirect_to = getattr(reports_service, 'REPORT_REDIRECTS', {}).get(report_type)
    if redirect_to:
        q = request.GET.urlencode()
        url = reverse('crm:finance_report_run', kwargs={'report_type': redirect_to})
        return redirect(f'{url}?{q}' if q else url)

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


# ── Phase 8: Founders & internal funds ───────────────────────────────────────

FUND_REPORT_TYPES = {
    'fund_balances': 'Current Fund Balances',
    'fund_utilization': 'Fund Utilization',
    'fund_transfers': 'Fund Transfer History',
    'founder_statement': 'Founder Statement',
}


@login_required
@finance_required
def founder_dashboard(request):
    """Legacy URL — Founder Pool lives under Funds."""
    return redirect('crm:fund_detail', fund_slug='founder-pool')


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def founder_settings(request):
    fund_service.ensure_default_founders()
    qs = Founder.objects.order_by('display_order', 'name')
    if request.method == 'POST':
        formset = FounderFormSet(request.POST, queryset=qs)
        if formset.is_valid():
            with transaction.atomic():
                formset.save()
            ok, total = fund_service.validate_founder_percentages()
            if not ok:
                messages.warning(
                    request,
                    f'Active founder percentages total {total}% — they should equal 100%.',
                )
            else:
                messages.success(request, 'Founder settings saved.')
                n = fund_service.backfill_founder_shares(limit=2000, actor=request.user)
                if n:
                    messages.info(request, f'Backfilled founder shares for {n} income entries.')
            return _hx_redirect(request, 'crm:founder_settings')
    else:
        formset = FounderFormSet(queryset=qs)

    ok, total = fund_service.validate_founder_percentages()
    return render(
        request,
        'crm/finance/founder_settings.html',
        {
            'formset': formset,
            'founders_ok': ok,
            'founders_pct_total': total,
            'page_title': 'Founder Settings',
        },
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def founder_withdrawal_create(request):
    """Founder withdrawal — modal POST; GET opens Founder Pool modal."""
    fund_service.ensure_default_founders()
    if request.method == 'GET':
        from urllib.parse import urlencode
        q = {'modal': 'withdraw'}
        if request.GET.get('founder'):
            q['founder'] = request.GET['founder']
        return redirect(
            f"{reverse('crm:fund_detail', kwargs={'fund_slug': 'founder-pool'})}"
            f"?{urlencode(q)}"
        )

    form = FounderWithdrawalForm(request.POST, prefix='wd')
    if form.is_valid():
        try:
            w = fund_service.create_founder_withdrawal(
                actor=request.user,
                ip_address=_client_ip(request),
                **form.cleaned_data,
            )
        except ValidationError as exc:
            form.add_error(None, '; '.join(getattr(exc, 'messages', [str(exc)])))
        else:
            return _hx_redirect(
                request,
                'crm:fund_detail',
                url_kwargs={'fund_slug': 'founder-pool'},
                msg=f'Withdrawal of Rs. {w.amount} for {w.founder.name} recorded.',
            )
    return _render_modal_error(
        request,
        'withdraw',
        founder_withdrawal_form=form,
    )


@login_required
@finance_required
@require_http_methods(['GET', 'POST'])
def fund_transfer_create(request):
    """Fund transfer — modal POST; GET opens Funds hub modal."""
    fund_service.ensure_bucket_codes()
    if request.method == 'GET':
        from urllib.parse import urlencode
        q = {'modal': 'transfer'}
        if request.GET.get('from'):
            q['from'] = request.GET['from']
        return redirect(f"{reverse('crm:funds_overview')}?{urlencode(q)}")

    form = FundTransferForm(request.POST, prefix='xfer')
    if form.is_valid():
        try:
            t = fund_service.create_fund_transfer(
                actor=request.user,
                ip_address=_client_ip(request),
                **form.cleaned_data,
            )
        except ValidationError as exc:
            form.add_error(None, '; '.join(getattr(exc, 'messages', [str(exc)])))
        else:
            return_to, slug = _modal_return_target(request)
            if return_to == 'detail' and slug:
                target_slug = slug
            else:
                target_slug = FUND_CODE_TO_SLUG.get(t.from_bucket.code) or 'operations'
            return _hx_redirect(
                request,
                'crm:fund_detail',
                url_kwargs={'fund_slug': target_slug},
                msg=(
                    f'Transferred Rs. {t.amount} from {t.from_bucket.name} '
                    f'to {t.to_bucket.name}.'
                ),
            )
    return _render_modal_error(request, 'transfer', fund_transfer_form=form)


def _fund_report_context(request, report_type: str):
    form = FundPeriodFilterForm(request.GET or None)
    if form.is_valid():
        data = form.cleaned_data
        filters = fund_service.filters_from_cleaned(data)
        founder = data.get('founder')
    else:
        filters = fund_service.filters_from_cleaned({'period': 'month'})
        founder = None

    if report_type == 'fund_balances':
        report = fund_service.get_fund_balances_report()
    elif report_type == 'fund_utilization':
        report = fund_service.get_fund_utilization_report(filters)
    elif report_type == 'fund_transfers':
        report = fund_service.get_transfer_report(filters)
    elif report_type == 'founder_statement':
        if not founder:
            founder = Founder.objects.filter(active=True).order_by(
                'display_order', 'name'
            ).first()
        if not founder:
            report = {
                'report_title': 'Founder Statement',
                'report_type': 'founder_statement',
                'period_label': filters.label,
                'filter_start': filters.date_from,
                'filter_end': filters.date_to,
                'columns': [],
                'rows': [],
                'summary': {},
            }
        else:
            report = fund_service.get_founder_statement(founder, filters)
    else:
        report = None
    return form, filters, report, founder


@login_required
@finance_required
def fund_reports_hub(request):
    return render(
        request,
        'crm/finance/fund_reports_hub.html',
        {
            'page_title': 'Fund Reports',
            'report_types': FUND_REPORT_TYPES,
            'fund_balances': fund_service.all_fund_balances(),
        },
    )


@login_required
@finance_required
def fund_report_run(request, report_type: str):
    report_type = (report_type or '').strip().lower().replace('-', '_')
    if report_type not in FUND_REPORT_TYPES:
        messages.error(request, 'Unknown fund report.')
        return redirect('crm:fund_reports_hub')

    form, filters, report, founder = _fund_report_context(request, report_type)
    export = (request.GET.get('export') or '').strip().lower()
    if export == 'csv':
        return fund_service.export_report_csv(report)
    if export in ('xlsx', 'excel'):
        return fund_service.export_report_xlsx(report)
    if export == 'pdf':
        return fund_service.export_report_pdf(report)

    return render(
        request,
        'crm/finance/fund_report.html',
        {
            'filter_form': form,
            'founder': founder,
            'report_types': FUND_REPORT_TYPES,
            'page_title': report['report_title'],
            **report,
        },
    )


# ── UI navigation: Funds overview / detail / Settings ────────────────────────


def _fund_balance_json() -> str:
    return json.dumps({
        str(row['bucket'].pk): float(row['remaining'])
        for row in fund_service.all_fund_balances()
    })


def _founder_balance_json() -> str:
    fund_service.ensure_default_founders()
    return json.dumps({
        str(f.pk): float(fund_service.founder_balance(f)['remaining'])
        for f in Founder.objects.filter(active=True).order_by('display_order', 'name')
    })


def _founder_pool_remaining() -> Decimal:
    pool = (
        RevenueAllocationBucket.objects.filter(code='founder_pool').first()
        or RevenueAllocationBucket.objects.filter(name='Founder Pool').first()
    )
    if not pool:
        return Decimal('0')
    return fund_service.bucket_balance(pool)['remaining']


def _fund_modal_context(**overrides) -> dict:
    """Shared forms + balance maps for fund action modals."""
    ctx = {
        'fund_transfer_form': FundTransferForm(prefix='xfer'),
        'founder_withdrawal_form': FounderWithdrawalForm(prefix='wd'),
        'fund_balance_json': _fund_balance_json(),
        'founder_balance_json': _founder_balance_json(),
        'founder_pool_remaining': _founder_pool_remaining(),
        'fund_modal_return_to': 'overview',
        'open_fund_modal': '',
        'fund_modal_prefill_bucket': '',
        'fund_modal_prefill_from': '',
        'fund_modal_prefill_founder': '',
    }
    ctx.update(overrides)
    return ctx


def _funds_overview_rows() -> list:
    rows = []
    for bal in fund_service.all_fund_balances():
        bucket = bal['bucket']
        code = bucket.code or FUND_SLUG_MAP.get(
            bucket.name.lower().replace(' ', '_'), ''
        )
        for name, c in {
            'Founder Pool': 'founder_pool',
            'Sales Commission': 'sales_commission',
            'Operations': 'operations',
            'Company Savings': 'company_savings',
        }.items():
            if bucket.name == name:
                code = c
                break
        slug = FUND_CODE_TO_SLUG.get(code)
        drawn = (
            bal['expense_out']
            + bal['usage_out']
            + bal['founder_withdrawals']
        )
        rows.append({
            **bal,
            'code': code,
            'slug': slug,
            'is_founder_pool': code == 'founder_pool' or bucket.name == 'Founder Pool',
            'drawn': drawn,
            'transferred_out': bal['transfer_out'],
            'used': drawn + bal['transfer_out'],
        })
    return rows


def _render_funds_overview(request, **modal_overrides):
    fund_service.ensure_bucket_codes()
    alloc_service.ensure_default_buckets()
    fund_rows = _funds_overview_rows()
    total_allocated = sum((r['allocated'] for r in fund_rows), Decimal('0'))
    total_remaining = sum((r['remaining'] for r in fund_rows), Decimal('0'))
    total_used = sum((r['drawn'] for r in fund_rows), Decimal('0'))
    ctx = _fund_modal_context(
        fund_modal_return_to='overview',
        **modal_overrides,
    )
    ctx.update({
        'fund_rows': fund_rows,
        'funds_total_allocated': total_allocated,
        'funds_total_remaining': total_remaining,
        'funds_total_used': total_used,
        'page_title': 'Funds',
    })
    # Auto-open from query when not set by POST error
    if not ctx.get('open_fund_modal'):
        modal = (request.GET.get('modal') or '').strip().lower()
        if modal == 'usage':
            from urllib.parse import urlencode
            q = {'modal': 'expense'}
            if request.GET.get('bucket'):
                q['funding'] = request.GET['bucket']
            return redirect(f"{reverse('crm:expense_list')}?{urlencode(q)}")
        if modal in ('transfer', 'withdraw'):
            ctx['open_fund_modal'] = modal
            ctx['fund_modal_prefill_from'] = request.GET.get('from') or ''
            ctx['fund_modal_prefill_founder'] = request.GET.get('founder') or ''
    return render(request, 'crm/finance/funds_overview.html', ctx)


def _render_fund_detail(request, fund_slug: str, **modal_overrides):
    """Build fund detail page (shared by GET and invalid modal POSTs)."""
    bucket = _resolve_fund_bucket(fund_slug)
    if not bucket:
        messages.error(request, 'Unknown fund.')
        return redirect('crm:funds_overview')

    bal = fund_service.bucket_balance(bucket)
    drawn = (
        bal['expense_out']
        + bal['usage_out']
        + bal['founder_withdrawals']
    )
    used = drawn + bal['transfer_out']
    allocations = list(
        IncomeAllocation.objects.filter(bucket=bucket)
        .select_related('income', 'income__client')
        .order_by('-income__payment_date', '-id')[:40]
    )
    expenses = list(
        Expense.objects.filter(funding_bucket=bucket)
        .select_related('category', 'project', 'created_by')
        .order_by('-expense_date', '-id')[:40]
    )
    usages = list(
        FundUsage.objects.filter(bucket=bucket)
        .select_related('created_by', 'project')
        .order_by('-usage_date', '-id')[:30]
    )
    transfers = list(
        FundTransfer.objects.filter(
            Q(from_bucket=bucket) | Q(to_bucket=bucket)
        )
        .select_related('from_bucket', 'to_bucket', 'created_by')
        .order_by('-transfer_date', '-id')[:40]
    )

    resolved_slug = FUND_CODE_TO_SLUG.get(bucket.code) or fund_slug
    is_founder_pool = bucket.code == 'founder_pool' or bucket.name == 'Founder Pool'
    modal_ctx = _fund_modal_context(
        fund_modal_return_to='detail',
        fund_slug=resolved_slug,
        **modal_overrides,
    )
    context = {
        **modal_ctx,
        'bucket': bucket,
        'fund_slug': resolved_slug,
        'balance': bal,
        'used': used,
        'drawn': drawn,
        'allocations': allocations,
        'expenses': expenses,
        'usages': usages,
        'transfers': transfers,
        'page_title': bucket.name,
        'is_founder_pool': is_founder_pool,
        'chart_alloc': float(bal['allocated']),
        'chart_used': float(drawn),
        'chart_remaining': float(bal['remaining']),
    }

    if is_founder_pool:
        fund_service.ensure_default_founders()
        fund_service.backfill_founder_shares(limit=200, actor=request.user)
        founder_data = fund_service.get_founder_dashboard(
            fund_service.filters_from_cleaned({'period': 'all'})
        )
        context.update({
            'founder_cards': founder_data['cards'],
            'founder_withdrawals': founder_data['withdrawals'],
            'founder_shares': founder_data['recent_shares'],
            'total_allocated': founder_data['total_allocated'],
            'total_withdrawn': founder_data['total_withdrawn'],
            'total_remaining': founder_data['total_remaining'],
        })

    if not context.get('open_fund_modal'):
        modal = (request.GET.get('modal') or '').strip().lower()
        if modal == 'usage':
            from urllib.parse import urlencode
            q = {'modal': 'expense', 'funding': str(bucket.pk)}
            return redirect(f"{reverse('crm:expense_list')}?{urlencode(q)}")
        if modal in ('transfer', 'withdraw'):
            context['open_fund_modal'] = modal
            context['fund_modal_prefill_from'] = (
                request.GET.get('from') or str(bucket.pk)
            )
            context['fund_modal_prefill_founder'] = request.GET.get('founder') or ''

    return render(request, 'crm/finance/fund_detail.html', context)


def _modal_return_target(request):
    """Where to re-render / redirect after a fund modal POST."""
    return_to = (request.POST.get('return_to') or 'overview').strip().lower()
    slug = (request.POST.get('return_slug') or '').strip()
    if return_to == 'detail' and slug:
        return 'detail', slug
    return 'overview', ''


def _render_modal_error(request, open_modal: str, **form_overrides):
    return_to, slug = _modal_return_target(request)
    overrides = {
        'open_fund_modal': open_modal,
        **form_overrides,
    }
    if return_to == 'detail' and slug:
        return _render_fund_detail(request, slug, **overrides)
    return _render_funds_overview(request, **overrides)


@login_required
@finance_required
def funds_overview(request):
    """Funds landing — cards for each allocation pot."""
    return _render_funds_overview(request)


def _resolve_fund_bucket(fund_slug: str) -> RevenueAllocationBucket | None:
    fund_service.ensure_bucket_codes()
    code = FUND_SLUG_MAP.get((fund_slug or '').strip().lower())
    if not code:
        return None
    return (
        RevenueAllocationBucket.objects.filter(code=code).first()
        or RevenueAllocationBucket.objects.filter(
            name={
                'operations': 'Operations',
                'company_savings': 'Company Savings',
                'founder_pool': 'Founder Pool',
                'sales_commission': 'Sales Commission',
            }.get(code, '')
        ).first()
    )


@login_required
@finance_required
def fund_detail(request, fund_slug: str):
    """Per-fund detail page — uses existing balance helpers only."""
    return _render_fund_detail(request, fund_slug)


@login_required
@finance_admin_required
def finance_settings(request):
    from .models import ExpenseCategory, Founder, IncomeCategory

    alloc_service.ensure_default_buckets()
    fund_service.ensure_default_founders()
    finance_service.ensure_default_categories()
    finance_service.ensure_default_expense_categories()

    income_cats = IncomeCategory.objects.filter(active=True).count()
    expense_cats = ExpenseCategory.objects.filter(active=True).count()
    founders = Founder.objects.filter(active=True).count()
    buckets = RevenueAllocationBucket.objects.filter(active=True).count()
    bank_count = (
        Income.objects.exclude(bank_account='')
        .values('bank_account')
        .distinct()
        .count()
    )

    return render(
        request,
        'crm/finance/settings_hub.html',
        {
            'page_title': 'Finance Settings',
            'settings_income_cats': income_cats,
            'settings_expense_cats': expense_cats,
            'settings_founders': founders,
            'settings_buckets': buckets,
            'settings_banks': bank_count,
        },
    )


@login_required
@finance_admin_required
@require_http_methods(['GET', 'POST'])
def finance_settings_categories(request):
    from .finance_forms import ExpenseCategoryForm, IncomeCategoryForm

    finance_service.ensure_default_categories()
    finance_service.ensure_default_expense_categories()
    income_form = IncomeCategoryForm(prefix='inc')
    expense_form = ExpenseCategoryForm(prefix='exp')

    if request.method == 'POST':
        if 'save_income' in request.POST:
            income_form = IncomeCategoryForm(request.POST, prefix='inc')
            if income_form.is_valid():
                income_form.save()
                messages.success(request, 'Income category saved.')
                return _hx_redirect(request, 'crm:finance_settings_categories')
        elif 'save_expense' in request.POST:
            expense_form = ExpenseCategoryForm(request.POST, prefix='exp')
            if expense_form.is_valid():
                expense_form.save()
                messages.success(request, 'Expense category saved.')
                return _hx_redirect(request, 'crm:finance_settings_categories')
        elif 'toggle_income' in request.POST:
            pk = request.POST.get('toggle_income')
            cat = get_object_or_404(IncomeCategory, pk=pk)
            cat.active = not cat.active
            cat.save(update_fields=['active'])
            return _hx_redirect(request, 'crm:finance_settings_categories')
        elif 'toggle_expense' in request.POST:
            pk = request.POST.get('toggle_expense')
            cat = get_object_or_404(ExpenseCategory, pk=pk)
            cat.active = not cat.active
            cat.save(update_fields=['active'])
            return _hx_redirect(request, 'crm:finance_settings_categories')

    return render(
        request,
        'crm/finance/settings_categories.html',
        {
            'income_categories': IncomeCategory.objects.order_by('name'),
            'expense_categories': ExpenseCategory.objects.order_by('name'),
            'income_form': income_form,
            'expense_form': expense_form,
            'page_title': 'Categories',
        },
    )


@login_required
@finance_admin_required
def finance_settings_bank_accounts(request):
    """Read-only list of bank accounts used on Income (free-text field)."""
    rows = list(
        Income.objects.exclude(bank_account='')
        .values('bank_account')
        .annotate(count=Count('id'), total=Sum('amount'))
        .order_by('bank_account')
    )
    return render(
        request,
        'crm/finance/settings_bank_accounts.html',
        {
            'bank_rows': rows,
            'page_title': 'Bank Accounts',
        },
    )
