"""
Django settings for BThinkX Dev site.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Local secrets (gitignored) — e.g. EMAIL_HOST_PASSWORD
def _load_env_file(path: Path) -> None:
  """Load KEY=value lines into os.environ (does not override existing vars)."""
  if not path.is_file():
    return
  for raw in path.read_text(encoding='utf-8').splitlines():
    line = raw.strip()
    if not line or line.startswith('#') or '=' not in line:
      continue
    key, _, value = line.partition('=')
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    if key:
      os.environ.setdefault(key, value)


_env_file = BASE_DIR / '.env'
_load_env_file(_env_file)
if _env_file.is_file():
  try:
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=False)
  except ImportError:
    pass

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'dev-secret-change-in-production')

DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() in ('1', 'true', 'yes')

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1,*').split(',')
    if h.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sitemaps',
    'pages',
    'crm',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'crm.context_processors.crm_header',
                'pages.context_processors.seo',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
# Indian Standard Time (IST) — CRM “today” / follow-ups use this
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True


STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'new']  # {% static 'assets/bthinkx.css' %} -> new/assets/bthinkx.css
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# CRM (separate app; does not affect public site)
LOGIN_URL = '/crm/login/'
LOGIN_REDIRECT_URL = '/crm/'
LOGOUT_REDIRECT_URL = '/crm/login/'

# CRM Phase 3: Fernet key for ProjectCredential vault (urlsafe base64, 32 bytes).
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CRM_CREDENTIALS_FERNET_KEY = os.environ.get('CRM_CREDENTIALS_FERNET_KEY', '').strip()

# Email: SMTP (Zoho Mail for @bthinkx.com — see .env.example). Credentials in .env only.
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND',
    'django.core.mail.backends.smtp.EmailBackend',
)
# bthinkx.com uses Zoho (MX: mx.zoho.in)
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.zoho.in')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'true').lower() in ('1', 'true', 'yes')
EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL', 'false').lower() in ('1', 'true', 'yes')
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', 'hr@bthinkx.com').strip()
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER).strip() or EMAIL_HOST_USER
EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '30'))

# Address to receive contact form submissions
CONTACT_EMAIL_TO = os.environ.get('CONTACT_EMAIL_TO', 'hr@bthinkx.com')

# CRM Phase 4: renewal reminder emails (management command + manual sends).
RENEWAL_FROM_EMAIL = os.environ.get('RENEWAL_FROM_EMAIL', '').strip() or DEFAULT_FROM_EMAIL
RENEWAL_INTERNAL_ALERT_EMAIL = os.environ.get(
    'RENEWAL_INTERNAL_ALERT_EMAIL', CONTACT_EMAIL_TO
)
RENEWAL_REMINDER_DAYS = [30, 7, 1]

# CRM Billing: receipt & invoice emails (From: hr@bthinkx.com)
BILLING_FROM_EMAIL = (
    os.environ.get('BILLING_FROM_EMAIL', 'hr@bthinkx.com').strip() or DEFAULT_FROM_EMAIL
)

# Public site URL (used in blog notification emails and unsubscribe links)
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')

# Delay between each subscriber email when notifying about a new blog post (SMTP rate limiting)
NEWSLETTER_EMAIL_INTERVAL_SECONDS = float(
    os.environ.get('NEWSLETTER_EMAIL_INTERVAL_SECONDS', '2.0')
)

# CRM: if phone is 10 digits (local), prepend this country code for wa.me links (no +). Example: 91 (India), 1 (US).
CRM_WHATSAPP_DEFAULT_COUNTRY_CODE = '91'

# WhatsApp Cloud API webhook verification token.
WHATSAPP_VERIFY_TOKEN = os.environ.get('WHATSAPP_VERIFY_TOKEN', 'bthinkx123').strip()

# WhatsApp transport configuration.
# We are migrating away from Meta Cloud API to a WhatsApp Web.js bridge.
WHATSAPP_TRANSPORT = os.environ.get('WHATSAPP_TRANSPORT', 'webjs').strip()
WHATSAPP_WEBJS_BRIDGE_URL = os.environ.get('WHATSAPP_WEBJS_BRIDGE_URL', '').strip()
WHATSAPP_WEBJS_BRIDGE_TOKEN = os.environ.get('WHATSAPP_WEBJS_BRIDGE_TOKEN', '').strip()

# Node -> Django API token (Bearer). If unset, we fall back to WHATSAPP_WEBJS_BRIDGE_TOKEN.
DJANGO_API_TOKEN = os.environ.get('DJANGO_API_TOKEN', '').strip()

# Deprecated Meta Cloud API settings (kept only to avoid breaking old envs).
# They are no longer used when WHATSAPP_TRANSPORT=webjs.
WHATSAPP_APP_SECRET = os.environ.get('WHATSAPP_APP_SECRET', '').strip()
WHATSAPP_ACCESS_TOKEN = os.environ.get('WHATSAPP_ACCESS_TOKEN', '').strip()
WHATSAPP_PHONE_NUMBER_ID = os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '').strip()

# Gemini AI WhatsApp qualification (human-like sales coordinator)
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash').strip()
GEMINI_AI_QUALIFICATION_ENABLED = os.environ.get(
    'GEMINI_AI_QUALIFICATION_ENABLED', 'false'
).lower() in ('1', 'true', 'yes')
GEMINI_MAX_HISTORY_MESSAGES = int(os.environ.get('GEMINI_MAX_HISTORY_MESSAGES', '10'))
GEMINI_REQUEST_TIMEOUT = int(os.environ.get('GEMINI_REQUEST_TIMEOUT', '60'))

# Pause auto-replies after executive sends from phone; resume after this many minutes.
WHATSAPP_HUMAN_TAKEOVER_COOLDOWN_MINUTES = int(
    os.environ.get('WHATSAPP_HUMAN_TAKEOVER_COOLDOWN_MINUTES', '30')
)

# WhatsApp Web.js reply buttons are DEPRECATED by Meta — often invisible on phones.
# Keep false; use numbered text menus (reliable). Set true only if buttons work on your account.
WHATSAPP_USE_REPLY_BUTTONS = os.environ.get('WHATSAPP_USE_REPLY_BUTTONS', 'false').lower() in (
    '1',
    'true',
    'yes',
)

# Optional local voice transcription (pip install faster-whisper).
# If false, voice notes use Gemini (recommended when GEMINI_API_KEY is set).
WHISPER_LOCAL_ENABLED = os.environ.get('WHISPER_LOCAL_ENABLED', 'false').lower() in (
    '1',
    'true',
    'yes',
)
WHISPER_MODEL_SIZE = os.environ.get('WHISPER_MODEL_SIZE', 'small').strip()
HF_TOKEN = os.environ.get('HF_TOKEN', '').strip()
if WHISPER_LOCAL_ENABLED:
    os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')
    if HF_TOKEN:
        os.environ.setdefault('HF_TOKEN', HF_TOKEN)

# Redis cache (recommended for webhook dedupe + rate limiting + multi-worker safety).
# Set REDIS_URL like: redis://127.0.0.1:6379/1
REDIS_URL = os.environ.get('REDIS_URL', '').strip()
if REDIS_URL:
  CACHES = {
      'default': {
          'BACKEND': 'django_redis.cache.RedisCache',
          'LOCATION': REDIS_URL,
          'OPTIONS': {
              'CLIENT_CLASS': 'django_redis.client.DefaultClient',
          },
          'TIMEOUT': 300,
      }
  }

# Celery (optional) — uses Redis when REDIS_URL is set.
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', '').strip() or (REDIS_URL or '')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', '').strip() or (REDIS_URL or '')
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
