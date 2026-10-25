"""HR pages: employees, attendance, leave, approvals, holidays and shifts (Part 12.2)."""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from crm.core import audit, india, permissions
from crm.core.views import Filter, ListPage, StaffRequiredMixin, staff_required
from crm.models import (Attendance, AttendanceCorrection, AuditLog, ChecklistItem, Department, Employee, Holiday, LeaveBalance,
                        LeaveRequest, LeaveType, Shift)
from crm.models.people import EMPLOYEE_STATUSES

from . import services as svc
from .forms import (AdjustForm, AttendanceForm, CorrectionForm, EmployeeForm, HolidayForm, ImportForm, LeaveForm, NewLoginForm,
                    OffboardForm, RevealForm, ShiftForm, TransferForm)


def _team_q(user):
    """Managers see their direct reports in addition to their own scope."""
    return Q(employee__manager__user=user)


class EmployeeList(ListPage):
    model = Employee
    module = 'employees'
    template_name = 'crm/hr/employee_list.html'
    title = 'Employees'
    search_fields = ('name__icontains', 'code__icontains', 'work_email__icontains', 'designation__icontains')
    select_related = ('department', 'manager')
    default_sort = 'name'
    filters = [Filter('status', 'Status', EMPLOYEE_STATUSES),
               Filter('department', 'Department', lambda r: [(d.pk, d.name) for d in Department.objects.all()], lookup='department_id')]
    quick_filters = {'active': ('Active', lambda qs, r: qs.exclude(status='exited')),
                     'onboarding': ('Onboarding', lambda qs, r: qs.filter(status='onboarding')),
                     'notice': ('On notice', lambda qs, r: qs.filter(status='on_notice'))}
    export_name = 'employees'
    export_columns = [('Code', 'code'), ('Name', 'name'), ('Email', 'work_email'), ('Department', 'department'),
                      ('Designation', 'designation'), ('Manager', 'manager'), ('Joined', 'joining_date'), ('Status', lambda e: e.get_status_display())]

    def dispatch(self, request, *args, **kwargs):
        """The directory (public fields only) is open to all staff (Part 12.2); export still needs the permission."""
        return StaffRequiredMixin.dispatch(self, request, *args, **kwargs)

    def base_queryset(self):
        qs = Employee.objects.select_related('department', 'manager')
        if not permissions.has_perm(self.request.user, 'employees', 'view'):
            qs = qs.exclude(status='exited')
        return qs

    def extra_context(self, qs):
        return {'module_code': 'employees', 'can_create': permissions.has_perm(self.request.user, 'employees', 'create')}


def _can_see(user):
    return lambda g: permissions.can_see(user, g)


@staff_required
def employee_form(request, pk=None):
    permissions.require(request.user, 'employees', 'edit' if pk else 'create', request)
    e = get_object_or_404(Employee, pk=pk) if pk else Employee(joining_date=india.today())
    form = EmployeeForm(request.POST or None, instance=e, can_see=_can_see(request.user))
    login = NewLoginForm(request.POST or None, prefix='login') if not pk else None
    if request.method == 'POST' and form.is_valid() and (login is None or login.is_valid()):
        try:
            if pk:
                obj = form.save(commit=False)
                obj.updated_by = request.user
                obj.save()
                audit.log('employee.saved', obj, actor=request.user, changes={'fields': form.changed_data})
            else:
                data = {f: form.cleaned_data[f] for f in form.cleaned_data if f != 'row_version'}
                obj = svc.create_employee(request.user, create_login=login.cleaned_data['create_login'], role=login.cleaned_data.get('role'), **data)
            messages.success(request, f'{obj.name} saved.')
            return redirect('crm:employee_detail', pk=obj.pk)
        except ValidationError as exc:
            form.add_error(None, ' '.join(exc.messages))
    return render(request, 'crm/hr/employee_form.html', {'form': form, 'login': login, 'e': e})


