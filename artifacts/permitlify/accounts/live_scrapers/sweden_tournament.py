"""Sweden individual tournaments (svtf.tournamentsoftware.com).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_tournament`
engine. ``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.

Player gender is inferred from names via Claude **only** (no fallback), exactly
like the original source: its ranking/players pass fed each new player's name to
``format_name_gender_claude`` and stored the resulting gender, which the detail
pass then looked up per player. The tournament/draw names carry no gender word,
so the draw-name heuristic yields nothing — hence Claude, same contract as
Sweden League / Finland / Croatia / Estonia. If no Anthropic key is configured
the run honest-fails and asks for the key rather than emitting genderless rows.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="Sweden Tournament",
    base="https://svtf.tournamentsoftware.com",
    country="Sweden",
    country_code="SWE",
    sanction_body="Sweden",
    # Gender via Claude only (no fallback); honest-fail if no key. See Sweden League.
    claude_gender=True,
    claude_gender_required=True,
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
