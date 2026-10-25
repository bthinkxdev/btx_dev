"""Expense, vendor, recurring and TDS payable pages (Part 11.4-11.5)."""
from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from crm.core import approvals, audit, india, permissions
from crm.core.money import ZERO
from crm.core.views import Filter, ListPage, get_scoped, staff_required
from crm.models import AuditLog, Expense, ExpenseCategory, RecurringExpense, TdsChallan, Vendor
from crm.models.expenses import EXPENSE_KINDS, EXPENSE_STATUSES

from . import services as svc
from .forms import AllocationFormSet, ChallanForm, ExpenseForm, PayForm, RecurringForm, VendorForm


def _cats(request):
    return [(c.pk, c.name) for c in ExpenseCategory.objects.filter(is_active=True)]


class ExpenseList(ListPage):
    date_fields = {'date': 'bill_date', 'due': 'due_date'}
    model = Expense
    module = 'expenses'
    template_name = 'crm/expenses/list.html'
    title = 'Expenses and bills'
    search_fields = ('number__icontains', 'description__icontains', 'vendor__name__icontains', 'bill_number__icontains')
    select_related = ('vendor', 'employee', 'category', 'project')
    default_sort = '-bill_date'
    sort_fields = {'date': 'bill_date', 'total': 'total', 'due': 'due_date'}
    filters = [Filter('status', 'Status', EXPENSE_STATUSES), Filter('kind', 'Type', EXPENSE_KINDS),
               Filter('category', 'Category', _cats, lookup='category_id'),
               Filter('vendor', 'Vendor', lambda r: [(v.pk, v.name) for v in Vendor.objects.all()[:300]], lookup='vendor_id')]
    quick_filters = {'mine': ('Mine', lambda qs, r: qs.filter(Q(created_by=r.user) | Q(employee=r.user))),
                     'to_pay': ('To pay', lambda qs, r: qs.filter(status__in=['approved', 'partially_paid'])),
                     'overdue': ('Overdue bills', lambda qs, r: qs.filter(status__in=['approved', 'partially_paid'], due_date__lt=india.today())),
                     'unallocated': ('Shared, unallocated', lambda qs, r: qs.filter(classification='shared', allocations__isnull=True)),
                     'reimbursements': ('Reimbursements', lambda qs, r: qs.filter(kind='reimbursement'))}
    export_name = 'expenses'
    export_columns = [('Number', 'number'), ('Type', 'kind'), ('Date', 'bill_date'), ('Vendor', 'vendor'), ('Employee', 'employee'),
                      ('Category', 'category'), ('Description', 'description'), ('Bill no.', 'bill_number'), ('Before tax', 'amount_before_tax'),
                      ('CGST', 'cgst'), ('SGST', 'sgst'), ('IGST', 'igst'), ('TDS', 'tds_amount'), ('Payable', 'payable_amount'),
                      ('Paid', 'paid_amount'), ('Status', lambda e: e.get_status_display())]

    def base_queryset(self):
        return super().base_queryset().distinct()

    def extra_context(self, qs):
        return {'module_code': 'expenses', 'can_create': permissions.has_perm(self.request.user, 'expenses', 'create'),
                'to_pay': sum((e.balance for e in qs.filter(status__in=['approved', 'partially_paid'])), ZERO),
                'search_placeholder': 'Search number, vendor, description'}


def get_expense(request, pk, action='view'):
    return get_scoped(request, Expense, pk, 'expenses', action,
                      queryset=Expense.objects.select_related('vendor', 'employee', 'category', 'project', 'approved_by'))