@staff_required
def employee_detail(request, pk):
    e = get_object_or_404(Employee.objects.select_related('department', 'manager', 'shift', 'user'), pk=pk)
    user = request.user
    is_self = e.user_id == user.pk
    hr = permissions.has_perm(user, 'employees', 'view')
    if not (hr or is_self or svc.is_manager_of(user, e)):
        # Directory card only.
        return render(request, 'crm/hr/employee_card.html', {'e': e})
    tab = request.GET.get('tab', 'overview')
    if tab == 'payroll' and not (permissions.can_see(user, 'salary') or is_self):
        permissions.denied(user, 'payroll', 'view', request, detail='salary tab')
    ctx = {'e': e, 'tab': tab, 'hr': hr, 'is_self': is_self, 'can_edit': permissions.has_perm(user, 'employees', 'edit'),
           'show_tax': permissions.can_see(user, 'tax_ids'), 'show_bank': permissions.can_see(user, 'bank_details'),
           'show_salary': permissions.can_see(user, 'salary') or is_self,
           'revealed': request.session.get(f'reveal:{e.pk}', []),
           'transfer': TransferForm(prefix='tr', initial={'department': e.department_id, 'manager': e.manager_id,
                                                          'designation': e.designation, 'effective': india.today()}),
           'offboard': OffboardForm(prefix='off', initial={'last_day': india.today() + timedelta(days=30)}), 'reveal': RevealForm(prefix='rv'),
           'checklist': e.checklist.select_related('owner', 'done_by'), 'open_modal': request.GET.get('modal', '')}
    if tab == 'attendance':
        ctx['attendance'] = e.attendance.all()[:62]
    elif tab == 'leave':
        year = india.today().year
        ctx['balances'] = [svc.balance(e, lt, year) for lt in LeaveType.objects.filter(is_active=True)]
        ctx['requests'] = e.leave_requests.select_related('leave_type')[:50]
    elif tab == 'payroll':
        from crm.models import SalaryStructure
        ctx['structures'] = SalaryStructure.objects.filter(employee=e).prefetch_related('lines__component')
        ctx['payslips'] = e.payslips.select_related('run').exclude(run__status='reversed')[:24]
        audit.log('employee.salary_viewed', e, actor=user)
    elif tab == 'documents':
        from django.contrib.contenttypes.models import ContentType
        from crm.models import Attachment
        ctx['attachments'] = Attachment.objects.filter(content_type=ContentType.objects.get_for_model(Employee), object_id=str(e.pk),
                                                       archived_at__isnull=True)
    elif tab == 'activity':
        ctx['activity'] = AuditLog.objects.filter(object_type='employee', object_id=str(e.pk))[:50]
    if e.status == 'on_notice':
        ctx['preview'] = svc.offboarding_preview(e)
    return render(request, 'crm/hr/employee_detail.html', ctx)


@staff_required
@require_POST
def employee_action(request, pk, action):
    e = get_object_or_404(Employee, pk=pk)
    user = request.user
    try:
        if action == 'reveal':
            f = RevealForm(request.POST, prefix='rv')
            if not f.is_valid():
                raise ValidationError('Choose what to reveal and give a reason.')
            svc.reveal(e, user, f.cleaned_data['group'], f.cleaned_data['reason'])
            key = f'reveal:{e.pk}'
            request.session[key] = sorted(set(request.session.get(key, [])) | {f.cleaned_data['group']})
            messages.success(request, 'Revealed for this session. The access was logged.')
        else:
            permissions.require(user, 'employees', 'edit', request)
            if action == 'transfer':
                f = TransferForm(request.POST, prefix='tr')
                if not f.is_valid():
                    raise ValidationError('Check the transfer details.')
                c = f.cleaned_data
                svc.transfer(e, user, department=c.get('department'), manager=c.get('manager'), effective=c['effective'],
                             designation=c.get('designation', ''))
                messages.success(request, 'Transfer recorded. Approval routing follows the new manager.')
            elif action == 'offboard':
                f = OffboardForm(request.POST, prefix='off')
                if not f.is_valid():
                    raise ValidationError('Give the last working day and reason.')
                svc.initiate_offboarding(e, user, last_day=f.cleaned_data['last_day'], reason=f.cleaned_data['reason'])
                messages.success(request, 'Offboarding started. Open work is reassigned on the last working day.')
            elif action == 'exit_now':
                s = svc.complete_exit(e, user)
                moved = s['leads'].get('moved', 0) if isinstance(s['leads'], dict) else 0
                messages.success(request, f'Exit completed. Reassigned {moved} leads, {s["tasks"]} tasks, {s["tickets"]} tickets; login disabled.')
            elif action == 'check':
                item = get_object_or_404(ChecklistItem, pk=request.POST.get('item'), employee=e)
                svc.complete_item(item, user)
                messages.success(request, 'Item completed.')
            elif action == 'activate':
                Employee.objects.filter(pk=e.pk, status='onboarding').update(status='active')
                audit.log('employee.activated', e, actor=user)
                messages.success(request, 'Employee is active.')
            else:
                raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:employee_detail', pk=e.pk)


# ---------------------------------------------------------------- attendance

