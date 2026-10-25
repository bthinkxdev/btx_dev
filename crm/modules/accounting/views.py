"""Accounting and banking pages (Part 8.4-8.5, 8.7)."""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import approvals, audit, india, permissions, posting
from crm.core.money import ZERO
from crm.core.views import Filter, ListPage, staff_required
from crm.models import (Account, AuditLog, BankAccount, BankReconciliation, BankStatementImport, BankTransaction, Client,
                        JournalEntry, JournalLine, Period, PostingOutbox, Project, Service)

from . import banking, reports
from . import services as svc
from .forms import (AccountForm, BankAccountForm, CreateFromLineForm, FinaliseForm, JournalHeaderForm, JournalLineFormSet,
                    ReasonDateForm, StatementImportForm, TransferForm)


def _need(request, action='view', module='accounting'):
    permissions.require(request.user, module, action, request)


def _date(raw, default):
    try:
        return date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


PRESETS = [('month', 'This month'), ('last_month', 'Last month'), ('quarter', 'This quarter'), ('fy', 'This financial year'),
           ('last_fy', 'Last financial year')]


def date_range(request):
    today = india.today()
    preset = request.GET.get('preset', '')
    fy_start, fy_end = india.fy_bounds(today)
    if preset == 'month':
        return india.month_start(today), today, preset
    if preset == 'last_month':
        end = india.month_start(today) - timedelta(days=1)
        return india.month_start(end), end, preset
    if preset == 'quarter':
        q0 = india.add_months(fy_start, ((today.month - 4) % 12) // 3 * 3)
        return q0, today, preset
    if preset == 'last_fy':
        s, e = india.fy_bounds(fy_start - timedelta(days=1))
        return s, e, preset
    start = _date(request.GET.get('start'), fy_start)
    end = _date(request.GET.get('end'), today)
    if end < start:
        start, end = end, start
    return start, end, preset or ('fy' if not request.GET.get('start') else '')


def csv_response(name: str, header: list, rows: list):
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{name}-{timezone.localtime():%Y%m%d-%H%M}.csv"'
    resp.write('﻿')
    w = csv.writer(resp)
    w.writerow(header)
    for r in rows:
        w.writerow([("'" + v) if isinstance(v, str) and v[:1] in '=+-@' else v for v in r])
    return resp


def _export_log(request, name, count):
    permissions.require(request.user, 'accounting', 'export', request)
    from crm.models import ExportLog
    ExportLog.objects.create(user=request.user, module='accounting', filters=dict(request.GET.lists()), columns=[name],
                             row_count=count, file_format='csv')
    audit.log('export.created', actor=request.user, object_type='accounting', object_id='', changes={'report': name, 'rows': count})


def _dims(request):
    dims = {}
    for key in ('client', 'project', 'service'):
        v = request.GET.get(key)
        if v and v.isdigit():
            dims[f'{key}_id'] = int(v)
    return dims


def _dim_choices():
    return {'clients': Client.objects.filter(archived_at__isnull=True).order_by('display_name')[:500],
            'projects': Project.objects.order_by('-id')[:300], 'services': Service.objects.order_by('name')}


# ---------------------------------------------------------------- dashboard

@staff_required
def dashboard(request):
    _need(request)
    today = india.today()
    banks = [{'acct': b, 'balance': posting.balance(b.ledger_account), 'unreconciled':
              BankTransaction.objects.filter(bank_account=b, status__in=['unmatched', 'suggested']).count(),
              'last_rec': BankReconciliation.objects.filter(bank_account=b).order_by('-statement_date').first()}
             for b in BankAccount.objects.filter(is_active=True).select_related('ledger_account')]
    gst = {h: posting.balance(posting.account(c), as_of=today) for h, c in
           (('out_cgst', posting.OUTPUT_CGST), ('out_sgst', posting.OUTPUT_SGST), ('out_igst', posting.OUTPUT_IGST),
            ('in_cgst', posting.INPUT_CGST), ('in_sgst', posting.INPUT_SGST), ('in_igst', posting.INPUT_IGST))}
    gst['net'] = gst['out_cgst'] + gst['out_sgst'] + gst['out_igst'] - gst['in_cgst'] - gst['in_sgst'] - gst['in_igst']
    queue = PostingOutbox.objects.filter(status__in=['pending', 'failed'])
    period = svc.get_period(today.year, today.month)
    pl = reports.profit_loss(india.month_start(today), today)
    return render(request, 'crm/accounting/dashboard.html', {
        'banks': banks, 'cash_total': sum((b['balance'] for b in banks), ZERO),
        'ar': posting.balance(posting.account(posting.AR)), 'ap': posting.balance(posting.account(posting.AP)),
        'advances': posting.balance(posting.account(posting.CUSTOMER_ADVANCES)),
        'tds_rec': posting.balance(posting.account(posting.TDS_RECEIVABLE)),
        'tds_pay': posting.balance(posting.account(posting.TDS_PAYABLE)), 'gst': gst, 'queue': queue[:20],
        'queue_count': queue.count(), 'failed_count': queue.filter(status='failed').count(), 'period': period, 'pl': pl,
        'ar_tie': reports.ar_tie_out(today), 'pending_journals': JournalEntry.objects.filter(status='pending_approval').count(),
        'can_retry': permissions.has_perm(request.user, 'accounting', 'approve'),
    })


@staff_required
@require_POST
def retry_postings(request):
    n = svc.retry_postings(request.user)
    left = PostingOutbox.objects.filter(status='failed').count()
    if left:
        messages.warning(request, f'Retried {n} posting(s); {left} still failing. Open each to see why.')
    else:
        messages.success(request, f'Retried {n} posting(s). The queue is clear.')
    return redirect('crm:accounting_dashboard')


# ---------------------------------------------------------------- chart of accounts

@staff_required
def account_list(request):
    _need(request)
    as_of = _date(request.GET.get('as_of'), india.today())
    from collections import defaultdict
    sums = defaultdict(lambda: (ZERO, ZERO))
    for r in JournalLine.objects.filter(posted=True, entry_date__lte=as_of).values('account_id').annotate(d=Sum('debit'), c=Sum('credit')):
        sums[r['account_id']] = (r['d'] or ZERO, r['c'] or ZERO)
    show_inactive = request.GET.get('inactive') == '1'
    groups = {}
    qs = Account.objects.all() if show_inactive else Account.objects.filter(is_active=True)
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__startswith=q) | Q(group__icontains=q))
    for acc in qs.order_by('code'):
        d, c = sums[acc.pk]
        groups.setdefault(acc.get_account_type_display(), []).append({'acc': acc, 'balance': reports.signed(acc, d, c)})
    return render(request, 'crm/accounting/account_list.html', {
        'groups': groups, 'as_of': as_of, 'q': q, 'show_inactive': show_inactive,
        'can_edit': permissions.has_perm(request.user, 'accounting', 'configure') or permissions.has_perm(request.user, 'accounting', 'approve')})


