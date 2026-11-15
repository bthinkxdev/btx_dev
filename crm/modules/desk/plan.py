"""Business plan upload: pull the text out of a document, let the free AI model propose monthly goals, and keep
nothing until the owner has reviewed and confirmed them.

Small models misread numbers, so a proposal is only ever a draft. The review page shows every goal in an editable
form and nothing reaches the reports until it is saved from there.
"""
from __future__ import annotations

import html
import io
import json
import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction

from crm.core import audit, india, llm
from crm.models import BusinessGoal, BusinessPlan
from crm.models.desk import METRIC_CHOICES

MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT = 60_000
PROMPT_CHARS = 7_000
ALLOWED = ('pdf', 'docx', 'xlsx', 'txt', 'md', 'csv')
METRICS = dict(METRIC_CHOICES)


class PlanError(Exception):
    pass


# ------------------------------------------------------------------ reading documents
def extract_text(name: str, data: bytes) -> str:
    ext = name.lower().rsplit('.', 1)[-1] if '.' in name else ''
    if ext not in ALLOWED:
        raise PlanError('Upload a PDF, Word (.docx), Excel (.xlsx), text or CSV file.')
    if len(data) > MAX_BYTES:
        raise PlanError('That file is larger than 5 MB.')
    try:
        if ext in ('txt', 'md', 'csv'):
            text = data.decode('utf-8-sig', errors='replace')
        elif ext == 'docx':
            text = _docx(data)
        elif ext == 'xlsx':
            text = _xlsx(data)
        else:
            text = _pdf(data)
    except PlanError:
        raise
    except Exception as exc:  # a corrupt or odd file must give a clear message, not a crash
        raise PlanError('That file could not be read. Try saving it again, or paste the text into a .txt file.') from exc
    text = re.sub(r'[ \t]+', ' ', text).replace('\r', '')
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(text) < 20:
        raise PlanError('No readable text was found. A scanned PDF needs to be converted to text first.')
    return text[:MAX_TEXT]


def _docx(data: bytes) -> str:
    # Plain regex on the XML instead of an XML parser: an uploaded file never gets to run entity expansion.
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo('word/document.xml')
        if info.file_size > 25 * 1024 * 1024:
            raise PlanError('That document is too large to read.')
        xml = z.read('word/document.xml').decode('utf-8', errors='ignore')
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<w:tab/>', '\t', xml)
    return html.unescape(re.sub(r'<[^>]+>', '', xml))


def _xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets[:10]:
        lines.append(f'[{ws.title}]')
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 500:
                break
            cells = [str(c).strip() for c in row if c not in (None, '')]
            if cells:
                lines.append(' | '.join(cells))
    return '\n'.join(lines)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PlanError('PDF reading is not installed on this server. Upload a Word, Excel or text file instead.') from exc
    reader = PdfReader(io.BytesIO(data))
    return '\n'.join((page.extract_text() or '') for page in reader.pages[:60])


# ------------------------------------------------------------------ proposing goals
PROPOSE_SYSTEM = """You read a small business's plan and pull out its monthly targets. Reply with ONLY a JSON object and nothing else:
{"summary": "<three sentences: what the business does and what it wants to achieve>",
 "goals": [{"metric": "<allowed metric>", "target": <number for ONE MONTH>, "note": "<where in the plan this came from, under 12 words>"}],
 "priorities": "<up to five short priorities, one per line>"}
Allowed metrics:
revenue_collected = cash received per month in INR
revenue_invoiced = amount billed per month in INR, before tax
won_value = value of deals closed per month in INR
new_leads = new leads per month
new_clients = new clients per month
deliverables_delivered = items delivered to clients per month
win_rate = percent of closed leads that are won
Rules: include a goal only when the plan states a number for it. Convert a yearly figure to monthly by dividing by 12 and a quarterly one by dividing by 3. Amounts are plain INR numbers, no symbols or commas. Never invent a number. If the plan states none, return an empty goals list.

PLAN:
{text}"""

KEYWORDS = {
    'revenue_collected': ('revenue', 'collection', 'turnover', 'income'),
    'won_value': ('sales target', 'deal value', 'won value', 'closed deals', 'bookings'),
    'new_leads': ('leads',),
    'new_clients': ('new clients', 'new customers', 'clients onboard', 'customers onboard', 'acquire'),
    'deliverables_delivered': ('deliverable', 'deliveries'),
    'win_rate': ('conversion', 'win rate'),
}
_NUM = re.compile(r'(?:rs\.?|₹|inr)?\s*([\d][\d,]*(?:\.\d+)?)\s*(crore|cr|lakhs?|lacs?|k|thousand|million|m)?\b', re.I)
_UNIT = {'crore': 10_000_000, 'cr': 10_000_000, 'lakh': 100_000, 'lakhs': 100_000, 'lac': 100_000, 'lacs': 100_000,
         'k': 1_000, 'thousand': 1_000, 'million': 1_000_000, 'm': 1_000_000}