def _form(request, e, is_new):
    user = request.user
    form = ExpenseForm(request.POST or None, request.FILES or None, instance=e, user=user,
                       can_pick_employee=permissions.has_perm(user, 'expenses', 'approve'),
                       can_set_itc=permissions.has_perm(user, 'accounting', 'approve'))
    dups = []
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        if is_new:
            obj.created_by = user
            if not permissions.has_perm(user, 'accounting', 'approve'):
                obj.itc_eligibility = obj.category.itc_default
        obj.updated_by = user
        if obj.vendor_id and not obj.vendor_gstin:
            obj.vendor_gstin = obj.vendor.gstin
        try:
            svc.recompute(obj, auto_tds=not form.cleaned_data.get('tds_amount'))
            dups = svc.duplicates(obj)
            if dups and not obj.duplicate_override_reason:
                form.add_error('duplicate_override_reason', 'This looks like a duplicate. Check the existing records or give a reason.')
                raise ValidationError('Possible duplicate.')
            with transaction.atomic():
                obj.save()
                if form.cleaned_data.get('proof'):
                    from crm.modules.common.views import save_attachment
                    save_attachment(obj, form.cleaned_data['proof'], user, purpose='bill')
                audit.log('expense.created' if is_new else 'expense.saved', obj, actor=user, changes={'total': str(obj.total)})
            if request.POST.get('submit'):
                try:
                    svc.submit(obj, user)
                    messages.success(request, 'Saved and submitted for approval.')
                except ValidationError as exc:
                    messages.warning(request, 'Saved as draft. ' + ' '.join(exc.messages))
            else:
                messages.success(request, 'Saved as draft.')
            return redirect('crm:expense_detail', pk=obj.pk)
        except ValidationError as exc:
            if 'duplicate' not in ' '.join(exc.messages).lower():
                form.add_error(None, ' '.join(exc.messages))
    elif request.method == 'POST':
        messages.error(request, 'Please fix the highlighted problems. Nothing was saved.')
    cats = {c.pk: {'itc': c.itc_default, 'tds': c.tds_section, 'rate': str(c.tds_rate), 'threshold': str(c.tds_threshold)}
            for c in ExpenseCategory.objects.filter(is_active=True)}
    return render(request, 'crm/expenses/form.html', {'form': form, 'e': e, 'is_new': is_new, 'dups': dups, 'cats': cats,
                                                      'proof_above': svc.CompanySettings.get().expense_proof_above})


@staff_required
def expense_create(request):
    permissions.require(request.user, 'expenses', 'create', request)
    e = Expense(kind=request.GET.get('kind', 'expense') if request.GET.get('kind') in dict(EXPENSE_KINDS) else 'expense')
    if e.kind == 'reimbursement':
        e.employee = request.user
    return _form(request, e, True)


@staff_required
def expense_edit(request, pk):
    e = get_expense(request, pk, 'edit' if permissions.has_perm(request.user, 'expenses', 'edit') else 'create')
    if e.status not in ('draft', 'correction_requested', 'rejected'):
        messages.info(request, 'Only a draft can be edited.')
        return redirect('crm:expense_detail', pk=e.pk)
    return _form(request, e, False)


def _detail_ctx(request, e, open_modal='', pay_form=None, alloc=None):
    from django.contrib.contenttypes.models import ContentType
    from crm.models import Attachment
    user = request.user
    pending = approvals.pending_for(e, 'expense')
    rows = [{'project': a.project, 'client': a.client, 'department': a.department, 'percent': a.percent} for a in e.allocations.all()]
    return {
        'e': e, 'pending': pending, 'can_decide': approvals.can_decide(user, pending)[0] if pending else False,
        'blockers': svc.submit_blockers(e) if e.status in ('draft', 'correction_requested', 'rejected') else [],
        'dups': svc.duplicates(e), 'allocations': e.allocations.select_related('project', 'client', 'department'),
        'payments': e.payments.select_related('bank_account', 'created_by'),
        'pay_form': pay_form or PayForm(prefix='pay', initial={'amount': e.balance, 'payment_date': india.today()}),
        'alloc': alloc or AllocationFormSet(prefix='alloc', initial=rows or None),
        'proofs': Attachment.objects.filter(content_type=ContentType.objects.get_for_model(Expense), object_id=str(e.pk), archived_at__isnull=True),
        'can_edit': e.created_by_id == user.pk or e.employee_id == user.pk or permissions.has_perm(user, 'expenses', 'edit'),
        'can_pay': permissions.has_perm(user, 'expenses', 'edit') and e.status in ('approved', 'partially_paid'),
        'can_reverse': permissions.has_perm(user, 'accounting', 'approve') and e.status == 'approved' and not e.paid_amount,
        'activity': AuditLog.objects.filter(object_type='expense', object_id=str(e.pk))[:30], 'open_modal': open_modal,
        'history': approvals.history_for(e),
    }


@staff_required
def expense_detail(request, pk):
    e = get_expense(request, pk)
    return render(request, 'crm/expenses/detail.html', _detail_ctx(request, e, request.GET.get('modal', '')))


