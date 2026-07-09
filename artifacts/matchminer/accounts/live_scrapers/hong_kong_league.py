"""Hong Kong League (hkta.tournamentsoftware.com).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_league` engine. The
production spider sets the sanction body equal to the country name ("Hong Kong").
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_league

CONFIG = _ts_league.TSLeagueConfig(
    label="Hong Kong League",
    base="https://hkta.tournamentsoftware.com",
    country="Hong Kong",
    country_code="HKG",
    sanction_body="Hong Kong",
)


def run(run_obj, log):
    return _ts_league.run(CONFIG, run_obj, log)
