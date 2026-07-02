"""Tennis Europe junior tournaments (te.tournamentsoftware.com).

A **dynamic-country** tournamentsoftware.com site: Tennis Europe aggregates
tournaments from across Europe on one host, so the country is read per-tournament
(from the search location) and per-player (from the profile flag) rather than
being a federation constant. Its ``id_type`` is ``Europe`` while its
import-source / sanction body is ``Tennis Europe``. Thin wrapper over the shared
:mod:`accounts.live_scrapers._ts_tournament` engine in ``dynamic_country`` mode.

Like the production source, this scraper is **Claude-dependent for gender**
(``claude_gender`` + ``claude_gender_required``): junior draw names ("BS14",
"GS16") carry no reliable gender word, so each player's gender is inferred from
their name via Claude — no key means the run fails honestly and asks for one,
matching the Finland / Estonia contract. DOB comes from the site-wide **ranking
tab** (``ranking_dob``): junior profiles hide DOB/YOB, so a pre-phase walks the
Tennis Europe Ranking's category lists and joins each match player to the
recorded ``1/1/<YOB>`` by profile GUID, exactly like the source's player
registry (unranked players keep a blank DOB — no fallback).
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _ts_tournament

CONFIG = _ts_tournament.TSTournamentConfig(
    label="Tennis Europe",
    base="https://te.tournamentsoftware.com",
    country="",        # unused in dynamic-country mode (read per tournament)
    country_code="",   # unused in dynamic-country mode (country[0:3] per row)
    sanction_body="",  # unused in dynamic-country mode (see org_label)
    dynamic_country=True,
    id_type_label="Europe",
    org_label="Tennis Europe",
    claude_gender=True,
    claude_gender_required=True,
    ranking_dob=True,
)


def run(run_obj, log):
    return _ts_tournament.run(CONFIG, run_obj, log)