@staff_required
@require_POST
def expense_action(request, pk, action):
    e = get_expense(request, pk)
    user = request.user
    try:
        if action == 'submit':
            svc.submit(e, user, request.POST.get('message', ''))
            messages.success(request, 'Submitted for approval.')
        elif action == 'approve':
            req = approvals.pending_for(e, 'expense')
            if req is None:
                raise ValidationError('Nothing to approve.')
            approvals.approve(req, user, request.POST.get('reason', ''))
            messages.success(request, 'Approved and posted to the ledger.')
        elif action == 'reject':
            req = approvals.pending_for(e, 'expense')
            if req is None:
                raise ValidationError('Nothing to reject.')
            approvals.reject(req, user, request.POST.get('reason', ''))
            messages.success(request, 'Rejected; the submitter was told why.')
        elif action == 'correction':
            svc.request_correction(e, user, request.POST.get('reason', ''))
            messages.success(request, 'Sent back for correction.')
        elif action == 'pay':
            permissions.require(user, 'expenses', 'edit', request)
            form = PayForm(request.POST, prefix='pay')
            if not form.is_valid():
                return render(request, 'crm/expenses/detail.html', _detail_ctx(request, e, 'pay', pay_form=form), status=400)
            c = form.cleaned_data
            svc.pay(e, user, amount=c['amount'], payment_date=c['payment_date'], bank_account=c['bank_account'], method=c['method'],
                    reference=c['reference'])
            messages.success(request, 'Payment recorded and posted.')
        elif action == 'allocate':
            permissions.require(user, 'expenses', 'edit', request)
            fs = AllocationFormSet(request.POST, prefix='alloc')
            if not fs.is_valid():
                return render(request, 'crm/expenses/detail.html', _detail_ctx(request, e, 'allocate', alloc=fs), status=400)
            rows = [f.cleaned_data for f in fs.forms if f.cleaned_data]
            svc.save_allocations(e, rows, user)
            messages.success(request, f'Allocated across {len(rows)} row(s). The expense total is unchanged.')
        elif action == 'reverse':
            svc.reverse_expense(e, user, request.POST.get('reason', ''))
            messages.success(request, 'Expense reversed.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:expense_detail', pk=e.pk)


class ApprovalQueue(ExpenseList):
    template_name = 'crm/expenses/approvals.html'
    action = 'approve'
    title = 'Expense approvals'
    quick_filters = {}

    def base_queryset(self):
        return super().base_queryset().filter(status='submitted').exclude(Q(created_by=self.request.user) | Q(employee=self.request.user))


# ---------------------------------------------------------------- vendors

class VendorList(ListPage):
    model = Vendor
    module = 'vendors'
    template_name = 'crm/expenses/vendor_list.html'
    title = 'Vendors'
    search_fields = ('name__icontains', 'gstin__icontains', 'pan__icontains')
    default_sort = 'name'
    filters = [Filter('status', 'Status', Vendor.STATUSES)]
    export_name = 'vendors'
    export_columns = [('Name', 'name'), ('GSTIN', 'gstin'), ('State', 'state'), ('TDS section', 'tds_section'), ('Status', 'status')]

    def extra_context(self, qs):
        rows = [{'v': v, 'outstanding': svc.vendor_outstanding(v), 'tds': svc.vendor_tds_ytd(v)} for v in qs[:200]]
        return {'module_code': 'vendors', 'rows': rows, 'can_create': permissions.has_perm(self.request.user, 'vendors', 'create'),
                'show_tax': permissions.can_see(self.request.user, 'tax_ids')}


@staff_required
def vendor_form(request, pk=None):
    permissions.require(request.user, 'vendors', 'edit' if pk else 'create', request)
    v = get_object_or_404(Vendor, pk=pk) if pk else Vendor()
    form = VendorForm(request.POST or None, instance=v, show_bank=permissions.can_see(request.user, 'bank_details'))
    dups = []
    if request.method == 'POST' and form.is_valid():
        try:
            obj = svc.save_vendor(form, request.user, confirm_new=bool(request.POST.get('confirm_new')))
            messages.success(request, f'Vendor {obj.name} saved.')
            nxt = request.GET.get('next', '')
            return redirect(nxt if nxt.startswith('/crm/') else reverse('crm:vendor_detail', args=[obj.pk]))
        except svc.DuplicateVendorError as exc:
            dups = exc.dupes
            messages.warning(request, 'A vendor with the same name, PAN or GSTIN exists. Use it, or tick "Create anyway".')
    return render(request, 'crm/expenses/vendor_form.html', {'form': form, 'v': v, 'dups': dups})


