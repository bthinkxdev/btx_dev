"""The AI business staff: a Dashboard tab and an Analyst tab for the owner, plus goals and business-plan upload."""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.files.base import ContentFile
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import audit, india, llm, permissions
from crm.core.views import json_body, staff_required
from crm.models import BusinessGoal, BusinessPlan, DeskAction, DeskBriefing
from crm.models.desk import METRIC_CHOICES, MONEY_METRICS

from . import assistant, metrics, plan as plan_svc, rules

logger = logging.getLogger('crm.desk')
MODULE = 'business_desk'


def _gate(request, action: str = 'view') -> None:
    """The business staff sees the whole company, so company-wide access is required, not just the permission."""
    permissions.require(request.user, MODULE, action, request)
    if not permissions.has_scope(request.user, MODULE, action, 'company'):
        permissions.denied(request.user, MODULE, action, request, detail='needs company-wide access')


def _month(value: str | None) -> date:
    try:
        d = datetime.strptime((value or '')[:7], '%Y-%m').date()
    except ValueError:
        d = india.today()
    return india.month_start(d)


# ------------------------------------------------------------------ the two tabs
def home_url() -> str:
    """The business analytics AI is the second tab of the owner dashboard, not a page of its own."""
    return f"{reverse('crm:dashboard_owner')}?tab=ai"


@staff_required
def desk_home(request):
    """Old address: send people to the owner dashboard's AI tab."""
    return redirect(home_url())


def render_home(request):
    """Draws the AI tab. Called by the owner dashboard view when it is asked for ?tab=ai."""
    _gate(request)
    today = india.today()
    ctx = metrics.full_context(today)
    if not DeskAction.objects.filter(for_date=today).exists():
        rules.suggest_actions(ctx, today)        # first visit of the day: fill the suggestions list once
    comp = [ctx['compare'][k] for k in ('leads_created', 'leads_won', 'won_value', 'collected', 'invoiced', 'delivered',
                                       'clients_onboarded', 'leads_lost')]
    team = sorted(ctx['team'], key=lambda p: ({'red': 0, 'amber': 1, 'green': 2}[p['status']], p['name']))
    active_plan = plan_svc.active_plan()
    return render(request, 'crm/desk/index.html', {
        'ctx': ctx, 'comp': comp, 'team': team, 'trend': metrics.trend(today),
        'briefing': assistant.latest_briefing(today),
        'actions': DeskAction.objects.filter(for_date=today, status='suggested').select_related('assignee', 'lead'),
        'handled_actions': DeskAction.objects.filter(for_date=today).exclude(status='suggested').select_related('assignee')[:6],
        'strengths': rules.strengths(ctx), 'ai_on': llm.configured(), 'plan': active_plan,
        'can_configure': permissions.has_perm(request.user, MODULE, 'configure'),
        'can_approve': permissions.has_perm(request.user, MODULE, 'approve'),
        'suggestions': ['What happened today?', 'Who is delayed and who is going well?', 'What should I do today?',
                        'What is planned for tomorrow?', 'Which customers might we lose?',
                        'Compare today with yesterday', 'What are our weak areas against the plan?',
                        'Plan for next month'],
        'today': today,
    })


@staff_required
@require_POST
def desk_ask(request):
    if not (permissions.has_perm(request.user, MODULE, 'view') and permissions.has_scope(request.user, MODULE, 'view', 'company')):
        return JsonResponse({'ok': False, 'error': 'You do not have access to the business staff.'}, status=403)
    data = json_body(request)
    history = data.get('history') if isinstance(data.get('history'), list) else []
    question = str(data.get('question', ''))
    try:
        result = assistant.ask(question, history)
    except Exception:
        logger.exception('Business staff could not answer')
        return JsonResponse({'ok': False, 'error': 'Something went wrong reading the numbers. Please try again.'}, status=500)
    audit.log('desk.asked', request.user, actor=request.user, changes={'q': question[:120], 'intents': result['intents']})
    return JsonResponse({'ok': True, **result})


@staff_required
@require_POST
def desk_briefing_generate(request):
    _gate(request)
    key = f'desk:brief:{request.user.pk}'
    if not cache.add(key, 1, 45):                 # a double-click must not cost two AI calls
        messages.info(request, 'A briefing was just written. Give it a few seconds.')
        return redirect(home_url())
    assistant.generate_briefing('manual')
    messages.success(request, 'Briefing written from the latest numbers.')
    return redirect(home_url())


@staff_required
@require_POST
def desk_actions_refresh(request):
    _gate(request)
    made = rules.suggest_actions(metrics.full_context(fresh=True))
    messages.success(request, f'{made} new suggestion{"" if made == 1 else "s"} added.' if made else 'No new suggestions right now.')
    return redirect(home_url())


@staff_required
@require_POST
def desk_action(request, pk, verb):
    _gate(request, 'approve')
    if verb not in ('approve', 'dismiss'):
        return JsonResponse({'ok': False, 'error': 'Unknown action.'}, status=400)
    action = get_object_or_404(DeskAction.objects.select_related('assignee', 'lead'), pk=pk)
    try:
        action = rules.approve(action, request.user) if verb == 'approve' else rules.dismiss(action, request.user)
    except rules.ActionError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=409)
    return JsonResponse({'ok': action.status != 'failed', 'status': action.status, 'result': action.result,
                         'error': action.result if action.status == 'failed' else ''})


