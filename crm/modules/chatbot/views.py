"""CRM side of the chatbot: website chat history (sales manager + owner) and the AI sales analyst."""
from __future__ import annotations

import json

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from crm.core import audit, llm, permissions
from crm.core.views import staff_required
from crm.models import ChatSession

from . import analyst, knowledge


@staff_required
def chat_list(request):
    permissions.require(request.user, 'chats', 'view', request)
    qs = ChatSession.objects.select_related('lead', 'lead__owner').filter(message_count__gt=1)
    q = request.GET.get('q', '').strip()
    if q:
        for word in q.split()[:5]:
            qs = qs.filter(Q(visitor_name__icontains=word) | Q(phone__icontains=word) | Q(email__icontains=word)
                           | Q(messages__content__icontains=word))
        qs = qs.distinct()
    status = request.GET.get('status', '')
    if status == 'lead':
        qs = qs.filter(lead__isnull=False)
    elif status == 'nolead':
        qs = qs.filter(lead__isnull=True)
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    return render(request, 'crm/chatbot/chat_list.html', {
        'page_obj': page, 'q': q, 'status': status, 'total': page.paginator.count,
        'ai_on': llm.configured(),
        'products': knowledge.PACKAGES,
    })


@staff_required
def chat_detail(request, pk):
    permissions.require(request.user, 'chats', 'view', request)
    session = get_object_or_404(ChatSession.objects.select_related('lead', 'lead__owner'), pk=pk)
    audit.log('chat.viewed', session, actor=request.user, changes={})
    return render(request, 'crm/chatbot/chat_detail.html', {
        'session': session, 'chat_messages': session.messages.all(),
        'interest': knowledge.PACKAGES.get(session.interest, {}).get('name', ''),
    })


@staff_required
def ai_analyst(request):
    permissions.require(request.user, 'ai_analyst', 'view', request)
    days = request.GET.get('days', '30')
    days = days if days in analyst.PERIODS else '30'
    return render(request, 'crm/chatbot/analyst.html', {
        'periods': analyst.PERIODS, 'days': days, 'ai_on': llm.configured(),
        'summary': analyst.builtin_summary(analyst.snapshot(int(days))),
        'suggestions': ['Who is performing best and why?', 'Who needs coaching right now?',
                        'Which executives are slow to respond to new leads?',
                        'Compare the sales manager with the executives.',
                        'What should we do this week to close more deals?'],
    })


@staff_required
@require_POST
def ai_analyst_ask(request):
    if not permissions.has_perm(request.user, 'ai_analyst', 'view'):
        return JsonResponse({'ok': False, 'error': 'You do not have access to the AI analyst.'}, status=403)
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        data = {}
    days = str(data.get('days', '30'))
    days = int(days) if days in analyst.PERIODS else 30
    history = data.get('history') if isinstance(data.get('history'), list) else []
    result = analyst.ask(str(data.get('question', '')), days, history)
    audit.log('ai_analyst.asked', request.user, actor=request.user, changes={'q': str(data.get('question', ''))[:120]})
    return JsonResponse({'ok': True, **result})