def _can_edit_coa(request):
    if not (permissions.has_perm(request.user, 'accounting', 'configure') or permissions.has_perm(request.user, 'accounting', 'approve')):
        permissions.denied(request.user, 'accounting', 'configure', request)


@staff_required
def account_form(request, pk=None):
    _need(request)
    _can_edit_coa(request)
    acc = get_object_or_404(Account, pk=pk) if pk else Account()
    locked = bool(pk) and svc.account_has_postings(acc)
    form = AccountForm(request.POST or None, instance=acc, locked=locked)
    if request.method == 'POST':
        if form.is_valid():
            try:
                svc.save_account(form.save(commit=False), request.user, is_new=not pk)
                messages.success(request, 'Account saved.')
                return redirect('crm:account_list')
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
        messages.error(request, 'Please fix the highlighted problems.')
    return render(request, 'crm/accounting/account_form.html', {'form': form, 'acc': acc, 'locked': locked,
                                                                'balance': posting.balance(acc) if pk else ZERO})


@staff_required
@require_POST
def account_deactivate(request, pk):
    _need(request)
    _can_edit_coa(request)
    acc = get_object_or_404(Account, pk=pk)
    try:
        svc.deactivate_account(acc, request.user)
        messages.success(request, f'{acc} deactivated.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:account_list')


# ---------------------------------------------------------------- journals

