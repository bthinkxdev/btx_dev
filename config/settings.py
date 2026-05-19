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
WHATSAPP_VERIFY_TOKEN = "bthinkx123"
WHATSAPP_ACCESS_TOKEN = 'EAAe8YaGfcZBEBRKSDf4ikI70kORTeeZBarsCZB4rjWcpvyGgyyjZAOFR4ZAb31JYCD48FZBYEJwfLJyzybfxvXCbx6uLeM6tZBLOjt2FZBj0PN1NIWMGpVQ2OvtkFIVZApLGoRwfMowc2dXGXh8JEJIHr9h02yN9F2o5WVRyjvdi4r56405KajaQfZCZBRJ1W7sIzAXR6lZCW7Yq6gbZCLNrUWCuFA8TUZB8wLjiGhqB3VpZCoyaAZCSK6JJh5rEFqW4ECHYX8LwAKx7k9Gjrm7MIfi5pLplycWG3gZDZD'
WHATSAPP_PHONE_NUMBER_ID = "984411908097951"
