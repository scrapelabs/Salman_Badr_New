"""FIP (padelfip.com) player-ranking scraper.

A two-stage rankings scraper, like the source:

1. **Discover** — paginate the WordPress ranking endpoint
   ``/wp-json/fip/v1/ranking/load-more`` for the men's **and** women's
   ``master`` tables (the source distinguishes the two via the ``gender`` param),
   enumerating each ranked player's name, profile URL, and — when the endpoint
   exposes them — rank / points / nationality.
2. **Enrich** — fetch each player's profile page concurrently for the remaining
   details (birthdate, plus authoritative rank / points / nationality fallbacks)
   and emit one row per player.

FIP exposes no ranking-publication date, so the snapshot date is simply the
run's date (read off ``run_obj`` exactly like the WTA/ATP scrapers) and stored
in the ``Rankdate`` column; ``Ranktype`` is the constant ``"FIP"``.

**Deterministic / AI-free port.** The original fed every player name through an
OpenAI call (``convert_name_gpt``) to split it into first/last name. That is
dropped — the scraped full name is kept and, where the shared 9-column schema
wants a ``"Lastname, Firstname"`` form, it is derived with a plain whitespace
split (last token = surname). No network, no AI.

padelfip.com sits behind a CDN; the shared :class:`ScraperClient` impersonates
Chrome, but without a residential proxy that clears the CDN the run **fails
honestly** (empty discovery → 0 rows → FAILED), exactly like the ATP scraper.

Returns the standard runner 5-tuple
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from django.db.models import F

from accounts.models import Run

from . import _rankings
from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets

# WordPress ranking endpoint the front-end "load more" button calls.
LOAD_MORE_URL = "https://www.padelfip.com/wp-json/fip/v1/ranking/load-more"
# The source paginates 500 rows at a time; this safety cap stops a misbehaving
# endpoint (non-empty forever) from looping without end.
PAGE_SIZE = 500
MAX_PAGES = 50
# The two tables every run collects, mapped to the schema's gender code.
GENDERS = (("male", "M"), ("female", "F"))
# This is a single, FIP-wide ranking (no singles/doubles split), so Ranktype is
# the constant the task specifies rather than _rankings.RANK_TYPES.
RANK_TYPE = "FIP"

_JSON_ACCEPT = {"Accept": "*/*"}


# --- deterministic helpers (AI-free ports of the source's formatters) ------
def _format_name(full_name):
    """Derive ``"Lastname, Firstname"`` from a full name with a plain split.

    The source used an OpenAI call to decide which tokens were the surname; this
    AI-free port treats the **last whitespace-delimited token** as the surname
    and everything before it as the first name(s). A single-token name is kept
    as-is.
    """
    cleaned = re.sub(r"\s+", " ", (full_name or "").strip())
    if not cleaned:
        return ""
    parts = cleaned.split(" ")
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _fip_id(url):
    """The FIP player id — the last path segment of the profile URL."""
    path = urlparse(url or "").path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def _first(mapping, *keys):
    """Return the first non-empty value among ``keys`` in ``mapping``."""
    for key in keys:
        value = (mapping or {}).get(key)
        if value not in (None, ""):
            return value
    return ""


def _field(sel, xpath):
    """Return normalised text for ``xpath``.

    For an ``xpath`` already targeting ``text()`` / ``@attr`` the first node is
    taken verbatim; otherwise every descendant text node is gathered so element
    selectors (e.g. the source's ``//h2[...]``) yield their full text.
    """
    stripped = xpath.rstrip()
    if stripped.endswith("text()") or "/@" in stripped:
        value = sel.xpath(xpath).get() or ""
    else:
        value = " ".join(t for t in sel.xpath(xpath + "//text()").getall())
    return re.sub(r"\s+", " ", value).strip()


def _results_list(payload):
    """Normalise the load-more payload to a list of player records."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "players", "items", "ranking", "rankings"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


# --- phase 1 · discovery ---------------------------------------------------
def _load_more_params(gender, page, snap):
    """Build the load-more query for one page of one gender's table."""
    year, week, _weekday = snap.isocalendar()
    return {
        "gender": gender,
        "category": "master",
        "year": year,
        "week": week,
        "offset": PAGE_SIZE * page,
        "limit": PAGE_SIZE,
        "country": "",
    }


def _discover(client, gender, gender_code, snap, log, seen):
    """Paginate one gender's ranking table; return the enumerated players."""
    players = []
    for page in range(MAX_PAGES):
        payload = client.get_json(
            LOAD_MORE_URL,
            params=_load_more_params(gender, page, snap),
            headers=_JSON_ACCEPT,
        )
        results = _results_list(payload)
        if not results:
            break  # empty page (or a hard failure) — end of this table
        for result in results:
            url = _first(result, "url", "link", "permalink") or ""
            player_id = _fip_id(url)
            if not player_id or player_id in seen:
                continue
            seen.add(player_id)
            players.append({
                "player_id": player_id,
                "url": url,
                "gender": gender_code,
                "name": _first(result, "name", "full_name", "player_name") or "",
                "rank": _first(result, "position", "ranking", "rank", "pos"),
                "points": _first(result, "points", "point", "score"),
                "nationality": _first(
                    result, "country", "nationality", "country_code", "flag"
                ),
            })
        log("INFO", f"   \U0001f50e {gender} page {page + 1}: {len(results)} row(s)")
    return players


# --- phase 2 · enrichment --------------------------------------------------
def _enrich_one(client, player):
    """Fetch a player's profile page and return a finished row dict, or ``None``.

    Mirrors the source: a row is only emitted when the profile page is fetched
    successfully (it is the only source of the birthdate).
    """
    sel = client.get_selector(player["url"])
    if sel is None:
        return None

    full_name = _field(sel, '//h2[contains(@class, "player__name")]') or player["name"]
    dob_raw = _field(
        sel,
        '//div[@class="section__additionalInfo"]'
        '//div[@class="additionalInfo__birth"]'
        '//span[@class="additionalInfo__data"]',
    )
    page_rank = _field(sel, '//span[contains(@class, "player__number")]/text()')
    page_points = _field(sel, '//span[contains(@class, "player__pointTNumber")]')
    page_country = _field(sel, '//p[contains(@class, "player__country")]')

    rank = re.sub(r"[^\d]", "", str(player["rank"] or page_rank))
    points = (str(player["points"]) if player["points"] != "" else page_points).strip()
    nationality = str(player["nationality"] or page_country).strip()

    return {
        "birthdate": _rankings.to_mdy(dob_raw, "%d/%m/%Y"),
        "gender": player["gender"],
        "player_id": player["player_id"],
        "name": _format_name(full_name),
        "nationality": nationality,
        "points": points,
        "rank": rank,
        "rankdate": _rankings.to_mdy(player["rankdate_iso"], "%Y-%m-%d"),
        "ranktype": RANK_TYPE,
    }


def run(run_obj, log):
    """Execute the FIP rankings scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    snap = _rankings.snapshot_date(run_obj)
    date_iso = snap.isoformat()
    log("INFO", f"\U0001f3be FIP (padelfip) rankings starting \u2014 snapshot {date_iso}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering ranked players \u2500\u2500\u2500\u2500")
    players = []
    seen = set()
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        for gender, gender_code in GENDERS:
            found = _discover(discovery, gender, gender_code, snap, log, seen)
            for player in found:
                player["rankdate_iso"] = date_iso
            players.extend(found)
            log(
                "INFO",
                f"   \U0001f3c6 {len(found)} {gender_code} player(s) collected",
            )

    total = len(players)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} player(s) to enrich")

    if total == 0:
        iso = snap.isocalendar()
        tele.record_error(
            f"FIP load-more returned no ranked players for {iso[0]}-W{iso[1]:02d}. "
            "The FIP ranking API only publishes the current ranking week, so "
            "historical snapshot dates return no data. Re-run with the current "
            "week's snapshot date."
        )
        log(
            "WARN",
            "\u26a0\ufe0f No players discovered \u2014 FIP serves only the current "
            "ranking week.",
        )

    csv_out = _rankings.RankingsCsv()

    def process(chunk):
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            for player in chunk:
                try:
                    row = _enrich_one(client, player)
                    if row:
                        csv_out.add(row)
                        log(
                            "INFO",
                            f"   \U0001f3c6 {row['gender']} #{row['rank'] or '?'}: "
                            f"{row['name'] or '?'} ({row['nationality'] or '?'})",
                        )
                except Exception as exc:  # noqa: BLE001 - one bad player can't kill the run
                    tele.record_error(
                        redact_secrets(
                            f"Player {player.get('player_id', '')} failed: {exc}"
                        ),
                        exc=exc,
                    )
                finally:
                    Run.objects.filter(pk=run_obj.pk).update(
                        progress_done=F("progress_done") + 1
                    )
        finally:
            client.close()

    if players:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 enriching players \u2500\u2500\u2500\u2500")
        n = max(1, min(workers, len(players)))
        chunks = [players[i::n] for i in range(n)]
        with ThreadPoolExecutor(max_workers=n) as executor:
            list(executor.map(process, chunks))

    row_count = csv_out.row_count
    log("INFO", "\u2500\u2500\u2500\u2500 summary \u2500\u2500\u2500\u2500")
    log("INFO", f"\U0001f4be Writing {row_count} row(s) to CSV")
    log(
        "INFO",
        f"\U0001f4ca Telemetry: {tele.request_count} request(s), "
        f"{tele.error_count} error(s)",
    )
    status = Run.Status.SUCCESS if row_count else Run.Status.FAILED
    icon = "\U0001f3c1" if status == Run.Status.SUCCESS else "\U0001f6d1"
    log("INFO", f"{icon} Run finished \u2014 status={status}, rows={row_count}")
    return (
        csv_out.value(),
        tele.requests_csv(),
        tele.errors_csv(),
        row_count,
        status,
    )