class JournalList(ListPage):
    model = JournalEntry
    module = 'accounting'
    template_name = 'crm/accounting/journal_list.html'
    title = 'Journal entries'
    search_fields = ('number__icontains', 'narration__icontains', 'source_id')
    select_related = ('created_by', 'posted_by')
    default_sort = '-entry_date'
    sort_fields = {'date': 'entry_date', 'amount': 'total_debit'}
    filters = [Filter('status', 'Status', JournalEntry.STATUSES),
               Filter('source_type', 'Source', lambda r: [(s, s.replace('_', ' ').capitalize()) for s in
                                                          JournalEntry.objects.values_list('source_type', flat=True).distinct().order_by('source_type')]),
               Filter('account', 'Account', lambda r: [(a.pk, str(a)) for a in Account.objects.filter(is_active=True)],
                      apply=lambda qs, v, r: qs.filter(lines__account_id=v).distinct())]
    quick_filters = {'manual': ('Manual', lambda qs, r: qs.filter(is_manual=True)),
                     'pending': ('Pending approval', lambda qs, r: qs.filter(status='pending_approval')),
                     'month': ('This month', lambda qs, r: qs.filter(entry_date__gte=india.month_start(india.today()))),
                     'reversals': ('Reversals', lambda qs, r: qs.filter(reversal_of__isnull=False))}
    export_name = 'journals'
    export_columns = [('Number', 'number'), ('Date', 'entry_date'), ('Narration', 'narration'), ('Source', 'source_type'),
                      ('Source id', 'source_id'), ('Debit', 'total_debit'), ('Credit', 'total_credit'),
                      ('Status', lambda j: j.get_status_display()), ('Created by', 'created_by')]

    def extra_context(self, qs):
        return {'module_code': 'journals', 'can_create': permissions.has_perm(self.request.user, 'accounting', 'create'),
                'search_placeholder': 'Search number or narration'}


SOURCE_URLS = {'invoice': 'crm:invoice_detail', 'payment': 'crm:payment_detail', 'paymentlink': 'crm:link_detail',
               'expense': 'crm:expense_detail', 'payrollrun': 'crm:payroll_detail'}


def source_link(je) -> str:
    from django.urls import NoReverseMatch
    name = SOURCE_URLS.get(je.source_type)
    pk = je.source_id
    if je.source_type == 'creditnote':
        from crm.models import CreditNote
        cn = CreditNote.objects.filter(pk=pk).first()
        return reverse('crm:invoice_detail', args=[cn.invoice_id]) if cn else ''
    if je.source_type in ('paymentallocation',):
        from crm.models import PaymentAllocation
        a = PaymentAllocation.objects.filter(pk=pk).first()
        return reverse('crm:payment_detail', args=[a.payment_id]) if a else ''
    if je.source_type == 'refund':
        from crm.models import Refund
        r = Refund.objects.filter(pk=pk).first()
        return reverse('crm:payment_detail', args=[r.payment_id]) if r else ''
    if je.source_type == 'expensepayment':
        from crm.models import ExpensePayment
        ep = ExpensePayment.objects.filter(pk=pk).first()
        name, pk = ('crm:expense_detail', ep.expense_id) if ep else (None, None)
    if not name or not pk or not str(pk).isdigit():
        return ''
    try:
        return reverse(name, args=[int(pk)])
    except NoReverseMatch:
        return ''


@staff_required
def journal_detail(request, pk):
    _need(request)
    je = get_object_or_404(JournalEntry.objects.select_related('created_by', 'posted_by', 'reversal_of'), pk=pk)
    pending = approvals.pending_for(je)
    return render(request, 'crm/accounting/journal_detail.html', {
        'je': je, 'lines': je.lines.select_related('account', 'client', 'project', 'service'),
        'reversed_by': getattr(je, 'reversed_by_entry', None), 'source_url': source_link(je), 'pending': pending,
        'can_decide': approvals.can_decide(request.user, pending)[0] if pending else False,
        'activity': AuditLog.objects.filter(object_type='journalentry', object_id=str(je.pk))[:30],
        'form': ReasonDateForm(prefix='rev'), 'can_reverse': permissions.has_perm(request.user, 'accounting', 'create'),
        'open_modal': request.GET.get('modal', ''),
    })


