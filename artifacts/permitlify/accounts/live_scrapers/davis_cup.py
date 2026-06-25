"""Davis Cup (ITF men's team competition) scraper.

Thin wrapper over the shared :mod:`accounts.live_scrapers._stadion` logic — the
Davis Cup and the Billie Jean King Cup share one public ITF / Stadion API,
differing only by draw code, id type, gender and the public match URL.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _stadion

CONFIG = _stadion.StadionConfig(
    label="Davis Cup",
    draw_code="dc",
    id_type="DavisCup",
    gender_full="Male",
    gender_short="M",
    url_builder=lambda tie_id, match_id: (
        f"https://www.daviscup.com/en/match/{match_id}"
    ),
)


def run(run_obj, log):
    return _stadion.run(CONFIG, run_obj, log)
