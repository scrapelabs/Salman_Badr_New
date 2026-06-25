"""Finland League (Tennis Finland / www.tennisassa.fi).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_league` engine. The
Finnish federation runs the same tournamentsoftware platform on its own domain
(``www.tennisassa.fi``), so the host is the only meaningful difference. ``run(run_obj,
log)`` returns ``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_league

CONFIG = _ts_league.TSLeagueConfig(
    label="Finland League",
    base="https://www.tennisassa.fi",
    country="Finland",
    country_code="FIN",
    sanction_body="Tennis Finland",
)


def run(run_obj, log):
    return _ts_league.run(CONFIG, run_obj, log)