@staff_required
def vendor_detail(request, pk):
    permissions.require(request.user, 'vendors', 'view', request)
    v = get_object_or_404(Vendor, pk=pk)
    if request.method == 'POST':
        permissions.require(request.user, 'vendors', 'edit', request)
        try:
            svc.set_vendor_status(v, request.user, request.POST.get('status', ''), request.POST.get('reason', ''))
            messages.success(request, 'Vendor status updated.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        return redirect('crm:vendor_detail', pk=v.pk)
    bills = v.expenses.select_related('category').order_by('-bill_date')[:100]
    return render(request, 'crm/expenses/vendor_detail.html', {
        'v': v, 'bills': bills, 'outstanding': svc.vendor_outstanding(v), 'tds_ytd': svc.vendor_tds_ytd(v),
        'show_bank': permissions.can_see(request.user, 'bank_details'), 'show_tax': permissions.can_see(request.user, 'tax_ids'),
        'can_edit': permissions.has_perm(request.user, 'vendors', 'edit')})


# ---------------------------------------------------------------- recurring

@staff_required
def recurring_list(request):
    permissions.require(request.user, 'expenses', 'approve', request)
    form = RecurringForm(request.POST or None, prefix='rec')
    open_modal = ''
    if request.method == 'POST':
        action = request.POST.get('action', 'add')
        try:
            if action == 'add':
                if form.is_valid():
                    r = form.save(commit=False)
                    r.created_by = request.user
                    r.save()
                    audit.log('recurring_expense.created', r, actor=request.user)
                    messages.success(request, f'{r.name} scheduled from {r.next_run:%d-%b-%Y}.')
                    return redirect('crm:recurring_expense_list')
                open_modal = 'add'
            else:
                r = get_object_or_404(RecurringExpense, pk=request.POST.get('rec'))
                if action == 'run':
                    n = svc.run_recurring(r, request.user)
                    messages.success(request, f'{n} bill(s) created. Running again for the same period creates nothing.')
                elif action in ('pause', 'resume', 'end'):
                    RecurringExpense.objects.filter(pk=r.pk).update(status={'pause': 'paused', 'resume': 'active', 'end': 'ended'}[action])
                    audit.log(f'recurring_expense.{action}', r, actor=request.user)
                    messages.success(request, 'Updated.')
                return redirect('crm:recurring_expense_list')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
    return render(request, 'crm/expenses/recurring.html', {'rows': RecurringExpense.objects.select_related('vendor', 'category'),
                                                           'form': form, 'open_modal': open_modal})


# ---------------------------------------------------------------- TDS payable

@staff_required
def tds_payable(request):
    permissions.require(request.user, 'expenses', 'approve', request)
    from crm.core import posting
    fy = request.GET.get('fy') or india.fy_label(india.today())
    form = ChallanForm(request.POST or None, prefix='ch', initial={'paid_on': india.today()})
    open_modal = ''
    if request.method == 'POST':
        if form.is_valid():
            c = form.cleaned_data
            try:
                svc.record_challan(request.user, section=c['section'], amount=c['amount'], paid_on=c['paid_on'], cin=c['cin'],
                                   bank_account=c['bank_account'])
                messages.success(request, 'Challan recorded and posted.')
                return redirect('crm:tds_payable')
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
        open_modal = 'challan'
    start_year = int(fy[:4])
    from datetime import date
    challans = TdsChallan.objects.filter(paid_on__range=(date(start_year, 4, 1), date(start_year + 1, 3, 31))).select_related('bank_account')
    by_section = Expense.objects.filter(bill_date__range=(date(start_year, 4, 1), date(start_year + 1, 3, 31)), tds_amount__gt=0,
                                        status__in=['approved', 'partially_paid', 'paid']).values('tds_section').annotate(s=Sum('tds_amount')).order_by('tds_section')
    paid = {r['section']: r['s'] for r in challans.values('section').annotate(s=Sum('amount'))}
    sections = [{'section': r['tds_section'], 'deducted': r['s'], 'paid': paid.get(r['tds_section'], ZERO),
                 'due': r['s'] - paid.get(r['tds_section'], ZERO)} for r in by_section]
    fys = sorted({india.fy_label(india.today())} | set(TdsChallan.objects.values_list('fiscal_year', flat=True)), reverse=True)
    return render(request, 'crm/expenses/tds_payable.html', {
        'fy': fy, 'fys': fys, 'sections': sections, 'challans': challans, 'vendors': svc.tds_by_vendor(fy), 'form': form,
        'open_modal': open_modal, 'balance': posting.balance(posting.account(posting.TDS_PAYABLE)),
        'show_tax': permissions.can_see(request.user, 'tax_ids')})
