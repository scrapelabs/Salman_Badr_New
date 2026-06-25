"""GLTA international tournaments (Gay & Lesbian Tennis Alliance / glta.tournamentsoftware.com).

A **dynamic-country** tournamentsoftware.com site: GLTA aggregates tournaments
from many countries on one host, so the country is read per-tournament (from the
search location) and per-player (from the profile flag) rather than being a
federation constant, while ``id_type`` / import-source / sanction are the fixed
``GLTA`` org label. Thin wrapper over the shared
:mod:`accounts.live_scrapers._ts_tournament` engine in ``dynamic_country`` mode.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="GLTA Tournament",
    base="https://glta.tournamentsoftware.com",
    country="",        # unused in dynamic-country mode (read per tournament)
    country_code="",   # unused in dynamic-country mode (country[0:3] per row)
    sanction_body="",  # unused in dynamic-country mode (see org_label)
    dynamic_country=True,
    id_type_label="GLTA",
    org_label="GLTA",
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
