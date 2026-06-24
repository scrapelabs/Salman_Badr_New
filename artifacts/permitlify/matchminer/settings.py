"""Django settings for the MatchMiner project.

Runs behind Replit's TLS-terminating reverse proxy and is shown inside the
editor preview iframe, which makes the app a cross-site context. The cookie /
CSRF / proxy settings below are what make auth work in that environment.
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Core -----------------------------------------------------------------
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() != "false"

# Fail closed in production: never run with the insecure dev key when DEBUG is off.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or os.environ.get("SESSION_SECRET")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-insecure-secret-key-change-me"
    else:
        raise ImproperlyConfigured(
            "Set DJANGO_SECRET_KEY (or SESSION_SECRET) when DJANGO_DEBUG=False."
        )

# The shared proxy forwards arbitrary Host headers; routing is by path.
ALLOWED_HOSTS = ["*"]


def _csrf_trusted_origins():
    origins = [
        "https://*.replit.dev",
        "https://*.replit.app",
        "https://*.repl.co",
        "https://*.worf.replit.dev",
        "https://*.riker.replit.dev",
    ]
    for domain in os.environ.get("REPLIT_DOMAINS", "").split(","):
        domain = domain.strip()
        if domain:
            origins.append(f"https://{domain}")
    return origins


CSRF_TRUSTED_ORIGINS = _csrf_trusted_origins()

# TLS is terminated at the proxy; trust its forwarded scheme so request.is_secure()
# is correct (required for Secure cookies to be set).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# --- Applications ---------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
]

# Note: django.middleware.clickjacking.XFrameOptionsMiddleware is intentionally
# omitted. It would send X-Frame-Options: DENY/SAMEORIGIN and block the app from
# rendering inside the Replit preview iframe (a cross-origin frame).
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "matchminer.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "matchminer.wsgi.application"

# --- Database (Postgres) --------------------------------------------------
DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get("DATABASE_URL"),
        conn_max_age=600,
    )
}

# --- Auth -----------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/"
LOGIN_REDIRECT_URL = "/overview/"
LOGOUT_REDIRECT_URL = "/"

# --- I18N -----------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static files (WhiteNoise) -------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Cookies (cross-site preview iframe) ----------------------------------
# Sessions are stored in Postgres (django_session table) via the default
# database session backend. The preview embeds the app in a cross-site iframe,
# so cookies must be SameSite=None + Secure to be sent at all.
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "None"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "None"
