"""Financial statements and tax reports straight from posted journal lines (Part 8.4)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q, Sum

from crm.core import india, posting
from crm.core.money import ZERO, D
from crm.models import Account, JournalLine

INCOME_TYPES = ('income',)
COST_TYPES = ('direct_cost',)
EXPENSE_TYPES = ('expense',)


def _lines(start=None, end=None, **dims):
    qs = JournalLine.objects.filter(posted=True)
    if start:
        qs = qs.filter(entry_date__gte=start)
    if end:
        qs = qs.filter(entry_date__lte=end)
    for k, v in dims.items():
        if v:
            qs = qs.filter(**{k: v})
    return qs


def _sums(qs) -> dict:
    out = {}
    for row in qs.values('account_id').annotate(d=Sum('debit'), c=Sum('credit')):
        out[row['account_id']] = (D(row['d'] or 0), D(row['c'] or 0))
    return out


def signed(acc: Account, dr: Decimal, cr: Decimal) -> Decimal:
    return dr - cr if acc.is_debit_normal else cr - dr


# ---------------------------------------------------------------- trial balance

def trial_balance(as_of: date, start: date | None = None) -> dict:
    sums = _sums(_lines(start, as_of))
    rows, total_dr, total_cr = [], ZERO, ZERO
    for acc in Account.objects.filter(pk__in=sums.keys()).order_by('code'):
        dr, cr = sums[acc.pk]
        net = dr - cr
        if net == 0 and dr == 0:
            continue
        bal_dr, bal_cr = (net, ZERO) if net > 0 else (ZERO, -net)
        total_dr += bal_dr
        total_cr += bal_cr
        rows.append({'account': acc, 'debit': bal_dr, 'credit': bal_cr, 'turnover_dr': dr, 'turnover_cr': cr})
    return {'rows': rows, 'debit': total_dr, 'credit': total_cr, 'balanced': total_dr == total_cr, 'as_of': as_of}


# ---------------------------------------------------------------- profit and loss

def profit_loss(start: date, end: date, **dims) -> dict:
    sums = _sums(_lines(start, end, **dims))
    sections = {'income': [], 'direct_cost': [], 'expense': []}
    totals = defaultdict(lambda: ZERO)
    for acc in Account.objects.filter(pk__in=sums.keys(), account_type__in=sections.keys()).order_by('code'):
        dr, cr = sums[acc.pk]
        amt = signed(acc, dr, cr)
        if amt == 0:
            continue
        sections[acc.account_type].append({'account': acc, 'amount': amt})
        totals[acc.account_type] += amt
    gross = totals['income'] - totals['direct_cost']
    net = gross - totals['expense']
    margin = (gross * 100 / totals['income']).quantize(Decimal('0.1')) if totals['income'] else None
    return {'income': sections['income'], 'direct_cost': sections['direct_cost'], 'expense': sections['expense'],
            'total_income': totals['income'], 'total_direct': totals['direct_cost'], 'total_expense': totals['expense'],
            'gross_profit': gross, 'net_profit': net, 'gross_margin': margin, 'start': start, 'end': end}


def previous_period(start: date, end: date) -> tuple[date, date]:
    length = (end - start).days + 1
    pend = start - timedelta(days=1)
    return pend - timedelta(days=length - 1), pend


# ---------------------------------------------------------------- balance sheet

def balance_sheet(as_of: date) -> dict:
    sums = _sums(_lines(None, as_of))
    groups = {'asset': defaultdict(list), 'liability': defaultdict(list), 'equity': defaultdict(list)}
    totals = defaultdict(lambda: ZERO)
    profit = ZERO
    for acc in Account.objects.filter(pk__in=sums.keys()).order_by('code'):
        dr, cr = sums[acc.pk]
        amt = signed(acc, dr, cr)
        if acc.account_type in groups:
            if amt:
                groups[acc.account_type][acc.group or 'Other'].append({'account': acc, 'amount': amt})
                totals[acc.account_type] += amt
        elif acc.account_type in INCOME_TYPES:
            profit += amt
        else:
            profit -= amt
    fy_start, _ = india.fy_bounds(as_of)
    current = profit_loss(fy_start, as_of)['net_profit']
    prior = profit - current
    equity_total = totals['equity'] + profit
    return {
        'assets': dict(groups['asset']), 'liabilities': dict(groups['liability']), 'equity': dict(groups['equity']),
        'total_assets': totals['asset'], 'total_liabilities': totals['liability'], 'total_equity': equity_total,
        'current_profit': current, 'prior_profit': prior, 'as_of': as_of,
        'balanced': totals['asset'] == totals['liability'] + equity_total,
        'difference': totals['asset'] - totals['liability'] - equity_total,
    }


# ---------------------------------------------------------------- general ledger

def general_ledger(acc: Account, start: date, end: date, **dims) -> dict:
    before = _lines(None, start - timedelta(days=1), **dims).filter(account=acc).aggregate(d=Sum('debit'), c=Sum('credit'))
    opening = signed(acc, D(before['d'] or 0), D(before['c'] or 0))
    bal = opening
    rows = []
    for ln in _lines(start, end, **dims).filter(account=acc).select_related('entry', 'client').order_by('entry_date', 'entry_id', 'id'):
        bal += signed(acc, ln.debit, ln.credit)
        rows.append({'line': ln, 'balance': bal})
    period = _lines(start, end, **dims).filter(account=acc).aggregate(d=Sum('debit'), c=Sum('credit'))
    return {'account': acc, 'opening': opening, 'closing': bal, 'rows': rows, 'debit': D(period['d'] or 0),
            'credit': D(period['c'] or 0)}


# ---------------------------------------------------------------- cash flow (direct)

CASH_CODES = ('BANK', 'CASH', 'GATEWAY')


def cash_accounts():
    return Account.objects.filter(Q(bank_account__isnull=False) | Q(system_code__in=['CASH', posting.GATEWAY_CLEARING, 'BANK_PRIMARY']))


def cash_flow(start: date, end: date) -> dict:
    accs = list(cash_accounts())
    ids = [a.pk for a in accs]
    labels = {'payment': 'Customer receipts', 'refund': 'Refunds to customers', 'settlement': 'Gateway settlements (internal)',
              'expensepayment': 'Vendor and expense payments', 'tdschallan': 'TDS remitted', 'payrollrun': 'Salaries paid',
              'banktransaction': 'Other bank movements', 'manual': 'Manual journals'}
    flows = defaultdict(lambda: ZERO)
    for ln in _lines(start, end).filter(account_id__in=ids).select_related('entry'):
        key = 'manual' if ln.entry.is_manual else ln.entry.source_type
        flows[key] += ln.debit - ln.credit
    rows = [{'label': labels.get(k, k.replace('_', ' ').capitalize()), 'amount': v} for k, v in flows.items() if v]
    rows.sort(key=lambda r: -abs(r['amount']))
    opening = sum((posting.balance(a, as_of=start - timedelta(days=1)) for a in accs), ZERO)
    closing = sum((posting.balance(a, as_of=end) for a in accs), ZERO)
    return {'rows': rows, 'opening': opening, 'closing': closing, 'net': closing - opening,
            'inflow': sum((r['amount'] for r in rows if r['amount'] > 0), ZERO),
            'outflow': sum((r['amount'] for r in rows if r['amount'] < 0), ZERO)}


# ---------------------------------------------------------------- GST (GSTR-1 / 3B data)

def gst_outward(start: date, end: date) -> dict:
    from crm.models import CreditNote, Invoice
    invs = Invoice.objects.filter(invoice_date__range=(start, end), number__gt='').exclude(status__in=['draft', 'pending_approval']) \
        .prefetch_related('lines').select_related('client')
    b2b, b2c = [], defaultdict(lambda: {'taxable': ZERO, 'cgst': ZERO, 'sgst': ZERO, 'igst': ZERO})
    by_rate = defaultdict(lambda: {'taxable': ZERO, 'cgst': ZERO, 'sgst': ZERO, 'igst': ZERO})
    exempt = ZERO
    for inv in invs:
        if inv.status == 'cancelled' and inv.cancelled_at and inv.cancelled_at.date() <= end:
            continue
        for ln in inv.lines.all():
            if ln.taxability != 'taxable':
                exempt += ln.taxable_value
                continue
            rate = ln.tax_rate.normalize()
            r = by_rate[f'{rate:f}']
            r['taxable'] += ln.taxable_value
            r['cgst'] += ln.cgst
            r['sgst'] += ln.sgst
            r['igst'] += ln.igst
            if inv.customer_gstin:
                continue
            k = (inv.place_of_supply, f'{rate:f}')
            b2c[k]['taxable'] += ln.taxable_value
            b2c[k]['cgst'] += ln.cgst
            b2c[k]['sgst'] += ln.sgst
            b2c[k]['igst'] += ln.igst
        if inv.customer_gstin:
            b2b.append(inv)
    cns = CreditNote.objects.filter(status='issued', note_date__range=(start, end)).select_related('invoice')
    cn_tot = cns.aggregate(t=Sum('taxable_value'), c=Sum('cgst'), s=Sum('sgst'), i=Sum('igst'))
    tot = {k: sum((v[k] for v in by_rate.values()), ZERO) for k in ('taxable', 'cgst', 'sgst', 'igst')}
    return {'b2b': b2b, 'b2c': [{'pos': k[0], 'rate': k[1], **v} for k, v in sorted(b2c.items())],
            'by_rate': [{'rate': k, **v} for k, v in sorted(by_rate.items(), key=lambda x: D(x[0]))],
            'credit_notes': list(cns), 'cn_taxable': D(cn_tot['t'] or 0), 'cn_cgst': D(cn_tot['c'] or 0),
            'cn_sgst': D(cn_tot['s'] or 0), 'cn_igst': D(cn_tot['i'] or 0), 'exempt': exempt, 'total': tot}


def gst_itc(start: date, end: date) -> dict:
    from crm.models import Expense
    qs = Expense.objects.filter(bill_date__range=(start, end), status__in=['approved', 'partially_paid', 'paid'])
    elig = qs.filter(itc_eligibility='eligible').aggregate(c=Sum('cgst'), s=Sum('sgst'), i=Sum('igst'), t=Sum('amount_before_tax'))
    blocked = qs.filter(itc_eligibility='blocked').aggregate(c=Sum('cgst'), s=Sum('sgst'), i=Sum('igst'))
    fees = _lines(start, end).filter(account__system_code=posting.GST_ON_FEES).aggregate(d=Sum('debit'), c=Sum('credit'))
    return {'cgst': D(elig['c'] or 0), 'sgst': D(elig['s'] or 0), 'igst': D(elig['i'] or 0), 'taxable': D(elig['t'] or 0),
            'blocked': D(blocked['c'] or 0) + D(blocked['s'] or 0) + D(blocked['i'] or 0),
            'gateway_fee_gst': D(fees['d'] or 0) - D(fees['c'] or 0), 'bills': qs.filter(itc_eligibility='eligible')}


def gstr3b(start: date, end: date) -> dict:
    out = gst_outward(start, end)
    itc = gst_itc(start, end)
    o = out['total']
    payable = {h: o[h] - out[f'cn_{h}'] - itc[h] for h in ('cgst', 'sgst', 'igst')}
    return {'outward_taxable': o['taxable'] - out['cn_taxable'], 'outward': {h: o[h] - out[f'cn_{h}'] for h in ('cgst', 'sgst', 'igst')},
            'itc': {h: itc[h] for h in ('cgst', 'sgst', 'igst')}, 'net': payable, 'exempt': out['exempt']}


def tds_summary(start: date, end: date) -> dict:
    from crm.models import Expense, TdsChallan, TdsReceivable
    rec = TdsReceivable.objects.filter(payment__payment_date__range=(start, end)).values('section', 'status') \
        .annotate(s=Sum('amount')).order_by('section')
    ded = Expense.objects.filter(bill_date__range=(start, end), tds_amount__gt=0, status__in=['approved', 'partially_paid', 'paid']) \
        .values('tds_section').annotate(s=Sum('tds_amount')).order_by('tds_section')
    paid = TdsChallan.objects.filter(paid_on__range=(start, end)).values('section').annotate(s=Sum('amount')).order_by('section')
    return {'receivable': list(rec), 'deducted': list(ded), 'remitted': list(paid),
            'payable_balance': posting.balance(posting.account(posting.TDS_PAYABLE), as_of=end),
            'receivable_balance': posting.balance(posting.account(posting.TDS_RECEIVABLE), as_of=end)}


# ---------------------------------------------------------------- tie-outs (8.9)

def ar_tie_out(as_of: date) -> dict:
    """Receivables sub-ledger (open invoice balances) against the AR control account."""
    from crm.models import Invoice
    control = posting.balance(posting.account(posting.AR), as_of=as_of)
    sub = Invoice.objects.filter(invoice_date__lte=as_of, status__in=['issued', 'partially_paid']).aggregate(s=Sum('balance'))['s'] or ZERO
    return {'control': control, 'subledger': sub, 'difference': control - sub, 'ok': control == sub}


def ap_tie_out(as_of: date) -> dict:
    from crm.models import Expense
    control = posting.balance(posting.account(posting.AP), as_of=as_of)
    open_bills = Expense.objects.filter(kind__in=['vendor_bill', 'expense'], bill_date__lte=as_of,
                                        status__in=['approved', 'partially_paid'])
    sub = sum((e.balance for e in open_bills), ZERO)
    return {'control': control, 'subledger': sub, 'difference': control - sub, 'ok': control == sub}
