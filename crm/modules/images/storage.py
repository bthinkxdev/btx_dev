"""Where client photos are kept, and how they are read back.

S3 when CRM_IMAGE_BUCKET is set (private bucket, server-side encryption, short-lived signed links); a private folder
otherwise. Either way the browser never gets a permanent or public address: every file is fetched through a CRM page that
checks who is asking, which then redirects to a link that works for a few minutes."""
from __future__ import annotations

import hashlib
import io
import mimetypes
import secrets
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

ALLOWED_TYPES = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/heic': 'heic'}
PREVIEW_SIZE = 2400            # a browser-friendly copy of a HEIC photo, for screens. The original is kept untouched.
THUMB_SIZE = 640
MAX_PIXELS = 100_000_000


MIN_NOTE = 12                 # the instructions a client must give: this many characters, or a voice message
VOICE_TYPES = {'audio/webm': 'webm', 'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a', 'audio/ogg': 'ogg', 'audio/mpeg': 'mp3', 'audio/wav': 'wav'}
MAX_VOICE_BYTES = 3 * 1024 * 1024


def max_voice_seconds() -> int:
    return int(getattr(settings, 'CRM_VOICE_MAX_SECONDS', 120))


class ImageFileError(ValueError):
    """The file is not an acceptable photo. The message is safe to show the person."""


@dataclass
class Processed:
    data: bytes
    thumb: bytes
    mime: str
    ext: str
    width: int
    height: int
    sha256: str
    preview: bytes | None = None      # a JPEG copy for screens, only for formats browsers cannot show (HEIC)


def max_bytes() -> int:
    return int(getattr(settings, 'CRM_IMAGE_MAX_MB', 25)) * 1024 * 1024


def process(upload) -> Processed:
    """Read an uploaded file, check it really is a JPEG, PNG or WebP photo, and make a small preview.
    The original bytes are kept exactly as sent."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    name = getattr(upload, 'name', 'photo')
    data = upload.read() if hasattr(upload, 'read') else bytes(upload)
    if not data:
        raise ImageFileError(f'{name} is empty.')
    if len(data) > max_bytes():
        raise ImageFileError(f'{name} is larger than {settings.CRM_IMAGE_MAX_MB} MB. Send a smaller photo.')
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        import pillow_heif                      # iPhone photos (HEIC / HEIF)
        pillow_heif.register_heif_opener()
    except ImportError:                         # not installed: HEIC is refused with a clear message below
        pass
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or '').upper()
        width, height = img.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ImageFileError(f'{name} is not a photo we can read. Send a JPEG, PNG, WebP or HEIC picture.') from None
    mime = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp', 'HEIF': 'image/heic', 'HEIC': 'image/heic'}.get(fmt)
    if mime is None:
        raise ImageFileError(f'{name} is a {fmt.title() or "unknown"} file. Send a JPEG, PNG, WebP or HEIC picture.')
    thumb = io.BytesIO()
    preview = ImageOps.exif_transpose(img)
    preview.thumbnail((THUMB_SIZE, THUMB_SIZE))
    if preview.mode not in ('RGB', 'L'):
        preview = preview.convert('RGB')
    preview.save(thumb, 'JPEG', quality=80, optimize=True)
    preview = None
    if mime == 'image/heic':
        big = ImageOps.exif_transpose(img)
        big.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE))
        if big.mode not in ('RGB', 'L'):
            big = big.convert('RGB')
        out = io.BytesIO()
        big.save(out, 'JPEG', quality=88, optimize=True)
        preview = out.getvalue()
    return Processed(data=data, thumb=thumb.getvalue(), mime=mime, ext=ALLOWED_TYPES[mime], width=width, height=height,
                     sha256=hashlib.sha256(data).hexdigest(), preview=preview)


def new_key(asset_pk: int, client_pk: int, number: int, ext: str, *, thumb: bool = False, preview: bool = False) -> str:
    """A key nobody can guess, laid out so one client's files are always under their own folder."""
    token = secrets.token_hex(6)
    suffix = f'-{token}-thumb.jpg' if thumb else (f'-{token}-view.jpg' if preview else f'-{token}.{ext}')
    return f'client-{client_pk}/image-{asset_pk}/v{number}{suffix}'