@staff_required
def journal_create(request):
    _need(request, 'create')
    header = JournalHeaderForm(request.POST or None, prefix='h', initial={'entry_date': india.today()})
    lines = JournalLineFormSet(request.POST or None, prefix='lines')
    if request.method == 'POST':
        if header.is_valid() and lines.is_valid():
            spec = [posting.Line(d['account'], debit=d['debit'], credit=d['credit'], description=d.get('description', ''),
                                 client_id=d['client'].pk if d.get('client') else None,
                                 project_id=d['project'].pk if d.get('project') else None)
                    for d in (f.cleaned_data for f in lines.forms) if d and d.get('account')]
            try:
                je = svc.create_manual_journal(request.user, entry_date=header.cleaned_data['entry_date'],
                                               narration=header.cleaned_data['narration'], lines=spec,
                                               post=request.POST.get('action') != 'draft', message=header.cleaned_data['message'])
            except ValidationError as exc:
                header.add_error(None, ' '.join(exc.messages))
            else:
                msg = {'posted': f'Journal {je.number} posted.', 'draft': 'Saved as draft.',
                       'pending_approval': 'Above the approval limit: sent to an Accountant to approve before posting.'}[je.status]
                messages.success(request, msg)
                return redirect('crm:journal_detail', pk=je.pk)
        messages.error(request, 'Please fix the highlighted problems. Nothing was posted.')
    accounts = [{'id': a.pk, 'label': str(a)} for a in Account.objects.filter(is_active=True, allow_manual=True).order_by('code')]
    return render(request, 'crm/accounting/journal_form.html', {'header': header, 'lines': lines, 'accounts': accounts,
                                                                'limit': svc.CompanySettings.get().journal_approval_above})


@staff_required
@require_POST
def journal_action(request, pk, action):
    _need(request, 'create')
    je = get_object_or_404(JournalEntry, pk=pk)
    try:
        if action == 'reverse':
            form = ReasonDateForm(request.POST, prefix='rev')
            if not form.is_valid():
                raise ValidationError('Give a reason of at least 10 characters.')
            res = svc.request_reversal(je, request.user, form.cleaned_data['reason'], form.cleaned_data.get('on'))
            if isinstance(res, JournalEntry):
                messages.success(request, f'Reversed by {res.number}. The original stays unchanged.')
                return redirect('crm:journal_detail', pk=res.pk)
            messages.success(request, 'Reversal requested. An Accountant must approve it.')
        elif action == 'post':
            svc.post_draft(je, request.user, request.POST.get('message', ''))
            je.refresh_from_db()
            messages.success(request, f'Journal {je.number} posted.' if je.status == 'posted' else 'Sent for approval.')
        elif action == 'discard':
            svc.discard_draft(je, request.user)
            messages.success(request, 'Draft discarded.')
            return redirect('crm:journal_list')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:journal_detail', pk=je.pk)


# ---------------------------------------------------------------- statements

def _range_ctx(request):
    start, end, preset = date_range(request)
    return {'start': start, 'end': end, 'preset': preset, 'presets': PRESETS}


@staff_required
def general_ledger(request):
    _need(request)
    ctx = _range_ctx(request)
    accounts = Account.objects.filter(is_active=True).order_by('code')
    acc = None
    aid = request.GET.get('account')
    if aid and aid.isdigit():
        acc = Account.objects.filter(pk=aid).first()
    data = reports.general_ledger(acc, ctx['start'], ctx['end'], **_dims(request)) if acc else None
    if data and request.GET.get('export') == 'csv':
        _export_log(request, f'ledger {acc.code}', len(data['rows']))
        rows = [[r['line'].entry_date, r['line'].entry.number, r['line'].entry.narration, r['line'].description,
                 r['line'].client or '', r['line'].debit, r['line'].credit, r['balance']] for r in data['rows']]
        return csv_response(f'ledger-{acc.code}', ['Date', 'Journal', 'Narration', 'Line', 'Client', 'Debit', 'Credit', 'Balance'],
                            [['', '', 'Opening balance', '', '', '', '', data['opening']]] + rows)
    return render(request, 'crm/accounting/general_ledger.html', {**ctx, 'accounts': accounts, 'acc': acc, 'data': data,
                                                                  **_dim_choices(), 'dims': request.GET})


