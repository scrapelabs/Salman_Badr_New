"""Resolve a full country name to its 3-letter code — known table + Claude.

The GLTA production source derived ``tournament_country_code`` from the
per-tournament country name via a fixed lookup table of known codes, falling
back to Claude for any country not in the table (``Utils.convert_full_country``
in the source). Notably the table keys are plain lower-cased names — GLTA's
dominant ``"U.S.A."`` is **not** a key (only ``"usa"`` is), so the Claude path
is the common case there, exactly as in the source. There is no other
fallback: if Claude can't answer, the code stays blank.

``KNOWN_CODES`` below is a byte-for-byte port of the source's table.

The Claude key lives only in the request header — it is never logged, and any
error body is scrubbed with :func:`redact_secrets` before it reaches
telemetry. The request goes through the caller's :class:`~accounts.
live_scrapers._http.ScraperClient`, so it passes the same central SSRF guard
as every other request. Answers are cached per worker process (one Claude call
per distinct country per run; the source called per row instead).
"""

import random
import threading

from django.conf import settings

from .telemetry import redact_secrets

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_COUNTRY_MODEL = getattr(
    settings, "CLAUDE_GENDER_MODEL", ""
) or "claude-haiku-4-5-20251001"
CLAUDE_MAX_TOKENS = 8
CLAUDE_TIMEOUT = 30
CLAUDE_RETRY_STATUSES = (429, 500, 502, 503, 504, 529)

_SYSTEM = (
    "You convert a country name to its 3-letter country code as used in "
    "international tennis (IOC style, e.g. 'United States' -> 'USA', "
    "'Spain' -> 'ESP', 'Germany' -> 'GER'). The name may be abbreviated or "
    "punctuated (e.g. 'U.S.A.'). Reply with EXACTLY the 3-letter uppercase "
    "code and nothing else. If the input is not a country you recognise, "
    "reply 'UNK'."
)

# The GLTA source's known-codes table (``Utils.known_codes``), ported exactly.
KNOWN_CODES = {
    "afghanistan": "AFG", "albania": "ALB", "algeria": "DZA", "angola": "ANG",
    "argentina": "ARG", "armenia": "ARM", "australia": "AUS", "austria": "AUT",
    "azerbaijan": "AZE", "bahrain": "BHR", "bangladesh": "BGD", "belarus": "BLR",
    "belgium": "BEL", "bolivia": "BOL", "bosnia": "BIH", "brazil": "BRA",
    "bulgaria": "BGR", "cameroon": "CMR", "canada": "CAN", "chile": "CHI",
    "china": "CHN", "colombia": "COL", "congo": "COD", "croatia": "CRO",
    "cuba": "CUB", "czech republic": "CZE", "czechia": "CZE", "denmark": "DEN",
    "ecuador": "ECU", "egypt": "EGY", "england": "ENG", "ethiopia": "ETH",
    "finland": "FIN", "france": "FRA", "georgia": "GEO", "germany": "GER",
    "ghana": "GHA", "greece": "GRE", "great britain": "BRI", "hungary": "HUN",
    "india": "IND", "indonesia": "IDN", "iran": "IRI", "iraq": "IRQ",
    "ireland": "IRL", "israel": "ISR", "italy": "ITA", "ivory coast": "CIV",
    "jamaica": "JAM", "japan": "JPN", "jordan": "JOR", "kazakhstan": "KAZ",
    "kenya": "KEN", "kuwait": "KUW", "kyrgyzstan": "KGZ", "latvia": "LAT",
    "lebanon": "LIB", "libya": "LBA", "lithuania": "LTU", "luxembourg": "LUX",
    "malaysia": "MAS", "mali": "MLI", "mexico": "MEX", "moldova": "MDA",
    "mongolia": "MGL", "morocco": "MAR", "mozambique": "MOZ", "myanmar": "MYA",
    "namibia": "NAM", "netherlands": "NED", "new zealand": "NZL", "nigeria": "NGR",
    "north korea": "PRK", "norway": "NOR", "oman": "OMA", "pakistan": "PAK",
    "palestine": "PLE", "panama": "PAN", "paraguay": "PAR", "peru": "PER",
    "philippines": "PHI", "poland": "POL", "portugal": "POR", "qatar": "QAT",
    "romania": "ROU", "russia": "RUS", "saudi arabia": "KSA", "senegal": "SEN",
    "serbia": "SRB", "scotland": "SCO", "slovakia": "SVK", "slovenia": "SLO",
    "somalia": "SOM", "south africa": "RSA", "south korea": "KOR", "spain": "ESP",
    "sri lanka": "SRI", "sudan": "SDN", "sweden": "SWE", "switzerland": "SUI",
    "syria": "SYR", "taiwan": "TPE", "tajikistan": "TJK", "tanzania": "TAN",
    "thailand": "THA", "tunisia": "TUN", "turkey": "TUR", "turkmenistan": "TKM",
    "ukraine": "UKR", "united arab emirates": "UAE", "uae": "UAE",
    "united kingdom": "GBR", "uk": "GBR", "united states": "USA", "usa": "USA",
    "united states of america": "USA", "uruguay": "URU", "uzbekistan": "UZB",
    "venezuela": "VEN", "vietnam": "VIE", "wales": "WAL", "yemen": "YEM",
    "zambia": "ZAM", "zimbabwe": "ZIM",
}

# Per-process cache: one Claude call per distinct country per run (the worker
# is a fresh process per run). Transient failures are NOT cached so a later
# row retries.
_cache = {}
_cache_lock = threading.Lock()


def _ask_claude(client, key, country_name):
    """Ask Claude for a 3-letter code. Returns the code, ``""`` for a definite
    "not a country" answer, or ``None`` on a transient failure (not cached)."""
    payload = {
        "model": CLAUDE_COUNTRY_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": country_name}],
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
        client.tele.record_error("Claude country-code request failed (no response).")
        return None
    if resp.status_code != 200:
        snippet = (resp.text or "")[:300]
        client.tele.record_error(
            redact_secrets(f"Claude country-code API HTTP {resp.status_code}: {snippet}")
        )
        client.log("WARN", f"\u26a0\ufe0f Claude country-code API HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
        blocks = data.get("content") or []
        text = (blocks[0].get("text", "") if blocks else "").strip().upper()
    except Exception:  # noqa: BLE001 - body wasn't the expected JSON
        client.tele.record_error("Claude country-code response was not valid JSON.")
        return None
    if text == "UNK":
        return ""
    if len(text) == 3 and text.isalpha():
        return text
    return ""


def resolve_country_code(client, claude_keys, country_name):
    """Return the 3-letter code for ``country_name`` — known table, else Claude.

    Mirrors the GLTA source's ``convert_full_country``: the known-codes table
    wins; anything else is asked to Claude (cached per process). Returns ``""``
    when the name is blank, Claude has no keys, or the call fails.
    """
    key = (country_name or "").strip().lower()
    if not key:
        return ""
    if key in KNOWN_CODES:
        return KNOWN_CODES[key]
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    if not claude_keys:
        return ""
    code = _ask_claude(client, random.choice(claude_keys), country_name.strip())
    if code is None:
        return ""  # transient - don't cache, retry on a later row
    with _cache_lock:
        _cache.setdefault(key, code)
    return code
