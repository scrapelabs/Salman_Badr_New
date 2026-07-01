"""Infer a tennis player's gender from their name via Claude, cached in the DB.

The tournamentsoftware player profile carries no gender field; the original
production pipeline derived gender by sending each new player's *name* to an LLM.
This module restores that behaviour for the scrapers that opt in (currently the
Croatia individual-tournament and league scrapers), where the draw / competition
name alone is not a reliable gender signal (e.g. Croatian league names such as
"Prva liga" carry no gender word).

:func:`resolve_gender` returns the per-player schema code ``"M"`` / ``"F"`` /
``""`` (``""`` for ambiguous, unknown, or when no Claude key is configured).
Every distinct name is asked at most once: the answer is cached in
:class:`accounts.models.PlayerGenderCache` (ambiguous answers cached as ``"U"``
so they are never re-asked). Transient API failures are *not* cached, so they
are retried on a later run.

The Claude key lives only in the request header — it is never logged, and any
error body is scrubbed with :func:`redact_secrets` before it reaches telemetry.
The request is sent through the caller's :class:`~accounts.live_scrapers._http.
ScraperClient`, so it passes the same central SSRF guard as every other request
(``api.anthropic.com`` is public).
"""

import random
import unicodedata

from django.conf import settings
from django.db import IntegrityError

from .telemetry import redact_secrets

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
# A small, fast model is plenty for a one-letter classification done once per
# distinct player. Overridable via env in case the id is rotated.
CLAUDE_GENDER_MODEL = getattr(
    settings, "CLAUDE_GENDER_MODEL", ""
) or "claude-haiku-4-5-20251001"
CLAUDE_MAX_TOKENS = 8
CLAUDE_TIMEOUT = 30
CLAUDE_RETRY_STATUSES = (429, 500, 502, 503, 504, 529)

_SYSTEM = (
    "You determine the most likely gender of a tennis player from their name. "
    "The name may be in 'Lastname, Firstname' order and from any country. "
    "Reply with EXACTLY ONE letter and nothing else: 'M' if the name is most "
    "likely male, 'F' if most likely female, 'U' if it is ambiguous or you "
    "cannot tell."
)

# Inference code -> output schema gender. Unknown/ambiguous map to empty.
_OUT = {"M": "M", "F": "F", "U": ""}


def resolve_claude_keys(scraper):
    """Per-scraper key (Lab -> Settings), else the env-sourced settings list."""
    scraper_key = (getattr(scraper, "claude_api_key", "") or "").strip()
    if scraper_key:
        return [k.strip() for k in scraper_key.split(",") if k.strip()]
    return [k for k in (getattr(settings, "CLAUDE_KEYS", []) or []) if k]


def _norm_key(name):
    """Accent-strip + lower-case + collapse whitespace for a stable cache key."""
    decomposed = unicodedata.normalize("NFKD", str(name or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def _ask_claude(client, key, name):
    """Ask Claude for one gender letter. Returns ``"M"``/``"F"``/``"U"`` on a
    successful answer, or ``None`` on a transient failure (so it isn't cached)."""
    payload = {
        "model": CLAUDE_GENDER_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": name}],
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = client.post(
        CLAUDE_API_URL,
        headers=headers,
        json=payload,
        timeout=CLAUDE_TIMEOUT,
        retry_statuses=CLAUDE_RETRY_STATUSES,
    )
    if resp is None:
        client.tele.record_error("Claude gender request failed (no response).")
        return None
    if resp.status_code != 200:
        snippet = (resp.text or "")[:300]
        client.tele.record_error(
            redact_secrets(f"Claude gender API HTTP {resp.status_code}: {snippet}")
        )
        client.log("WARN", f"\u26a0\ufe0f Claude gender API HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
        blocks = data.get("content") or []
        text = (blocks[0].get("text", "") if blocks else "").strip().upper()
    except Exception:  # noqa: BLE001 - body wasn't the expected JSON
        client.tele.record_error("Claude gender response was not valid JSON.")
        return None
    for ch in text:
        if ch in ("M", "F", "U"):
            return ch
    return "U"


def resolve_gender(client, claude_keys, name):
    """Return ``"M"`` / ``"F"`` / ``""`` for ``name`` (cached by normalised name).

    ``client`` is a live :class:`ScraperClient` (used for its ``post`` plus its
    ``log`` / ``tele``). ``claude_keys`` is a list of API keys; an empty list
    degrades to ``""`` (no inference) without failing.
    """
    key = _norm_key(name)
    if not key:
        return ""
    try:
        row = PlayerGenderCache.objects.filter(name_key=key).only("gender").first()
    except Exception:  # noqa: BLE001 - a cache read must never kill a run
        row = None
    if row is not None:
        return _OUT.get(row.gender, "")
    if not claude_keys:
        return ""
    code = _ask_claude(client, random.choice(claude_keys), name)
    if code is None:
        # Transient failure - don't poison the cache; retry on a later run.
        return ""
    try:
        PlayerGenderCache.objects.get_or_create(
            name_key=key,
            defaults={"gender": code, "display_name": (name or "")[:255]},
        )
    except IntegrityError:
        # Another worker thread cached the same name first - harmless.
        pass
    except Exception:  # noqa: BLE001 - a cache write must never kill a run
        pass
    return _OUT.get(code, "")


# Imported late to keep this module importable without a configured app registry
# at import time (mirrors how other live-scraper helpers touch the ORM lazily).
from accounts.models import PlayerGenderCache  # noqa: E402
