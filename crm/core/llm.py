"""Tiny client for a free, OpenAI-compatible chat-completions API (default: Groq free tier).

Used by the website chatbot and the CRM sales analyst. Raises `LLMUnavailable` for anything that
should make the caller fall back to its built-in (non-AI) answer: no key, quota used up, timeout,
or provider error. A per-day call counter keeps usage inside the free quota.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('crm.llm')


class LLMUnavailable(Exception):
    pass


def configured() -> bool:
    return bool(settings.CHATBOT_API_KEY)


def _take_daily_slot() -> bool:
    key = f'llm-calls:{timezone.localdate().isoformat()}'
    try:
        cache.add(key, 0, 60 * 60 * 26)
        return cache.incr(key) <= settings.CHATBOT_DAILY_AI_LIMIT
    except Exception:  # cache trouble must not take the chat down
        return True


def complete(messages: list[dict], *, max_tokens: int = 450, temperature: float = 0.4, timeout: int = 20) -> tuple[str, str]:
    """Return (text, model_used) or raise LLMUnavailable."""
    if not configured():
        raise LLMUnavailable('no API key')
    if not _take_daily_slot():
        raise LLMUnavailable('daily AI limit reached')
    models = [settings.CHATBOT_MODEL]
    if settings.CHATBOT_FALLBACK_MODEL and settings.CHATBOT_FALLBACK_MODEL != settings.CHATBOT_MODEL:
        models.append(settings.CHATBOT_FALLBACK_MODEL)
    last = 'unknown'
    for model in models:
        try:
            r = requests.post(
                f'{settings.CHATBOT_BASE_URL}/chat/completions',
                headers={'Authorization': f'Bearer {settings.CHATBOT_API_KEY}', 'Content-Type': 'application/json'},
                json={'model': model, 'messages': messages, 'max_tokens': max_tokens, 'temperature': temperature},
                timeout=timeout)
        except requests.RequestException as exc:
            last = f'{model}: {exc.__class__.__name__}'
            logger.warning('LLM request failed (%s)', last)
            continue
        if r.status_code == 200:
            try:
                text = (r.json()['choices'][0]['message']['content'] or '').strip()
            except (KeyError, IndexError, ValueError):
                last = f'{model}: bad response'
                continue
            if text:
                return text, model
            last = f'{model}: empty'
            continue
        last = f'{model}: HTTP {r.status_code}'
        logger.warning('LLM error %s %s', r.status_code, r.text[:200])
        if r.status_code in (401, 403):   # bad key: the other model will not help
            break
    raise LLMUnavailable(last)
