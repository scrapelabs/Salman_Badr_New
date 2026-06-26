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

# Load a local .env (if present) for development on machines without real
# environment variables, e.g. a Windows checkout. Existing OS environment
# variables (such as those Replit injects) always take precedence, so this is a
# no-op in the hosted environment. python-dotenv is optional; skip if absent.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")  # artifacts/permitlify/.env
    load_dotenv(BASE_DIR.parent.parent / ".env")  # workspace-root .env
except ImportError:
    pass

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
                "accounts.context_processors.notifications",
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

# --- Cookies --------------------------------------------------------------
# Sessions are stored in Postgres (django_session table) via the default
# database session backend.
#
# On Replit the app is embedded in a cross-site preview iframe, so cookies must
# be SameSite=None + Secure to be sent at all. But Secure cookies are NEVER sent
# over plain HTTP, which would break login for a local checkout served at
# http://localhost:8000. Set DJANGO_LOCAL_HTTP=True (see .env.example) for local
# HTTP development to fall back to ordinary Lax, non-Secure cookies. The hosted
# Replit environment leaves this unset and keeps the secure cross-site cookies.
LOCAL_HTTP = os.environ.get("DJANGO_LOCAL_HTTP", "False").lower() == "true"
if LOCAL_HTTP:
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SECURE = False
    CSRF_COOKIE_SAMESITE = "Lax"
else:
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = "None"
    CSRF_COOKIE_SECURE = True
    CSRF_COOKIE_SAMESITE = "None"

# --- Scraper credentials ---------------------------------------------------
# The Ioncourt scraper authenticates against api.ioncourt.com with a phone +
# password. These are read from the environment (Replit secrets in the hosted
# app, or the local .env) and are NEVER hard-coded or logged. When unset, the
# Ioncourt run fails honestly (like a Stadion scraper without its proxy).
IONCOURT_PHONE = os.environ.get("IONCOURT_PHONE", "")
IONCOURT_PASSWORD = os.environ.get("IONCOURT_PASSWORD", "")

# The PrestoSports scraper logs into gameday-api.prestosports.com with a
# username + password. Same rules as Ioncourt: read from the environment, never
# hard-coded or logged; when unset the PrestoSports run fails honestly.
PRESTOSPORTS_USERNAME = os.environ.get("PRESTOSPORTS_USERNAME", "")
PRESTOSPORTS_PASSWORD = os.environ.get("PRESTOSPORTS_PASSWORD", "")

# The Australia Tennis scraper reads match JSON from an Azure Blob container via
# a SAS URL (a credential: it embeds a signature). Read from the environment,
# never hard-coded or logged; when unset the run fails honestly.
AUSTRALIA_TENNIS_SAS_URL = os.environ.get("AUSTRALIA_TENNIS_SAS_URL", "")

# The USTA Team Captains scraper logs into tennislink.usta.com with a USTA
# account email + password. Same rules: read from the environment, never
# hard-coded or logged; when unset the run fails honestly.
USTA_USERNAME = os.environ.get("USTA_USERNAME", "")
USTA_PASSWORD = os.environ.get("USTA_PASSWORD", "")

# The College Dual Match (AI) scraper extracts matches from box-score PDFs/HTML
# with Anthropic Claude. Keys come from the environment as a comma-separated
# list (CLAUDE_KEYS) and/or a single ANTHROPIC_API_KEY; the worker rotates
# across them. When none are set the run fails honestly. OPENAI_API_KEY is an
# optional fallback used only to recover a missing tournament date.
CLAUDE_KEYS = [
    k.strip()
    for k in (
        os.environ.get("CLAUDE_KEYS", "").split(",")
        + [os.environ.get("ANTHROPIC_API_KEY", "")]
    )
    if k.strip()
]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# --- Stealth browser (patchright) for anti-bot origins (itftennis family) ---
# www.itftennis.com sits behind Imperva/Incapsula. patchright's recommended
# stealth configuration is a *persistent* profile (clearance cookies survive
# between runs) + real Google **Chrome** + *headed* mode. On a local Windows box
# all three work natively, so those are the defaults there. The Replit Linux
# container has only headless Chromium (no real Chrome, no X display), so it
# degrades to headless Chromium automatically. All three are env-overridable.
_IS_WINDOWS = os.name == "nt"
# headless: default OFF (headed) on Windows, ON (headless) on Linux/Replit.
SCRAPER_BROWSER_HEADLESS = (
    os.environ.get(
        "SCRAPER_BROWSER_HEADLESS", "False" if _IS_WINDOWS else "True"
    ).lower()
    != "false"
)
# channel: "chrome" launches real Google Chrome (most stealthy); an empty string
# falls back to the bundled/Nix Chromium (resolved in _browser.py).
SCRAPER_BROWSER_CHANNEL = os.environ.get(
    "SCRAPER_BROWSER_CHANNEL", "chrome" if _IS_WINDOWS else ""
)
# Persistent-profile root. Each scraper gets its own sub-directory so Incapsula
# clearance cookies persist across runs without two scrapers sharing one locked
# Chrome profile. Git-ignored; created on first use.
SCRAPER_BROWSER_PROFILE_DIR = os.environ.get(
    "SCRAPER_BROWSER_PROFILE_DIR", str(BASE_DIR / ".browser_profiles")
)
# Per-request rotation: open a *fresh* browser (new fingerprint + a throwaway
# ephemeral profile) and a *fresh* proxy IP for every tournament, instead of
# reusing one persistent session for the whole run. Imperva/Incapsula
# re-challenges a single identity after a handful of records, so rotating per
# tournament keeps every visit looking like a brand-new visitor. Default ON.
# When ON, the persistent profile above is bypassed (each launch is ephemeral)
# and, if the proxy address carries a ``{session}`` placeholder, a fresh token
# is substituted each launch so a sticky-session residential proxy hands out a
# new exit IP (a rotating gateway rotates per connection on its own). Direct
# (no proxy) can't change IP, but the fingerprint still rotates.
SCRAPER_BROWSER_ROTATE_PER_REQUEST = (
    os.environ.get("SCRAPER_BROWSER_ROTATE_PER_REQUEST", "True").lower() != "false"
)

# itftennis player-DOB pacing/rotation. ``GetHeadToHeadPlayerDetails`` is gated by
# Incapsula's JS challenge AND a *rate* re-challenge: the tournament browser holds
# clearance (the drawsheet API calls work), but a burst of DOB fetches from one IP
# trips a fresh challenge. So pace each DOB lookup by this many milliseconds to
# stay under the rate threshold; when one is still blocked, the engine relaunches
# the browser (fresh IP + re-solved clearance) and retries, up to MAX_ROTATIONS
# times, then leaves that DOB blank (best-effort) so a run never stalls on a
# stubborn lookup. Set DELAY to 0 to disable pacing; ROTATIONS to 0 to never
# relaunch (blocked DOBs just blank out).
SCRAPER_ITF_DOB_DELAY_MS = int(os.environ.get("SCRAPER_ITF_DOB_DELAY_MS", "250"))
SCRAPER_ITF_DOB_MAX_ROTATIONS = int(
    os.environ.get("SCRAPER_ITF_DOB_MAX_ROTATIONS", "2")
)
