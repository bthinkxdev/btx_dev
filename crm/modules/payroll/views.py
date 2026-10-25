"""Payroll, salary structures, payslips and commissions pages (Part 12.4-12.5)."""
from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from crm.core import approvals, audit, india, pdf, permissions
from crm.core.money import ZERO
from crm.core.views import staff_required
from crm.models import (CommissionEntry, CommissionRule, Employee, PayrollRun, Payslip, SalaryStructure, SalaryStructureLine)
from crm.modules.hr.forms import AdjustmentForm, CommissionRuleForm, MarkPaidForm, StructureForm
from crm.modules.hr.services import employee_for

from . import runs
from .commissions import approve_entries


def _need(request, action='view'):
    permissions.require(request.user, 'payroll', action, request)
    if not permissions.can_see(request.user, 'salary'):
        permissions.denied(request.user, 'payroll', action, request, detail='salary field group')


@staff_required
def payroll_list(request):
    _need(request)
    if request.method == 'POST':
        _need(request, 'create')
        try:
            raw = request.POST.get('period', '')
            period = date.fromisoformat(raw + '-01') if len(raw) == 7 else date.fromisoformat(raw)
            run = runs.create_run(period, request.user)
            messages.success(request, f'{run} created as a draft.')
            return redirect('crm:payroll_detail', pk=run.pk)
        except ValueError:
            messages.error(request, 'Choose the month.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        return redirect('crm:payroll_list')
    prev_month = india.add_months(india.month_start(india.today()), -1)
    return render(request, 'crm/payroll/list.html', {'runs': PayrollRun.objects.all()[:36], 'default_period': prev_month,
                                                     'can_create': permissions.has_perm(request.user, 'payroll', 'create'),
                                                     'adj_form': AdjustmentForm(prefix='adj')})


@staff_required
def payroll_detail(request, pk):
    _need(request)
    run = get_object_or_404(PayrollRun, pk=pk)
    pending = approvals.pending_for(run)
    return render(request, 'crm/payroll/detail.html', {
        'run': run, 'slips': run.payslips.select_related('employee'), 'pending': pending,
        'can_decide': approvals.can_decide(request.user, pending)[0] if pending else False,
        'blocking': runs.blocking(run), 'pay_form': MarkPaidForm(prefix='pay'), 'can_edit': permissions.has_perm(request.user, 'payroll', 'edit'),
        'is_owner': permissions.has_perm(request.user, 'settings', 'configure'),
        'slip_total': run.payslips.aggregate(s=Sum('net'))['s'] or ZERO, 'open_modal': request.GET.get('modal', ''),
    })


@staff_required
@require_POST
def payroll_action(request, pk, action):
    _need(request)
    run = get_object_or_404(PayrollRun, pk=pk)
    user = request.user
    try:
        if action == 'recalculate':
            _need(request, 'edit')
            r = runs.recalculate(run, user)
            messages.success(request, f'Recalculated. Net was ₹{r["before"]["net"]:,.2f}, now ₹{r["after"]["net"]:,.2f}.')
        elif action == 'review':
            _need(request, 'edit')
            runs.to_review(run, user)
            messages.success(request, 'Moved to review.')
        elif action == 'submit':
            _need(request, 'edit')
            runs.submit(run, user, request.POST.get('message', ''))
            messages.success(request, 'Submitted for Owner approval.')
        elif action == 'payment_file':
            data = runs.payment_file(run, user)
            resp = HttpResponse(data, content_type='text/csv; charset=utf-8')
            resp['Content-Disposition'] = f'attachment; filename="salary-{run.period:%Y-%m}.csv"'
            return resp
        elif action == 'mark_paid':
            _need(request, 'edit')
            f = MarkPaidForm(request.POST, prefix='pay')
            if not f.is_valid():
                raise ValidationError('Choose the bank account, date and reference.')
            ids = [int(x) for x in request.POST.getlist('slips') if x.isdigit()]
            amount = runs.mark_paid(run, user, slip_ids=ids, bank_account=f.cleaned_data['bank_account'], paid_on=f.cleaned_data['paid_on'],
                                    reference=f.cleaned_data['reference'])
            messages.success(request, f'Marked ₹{amount:,.2f} paid and posted.')
        elif action == 'release':
            n = runs.release_payslips(run, user)
            messages.success(request, f'Payslips released to {n} employees.')
        elif action == 'reverse':
            runs.reverse_run(run, user, request.POST.get('reason', ''))
            messages.success(request, 'Run reversed. Create a new run for the month.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:payroll_detail', pk=run.pk)


@staff_required
@require_POST
def adjustment_add(request):
    _need(request, 'edit')
    f = AdjustmentForm(request.POST, prefix='adj')
    try:
        if not f.is_valid():
            raise ValidationError('Choose the employee, amount and a reason of at least 10 characters.')
        runs.add_adjustment(f.cleaned_data['employee'], request.user, amount=f.cleaned_data['amount'], reason=f.cleaned_data['reason'])
        messages.success(request, 'Adjustment added; it is picked up by the next payroll run.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:payroll_list')


def _payslip_allowed(request, slip):
    if slip.employee.user_id == request.user.pk:
        return slip.run.payslips_released_at is not None
    return permissions.has_perm(request.user, 'payroll', 'view') and permissions.can_see(request.user, 'salary')


@staff_required
def payslip_pdf_view(request, pk):
    slip = get_object_or_404(Payslip.objects.select_related('run', 'employee__department'), pk=pk)
    if not _payslip_allowed(request, slip):
        raise Http404
    audit.log('payslip.downloaded', slip.employee, actor=request.user, changes={'run': str(slip.run)})
    resp = HttpResponse(pdf.payslip_pdf(slip), content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="payslip-{slip.employee.code}-{slip.run.period:%Y-%m}.pdf"'
    return resp


@staff_required
def my_payslips(request):
    me = employee_for(request.user)
    slips = me.payslips.filter(run__payslips_released_at__isnull=False).exclude(run__status='reversed').select_related('run') if me else []
    return render(request, 'crm/payroll/my_payslips.html', {'me': me, 'slips': slips})


@staff_required
def salary_structures(request):
    _need(request, 'edit')
    emp = None
    if request.GET.get('employee') or request.POST.get('employee'):
        emp = get_object_or_404(Employee, pk=request.GET.get('employee') or request.POST.get('employee'))
    form = StructureForm(request.POST or None, prefix='st', initial={'effective_from': india.month_start(india.today())}) if emp else None
    if request.method == 'POST' and emp and form.is_valid():
        values = form.component_values()
        if not any(c.code == 'basic' for c in values):
            form.add_error(None, 'Basic pay is required.')
        else:
            try:
                with transaction.atomic():
                    s = SalaryStructure.objects.create(employee=emp, effective_from=form.cleaned_data['effective_from'],
                                                       note=form.cleaned_data['note'], created_by=request.user)
                    for c, v in values.items():
                        SalaryStructureLine.objects.create(structure=s, component=c, value=v)
                    audit.log('salary.structure_added', emp, actor=request.user, changes={'effective': s.effective_from,
                                                                                         'components': {c.code: str(v) for c, v in values.items()}})
                messages.success(request, f'Salary structure from {s.effective_from:%d-%b-%Y} saved.')
                return redirect(f'{reverse("crm:salary_structures")}?employee={emp.pk}')
            except IntegrityError:
                form.add_error('effective_from', 'A structure with this effective date exists. Use a new date.')
    rows = []
    for e in Employee.objects.exclude(status='exited').order_by('name'):
        s = runs.structure_for(e, india.today())
        rows.append({'e': e, 's': s, 'cost': runs.loaded_cost_rate(e) if s else ZERO})
    history = SalaryStructure.objects.filter(employee=emp).prefetch_related('lines__component') if emp else []
    return render(request, 'crm/payroll/structures.html', {'rows': rows, 'emp': emp, 'form': form, 'history': history})


# ---------------------------------------------------------------- commissions

@staff_required
def commission_list(request):
    permissions.require(request.user, 'commissions', 'view', request)
    user = request.user
    qs = permissions.scoped(CommissionEntry.objects.select_related('user', 'rule', 'lead', 'payment', 'invoice', 'payroll_run'),
                            user, 'commissions', 'view')
    status = request.GET.get('status', '')
    if status in dict(CommissionEntry.STATUSES):
        qs = qs.filter(status=status)
    if request.GET.get('person', '').isdigit():
        qs = qs.filter(user_id=request.GET['person'])
    can_approve = permissions.has_perm(user, 'commissions', 'approve')
    can_rules = permissions.has_perm(user, 'settings', 'configure') or permissions.has_perm(user, 'payroll', 'edit')
    rule_form = CommissionRuleForm(request.POST or None, prefix='rule') if can_rules else None
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'approve':
                if not can_approve:
                    permissions.denied(user, 'commissions', 'approve', request)
                ids = [int(x) for x in request.POST.getlist('ids') if x.isdigit()]
                allowed = permissions.scoped(CommissionEntry.objects.filter(pk__in=ids), user, 'commissions', 'approve')
                n = approve_entries(list(allowed), user)
                messages.success(request, f'{n} entr{"y" if n == 1 else "ies"} approved for the next payroll.')
            elif request.POST.get('action') == 'rule' and rule_form is not None:
                if rule_form.is_valid():
                    r = rule_form.save(commit=False)
                    r.created_by = user
                    r.save()
                    rule_form.save_m2m()
                    audit.log('commission.rule_saved', r, actor=user)
                    messages.success(request, f'Rule "{r.name}" saved. It applies to new events from {r.effective_from:%d-%b-%Y}.')
                else:
                    messages.error(request, 'Check the rule details.')
                    return render(request, 'crm/payroll/commissions.html', _comm_ctx(request, qs, can_approve, rule_form, 'rule'), status=400)
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        return redirect('crm:commission_list')
    return render(request, 'crm/payroll/commissions.html', _comm_ctx(request, qs, can_approve, rule_form, ''))


def _comm_ctx(request, qs, can_approve, rule_form, open_modal):
    totals = {s: qs.filter(status=s).aggregate(t=Sum('amount'))['t'] or ZERO for s, _ in CommissionEntry.STATUSES}
    people = qs.values('user_id', 'user__first_name', 'user__last_name', 'user__username').annotate(t=Sum('amount')).order_by('-t')
    return {'entries': qs[:300], 'totals': totals, 'people': people, 'can_approve': can_approve, 'rule_form': rule_form,
            'rules': CommissionRule.objects.prefetch_related('roles').select_related('service'), 'open_modal': open_modal,
            'statuses': CommissionEntry.STATUSES, 'status': request.GET.get('status', '')}