@staff_required
def trial_balance(request):
    _need(request)
    as_of = _date(request.GET.get('as_of'), india.today())
    tb = reports.trial_balance(as_of)
    if request.GET.get('export') == 'csv':
        _export_log(request, 'trial balance', len(tb['rows']))
        return csv_response('trial-balance', ['Code', 'Account', 'Type', 'Debit', 'Credit'],
                            [[r['account'].code, r['account'].name, r['account'].get_account_type_display(), r['debit'], r['credit']]
                             for r in tb['rows']] + [['', 'Total', '', tb['debit'], tb['credit']]])
    compare = None
    cmp_raw = request.GET.get('compare')
    if cmp_raw:
        c = reports.trial_balance(_date(cmp_raw, as_of))
        prev = {r['account'].pk: r['debit'] - r['credit'] for r in c['rows']}
        compare = {'as_of': c['as_of'], 'map': prev}
        for r in tb['rows']:
            r['prev'] = prev.get(r['account'].pk, ZERO)
            r['change'] = (r['debit'] - r['credit']) - r['prev']
    return render(request, 'crm/accounting/trial_balance.html', {'tb': tb, 'as_of': as_of, 'compare': compare})


@staff_required
def profit_loss(request):
    _need(request)
    ctx = _range_ctx(request)
    dims = _dims(request)
    pl = reports.profit_loss(ctx['start'], ctx['end'], **dims)
    ps, pe = reports.previous_period(ctx['start'], ctx['end'])
    prev = reports.profit_loss(ps, pe, **dims)
    prev_map = {r['account'].pk: r['amount'] for sec in ('income', 'direct_cost', 'expense') for r in prev[sec]}
    if request.GET.get('export') == 'csv':
        rows = []
        for title, key in (('Income', 'income'), ('Direct costs', 'direct_cost'), ('Expenses', 'expense')):
            for r in pl[key]:
                rows.append([title, r['account'].code, r['account'].name, r['amount'], prev_map.get(r['account'].pk, ZERO)])
        rows += [['', '', 'Gross profit', pl['gross_profit'], prev['gross_profit']], ['', '', 'Net profit', pl['net_profit'], prev['net_profit']]]
        _export_log(request, 'profit and loss', len(rows))
        return csv_response('profit-and-loss', ['Section', 'Code', 'Account', f'{ctx["start"]} to {ctx["end"]}', f'{ps} to {pe}'], rows)
    return render(request, 'crm/accounting/profit_loss.html', {**ctx, 'pl': pl, 'prev': prev, 'prev_map': prev_map, 'ps': ps, 'pe': pe,
                                                               **_dim_choices(), 'dims': request.GET})


@staff_required
def balance_sheet(request):
    _need(request)
    as_of = _date(request.GET.get('as_of'), india.today())
    bs = reports.balance_sheet(as_of)
    if request.GET.get('export') == 'csv':
        rows = []
        for title, key in (('Assets', 'assets'), ('Liabilities', 'liabilities'), ('Equity', 'equity')):
            for group, items in bs[key].items():
                for r in items:
                    rows.append([title, group, r['account'].code, r['account'].name, r['amount']])
        rows += [['Equity', 'Profit', '', 'Profit to date', bs['current_profit'] + bs['prior_profit']]]
        _export_log(request, 'balance sheet', len(rows))
        return csv_response('balance-sheet', ['Side', 'Group', 'Code', 'Account', 'Amount'], rows)
    return render(request, 'crm/accounting/balance_sheet.html', {'bs': bs, 'as_of': as_of})


@staff_required
def cash_flow(request):
    _need(request)
    ctx = _range_ctx(request)
    cf = reports.cash_flow(ctx['start'], ctx['end'])
    return render(request, 'crm/accounting/cash_flow.html', {**ctx, 'cf': cf})


