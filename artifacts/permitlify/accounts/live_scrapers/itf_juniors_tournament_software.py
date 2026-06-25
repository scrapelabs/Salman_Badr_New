"""ITF Juniors tournaments (itfjuniors.tournamentsoftware.com).

A **dynamic-country** tournamentsoftware.com site: the ITF Junior circuit
aggregates tournaments worldwide on one host, so the country is read
per-tournament (from the search location) and per-player (from the profile flag)
rather than being a federation constant; ``id_type`` / import-source / sanction
are the fixed ``ITF Juniors`` org label. Thin wrapper over the shared
:mod:`accounts.live_scrapers._ts_tournament` engine in ``dynamic_country`` mode.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="ITF Juniors",
    base="https://itfjuniors.tournamentsoftware.com",
    country="",        # unused in dynamic-country mode (read per tournament)
    country_code="",   # unused in dynamic-country mode (country[0:3] per row)
    sanction_body="",  # unused in dynamic-country mode (see org_label)
    dynamic_country=True,
    id_type_label="ITF Juniors",
    org_label="ITF Juniors",
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
