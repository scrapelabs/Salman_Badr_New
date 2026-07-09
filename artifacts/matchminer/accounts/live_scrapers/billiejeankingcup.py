"""Billie Jean King Cup (ITF Fed Cup) scraper.

Thin wrapper over the shared :mod:`accounts.live_scrapers._stadion` logic — the
BJK Cup and its sibling team competitions (e.g. Davis Cup) share one public ITF
/ Stadion API, differing only by draw code and a few constant fields.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _stadion

CONFIG = _stadion.StadionConfig(
    label="Billie Jean King Cup",
    draw_code="bjkc",
    id_type="Fedcup",
    gender_full="Female",
    gender_short="F",
    url_builder=lambda tie_id, match_id: (
        f"https://www.billiejeankingcup.com/en/tie/{tie_id}"
    ),
)


def run(run_obj, log):
    return _stadion.run(CONFIG, run_obj, log)
