import os
import sys
import io
from pathlib import Path
from dotenv import load_dotenv

# Ensure Windows console doesn't fail on Unicode
if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'buffer') and sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file
load_dotenv(BASE_DIR / '.env')

# Add apps/ directory to sys.path so bare imports (e.g. from accounts.models...) work
APPS_DIR = str(BASE_DIR / 'apps')
if APPS_DIR not in sys.path:
    sys.path.insert(0, APPS_DIR)

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-email-receptionist-dev-key-1234567890')
DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = [
    'https://*.ngrok-free.app',
    'https://*.ngrok-free.dev',
    'https://*.ngrok.io',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',

    # Isolated Agentyne Apps
    'accounts',
    'email_receptionist',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'accounts.middleware.OrganizationMiddleware',
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
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 4}},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = 'accounts:login'

# Email backend (defaults to console for development)
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@agentyne.com')

# AI Provider Keys
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '') or os.getenv('GOOGLE_API_KEY', '')
GOOGLE_API_KEY = GEMINI_API_KEY

# Optional model configurations
EMAIL_RECEPTIONIST_AI_PROVIDER = os.getenv('EMAIL_RECEPTIONIST_AI_PROVIDER', 'auto')
EMAIL_RECEPTIONIST_MODEL = os.getenv('EMAIL_RECEPTIONIST_MODEL', 'claude-sonnet-4-6')
EMAIL_RECEPTIONIST_OPENAI_MODEL = os.getenv('EMAIL_RECEPTIONIST_OPENAI_MODEL', 'gpt-4o')
EMAIL_RECEPTIONIST_GEMINI_MODEL = os.getenv('EMAIL_RECEPTIONIST_GEMINI_MODEL', 'gemini-3.5-flash')

# Fernet encryption key for mailbox app passwords
EMAIL_RECEPTIONIST_ENCRYPTION_KEY = os.getenv('EMAIL_RECEPTIONIST_ENCRYPTION_KEY', '')

# OAuth credentials (optional for 1-click connect)
GOOGLE_OAUTH_CLIENT_ID = os.getenv('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET', '')
MICROSOFT_OAUTH_CLIENT_ID = os.getenv('MICROSOFT_OAUTH_CLIENT_ID', '')
MICROSOFT_OAUTH_CLIENT_SECRET = os.getenv('MICROSOFT_OAUTH_CLIENT_SECRET', '')
ZOHO_OAUTH_CLIENT_ID = os.getenv('ZOHO_OAUTH_CLIENT_ID', '')
ZOHO_OAUTH_CLIENT_SECRET = os.getenv('ZOHO_OAUTH_CLIENT_SECRET', '')

# Celery Configuration
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = os.getenv('CELERY_ALWAYS_EAGER', 'True') == 'True'
CELERY_TASK_EAGER_PROPAGATES = True
