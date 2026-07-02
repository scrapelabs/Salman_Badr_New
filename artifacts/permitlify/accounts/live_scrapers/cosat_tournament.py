"""COSAT South American tournaments (cosat.tournamentsoftware.com).

A **dynamic-country** tournamentsoftware.com site: the Confederación Sudamericana
de Tenis aggregates tournaments from across South America on one host, so the
country is read per-tournament (from the search location) and per-player (from the
profile flag) rather than being a federation constant; ``id_type`` / import-source
/ sanction are the fixed ``COSAT`` org label. Thin wrapper over the shared
:mod:`accounts.live_scrapers._ts_tournament` engine in ``dynamic_country`` mode.

Player gender is inferred from the player's name via Claude (the source's
``format_name_gender_claude`` at ranking-registry time — hard requirement, no
draw-name fallback), and DOB comes from the site-wide **ranking tab**: the
source pre-built a player registry from every ``More`` category list on
``/ranking/`` (profile link ``td[5]``, **full DOB** ``td[6]``, 100 rows/page)
and joined match players against it by the ``/player-profile/<GUID>`` tail —
``ranking_dob`` + ``ranking_dob_full_date`` reproduce that walk exactly.
Unranked players keep a blank DOB, exactly like the source's registry miss.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="COSAT Tournament",
    base="https://cosat.tournamentsoftware.com",
    country="",        # unused in dynamic-country mode (read per tournament)
    country_code="",   # unused in dynamic-country mode (country[0:3] per row)
    sanction_body="",  # unused in dynamic-country mode (see org_label)
    dynamic_country=True,
    id_type_label="COSAT",
    org_label="COSAT",
    # en-US cookiewall locale, exactly like the source ("cookiewall?lcid=1033"):
    # COSAT ignores the default en-GB 2057 and would stay Spanish ("Más" More
    # links, d/m/Y ranking dates the registry parse would reject).
    lcid="1033",
    claude_gender=True,
    claude_gender_required=True,
    ranking_dob=True,
    ranking_dob_full_date=True,
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
