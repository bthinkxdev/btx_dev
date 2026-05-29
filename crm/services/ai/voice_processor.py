"""WhatsApp voice note → transcript for AI pipeline."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

AUDIO_MESSAGE_TYPES = frozenset({'ptt', 'audio', 'voice'})

_whisper_model = None
_whisper_model_key: str | None = None


def is_voice_message(message_type: str, media: dict | None) -> bool:
    mt = str(message_type or '').strip().lower()
    if mt in AUDIO_MESSAGE_TYPES:
        return True
    if not media:
        return False
    mime = str(media.get('mimetype') or media.get('mime_type') or '').lower()
    return mime.startswith('audio/')


def _decode_media_bytes(media: dict) -> tuple[bytes, str]:
    """Resolve audio bytes from gateway payload."""
    mime = str(media.get('mimetype') or media.get('mime_type') or 'audio/ogg')
    b64 = media.get('data_base64') or media.get('base64') or media.get('data')
    if b64:
        return base64.b64decode(str(b64)), mime

    temp_path = str(media.get('temp_path') or '').strip()
    if temp_path and os.path.isfile(temp_path):
        return Path(temp_path).read_bytes(), mime

    bridge_url = str(getattr(settings, 'WHATSAPP_WEBJS_BRIDGE_URL', '') or '').strip().rstrip('/')
    media_id = str(media.get('id') or '').strip()
    if bridge_url and media_id:
        try:
            import requests

            token = str(getattr(settings, 'WHATSAPP_WEBJS_BRIDGE_TOKEN', '') or '').strip()
            headers = {'Authorization': f'Bearer {token}'} if token else {}
            resp = requests.get(f'{bridge_url}/api/media/{media_id}', headers=headers, timeout=30)
            if resp.ok:
                return resp.content, mime
        except Exception:
            logger.exception('Failed to fetch media from gateway')

    return b'', mime


def _get_whisper_model():
    """Load faster-whisper once per process (avoids re-downloading from HF Hub each voice note)."""
    global _whisper_model, _whisper_model_key
    model_size = str(getattr(settings, 'WHISPER_MODEL_SIZE', 'small') or 'small')
    if _whisper_model is not None and _whisper_model_key == model_size:
        return _whisper_model

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.warning('faster-whisper not installed; pip install faster-whisper or set WHISPER_LOCAL_ENABLED=false')
        return None

    os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')
    hf_token = str(getattr(settings, 'HF_TOKEN', '') or os.environ.get('HF_TOKEN', '')).strip()
    if hf_token:
        os.environ.setdefault('HF_TOKEN', hf_token)

    logger.info('Loading faster-whisper model=%s (first voice note may take a minute)', model_size)
    _whisper_model = WhisperModel(model_size, device='cpu', compute_type='int8')
    _whisper_model_key = model_size
    return _whisper_model


def _transcribe_faster_whisper(audio_bytes: bytes) -> str:
    model = _get_whisper_model()
    if model is None:
        return ''

    import tempfile

    suffix = '.ogg'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        segments, _info = model.transcribe(
            tmp_path,
            language=None,
            task='transcribe',
            vad_filter=True,
        )
        parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return ' '.join(parts).strip()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def transcribe_voice(media: dict | None) -> dict:
    """
    Returns dict: transcript, language, intent_summary, media_path (saved under MEDIA).
    """
    if not media:
        return {}

    audio_bytes, mime = _decode_media_bytes(media)
    if not audio_bytes:
        logger.warning('Voice message had no decodable audio bytes')
        return {}

    saved_path = ''
    try:
        rel_dir = Path('wa_voice') / 'inbound'
        abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        ext = '.ogg' if 'ogg' in mime else '.mp3' if 'mpeg' in mime or 'mp3' in mime else '.wav'
        fname = f"{media.get('id') or os.urandom(8).hex()}{ext}"
        full = abs_dir / fname
        full.write_bytes(audio_bytes)
        saved_path = str(rel_dir / fname).replace('\\', '/')
    except Exception:
        logger.exception('Failed to persist voice media')

    transcript = ''
    language = ''
    intent_summary = ''

    if getattr(settings, 'WHISPER_LOCAL_ENABLED', False):
        transcript = _transcribe_faster_whisper(audio_bytes)

    if not transcript:
        from . import gemini_client

        if gemini_client.is_configured():
            result = gemini_client.transcribe_audio(audio_bytes=audio_bytes, mime_type=mime)
            transcript = str(result.get('transcript') or '').strip()
            language = str(result.get('language') or '').strip()
            intent_summary = str(result.get('intent_summary') or '').strip()

    return {
        'transcript': transcript,
        'language': language,
        'intent_summary': intent_summary,
        'media_path': saved_path,
    }