@staff_required
def tax_reports(request):
    _need(request)
    ctx = _range_ctx(request)
    if not request.GET.get('start') and not request.GET.get('preset'):
        today = india.today()
        end = india.month_start(today) - timedelta(days=1)
        ctx.update(start=india.month_start(end), end=end, preset='last_month')
    start, end = ctx['start'], ctx['end']
    out = reports.gst_outward(start, end)
    itc = reports.gst_itc(start, end)
    g3b = reports.gstr3b(start, end)
    tds = reports.tds_summary(start, end)
    fmt = request.GET.get('export')
    if fmt == 'gstr1_csv':
        rows = [[i.customer_gstin, i.customer_name or i.client.legal_name, i.number, i.invoice_date, i.grand_total, i.place_of_supply,
                 l.tax_rate, l.taxable_value, l.igst, l.cgst, l.sgst] for i in out['b2b'] for l in i.lines.all() if l.taxability == 'taxable']
        rows += [['B2C', '', '', '', '', r['pos'], r['rate'], r['taxable'], r['igst'], r['cgst'], r['sgst']] for r in out['b2c']]
        _export_log(request, 'GSTR-1', len(rows))
        return csv_response(f'gstr1-{start:%Y%m}', ['GSTIN', 'Receiver', 'Invoice', 'Date', 'Invoice value', 'Place of supply', 'Rate',
                                                    'Taxable', 'IGST', 'CGST', 'SGST'], rows)
    if fmt == 'gstr3b_json':
        _export_log(request, 'GSTR-3B', 1)
        payload = {'period': f'{start:%m%Y}', 'outward_taxable': str(g3b['outward_taxable']),
                   'outward_tax': {k: str(v) for k, v in g3b['outward'].items()}, 'itc': {k: str(v) for k, v in g3b['itc'].items()},
                   'net_payable': {k: str(v) for k, v in g3b['net'].items()}, 'exempt': str(g3b['exempt'])}
        resp = HttpResponse(json.dumps(payload, indent=2), content_type='application/json')
        resp['Content-Disposition'] = f'attachment; filename="gstr3b-{start:%Y%m}.json"'
        return resp
    if fmt == 'itc_csv':
        rows = [[e.number, e.vendor or '', e.vendor_gstin, e.bill_number, e.bill_date, e.amount_before_tax, e.igst, e.cgst, e.sgst]
                for e in itc['bills'].select_related('vendor')]
        _export_log(request, 'ITC register', len(rows))
        return csv_response(f'itc-{start:%Y%m}', ['Expense', 'Vendor', 'Vendor GSTIN', 'Bill', 'Date', 'Taxable', 'IGST', 'CGST', 'SGST'], rows)
    return render(request, 'crm/accounting/tax_reports.html', {**ctx, 'out': out, 'itc': itc, 'g3b': g3b, 'tds': tds})


# ---------------------------------------------------------------- periods

@staff_required
def period_list(request):
    _need(request)
    today = india.today()
    months = []
    m = india.month_start(today)
    for _ in range(15):
        months.append(svc.get_period(m.year, m.month))
        m = india.add_months(m, -1)
    sel = None
    y, mo = request.GET.get('year'), request.GET.get('month')
    if y and mo and y.isdigit() and mo.isdigit() and 1 <= int(mo) <= 12:
        sel = svc.get_period(int(y), int(mo))
    sel = sel or months[1]
    return render(request, 'crm/accounting/period_list.html', {
        'periods': months, 'sel': sel, 'checklist': svc.close_checklist(sel), 'pending': approvals.pending_for(sel),
        'can_close': permissions.has_perm(request.user, 'accounting', 'approve'),
        'is_owner': permissions.has_perm(request.user, 'settings', 'configure')})