@staff_required
def attendance(request):
    permissions.require(request.user, 'attendance', 'view', request)
    user = request.user
    me = svc.employee_for(user)
    day = india.today()
    try:
        from datetime import date
        day = date.fromisoformat(request.GET['date']) if request.GET.get('date') else day
    except ValueError:
        pass
    is_hr = permissions.has_perm(user, 'attendance', 'edit')
    form = AttendanceForm(request.POST or None, prefix='att', initial={'on': day})
    corr = CorrectionForm(request.POST or None, prefix='corr', initial={'on': india.today()})
    imp = ImportForm(request.POST or None, request.FILES or None, prefix='imp')
    open_modal = ''
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action in ('in', 'out'):
                if me is None:
                    raise ValidationError('Your login is not linked to an employee record.')
                a = svc.self_check(me, user, action)
                messages.success(request, f'Checked {action} at {(a.check_in if action == "in" else a.check_out):%H:%M}.')
            elif action == 'mark':
                permissions.require(user, 'attendance', 'edit', request)
                if not form.is_valid():
                    open_modal = 'mark'
                    raise ValidationError('Check the attendance details.')
                c = form.cleaned_data
                svc.mark(c['employee'], user, on=c['on'], status=c['status'], check_in=c.get('check_in'), check_out=c.get('check_out'),
                         note=c.get('note', ''))
                messages.success(request, 'Attendance saved.')
            elif action == 'correction':
                if me is None:
                    raise ValidationError('Your login is not linked to an employee record.')
                if not corr.is_valid():
                    open_modal = 'correction'
                    raise ValidationError('Check the correction details.')
                c = corr.cleaned_data
                svc.request_correction(me, user, on=c['on'], status=c['status'], check_in=c.get('check_in'), check_out=c.get('check_out'),
                                       reason=c['reason'])
                messages.success(request, 'Correction requested. Your manager approves it.')
            elif action == 'import':
                permissions.require(user, 'attendance', 'import', request)
                if not imp.is_valid():
                    raise ValidationError('Choose a CSV file.')
                r = svc.import_attendance(imp.cleaned_data['file'], user)
                if r['errors']:
                    messages.warning(request, f'Imported {r["done"]} rows; {len(r["errors"])} problems: ' + '; '.join(r['errors'][:5]))
                else:
                    messages.success(request, f'Imported {r["done"]} rows.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
            if open_modal:
                return render(request, 'crm/hr/attendance.html', _att_ctx(request, me, day, is_hr, form, corr, imp, open_modal), status=400)
        return redirect(f'{reverse("crm:attendance")}?date={day}')
    return render(request, 'crm/hr/attendance.html', _att_ctx(request, me, day, is_hr, form, corr, imp, open_modal))


def _att_ctx(request, me, day, is_hr, form, corr, imp, open_modal):
    today_rec = Attendance.objects.filter(employee=me, att_date=india.today()).first() if me else None
    rows = []
    if is_hr or permissions.get_scope(request.user, 'attendance', 'view') in ('team', 'department', 'company'):
        emps = Employee.objects.exclude(status='exited')
    else:
        emps = Employee.objects.filter(Q(pk=me.pk if me else 0) | Q(manager=me)).exclude(status='exited') if me else Employee.objects.none()
    recs = {a.employee_id: a for a in Attendance.objects.filter(att_date=day, employee__in=emps)}
    for e in emps.order_by('name'):
        rows.append({'e': e, 'a': recs.get(e.pk)})
    return {'me': me, 'today_rec': today_rec, 'day': day, 'rows': rows, 'is_hr': is_hr, 'form': form, 'corr': corr, 'imp': imp,
            'open_modal': open_modal, 'prev': day - timedelta(days=1), 'next': day + timedelta(days=1),
            'holiday': Holiday.objects.filter(holiday_date=day).first(), 'my_month': me.attendance.filter(att_date__gte=india.month_start(day))[:31] if me else []}


# ---------------------------------------------------------------- leave

@staff_required
def leave_list(request):
    permissions.require(request.user, 'leave', 'view', request)
    user = request.user
    me = svc.employee_for(user)
    form = LeaveForm(request.POST or None, request.FILES or None, prefix='lv')
    open_modal = ''
    if request.method == 'POST':
        action = request.POST.get('action', 'apply')
        try:
            if action == 'apply':
                if me is None:
                    raise ValidationError('Your login is not linked to an employee record.')
                if not form.is_valid():
                    open_modal = 'apply'
                    raise ValidationError('Check the leave details.')
                c = form.cleaned_data
                clash = svc.team_clash(me, c['start'], c['end'])
                svc.apply_leave(me, user, leave_type=c['leave_type'], start=c['start'], end=c['end'], reason=c['reason'],
                                half_start=c['half_start'], half_end=c['half_end'], attachment=c.get('attachment'))
                msg = 'Leave requested.'
                if clash:
                    msg += ' Note: ' + ', '.join(r.employee.name for r in clash[:3]) + ' from your team are also away then.'
                messages.success(request, msg)
            elif action == 'cancel':
                r = get_object_or_404(LeaveRequest, pk=request.POST.get('request'))
                svc.cancel_leave(r, user)
                messages.success(request, 'Leave cancelled.')
            elif action == 'adjust':
                b = get_object_or_404(LeaveBalance, pk=request.POST.get('balance'))
                f = AdjustForm(request.POST, prefix='adj')
                if not f.is_valid():
                    raise ValidationError('Give the days and a reason of at least 10 characters.')
                svc.adjust_balance(b, user, days=f.cleaned_data['days'], reason=f.cleaned_data['reason'])
                messages.success(request, 'Balance adjusted.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
            if open_modal:
                return render(request, 'crm/hr/leave.html', _leave_ctx(request, me, form, open_modal), status=400)
        return redirect('crm:leave_list')
    return render(request, 'crm/hr/leave.html', _leave_ctx(request, me, form, request.GET.get('modal', '')))


def _leave_ctx(request, me, form, open_modal):
    user = request.user
    year = india.today().year
    balances = [svc.balance(me, lt, year) for lt in LeaveType.objects.filter(is_active=True)] if me else []
    mine = me.leave_requests.select_related('leave_type')[:30] if me else []
    scope_all = permissions.get_scope(user, 'leave', 'view') == 'company'
    others = LeaveRequest.objects.filter(status='approved', end_date__gte=india.today(), start_date__lte=india.today() + timedelta(days=30))
    if not scope_all:
        others = others.filter(Q(employee__manager=me) | Q(employee__manager=me.manager if me and me.manager_id else None)) if me else others.none()
    all_balances = []
    if permissions.has_perm(user, 'leave', 'configure'):
        all_balances = LeaveBalance.objects.filter(year=year).select_related('employee', 'leave_type').order_by('employee__name')[:300]
    return {'me': me, 'balances': balances, 'mine': mine, 'upcoming': others.select_related('employee', 'leave_type')[:50], 'form': form,
            'open_modal': open_modal, 'all_balances': all_balances, 'adjust': AdjustForm(prefix='adj'), 'today': india.today()}


@staff_required
def hr_approvals(request):
    user = request.user
    me = svc.employee_for(user)
    can_all = permissions.get_scope(user, 'leave', 'approve') == 'company'
    if not can_all and not (me and me.reports.exists()):
        permissions.denied(user, 'leave', 'approve', request)
    leaves = LeaveRequest.objects.filter(status='pending').select_related('employee', 'leave_type').exclude(employee__user=user)
    corrections = AttendanceCorrection.objects.filter(status='pending').select_related('employee').exclude(employee__user=user)
    if not can_all:
        leaves = leaves.filter(employee__manager=me)
        corrections = corrections.filter(employee__manager=me)
    if request.method == 'POST':
        kind = request.POST.get('kind')
        approve = request.POST.get('decision') == 'approve'
        try:
            if kind == 'leave':
                svc.decide_leave(get_object_or_404(leaves, pk=request.POST.get('id')), user, approve=approve, comment=request.POST.get('reason', ''))
            elif kind == 'correction':
                svc.decide_correction(get_object_or_404(corrections, pk=request.POST.get('id')), user, approve=approve,
                                      comment=request.POST.get('reason', ''))
            else:
                raise Http404
            messages.success(request, 'Approved.' if approve else 'Rejected; the employee was told why.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        return redirect('crm:hr_approvals')
    rows = []
    for r in leaves:
        b = svc.balance(r.employee, r.leave_type, r.start_date.year) if r.leave_type.is_paid else None
        rows.append({'r': r, 'balance': b, 'clash': svc.team_clash(r.employee, r.start_date, r.end_date)})
    return render(request, 'crm/hr/approvals.html', {'leaves': rows, 'corrections': corrections})


@staff_required
def holiday_list(request):
    permissions.require(request.user, 'leave', 'configure', request)
    hform = HolidayForm(request.POST or None, prefix='h')
    sform = ShiftForm(request.POST or None, prefix='s')
    if request.method == 'POST':
        which = request.POST.get('form')
        if which == 'holiday' and hform.is_valid():
            h = hform.save()
            audit.log('holiday.added', h, actor=request.user)
            messages.success(request, f'{h} added.')
            return redirect('crm:holiday_list')
        if which == 'shift' and sform.is_valid():
            s = sform.save()
            audit.log('shift.added', s, actor=request.user)
            messages.success(request, f'Shift {s} added.')
            return redirect('crm:holiday_list')
        if which == 'delete_holiday':
            h = get_object_or_404(Holiday, pk=request.POST.get('id'))
            if h.holiday_date < india.today():
                messages.error(request, 'Past holidays are kept for payroll history.')
            else:
                audit.log('holiday.removed', h, actor=request.user)
                h.delete()
                messages.success(request, 'Holiday removed.')
            return redirect('crm:holiday_list')
    year = india.today().year
    return render(request, 'crm/hr/holidays.html', {'holidays': Holiday.objects.filter(holiday_date__year__gte=year - 1),
                                                     'shifts': Shift.objects.all(), 'hform': hform, 'sform': sform, 'today': india.today(),
                                                     'open_modal': 'holiday' if hform.errors else 'shift' if sform.errors else ''})
