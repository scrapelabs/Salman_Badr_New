"""Ireland individual tournaments (Tennis Ireland / ti.tournamentsoftware.com).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_tournament`
engine. ``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="Ireland Tournament",
    base="https://ti.tournamentsoftware.com",
    country="Ireland",
    country_code="IRE",
    sanction_body="Ireland",
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
