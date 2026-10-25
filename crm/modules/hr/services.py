"""
Employees, onboarding and offboarding, attendance and leave (Part 12.1-12.3).

HR data is the most sensitive in the system: reading salary, bank or ID fields needs the
field-group permission and is logged; reveals require a reason.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from crm.core import audit, india, notify, permissions
from crm.models import (Attendance, AttendanceCorrection, ChecklistItem, Employee, Holiday, LeaveAdjustment, LeaveBalance,
                        LeaveRequest, LeaveType)

ONBOARDING = ['Collect ID and address proof', 'Collect PAN and bank details', 'Sign offer letter and NDA',
              'Create email and system login', 'Assign laptop and assets', 'Add to payroll and salary structure']
OFFBOARDING = ['Serve notice / agree last working day', 'Handover of work', 'Return laptop and assets',
               'Remove system access', 'Full-and-final settlement', 'Issue relieving letter']
CORRECTION_WINDOW_DAYS = 7


class HRError(ValidationError):
    pass


def emp_url(e) -> str:
    return reverse('crm:employee_detail', args=[e.pk])


def employee_for(user):
    return Employee.objects.filter(user=user).first() if user and user.is_authenticated else None


def is_manager_of(user, emp: Employee) -> bool:
    return bool(emp.manager_id and emp.manager.user_id == user.pk)


def can_approve_for(user, emp: Employee) -> bool:
    if emp.user_id == user.pk:
        return False
    return is_manager_of(user, emp) or permissions.get_scope(user, 'leave', 'approve') == 'company'


# ---------------------------------------------------------------- employees

def create_employee(actor, *, create_login=False, role=None, **fields) -> Employee:
    with transaction.atomic():
        e = Employee(**fields, created_by=actor)
        if Employee.objects.filter(work_email__iexact=e.work_email).exists():
            raise HRError('An employee with this work email exists.')
        e.save()
        if create_login:
            User = get_user_model()
            if User.objects.filter(Q(username__iexact=e.work_email) | Q(email__iexact=e.work_email)).exists():
                raise HRError('A login with this email already exists. Link it instead.')
            first, _, last = e.name.partition(' ')
            u = User.objects.create_user(username=e.work_email.lower(), email=e.work_email.lower(), first_name=first, last_name=last)
            u.set_unusable_password()
            u.save()
            from crm.models import UserProfile
            p, _ = UserProfile.objects.get_or_create(user=u)
            p.must_change_password = True
            p.department = e.department
            p.manager = e.manager.user if e.manager_id and e.manager.user_id else None
            p.save()
            if role is not None:
                if not role_grantable(actor, role):
                    raise HRError('You cannot grant a role with more access than your own.')
                p.roles.add(role)
            e.user = u
            e.save(update_fields=['user'])
        for i, title in enumerate(ONBOARDING):
            ChecklistItem.objects.create(employee=e, kind='onboarding', title=title, owner=actor,
                                         due_date=e.joining_date + timedelta(days=min(i, 3)))
        audit.log('employee.created', e, actor=actor, changes={'code': e.code, 'login': create_login})
    return e


def role_grantable(actor, role) -> bool:
    """Cannot grant above own permissions (Part 12.3)."""
    for module, actions in (role.grants or {}).items():
        for action, scope in actions.items():
            mine = permissions.get_scope(actor, module, action)
            if mine is None or permissions.SCOPE_RANK[mine] < permissions.SCOPE_RANK.get(scope, 0):
                return False
    return True


def transfer(e: Employee, actor, *, department=None, manager=None, effective: date, designation: str = ''):
    if manager is not None and manager.pk == e.pk:
        raise HRError('An employee cannot report to themselves.')
    m = manager
    while m is not None:
        if m.manager_id == e.pk:
            raise HRError('This would create a reporting loop.')
        m = m.manager
    old = {'department': str(e.department or ''), 'manager': str(e.manager or ''), 'designation': e.designation}
    with transaction.atomic():
        if department is not None:
            e.department = department
        e.manager = manager
        if designation:
            e.designation = designation
        e.save(update_fields=['department', 'manager', 'designation', 'updated_at'])
        if e.user_id:
            from crm.models import UserProfile
            UserProfile.objects.filter(user_id=e.user_id).update(department=e.department,
                                                                 manager=manager.user if manager and manager.user_id else None)
            permissions.clear_cache(e.user)
        audit.log('employee.transferred', e, actor=actor, changes={'from': old, 'to': {'department': str(e.department or ''),
                                                                                     'manager': str(e.manager or ''), 'designation': e.designation},
                                                                  'effective': effective})


def reveal(e: Employee, actor, group: str, reason: str):
    """Sensitive fields stay masked until revealed with a logged reason (Part 3.3)."""
    if not permissions.can_see(actor, group):
        permissions.denied(actor, 'employees', 'reveal_sensitive', detail=group)
    if len(reason.strip()) < 5:
        raise HRError('Give a reason for revealing this information.')
    audit.log('employee.sensitive_revealed', e, actor=actor, changes={'group': group}, reason=reason)


def complete_item(item: ChecklistItem, actor):
    ChecklistItem.objects.filter(pk=item.pk, done_at__isnull=True).update(done_at=timezone.now(), done_by=actor)
    e = item.employee
    if item.kind == 'onboarding' and not e.checklist.filter(kind='onboarding', done_at__isnull=True).exists() and e.status == 'onboarding':
        Employee.objects.filter(pk=e.pk).update(status='active')
    audit.log('employee.checklist_done', e, actor=actor, changes={'item': item.title})


def offboarding_preview(e: Employee) -> dict:
    if not e.user_id:
        return {'leads': 0, 'tasks': 0, 'tickets': 0, 'followups': 0}
    from crm.models import FollowUp, Lead, Task, Ticket
    from crm.models.leads import OPEN_STAGES
    u = e.user
    return {'leads': Lead.objects.filter(owner=u, stage__in=OPEN_STAGES, archived_at__isnull=True).count(),
            'tasks': Task.objects.filter(assignee=u).exclude(status__in=['done', 'cancelled']).count(),
            'tickets': Ticket.objects.filter(assignee=u, status__in=['open', 'in_progress', 'waiting']).count(),
            'followups': FollowUp.objects.filter(assignee=u, status='pending').count()}


def initiate_offboarding(e: Employee, actor, *, last_day: date, reason: str):
    if e.status == 'exited':
        raise HRError('This employee has already exited.')
    if len(reason.strip()) < 5:
        raise HRError('Give the exit reason.')
    with transaction.atomic():
        Employee.objects.filter(pk=e.pk).update(status='on_notice', last_working_day=last_day, exit_reason=reason[:300])
        for title in OFFBOARDING:
            ChecklistItem.objects.get_or_create(employee=e, kind='offboarding', title=title, defaults={'owner': actor, 'due_date': last_day})
        audit.log('employee.offboarding_started', e, actor=actor, changes={'last_day': last_day}, reason=reason)


def complete_exit(e: Employee, actor) -> dict:
    """On the last working day: reassign open work, deactivate the login and end sessions."""
    summary = {'leads': {}, 'tasks': 0, 'tickets': 0}
    with transaction.atomic():
        e = Employee.objects.select_for_update().get(pk=e.pk)
        if e.status == 'exited':
            return summary
        u = e.user
        if u is not None:
            from crm.models import Task, Ticket
            from crm.modules.leads.services import reassign_open_work
            summary['leads'] = reassign_open_work(u, actor, reason=f'{e.name} left the company')
            to = e.manager.user if e.manager_id and e.manager.user_id else None
            summary['tasks'] = Task.objects.filter(assignee=u).exclude(status__in=['done', 'cancelled']).update(assignee=to)
            summary['tickets'] = Ticket.objects.filter(assignee=u, status__in=['open', 'in_progress', 'waiting']).update(assignee=to)
            u.is_active = False
            u.save(update_fields=['is_active'])
            end_sessions(u)
        Employee.objects.filter(pk=e.pk).update(status='exited', last_working_day=e.last_working_day or india.today())
        audit.log('employee.exited', e, actor=actor, changes={'reassigned': summary})
    mgr = e.manager.user if e.manager_id and e.manager.user_id else None
    notify.notify([mgr] if mgr else notify.owners(), f'{e.name} exited: work reassigned',
                  body=f'Leads {summary["leads"].get("moved", 0) if isinstance(summary["leads"], dict) else 0}, tasks {summary["tasks"]}, '
                       f'tickets {summary["tickets"]}.', url=emp_url(e), event_type='offboarding', level='info', email=True)
    return summary


def end_sessions(user) -> int:
    """Sign a user out everywhere (deactivation, offboarding)."""
    from django.contrib.sessions.models import Session
    from crm.models import UserSession
    keys = list(UserSession.objects.filter(user=user, ended_at__isnull=True).values_list('session_key', flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    return UserSession.objects.filter(user=user, ended_at__isnull=True).update(ended_at=timezone.now())


def exits_due() -> int:
    n = 0
    for e in Employee.objects.filter(status='on_notice', last_working_day__lt=india.today()):
        complete_exit(e, None)
        n += 1
    return n


# ---------------------------------------------------------------- calendar

def is_working_day(d: date) -> bool:
    return d.weekday() != 6 and not Holiday.objects.filter(holiday_date=d).exists()


def working_days(start: date, end: date) -> int:
    holidays = set(Holiday.objects.filter(holiday_date__range=(start, end)).values_list('holiday_date', flat=True))
    n, d = 0, start
    while d <= end:
        if d.weekday() != 6 and d not in holidays:
            n += 1
        d += timedelta(days=1)
    return n


# ---------------------------------------------------------------- attendance

def _flags(e: Employee, check_in, check_out):
    late, ot = False, 0
    if e.shift_id and check_in:
        start = datetime.combine(date.today(), e.shift.start_time) + timedelta(minutes=e.shift.grace_minutes)
        late = datetime.combine(date.today(), check_in) > start
    if e.shift_id and check_out:
        end = datetime.combine(date.today(), e.shift.end_time)
        out = datetime.combine(date.today(), check_out)
        ot = max(0, int((out - end).total_seconds() // 60))
    return late, ot


def mark(e: Employee, actor, *, on: date, status: str, check_in=None, check_out=None, note='', source='manual') -> Attendance:
    if on > india.today():
        raise HRError('Attendance cannot be marked for a future date.')
    if check_in and check_out and check_out <= check_in:
        raise HRError('Check-out must be after check-in.')
    from crm.models import PayrollRun
    if PayrollRun.objects.filter(period=india.month_start(on), status__in=['approved', 'paid', 'partially_paid']).exists():
        raise HRError('Payroll for that month is locked. Corrections go into the next run as adjustments.')
    late, ot = _flags(e, check_in, check_out)
    a, _ = Attendance.objects.update_or_create(employee=e, att_date=on, defaults={
        'status': status, 'check_in': check_in, 'check_out': check_out, 'is_late': late, 'overtime_minutes': ot, 'note': note[:300],
        'marked_by': actor, 'source': source})
    audit.log('attendance.marked', e, actor=actor, changes={'date': on, 'status': status})
    return a


def self_check(e: Employee, actor, direction: str) -> Attendance:
    now = timezone.localtime()
    a = Attendance.objects.filter(employee=e, att_date=now.date()).first()
    if direction == 'in':
        if a and a.check_in:
            raise HRError(f'You already checked in at {a.check_in:%H:%M}.')
        return mark(e, actor, on=now.date(), status='present', check_in=now.time().replace(second=0, microsecond=0), source='self')
    if not a or not a.check_in:
        raise HRError('Check in first.')
    if a.check_out:
        raise HRError(f'You already checked out at {a.check_out:%H:%M}.')
    return mark(e, actor, on=now.date(), status=a.status, check_in=a.check_in,
                check_out=now.time().replace(second=0, microsecond=0), source='self')


def request_correction(e: Employee, actor, *, on: date, status: str, check_in=None, check_out=None, reason: str) -> AttendanceCorrection:
    if on > india.today():
        raise HRError('Cannot correct a future date.')
    if (india.today() - on).days > CORRECTION_WINDOW_DAYS:
        raise HRError(f'Corrections are allowed within {CORRECTION_WINDOW_DAYS} days.')
    if len(reason.strip()) < 5:
        raise HRError('Give a reason.')
    c = AttendanceCorrection.objects.create(employee=e, att_date=on, status_requested=status, check_in=check_in, check_out=check_out,
                                            reason=reason[:300], requested_by=actor)
    mgr = e.manager.user if e.manager_id and e.manager.user_id else None
    notify.notify([mgr] if mgr else notify.users_with_perm('attendance', 'approve'), f'Attendance correction: {e.name} {on:%d-%b}',
                  body=reason, url=reverse('crm:hr_approvals'), event_type='attendance_correction')
    return c


def decide_correction(c: AttendanceCorrection, actor, *, approve: bool, comment: str = ''):
    if c.status != 'pending':
        raise HRError('Already decided.')
    if c.employee.user_id == actor.pk:
        raise HRError('You cannot approve your own correction.')
    if not (is_manager_of(actor, c.employee) or permissions.has_perm(actor, 'attendance', 'approve')):
        permissions.denied(actor, 'attendance', 'approve')
    if not approve and len(comment.strip()) < 10:
        raise HRError('Give a comment of at least 10 characters.')
    with transaction.atomic():
        if approve:
            mark(c.employee, actor, on=c.att_date, status=c.status_requested, check_in=c.check_in, check_out=c.check_out,
                 note=f'Correction: {c.reason}')
        AttendanceCorrection.objects.filter(pk=c.pk).update(status='approved' if approve else 'rejected', decided_by=actor,
                                                            decided_at=timezone.now(), comment=comment[:300])


def import_attendance(upload, actor) -> dict:
    """CSV columns: code, date, status, in, out (device export)."""
    from crm.core.forms import validate_upload
    validate_upload(upload, ('.csv',))
    rows = list(csv.DictReader(io.StringIO(upload.read().decode('utf-8-sig'))))
    done, errors = 0, []
    by_code = {e.code.lower(): e for e in Employee.objects.all()}
    for i, r in enumerate(rows, start=2):
        r = {k.strip().lower(): (v or '').strip() for k, v in r.items() if k}
        e = by_code.get(r.get('code', '').lower())
        if e is None:
            errors.append(f'Row {i}: unknown employee code "{r.get("code", "")}"')
            continue
        try:
            from crm.modules.accounting.banking import parse_date
            d = parse_date(r.get('date', ''))
            st = r.get('status') or 'present'
            if st not in dict(Attendance._meta.get_field('status').choices):
                raise ValueError(f'unknown status "{st}"')
            ci = datetime.strptime(r['in'], '%H:%M').time() if r.get('in') else None
            co = datetime.strptime(r['out'], '%H:%M').time() if r.get('out') else None
            mark(e, actor, on=d, status=st, check_in=ci, check_out=co, source='import')
            done += 1
        except (ValueError, ValidationError) as exc:
            msg = ' '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            errors.append(f'Row {i}: {msg}')
    audit.log('attendance.imported', actor=actor, object_type='attendance', object_id='', changes={'rows': done, 'errors': len(errors)})
    return {'done': done, 'errors': errors}


# ---------------------------------------------------------------- leave

def balance(e: Employee, lt: LeaveType, year: int) -> LeaveBalance:
    b, _ = LeaveBalance.objects.get_or_create(employee=e, leave_type=lt, year=year, defaults={'allocated': lt.annual_quota})
    return b


def leave_days(start: date, end: date, half_start: bool, half_end: bool) -> Decimal:
    days = Decimal(working_days(start, end))
    if half_start and is_working_day(start):
        days -= Decimal('0.5')
    if half_end and end != start and is_working_day(end):
        days -= Decimal('0.5')
    return max(days, Decimal('0'))


def team_clash(e: Employee, start: date, end: date):
    if not e.manager_id:
        return []
    return list(LeaveRequest.objects.filter(employee__manager=e.manager, status='approved', start_date__lte=end, end_date__gte=start)
                .exclude(employee=e).select_related('employee'))


def apply_leave(e: Employee, actor, *, leave_type: LeaveType, start: date, end: date, reason: str, half_start=False, half_end=False,
                attachment=None) -> LeaveRequest:
    if end < start:
        raise HRError('The end date cannot be before the start date.')
    if start.year != end.year:
        raise HRError('Split leave that crosses the year end into two requests.')
    if len(reason.strip()) < 3:
        raise HRError('Give a reason.')
    days = leave_days(start, end, half_start, half_end)
    if days <= 0:
        raise HRError('These dates are holidays or weekly offs only.')
    if leave_type.attachment_after_days and days > leave_type.attachment_after_days and attachment is None:
        raise HRError(f'Attach a document for {leave_type.name} longer than {leave_type.attachment_after_days} days.')
    with transaction.atomic():
        overlap = LeaveRequest.objects.select_for_update().filter(employee=e, status__in=['pending', 'approved'], start_date__lte=end,
                                                                  end_date__gte=start)
        if overlap.exists():
            raise HRError('These dates overlap another leave request.')
        if leave_type.is_paid:
            b = balance(e, leave_type, start.year)
            pending = sum((r.days for r in LeaveRequest.objects.filter(employee=e, leave_type=leave_type, status='pending',
                                                                        start_date__year=start.year)), Decimal('0'))
            if days > b.available - pending:
                raise HRError(f'Not enough {leave_type.name}: {b.available - pending} day(s) available, {days} requested.')
        approver = e.manager.user if e.manager_id and e.manager.user_id else None
        r = LeaveRequest.objects.create(employee=e, leave_type=leave_type, start_date=start, end_date=end, half_day_start=half_start,
                                        half_day_end=half_end, days=days, reason=reason[:500], approver=approver)
        if attachment is not None:
            from crm.modules.common.views import save_attachment
            save_attachment(r, attachment, actor, purpose='leave')
        audit.log('leave.applied', e, actor=actor, changes={'type': leave_type.code, 'from': start, 'to': end, 'days': str(days)})
    targets = [approver] if approver else notify.users_with_perm('leave', 'approve')
    notify.notify(targets, f'Leave request: {e.name} {start:%d-%b} to {end:%d-%b} ({days} d)', body=reason, url=reverse('crm:hr_approvals'),
                  event_type='leave_requested')
    return r


def decide_leave(r: LeaveRequest, actor, *, approve: bool, comment: str = ''):
    if r.status != 'pending':
        raise HRError('This request was already decided.')
    if not can_approve_for(actor, r.employee):
        raise HRError('Only the reporting manager or HR can decide, and never on their own leave.')
    if not approve and len(comment.strip()) < 10:
        raise HRError('Give a comment of at least 10 characters when rejecting.')
    with transaction.atomic():
        r = LeaveRequest.objects.select_for_update().get(pk=r.pk)
        if approve:
            if LeaveRequest.objects.filter(employee=r.employee, status='approved', start_date__lte=r.end_date,
                                           end_date__gte=r.start_date).exists():
                raise HRError('This overlaps approved leave.')
            if r.leave_type.is_paid:
                b = LeaveBalance.objects.select_for_update().get(pk=balance(r.employee, r.leave_type, r.start_date.year).pk)
                if r.days > b.available:
                    raise HRError(f'Only {b.available} day(s) left.')
                b.used += r.days
                b.save(update_fields=['used'])
            d = r.start_date
            while d <= r.end_date:
                if is_working_day(d) and d <= india.today():
                    Attendance.objects.update_or_create(employee=r.employee, att_date=d, defaults={'status': 'leave', 'marked_by': actor,
                                                                                                    'note': f'{r.leave_type.name}'})
                d += timedelta(days=1)
        r.status = 'approved' if approve else 'rejected'
        r.decided_by, r.decided_at, r.comment = actor, timezone.now(), comment[:500]
        r.save(update_fields=['status', 'decided_by', 'decided_at', 'comment'])
        audit.log('leave.approved' if approve else 'leave.rejected', r.employee, actor=actor, changes={'request': r.pk}, reason=comment)
    if r.employee.user_id:
        notify.notify([r.employee.user], f'Leave {r.status}: {r.start_date:%d-%b} to {r.end_date:%d-%b}', body=comment,
                      url=reverse('crm:leave_list'), event_type='leave_decided', level='success' if approve else 'warning')


def cancel_leave(r: LeaveRequest, actor):
    if r.employee.user_id != actor.pk and not permissions.has_perm(actor, 'leave', 'approve'):
        raise HRError('Only the applicant or HR can cancel.')
    if r.status not in ('pending', 'approved'):
        raise HRError('This request cannot be cancelled.')
    if r.status == 'approved' and r.start_date <= india.today() and not permissions.has_perm(actor, 'leave', 'approve'):
        raise HRError('Leave that has started can only be cancelled by HR.')
    with transaction.atomic():
        if r.status == 'approved' and r.leave_type.is_paid:
            b = balance(r.employee, r.leave_type, r.start_date.year)
            LeaveBalance.objects.filter(pk=b.pk).update(used=b.used - r.days)
            Attendance.objects.filter(employee=r.employee, att_date__range=(r.start_date, r.end_date), status='leave').delete()
        LeaveRequest.objects.filter(pk=r.pk).update(status='cancelled')
        audit.log('leave.cancelled', r.employee, actor=actor, changes={'request': r.pk})


def adjust_balance(b: LeaveBalance, actor, *, days: Decimal, reason: str):
    if not permissions.has_perm(actor, 'leave', 'configure'):
        permissions.denied(actor, 'leave', 'configure', detail='adjust balance')
    if len(reason.strip()) < 10:
        raise HRError('Give a reason of at least 10 characters.')
    if b.available + days < 0:
        raise HRError('The balance cannot go below zero.')
    with transaction.atomic():
        LeaveAdjustment.objects.create(balance=b, days=days, reason=reason[:300], created_by=actor)
        LeaveBalance.objects.filter(pk=b.pk).update(adjusted=b.adjusted + days)
        audit.log('leave.balance_adjusted', b.employee, actor=actor, changes={'type': b.leave_type.code, 'days': str(days)}, reason=reason)


def lop_days(e: Employee, start: date, end: date) -> Decimal:
    """Loss-of-pay days in a period: approved unpaid leave plus unexcused absences."""
    lop = Decimal('0')
    for r in LeaveRequest.objects.filter(employee=e, status='approved', leave_type__is_paid=False, start_date__lte=end, end_date__gte=start):
        lop += leave_days(max(r.start_date, start), min(r.end_date, end), r.half_day_start and r.start_date >= start,
                          r.half_day_end and r.end_date <= end)
    absent = Attendance.objects.filter(employee=e, att_date__range=(start, end), status='absent').count()
    half = Attendance.objects.filter(employee=e, att_date__range=(start, end), status='half_day').count()
    return lop + Decimal(absent) + Decimal(half) / 2
