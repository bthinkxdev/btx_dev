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
    'django.contrib.humanize',
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
    'crm.core.middleware.CrmRequestMiddleware',
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
                'pages.context_processors.seo',
                'crm.core.context_processors.crm',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Remote MCP "Business Manager" server (crm.mcp) - a separate process, run with
# `manage.py run_mcp_http`, not part of the WSGI site above. Put TLS termination in
# front of it before exposing it beyond localhost.
MCP_HTTP_HOST = os.environ.get('MCP_HTTP_HOST', '127.0.0.1')
MCP_HTTP_PORT = int(os.environ.get('MCP_HTTP_PORT', '8800'))
MCP_HTTP_PATH = os.environ.get('MCP_HTTP_PATH', '/mcp')

# PostgreSQL is the production database (Business OS spec). Set DB_ENGINE=postgres and
# DB_NAME / DB_USER / DB_PASSWORD / DB_HOST / DB_PORT in .env. SQLite remains the
# zero-config default for local development.
if os.environ.get('DB_ENGINE', '').strip().lower() in ('postgres', 'postgresql'):
  DATABASES = {
      'default': {
          'ENGINE': 'django.db.backends.postgresql',
          'NAME': os.environ.get('DB_NAME', 'bthinkx'),
          'USER': os.environ.get('DB_USER', 'bthinkx'),
          'PASSWORD': os.environ.get('DB_PASSWORD', ''),
          'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
          'PORT': os.environ.get('DB_PORT', '5432'),
          'CONN_MAX_AGE': int(os.environ.get('DB_CONN_MAX_AGE', '60')),
          'ATOMIC_REQUESTS': False,
      }
  }
else:
  DATABASES = {
      'default': {
          'ENGINE': 'django.db.backends.sqlite3',
          'NAME': BASE_DIR / 'db.sqlite3',
          'OPTIONS': {'timeout': 20},
      }
  }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
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

# Public site URL (used in blog notification emails and unsubscribe links)
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')

# Delay between each subscriber email when notifying about a new blog post (SMTP rate limiting)
NEWSLETTER_EMAIL_INTERVAL_SECONDS = float(
    os.environ.get('NEWSLETTER_EMAIL_INTERVAL_SECONDS', '2.0')
)

# Google Analytics 4 (public site only — gtag.js is only injected when this is set,
# so local/dev environments that leave it blank don't pollute real traffic data).
GA_MEASUREMENT_ID = os.environ.get('GA_MEASUREMENT_ID', '').strip()

# Cashfree Payment Gateway — public-site offer landing pages (e.g. the Navaratri
# offer). 'sandbox' hits sandbox.cashfree.com with test credentials; 'production'
# hits api.cashfree.com with live credentials. Never commit real keys — set them
# in .env only.
CASHFREE_APP_ID = os.environ.get('CASHFREE_APP_ID', '').strip()
CASHFREE_SECRET_KEY = os.environ.get('CASHFREE_SECRET_KEY', '').strip()
CASHFREE_ENV = os.environ.get('CASHFREE_ENV', 'sandbox').strip().lower()

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


# ---------------------------------------------------------------------------
# BThinkX Business OS (crm app)
# ---------------------------------------------------------------------------
LOGIN_URL = 'crm:login'
LOGIN_REDIRECT_URL = 'crm:home'

# Private uploads (attachments, PDFs, imports) live outside MEDIA_ROOT and are only
# served through signed, permission-checked URLs.
CRM_PRIVATE_MEDIA_ROOT = Path(os.environ.get('CRM_PRIVATE_MEDIA_ROOT', str(BASE_DIR / 'private_media')))

# Key used to encrypt gateway secrets at rest. Keep it outside the database (env / secret
# manager). Falls back to a key derived from SECRET_KEY for development only.
CRM_ENCRYPTION_KEY = os.environ.get('CRM_ENCRYPTION_KEY', '').strip()

# Web Push (PWA install + notifications). Same keypair in every environment — see
# `python manage.py crm_generate_vapid_keys` and .env.example. Push is simply disabled
# (devices can still install the app, they just won't get push notifications) when a
# keypair isn't configured, e.g. on a fresh local checkout.
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '').strip()
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '').strip()
VAPID_CLAIM_EMAIL = os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:hr@bthinkx.com').strip()

# Upload limits (Part 15.1)
CRM_MAX_UPLOAD_MB = int(os.environ.get('CRM_MAX_UPLOAD_MB', '10'))
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000

# Session hygiene for staff (Part 3.6). Idle timeout is enforced per request by
# crm.core.middleware using CompanySettings.idle_timeout_minutes.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
if not DEBUG:
  SESSION_COOKIE_SECURE = True
  CSRF_COOKIE_SECURE = True
  SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
  SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '0'))
  SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'false').lower() in ('1', 'true', 'yes')

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# Scheduled jobs (Part 13.4). Run either with Celery beat (when REDIS_URL is set) or
# with cron:  */5 * * * *  python manage.py crm_run_jobs
CELERY_BEAT_SCHEDULE = {
    'crm-frequent-jobs': {'task': 'crm.tasks.run_frequent_jobs', 'schedule': 300.0},
    'crm-daily-jobs': {'task': 'crm.tasks.run_daily_jobs', 'schedule': 86400.0},
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {'request_id': {'()': 'crm.core.middleware.RequestIdLogFilter'}},
    'formatters': {
        'standard': {'format': '%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'standard', 'filters': ['request_id']},
    },
    'root': {'handlers': ['console'], 'level': os.environ.get('LOG_LEVEL', 'INFO')},
    'loggers': {
        'django.db.backends': {'level': 'WARNING'},
    },
}
