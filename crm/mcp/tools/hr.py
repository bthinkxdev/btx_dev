"""Employee tools. Identity-grade fields (bank account, IFSC, PAN, UAN, date of birth, address) are never
accepted or returned by any tool here, whatever the role allows. Salary figures are returned only when the
calling role holds the `salary` field group (the owner grants it to the Business Manager role)."""
from __future__ import annotations

from datetime import date

from crm.core import permissions
from crm.mcp import context, helpers, serialize
from crm.modules.hr import services as svc

FIELDS = ('code', 'name', 'work_email', 'department', 'designation', 'employment_type', 'joining_date', 'status')
PROFILE_FIELDS = FIELDS + ('phone', 'manager', 'location', 'shift', 'last_working_day', 'exit_reason')
CHECKLIST_FIELDS = ('title', 'owner', 'due_date', 'done_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_employees(status: str = '', limit: int = 100) -> list[dict]:
        """List employees (non-sensitive fields only - no salary or bank details)."""
        from crm.models import Employee
        user = context.current_user()
        qs = permissions.scoped(Employee.objects.all(), user, 'employees', 'view')
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('name'), FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def get_employee(employee_id: int) -> dict:
        """One employee's full working picture: profile, manager, onboarding and offboarding checklists with
        progress, this year's leave balances, the last 30 days of attendance, and (if permitted) salary."""
        from datetime import timedelta

        from django.db.models import Count
        from django.utils import timezone

        from crm.models import Attendance, Employee, LeaveBalance, Payslip
        user = context.current_user()
        e = helpers.get_scoped(user, Employee, employee_id, 'employees', 'view')
        out = serialize.row(e, PROFILE_FIELDS)
        out['checklists'] = {}
        today = timezone.localdate()
        for kind in ('onboarding', 'offboarding'):
            items = list(e.checklist.filter(kind=kind).order_by('due_date', 'id'))
            if items:
                done = sum(1 for i in items if i.done_at)
                out['checklists'][kind] = {
                    'done': done, 'total': len(items), 'complete': done == len(items),
                    'overdue': sum(1 for i in items if not i.done_at and i.due_date and i.due_date < today),
                    'items': serialize.rows(items, CHECKLIST_FIELDS, limit=50)}
        if permissions.has_perm(user, 'leave', 'view'):
            out['leave_balances'] = [
                {'type': str(b.leave_type), 'allocated': str(b.allocated), 'used': str(b.used), 'adjusted': str(b.adjusted)}
                for b in LeaveBalance.objects.filter(employee=e, year=today.year).select_related('leave_type')]
            out['pending_leave_requests'] = e.leave_requests.filter(status='pending').count()
        if permissions.has_perm(user, 'attendance', 'view'):
            since = today - timedelta(days=30)
            counts = Attendance.objects.filter(employee=e, att_date__gte=since).values('status').annotate(n=Count('id'))
            out['attendance_last_30_days'] = {r['status']: r['n'] for r in counts}
            out['late_days_last_30'] = Attendance.objects.filter(employee=e, att_date__gte=since, is_late=True).count()
        if permissions.can_see(user, 'salary') and permissions.has_perm(user, 'payroll', 'view'):
            st = e.salary_structures.order_by('-effective_from').first()
            if st:
                out['salary_structure'] = {'effective_from': st.effective_from.isoformat(), 'lines': [
                    {'component': ln.component.name, 'kind': ln.component.kind, 'value': str(ln.value)}
                    for ln in st.lines.select_related('component')]}
            slip = Payslip.objects.filter(employee=e).select_related('run').order_by('-run__period').first()
            if slip:
                out['latest_payslip'] = {'period': slip.run.period.isoformat(), 'gross': str(slip.gross),
                                         'net': str(slip.net), 'status': slip.status, 'run_status': slip.run.status}
        else:
            out['salary'] = 'Not available to this role.'
        return out

    @helpers.sync_tool(mcp)
    def list_onboarding(include_complete: bool = False) -> list[dict]:
        """Employees being onboarded (or everyone with an onboarding checklist if include_complete): checklist
        progress, overdue steps and the next open step with its owner."""
        from django.utils import timezone

        from crm.models import Employee
        user = context.current_user()
        qs = permissions.scoped(Employee.objects.all(), user, 'employees', 'view')
        qs = qs.filter(checklist__kind='onboarding').distinct() if include_complete else qs.filter(status='onboarding')
        today = timezone.localdate()
        out = []
        for e in qs.order_by('joining_date')[:100]:
            items = list(e.checklist.filter(kind='onboarding').order_by('due_date', 'id'))
            open_items = [i for i in items if not i.done_at]
            nxt = open_items[0] if open_items else None
            out.append({**serialize.row(e, FIELDS), 'done': len(items) - len(open_items), 'total': len(items),
                        'overdue': sum(1 for i in open_items if i.due_date and i.due_date < today),
                        'next_step': nxt.title if nxt else None,
                        'next_step_owner': serialize._value(nxt.owner) if nxt else None,
                        'next_step_due': nxt.due_date.isoformat() if nxt and nxt.due_date else None})
        return out

    @helpers.sync_tool(mcp)
    def hr_overview() -> dict:
        """Headcount by status and department, onboarding in flight, pending leave and the latest payroll
        run's status."""
        from django.db.models import Count

        from crm.models import Employee, LeaveRequest, PayrollRun
        user = context.current_user()
        qs = permissions.scoped(Employee.objects.all(), user, 'employees', 'view')
        out = {'by_status': {r['status']: r['n'] for r in qs.values('status').annotate(n=Count('id'))},
               'by_department': {(r['department__name'] or 'Unassigned'): r['n']
                                 for r in qs.exclude(status='exited').values('department__name').annotate(n=Count('id'))},
               'onboarding_in_progress': qs.filter(status='onboarding').count(),
               'on_notice': qs.filter(status='on_notice').count()}
        if permissions.has_perm(user, 'leave', 'view'):
            out['pending_leave_requests'] = LeaveRequest.objects.filter(status='pending').count()
        if permissions.has_perm(user, 'payroll', 'view'):
            run = PayrollRun.objects.order_by('-period').first()
            if run:
                out['latest_payroll'] = {'period': run.period.isoformat(), 'status': run.status}
        return out

    @helpers.sync_tool(mcp)
    def create_employee(code: str, name: str, work_email: str, joining_date_iso: str, designation: str = '') -> dict:
        """Create an employee record (onboarding status). Salary structure and bank details are
        set up separately by HR in the web app - this tool cannot touch them."""
        user = context.current_user()
        permissions.require(user, 'employees', 'create')
        try:
            e = svc.create_employee(user, code=code, name=name, work_email=work_email,
                                    joining_date=date.fromisoformat(joining_date_iso), designation=designation)
        except svc.HRError as exc:
            raise ValueError(' '.join(exc.messages))
        return serialize.row(e, FIELDS)
