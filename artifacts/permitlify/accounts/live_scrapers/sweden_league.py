"""Sweden League (Tennis Sweden / svtf.tournamentsoftware.com).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_league` engine.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.

Player gender is inferred from names via Claude **only** (no fallback), exactly
like the original source: its ranking pass fed each new player's name to
``format_name_gender_claude`` and stored the resulting gender, which the detail
pass then looked up per player. The league/draw names carry no gender word, so
the draw-name heuristic yields nothing — hence Claude, same contract as Finland
/ Croatia / Estonia. If no Anthropic key is configured the run honest-fails and
asks for the key rather than emitting genderless rows.
"""

from . import _ts_league

CONFIG = _ts_league.TSLeagueConfig(
    label="Sweden League",
    base="https://svtf.tournamentsoftware.com",
    country="Sweden",
    country_code="SWE",
    sanction_body="Tennis Sweden",
    # Gender via Claude only (no fallback); honest-fail if no key. See Finland.
    claude_gender=True,
    claude_gender_required=True,
)


def run(run_obj, log):
    return _ts_league.run(CONFIG, run_obj, log)
