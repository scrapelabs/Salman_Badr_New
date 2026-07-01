"""Finland individual tournaments (Tennis Finland / www.tennisassa.fi).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_tournament`
engine. tennisassa.fi is a tournamentsoftware.com white-label, so it shares the
same markup and endpoints as the other federations. ``run(run_obj, log)``
returns ``(items_csv, requests_csv, errors_csv, row_count, status)``.

Player gender is inferred from names via Claude **only** (no fallback), exactly
like the original source and the Estonia scraper; if no Anthropic key is
configured the run honest-fails and asks for the key rather than emitting
genderless rows (see ``claude_gender`` / ``claude_gender_required`` on the
engine config).
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="Finland Tournament",
    base="https://www.tennisassa.fi",
    country="Finland",
    country_code="FIN",
    sanction_body="Tennis Finland",
    # Gender via Claude only (no fallback); honest-fail if no key. See Estonia.
    claude_gender=True,
    claude_gender_required=True,
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
