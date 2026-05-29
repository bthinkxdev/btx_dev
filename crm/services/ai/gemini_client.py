"""Thin wrapper around Google Generative AI (Gemini)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_ID = ''

# Fallbacks if primary model is unavailable on this API key.
_MODEL_FALLBACKS = (
    'gemini-2.5-flash-lite',
    'gemini-2.0-flash',
)


def _model_name() -> str:
    return str(getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash') or 'gemini-2.5-flash')


def _model_candidates() -> list[str]:
    primary = _model_name()
    out = []
    for name in (primary, *_MODEL_FALLBACKS):
        name = str(name or '').strip()
        if name and name not in out:
            out.append(name)
    return out


def is_configured() -> bool:
    return bool(str(getattr(settings, 'GEMINI_API_KEY', '') or '').strip())


def _is_model_not_found(exc: BaseException) -> bool:
    err = str(exc).lower()
    name = type(exc).__name__.lower()
    return 'notfound' in name or '404' in err or 'not found' in err or 'no longer available' in err


def _get_model(model_name: str | None = None):
    global _MODEL, _MODEL_ID
    name = str(model_name or _model_name()).strip()
    if _MODEL is not None and _MODEL_ID == name:
        return _MODEL
    api_key = str(getattr(settings, 'GEMINI_API_KEY', '') or '').strip()
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY is not configured')
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    _MODEL_ID = name
    _MODEL = genai.GenerativeModel(name)
    logger.info('Gemini model loaded: %s', name)
    return _MODEL


def _generate_content_with_fallback(*, prompt_parts, generation_config: dict, timeout: int):
    last_exc = None
    for model_name in _model_candidates():
        try:
            model = _get_model(model_name)
            return model.generate_content(
                prompt_parts,
                generation_config=generation_config,
                request_options={'timeout': timeout},
            )
        except Exception as exc:
            last_exc = exc
            if _is_model_not_found(exc):
                global _MODEL, _MODEL_ID
                _MODEL = None
                _MODEL_ID = ''
                logger.warning('Gemini model unavailable (%s), trying next', model_name)
                continue
            if _is_quota_error(exc):
                _mark_quota_exhausted()
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError('No Gemini model available')


def _strip_json_fence(text: str) -> str:
    raw = str(text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.I)
        raw = re.sub(r'\s*```$', '', raw)
    return raw.strip()


def parse_json_response(text: str) -> dict[str, Any]:
    raw = _strip_json_fence(text)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                data = json.loads(match.group(0))
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                pass
    logger.warning('Gemini returned non-JSON: %s', raw[:200])
    return {}


def _quota_cache_key() -> str:
    return 'gemini:quota_exhausted'


def _quota_blocked() -> bool:
    try:
        from django.core.cache import cache

        return bool(cache.get(_quota_cache_key()))
    except Exception:
        return False


def _mark_quota_exhausted() -> None:
    try:
        from django.core.cache import cache

        cache.set(_quota_cache_key(), True, timeout=60 * 60)
    except Exception:
        pass


def _is_quota_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    err = str(exc).lower()
    return 'resourceexhausted' in name or '429' in err or 'quota' in err


def generate_json(*, system: str, user: str, temperature: float = 0.4) -> dict[str, Any]:
    if _quota_blocked():
        raise RuntimeError('Gemini quota exhausted (cached)')
    prompt = f"{system.strip()}\n\n---\n\n{user.strip()}"
    timeout = int(getattr(settings, 'GEMINI_REQUEST_TIMEOUT', 60) or 60)
    response = _generate_content_with_fallback(
        prompt_parts=prompt,
        generation_config={
            'temperature': temperature,
            'response_mime_type': 'application/json',
        },
        timeout=timeout,
    )
    text = ''
    try:
        text = response.text or ''
    except Exception:
        text = ''
    return parse_json_response(text)


def generate_text(*, system: str, user: str, temperature: float = 0.3) -> str:
    prompt = f"{system.strip()}\n\n---\n\n{user.strip()}"
    timeout = int(getattr(settings, 'GEMINI_REQUEST_TIMEOUT', 60) or 60)
    response = _generate_content_with_fallback(
        prompt_parts=prompt,
        generation_config={'temperature': temperature},
        timeout=timeout,
    )
    try:
        return str(response.text or '').strip()
    except Exception:
        return ''


def transcribe_audio(*, audio_bytes: bytes, mime_type: str) -> dict[str, Any]:
    """Transcribe voice note via Gemini multimodal."""
    import google.generativeai as genai

    api_key = str(getattr(settings, 'GEMINI_API_KEY', '') or '').strip()
    if not api_key:
        return {}
    from .prompts import VOICE_TRANSCRIBE_PROMPT

    blob = {'mime_type': mime_type or 'audio/ogg', 'data': audio_bytes}
    timeout = int(getattr(settings, 'GEMINI_REQUEST_TIMEOUT', 60) or 60)
    response = _generate_content_with_fallback(
        prompt_parts=[VOICE_TRANSCRIBE_PROMPT, blob],
        generation_config={
            'temperature': 0.1,
            'response_mime_type': 'application/json',
        },
        timeout=timeout,
    )
    try:
        return parse_json_response(response.text or '')
    except Exception:
        logger.exception('Gemini audio transcription failed')
        return {}
