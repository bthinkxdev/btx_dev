"""Employee tools, deliberately narrow: only non-sensitive fields are ever accepted or
returned here. No parameter exists for bank_account_number/bank_ifsc/pan/date_of_birth/
salary - the Business Manager role's field_groups exclude salary and bank_details, and this
tool's signature enforces that at the interface level too, not just by permission."""
from __future__ import annotations

from datetime import date

from crm.core import permissions
from crm.mcp import context, helpers, serialize
from crm.modules.hr import services as svc

FIELDS = ('code', 'name', 'work_email', 'department', 'designation', 'employment_type', 'joining_date', 'status')


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
