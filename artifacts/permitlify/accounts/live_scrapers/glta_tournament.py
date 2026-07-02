"""GLTA international tournaments (Gay & Lesbian Tennis Alliance / glta.tournamentsoftware.com).

A **dynamic-country** tournamentsoftware.com site: GLTA aggregates tournaments
from many countries on one host, so the country is read per-tournament (from the
search location) and per-player (from the profile flag) rather than being a
federation constant, while ``id_type`` / import-source / sanction are the fixed
``GLTA`` org label. Thin wrapper over the shared
:mod:`accounts.live_scrapers._ts_tournament` engine in ``dynamic_country`` mode.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.

Source-faithful specifics (glta_tournament production source):

- ``lcid="1033"`` — the source hit the cookiewall with ``?lcid=1033`` (en-US)
  and parsed search/tournament dates with ``%m/%d/%Y``. The site renders
  ``m/d/Y`` (non-padded, e.g. ``3/17/2024``) regardless of locale, so the
  engine's en-GB default of ``%d/%m/%Y`` blanked or silently swapped every
  tournament start/end date.
- ``claude_country=True`` — the source mapped the tournament country name to
  its 3-letter code via a known-codes table with a **Claude** fallback
  (``Utils.convert_full_country``); GLTA's dominant ``"U.S.A."`` is not a
  table key, so Claude is the common case. No other fallback: a Claude key
  is required to run.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="GLTA Tournament",
    base="https://glta.tournamentsoftware.com",
    country="",        # unused in dynamic-country mode (read per tournament)
    country_code="",   # unused in dynamic-country mode (resolved per row)
    sanction_body="",  # unused in dynamic-country mode (see org_label)
    dynamic_country=True,
    id_type_label="GLTA",
    org_label="GLTA",
    lcid="1033",       # en-US cookiewall + m/d/Y dates, like the source
    claude_country=True,
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