@staff_required
@require_POST
def period_action(request, pk, action):
    _need(request, 'approve')
    p = get_object_or_404(Period, pk=pk)
    try:
        if action == 'soft_close':
            svc.soft_close(p, request.user)
            messages.success(request, f'{p} soft-closed. Only accountants can post into it now.')
        elif action == 'close':
            req = svc.request_close(p, request.user, request.POST.get('reason', ''))
            messages.success(request, f'{p} closed.' if req is None else 'Close requested. The Owner must approve.')
        elif action == 'reopen':
            req = svc.request_reopen(p, request.user, request.POST.get('reason', ''))
            messages.success(request, f'{p} reopened.' if req is None else 'Reopen requested. The Owner must approve.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect(f'{reverse("crm:period_list")}?year={p.year}&month={p.month}')


# ---------------------------------------------------------------- banking

@staff_required
def bank_account_list(request):
    _need(request, 'view', 'banking')
    can_add = permissions.has_perm(request.user, 'banking', 'create')
    form = BankAccountForm(request.POST or None, prefix='ba') if request.method == 'POST' and request.POST.get('form') == 'add' else BankAccountForm(prefix='ba')
    tform = TransferForm(request.POST or None, prefix='tr', initial={'on': india.today()}) if request.method == 'POST' and request.POST.get('form') == 'transfer' else TransferForm(prefix='tr', initial={'on': india.today()})
    open_modal = ''
    if request.method == 'POST':
        permissions.require(request.user, 'banking', 'create', request)
        which = request.POST.get('form')
        if which == 'add' and form.is_valid():
            c = form.cleaned_data
            try:
                svc.create_bank_account(request.user, name=c['name'], kind=c['kind'], bank_name=c['bank_name'],
                                        account_number=c['account_number'], ifsc=c['ifsc'], opening_balance=c.get('opening_balance') or ZERO,
                                        opening_date=c.get('opening_date'), is_default_receipt=c['is_default_receipt'])
                messages.success(request, f'Account {c["name"]} added.')
                return redirect('crm:bank_account_list')
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
        elif which == 'transfer' and tform.is_valid():
            c = tform.cleaned_data
            try:
                je = svc.transfer(request.user, from_acct=c['from_acct'], to_acct=c['to_acct'], amount=c['amount'], on=c['on'], note=c['note'])
                messages.success(request, f'Transfer posted as {je.number}.')
                return redirect('crm:bank_account_list')
            except ValidationError as exc:
                tform.add_error(None, ' '.join(exc.messages))
        open_modal = 'add' if which == 'add' else 'transfer'
    rows = []
    for b in BankAccount.objects.select_related('ledger_account'):
        last = BankTransaction.objects.filter(bank_account=b, running_balance__isnull=False).order_by('-txn_date', '-id').first()
        rows.append({'acct': b, 'book': posting.balance(b.ledger_account), 'statement': last.running_balance if last else None,
                     'statement_date': last.txn_date if last else None,
                     'unmatched': BankTransaction.objects.filter(bank_account=b, status__in=['unmatched', 'suggested']).count()})
    return render(request, 'crm/accounting/bank_account_list.html', {'rows': rows, 'form': form, 'tform': tform, 'can_add': can_add,
                                                                     'open_modal': open_modal})


@staff_required
def reconciliation(request):
    _need(request, 'view', 'banking')
    accounts = list(BankAccount.objects.filter(is_active=True).exclude(kind='cash'))
    if not accounts:
        messages.info(request, 'Add a bank account first.')
        return redirect('crm:bank_account_list')
    aid = request.GET.get('account') or request.POST.get('account')
    bank = next((a for a in accounts if str(a.pk) == str(aid)), accounts[0])
    status = request.GET.get('status', 'open')
    txns = BankTransaction.objects.filter(bank_account=bank)
    if status == 'open':
        txns = txns.filter(status__in=['unmatched', 'suggested'])
    elif status in dict(BankTransaction.STATUSES):
        txns = txns.filter(status=status)
    book = list(banking.open_book_lines(bank).order_by('entry_date')[:300])
    as_of = _date(request.GET.get('as_of'), india.today())
    stmt_raw = request.GET.get('statement_balance')
    position = None
    if stmt_raw:
        try:
            from decimal import Decimal
            position = banking.reconciliation_position(bank, as_of, Decimal(stmt_raw))
        except (ValueError, ArithmeticError):
            messages.error(request, 'Enter the statement balance as a number.')
    counts = dict(BankTransaction.objects.filter(bank_account=bank).values_list('status').annotate(n=Count('id')))
    can_edit = permissions.has_perm(request.user, 'banking', 'create')
    return render(request, 'crm/accounting/reconciliation.html', {
        'accounts': accounts, 'bank': bank, 'txns': txns.order_by('txn_date', 'id')[:300], 'book': book, 'status': status,
        'counts': counts, 'import_form': StatementImportForm(), 'create_form': CreateFromLineForm(exclude_account=bank.ledger_account_id),
        'finalise_form': FinaliseForm(initial={'as_of': as_of, 'statement_balance': stmt_raw or ''}), 'position': position,
        'as_of': as_of, 'history': BankReconciliation.objects.filter(bank_account=bank)[:12],
        'imports': BankStatementImport.objects.filter(bank_account=bank)[:5], 'can_edit': can_edit,
        'can_finalise': permissions.has_perm(request.user, 'banking', 'approve'),
        'adjust_accounts': Account.objects.filter(is_active=True, allow_manual=True).exclude(pk=bank.ledger_account_id).order_by('code'),
    })


@staff_required
@require_POST
def reconciliation_action(request, action):
    _need(request, 'import' if action == 'import' else 'create', 'banking')
    bank = get_object_or_404(BankAccount, pk=request.POST.get('account'))
    url = f'{reverse("crm:reconciliation")}?account={bank.pk}'
    try:
        if action == 'import':
            form = StatementImportForm(request.POST, request.FILES)
            if not form.is_valid():
                raise ValidationError(' '.join(e for errs in form.errors.values() for e in errs))
            imp = banking.import_statement(bank, form.cleaned_data['file'], request.user)
            msg = f'Imported {imp.line_count} line(s).'
            if imp.duplicate_count:
                msg += f' {imp.duplicate_count} duplicate(s) skipped.'
            if imp.error_count:
                messages.warning(request, msg + f' {imp.error_count} row(s) could not be read: ' +
                                 '; '.join(f'row {e["row"]}: {e["error"]}' for e in imp.errors[:5]))
            else:
                messages.success(request, msg)
        elif action == 'auto':
            r = banking.auto_match(bank, request.user)
            messages.success(request, f'Auto-match: {r["matched"]} matched, {r["suggested"]} suggested for review.')
        else:
            txn = get_object_or_404(BankTransaction, pk=request.POST.get('txn'), bank_account=bank)
            if action == 'confirm':
                banking.confirm_suggestion(txn, request.user)
                messages.success(request, 'Match confirmed.')
            elif action == 'match':
                ids = [int(x) for x in request.POST.getlist('lines') if x.isdigit()]
                if not ids:
                    raise ValidationError('Select at least one book item.')
                adj = Account.objects.filter(pk=request.POST.get('adjustment_account') or 0).first()
                banking.manual_match(txn, ids, request.user, adjustment_account=adj, reason=request.POST.get('reason', ''))
                messages.success(request, 'Matched.')
            elif action == 'create':
                form = CreateFromLineForm(request.POST, exclude_account=bank.ledger_account_id)
                if not form.is_valid():
                    raise ValidationError('Choose the other side of the entry.')
                je = banking.create_from_line(txn, request.user, account=form.cleaned_data['counter_account'], narration=form.cleaned_data['narration'])
                messages.success(request, f'Entry {je.number} posted and matched.')
            elif action == 'ignore':
                banking.ignore_line(txn, request.user, request.POST.get('reason', ''))
                messages.success(request, 'Line ignored.')
            elif action == 'unmatch':
                banking.unmatch(txn, request.user, request.POST.get('reason', ''))
                messages.success(request, 'Match removed.')
            else:
                raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect(url)


@staff_required
@require_POST
def reconciliation_finalise(request):
    _need(request, 'approve', 'banking')
    bank = get_object_or_404(BankAccount, pk=request.POST.get('account'))
    form = FinaliseForm(request.POST)
    try:
        if not form.is_valid():
            raise ValidationError('Enter the statement date and closing balance.')
        c = form.cleaned_data
        rec = banking.finalise(bank, request.user, as_of=c['as_of'], statement_balance=c['statement_balance'], note=c['note'])
        messages.success(request, f'Reconciled to {rec.statement_date:%d-%b-%Y}. Matched items are locked.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect(f'{reverse("crm:reconciliation")}?account={bank.pk}')


@staff_required
@require_POST
def reconciliation_undo(request, pk):
    rec = get_object_or_404(BankReconciliation, pk=pk)
    try:
        banking.unreconcile(rec, request.user, request.POST.get('reason', ''))
        messages.success(request, 'Statement un-reconciled.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect(f'{reverse("crm:reconciliation")}?account={rec.bank_account_id}')


@staff_required
def reconciliation_report(request, pk):
    _need(request, 'view', 'banking')
    rec = get_object_or_404(BankReconciliation.objects.select_related('bank_account', 'finalised_by'), pk=pk)
    txns = rec.transactions.prefetch_related('matches__journal_line__entry').order_by('txn_date')
    outstanding = [ln for ln in banking.open_book_lines(rec.bank_account, rec.statement_date)]
    return render(request, 'crm/accounting/reconciliation_report.html', {'rec': rec, 'txns': txns, 'outstanding': outstanding})
