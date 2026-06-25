"""Denmark individual tournaments (Danish Tennis Federation / dtf.tournamentsoftware.com).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_tournament`
engine. ``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="Denmark Tournament",
    base="https://dtf.tournamentsoftware.com",
    country="Denmark",
    country_code="DEN",
    sanction_body="Danish Tennis Federation",
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