def rule_goals(text: str) -> list[dict]:
    """A rough fallback when the AI is unavailable: read a number off any line that names a metric."""
    found: dict[str, dict] = {}
    for line in re.split(r'[\n.;]', text):
        low = line.lower()
        for metric, words in KEYWORDS.items():
            if metric in found or not any(w in low for w in words):
                continue
            if metric == 'win_rate' and '%' not in line:
                continue
            m = _NUM.search(line)
            if not m:
                continue
            try:
                value = Decimal(m.group(1).replace(',', ''))
            except InvalidOperation:
                continue
            value *= _UNIT.get((m.group(2) or '').lower(), 1)
            if metric != 'win_rate':
                if any(w in low for w in ('year', 'annual', 'fy')):
                    value /= 12
                elif 'quarter' in low:
                    value /= 3
            if value > 0:
                found[metric] = {'metric': metric, 'target': float(round(value, 2)), 'note': line.strip()[:120]}
    return list(found.values())


def _clean_goals(raw) -> list[dict]:
    out, seen = [], set()
    for g in raw if isinstance(raw, list) else []:
        if not isinstance(g, dict) or g.get('metric') not in METRICS or g['metric'] in seen:
            continue
        try:
            target = Decimal(str(g.get('target')).replace(',', ''))
        except InvalidOperation:
            continue
        if target <= 0 or target > Decimal('100000000000'):
            continue
        seen.add(g['metric'])
        out.append({'metric': g['metric'], 'target': float(round(target, 2)), 'note': str(g.get('note') or '')[:200]})
    return out


def _json_object(text: str) -> dict:
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('no JSON')
    return json.loads(text[start:end + 1])


def propose(plan: BusinessPlan) -> BusinessPlan:
    """Fill the plan's summary and proposed goals. Uses the AI when available and rules otherwise."""
    text = plan.extracted_text[:PROMPT_CHARS]
    summary, goals, priorities, engine = '', [], '', 'rules'
    try:
        reply, model = llm.complete([{'role': 'user', 'content': PROPOSE_SYSTEM.replace('{text}', text)}],
                                    max_tokens=700, temperature=0.0, timeout=40)
        data = _json_object(reply)
        summary = str(data.get('summary') or '')[:1200]
        goals = _clean_goals(data.get('goals'))
        priorities = str(data.get('priorities') or '')[:800]
        engine = model
    except (llm.LLMUnavailable, ValueError, TypeError):
        pass
    if not goals:
        goals = _clean_goals(rule_goals(plan.extracted_text))
        if goals and engine != 'rules':
            engine += ' + rules'
        elif goals:
            engine = 'rules'
    plan.summary = summary or plan.summary
    plan.proposed = goals
    plan.proposed_by = engine
    if priorities and not plan.context_notes:
        plan.context_notes = priorities
    plan.status = 'proposed'
    plan.save(update_fields=['summary', 'proposed', 'proposed_by', 'context_notes', 'status', 'updated_at'])
    return plan


def create(user, title: str, filename: str, data: bytes, content) -> BusinessPlan:
    text = extract_text(filename, data)
    plan = BusinessPlan(title=(title or filename.rsplit('.', 1)[0])[:200], original_name=filename[:255], extracted_text=text,
                        created_by=user, updated_by=user)
    plan.file.save(filename[:120], content, save=False)
    plan.save()
    propose(plan)
    audit.log('desk.plan_uploaded', plan, actor=user, changes={'file': filename, 'chars': len(text), 'goals_found': len(plan.proposed)})
    return plan


# ------------------------------------------------------------------ confirming
def confirm(plan: BusinessPlan, user, rows: list[dict], notes: str, month: date | None = None) -> int:
    """Save the goals exactly as the owner edited them. Returns how many were saved."""
    month = month or india.month_start(india.today())
    saved = 0
    with transaction.atomic():
        for r in rows:
            metric = r.get('metric')
            if metric not in METRICS:
                continue
            try:
                target = Decimal(str(r.get('target')).replace(',', '').strip())
            except InvalidOperation:
                raise PlanError(f"'{r.get('target')}' is not a number for {METRICS[metric]}.")
            if target <= 0:
                continue
            row_month = r.get('month') or month
            BusinessGoal.objects.update_or_create(
                metric=metric, month=row_month,
                defaults={'target': target, 'note': str(r.get('note') or '')[:300], 'source': 'plan', 'plan': plan,
                          'updated_by': user, 'created_by': user})
            saved += 1
        BusinessPlan.objects.exclude(pk=plan.pk).filter(is_active=True).update(is_active=False)
        plan.context_notes = notes[:3000]
        plan.status = 'confirmed'
        plan.is_active = True
        plan.updated_by = user
        plan.save(update_fields=['context_notes', 'status', 'is_active', 'updated_by', 'updated_at'])
    audit.log('desk.plan_confirmed', plan, actor=user, changes={'goals_saved': saved})
    return saved


def active_plan() -> BusinessPlan | None:
    return BusinessPlan.objects.filter(is_active=True).first()


def context_text(limit: int = 1500) -> str:
    """The owner's plan, condensed for the AI prompt: summary plus the priorities they wrote or approved."""
    plan = active_plan()
    if not plan:
        return ''
    parts = []
    if plan.summary:
        parts.append(f'About the business: {plan.summary}')
    if plan.context_notes:
        parts.append(f'Owner priorities:\n{plan.context_notes}')
    return '\n'.join(parts)[:limit]
