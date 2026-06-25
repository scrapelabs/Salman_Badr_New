"""Sweden League (Tennis Sweden / svtf.tournamentsoftware.com).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_league` engine.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_league

CONFIG = _ts_league.TSLeagueConfig(
    label="Sweden League",
    base="https://svtf.tournamentsoftware.com",
    country="Sweden",
    country_code="SWE",
    sanction_body="Tennis Sweden",
)


def run(run_obj, log):
    return _ts_league.run(CONFIG, run_obj, log)
