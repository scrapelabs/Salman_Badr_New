"""Hong Kong individual tournaments (hkta.tournamentsoftware.com).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_tournament`
engine. ``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="Hong Kong Tournament",
    base="https://hkta.tournamentsoftware.com",
    country="Hong Kong",
    country_code="HKG",
    sanction_body="Hong Kong",
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
