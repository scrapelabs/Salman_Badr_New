"""Croatia League (Croatian Tennis Association / hts.tournamentsoftware.com).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_league` engine —
every tournamentsoftware.com league site shares one markup and endpoint set,
differing only by host and a few constant fields. ``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_league

CONFIG = _ts_league.TSLeagueConfig(
    label="Croatia League",
    base="https://hts.tournamentsoftware.com",
    country="Croatia",
    country_code="CRO",
    sanction_body="Croatian Tennis Association",
    # League names often carry no gender word ("Prva liga"), so infer each
    # player's gender from their name via Claude (cached), exactly like the
    # original source (format_name_gender_claude per player). HARD mode per
    # user directive: no key -> honest-fail the run, never fall back to
    # league-name gender (same contract as Finland / Estonia / Tennis Europe).
    claude_gender=True,
    claude_gender_required=True,
)


def run(run_obj, log):
    return _ts_league.run(CONFIG, run_obj, log)
