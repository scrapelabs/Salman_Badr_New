"""Finland individual tournaments (Tennis Finland / www.tennisassa.fi).

Thin wrapper over the shared :mod:`accounts.live_scrapers._ts_tournament`
engine. tennisassa.fi is a tournamentsoftware.com white-label, so it shares the
same markup and endpoints as the other federations. ``run(run_obj, log)``
returns ``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="Finland Tournament",
    base="https://www.tennisassa.fi",
    country="Finland",
    country_code="FIN",
    sanction_body="Tennis Finland",
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