# ------------------------------------------------------------------ goals
@staff_required
def desk_goals(request):
    _gate(request)
    month = _month(request.GET.get('month') or request.POST.get('month'))
    if request.method == 'POST':
        _gate(request, 'configure')
        metric = request.POST.get('metric', '')
        try:
            target = Decimal(request.POST.get('target', '').replace(',', '').strip())
        except InvalidOperation:
            target = Decimal(0)
        if metric not in dict(METRIC_CHOICES):
            messages.error(request, 'Choose what the goal measures.')
        elif target <= 0:
            messages.error(request, 'Enter a target above zero.')
        elif metric == 'win_rate' and target > 100:
            messages.error(request, 'A win rate cannot be above 100%.')
        else:
            goal, created = BusinessGoal.objects.update_or_create(
                metric=metric, month=month,
                defaults={'target': target, 'note': request.POST.get('note', '')[:300], 'source': 'manual',
                          'plan': None, 'updated_by': request.user})
            if created:
                goal.created_by = request.user
                goal.save(update_fields=['created_by'])
            audit.log('desk.goal_saved', goal, actor=request.user, changes={'metric': metric, 'target': str(target), 'month': f'{month:%Y-%m}'})
            cache.delete(f'desk:ctx:{india.today().isoformat()}')
            messages.success(request, f'Goal saved: {goal.get_metric_display()} {target:g} for {month:%B %Y}.')
        return redirect(f"{reverse('crm:desk_goals')}?month={month:%Y-%m}")
    progress = metrics.goal_progress(india.today()) if month == india.month_start(india.today()) else []
    return render(request, 'crm/desk/goals.html', {
        'month': month, 'goals': BusinessGoal.objects.filter(month=month), 'progress': progress,
        'metrics': METRIC_CHOICES, 'money_metrics': MONEY_METRICS,
        'can_configure': permissions.has_perm(request.user, MODULE, 'configure'),
        'prev_month': india.add_months(month, -1), 'next_month': india.add_months(month, 1),
    })


@staff_required
@require_POST
def desk_goal_delete(request, pk):
    _gate(request, 'configure')
    goal = get_object_or_404(BusinessGoal, pk=pk)
    month = goal.month
    audit.log('desk.goal_deleted', goal, actor=request.user, changes={'metric': goal.metric, 'target': str(goal.target)})
    goal.delete()
    cache.delete(f'desk:ctx:{india.today().isoformat()}')
    messages.success(request, 'Goal removed.')
    return redirect(f"{reverse('crm:desk_goals')}?month={month:%Y-%m}")


# ------------------------------------------------------------------ business plan
@staff_required
def desk_plan(request):
    _gate(request)
    if request.method == 'POST':
        _gate(request, 'configure')
        upload = request.FILES.get('file')
        if not upload:
            messages.error(request, 'Choose a file to upload.')
            return redirect('crm:desk_plan')
        data = upload.read(plan_svc.MAX_BYTES + 1)
        try:
            plan = plan_svc.create(request.user, request.POST.get('title', '').strip(), upload.name, data, ContentFile(data))
        except plan_svc.PlanError as exc:
            messages.error(request, str(exc))
            return redirect('crm:desk_plan')
        found = len(plan.proposed)
        messages.success(request, f'Read {len(plan.extracted_text):,} characters and found {found} goal{"" if found == 1 else "s"}. '
                                  'Please check them before saving.')
        return redirect('crm:desk_plan_review', pk=plan.pk)
    return render(request, 'crm/desk/plan.html', {
        'plans': BusinessPlan.objects.all(), 'ai_on': llm.configured(), 'allowed': ', '.join(plan_svc.ALLOWED),
        'can_configure': permissions.has_perm(request.user, MODULE, 'configure'),
    })


@staff_required
def desk_plan_review(request, pk):
    _gate(request, 'configure')
    plan = get_object_or_404(BusinessPlan, pk=pk)
    month = india.month_start(india.today())
    if request.method == 'POST':
        rows = []
        for i in range(30):
            metric = request.POST.get(f'metric_{i}')
            if metric is None:
                break
            if request.POST.get(f'keep_{i}') and request.POST.get(f'target_{i}', '').strip():
                rows.append({'metric': metric, 'target': request.POST.get(f'target_{i}'), 'note': request.POST.get(f'note_{i}', ''),
                             'month': _month(request.POST.get(f'month_{i}'))})
        try:
            saved = plan_svc.confirm(plan, request.user, rows, request.POST.get('context_notes', ''), month)
        except plan_svc.PlanError as exc:
            messages.error(request, str(exc))
            return redirect('crm:desk_plan_review', pk=pk)
        cache.delete(f'desk:ctx:{india.today().isoformat()}')
        messages.success(request, f'Plan saved with {saved} goal{"" if saved == 1 else "s"}. The business staff now measures you against it.')
        return redirect(home_url())
    existing = {g.metric: float(g.target) for g in BusinessGoal.objects.filter(month=month)}
    return render(request, 'crm/desk/plan_review.html', {
        'plan': plan, 'month': month, 'metrics': METRIC_CHOICES, 'existing': existing,
        'preview': plan.extracted_text[:2500], 'ai_on': llm.configured(),
        'rows': [{**g, 'label': dict(METRIC_CHOICES)[g['metric']], 'current': existing.get(g['metric'])} for g in plan.proposed],
    })


@staff_required
@require_POST
def desk_plan_reread(request, pk):
    _gate(request, 'configure')
    plan = get_object_or_404(BusinessPlan, pk=pk)
    plan_svc.propose(plan)
    messages.success(request, f'Read the plan again and found {len(plan.proposed)} goal(s).')
    return redirect('crm:desk_plan_review', pk=pk)


@staff_required
@require_POST
def desk_plan_delete(request, pk):
    _gate(request, 'configure')
    plan = get_object_or_404(BusinessPlan, pk=pk)
    audit.log('desk.plan_deleted', plan, actor=request.user, changes={'title': plan.title})
    if plan.file:
        plan.file.delete(save=False)
    plan.delete()
    messages.success(request, 'Plan deleted. Goals already saved from it are kept.')
    return redirect('crm:desk_plan')
