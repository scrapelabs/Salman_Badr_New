"""Davis Cup scraper.

Thin wrapper over the shared :mod:`accounts.live_scrapers._stadion` logic. The
Davis Cup uses the same public ITF / Stadion data API as the Billie Jean King
Cup, with draw code ``dc``, men's fields and a ``daviscup.com`` match URL.
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