def _looks_like(mime: str, data: bytes) -> bool:
    """A quick look at the first bytes, so a text file renamed to .webm is refused. Nothing is decoded."""
    head = data[:16]
    if mime == 'audio/webm':
        return head[:4] == b'\x1a\x45\xdf\xa3'
    if mime in ('audio/mp4', 'audio/x-m4a'):
        return head[4:8] == b'ftyp'
    if mime == 'audio/ogg':
        return head[:4] == b'OggS'
    if mime == 'audio/mpeg':
        return head[:3] == b'ID3' or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if mime == 'audio/wav':
        return head[:4] == b'RIFF'
    return False


def check_voice(upload, seconds) -> tuple:
    """Check a recorded voice message without processing it. Returns (bytes, mime type, file extension, seconds)."""
    data = upload.read() if hasattr(upload, 'read') else bytes(upload)
    mime = (getattr(upload, 'content_type', '') or '').split(';')[0].strip().lower()
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        seconds = 0
    if not data:
        raise ImageFileError('The voice message is empty. Record it again.')
    if len(data) > MAX_VOICE_BYTES:
        raise ImageFileError('That voice message is too long. Keep it under two minutes.')
    if seconds > max_voice_seconds() + 5:
        raise ImageFileError('That voice message is too long. Keep it under two minutes.')
    if mime not in VOICE_TYPES or not _looks_like(mime, data):
        raise ImageFileError('That is not a voice recording this phone or browser can send. Please type your instructions instead.')
    return data, ('audio/mp4' if mime == 'audio/x-m4a' else mime), VOICE_TYPES[mime], max(1, min(seconds, max_voice_seconds()))


def voice_key(client_pk: int, ext: str) -> str:
    return f'client-{client_pk}/voice/{secrets.token_hex(8)}.{ext}'


class LocalStorage:
    name = 'local'

    def __init__(self):
        self.root = Path(settings.CRM_PRIVATE_MEDIA_ROOT) / 'images'

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError('Bad storage key.')
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def open(self, key: str):
        return open(self._path(key), 'rb')

    def signed_url(self, key: str, filename: str = '', content_type: str = '', download: bool = False) -> str | None:
        return None    # served by our own page


class S3Storage:
    name = 's3'

    def __init__(self):
        self.bucket = settings.CRM_IMAGE_BUCKET
        self.prefix = settings.CRM_IMAGE_PREFIX
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3
            kwargs = {'region_name': settings.CRM_IMAGE_REGION} if settings.CRM_IMAGE_REGION else {}
            self._client = boto3.client('s3', **kwargs)
        return self._client

    def _key(self, key: str) -> str:
        return f'{self.prefix}/{key}' if self.prefix else key

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type,
                               ServerSideEncryption='AES256')

    def read(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))['Body'].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception:
            return False

    def open(self, key: str):
        return io.BytesIO(self.read(key))

    def signed_url(self, key: str, filename: str = '', content_type: str = '', download: bool = False) -> str:
        params = {'Bucket': self.bucket, 'Key': self._key(key)}
        if filename:
            safe = filename.replace('"', '').replace('\r', '').replace('\n', '')
            params['ResponseContentDisposition'] = f'{"attachment" if download else "inline"}; filename="{safe}"'
        if content_type:
            params['ResponseContentType'] = content_type
        return self.client.generate_presigned_url('get_object', Params=params, ExpiresIn=int(settings.CRM_IMAGE_URL_SECONDS))


_STORAGE = None


def get_storage():
    global _STORAGE
    if _STORAGE is None:
        _STORAGE = S3Storage() if getattr(settings, 'CRM_IMAGE_BUCKET', '') else LocalStorage()
    return _STORAGE


def reset_storage():
    """For tests and settings changes."""
    global _STORAGE
    _STORAGE = None


def guess_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or 'application/octet-stream'
