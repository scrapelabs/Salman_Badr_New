"""Tennis Wallonie-Bruxelles tournament results by date range.

The public TPPWB search discovers tournaments overlapping the requested dates.
Each tournament exposes its official metadata, published category/draw pairs,
double-encoded draw JSON, and player profile genders without authentication or
browser automation.
"""

import csv
import html
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urljoin, urlsplit

from django.db.models import F
from parsel import Selector

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from ._names import last_first
from .telemetry import Telemetry, redact_secrets, sanitize_cell

BASE = "https://tennis.tppwb.be"
SEARCH_URL = f"{BASE}/MyAFT/Competitions/TournamentSearchResultData"
DETAIL_URL = f"{BASE}/MyAFT/Competitions/TournamentDetail/{{tournament_id}}"
CATEGORIES_URL = f"{BASE}/MyAFT/Competitions/TournamentDetailDraw"
DRAW_DATA_URL = f"{BASE}/MyAFT/Competitions/GetTournamentDrawData"
PLAYER_URL = f"{BASE}/MyAFT/Players/Detail/{{player_id}}"
ALLOWED_HOSTS = ("tennis.tppwb.be",)
REGIONS = ("1", "3", "4", "6")
SEARCH_RESULT_LIMIT = 100

IMPORT_SOURCE = "Association Francophone de Tennis - Belgium"

COLUMNS = [
    "match_id",
    "ball_type",
    "draw_bracket_value",
    "draw_name",
    "draw_team_type",
    "tournament_name",
    "date",
    "round",
    "score",
    "winner_1_name",
    "winner_1_gender",
    "winner_1_third_party_id",
    "winner_1_city",
    "winner_1_country",
    "winner_1_state",
    "winner_2_name",
    "winner_2_gender",
    "winner_2_third_party_id",
    "winner_2_city",
    "winner_2_state",
    "loser_1_name",
    "loser_1_gender",
    "loser_1_third_party_id",
    "loser_1_city",
    "loser_1_state",
    "loser_1_country",
    "loser_2_name",
    "loser_2_gender",
    "loser_2_third_party_id",
    "loser_2_city",
    "loser_2_state",
    "outcome",
    "id_type",
    "draw_gender",
    "draw_bracket_type",
    "draw_type",
    "tournament_city",
    "tournament_state",
    "tournament_country_code",
    "tournament_host",
    "tournament_location_type",
    "tournament_surface",
    "tournament_event_category",
    "tournament_event_grade",
    "tournament_import_source",
    "tournament_sanction_body",
    "winner_2_country",
    "winner_2_college",
    "loser_2_country",
    "loser_2_college",
    "tournament_event_type",
    "winner_1_college",
    "loser_1_college",
    "tournament_url",
    "winner_1_dob",
    "winner_2_dob",
    "loser_1_dob",
    "loser_2_dob",
    "tournament_country",
    "tournament_start_date",
    "tournament_end_date",
]

HEADER = [
    "Match ID",
    "Ball Type",
    "Draw Bracket Value",
    "Draw Name",
    "Draw Team Type",
    "Tournament Name",
    "Date",
    "Round",
    "Score",
    "Winner 1 Name",
    "Winner 1 Gender",
    "Winner 1 Third Party ID",
    "Winner 1 City",
    "Winner 1 Country",
    "Winner 1 State",
    "Winner 2 Name",
    "Winner 2 Gender",
    "Winner 2 Third Party ID",
    "Winner 2 City",
    "Winner 2 State",
    "Loser 1 Name",
    "Loser 1 Gender",
    "Loser 1 Third Party ID",
    "Loser 1 City",
    "Loser 1 State",
    "Loser 1 Country",
    "Loser 2 Name",
    "Loser 2 Gender",
    "Loser 2 Third Party ID",
    "Loser 2 City",
    "Loser 2 State",
    "Outcome",
    "ID Type",
    "Draw Gender",
    "Draw Bracket Type",
    "Draw Type",
    "Tournament City",
    "Tournament State",
    "Tournament Country Code",
    "Tournament Host",
    "Tournament Location Type",
    "Tournament Surface",
    "Tournament Event Category",
    "Tournament Event Grade",
    "Tournament Import Source",
    "Tournament Sanction Body",
    "Winner 2 Country",
    "Winner 2 College",
    "Loser 2 Country",
    "Loser 2 College",
    "Tournament Event Type",
    "Winner 1 College",
    "Loser 1 College",
    "Tournament URL",
    "Winner 1 DOB",
    "Winner 2 DOB",
    "Loser 1 DOB",
    "Loser 2 DOB",
    "Tournament Country",
    "Tournament Start Date",
    "Tournament End Date",
]

assert len(COLUMNS) == len(HEADER) == 61

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
}
_FORM_HEADERS = {
    "Accept": "text/html, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}
_JSON_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}
_DETAIL_DATES_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+au\s+(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
_DETAIL_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
_TOURNAMENT_ID_PATTERNS = (
    re.compile(r"TournamentDetails?/(\d+)", re.IGNORECASE),
    re.compile(r"TournamentCategories/(\d+)", re.IGNORECASE),
    re.compile(r"[?&]IdTournoi=(\d+)", re.IGNORECASE),
)


def _clean(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _field(selector, xpath):
    value = selector.xpath(xpath).get()
    return _clean(value)


def _join_text(selector):
    return _clean(" ".join(selector.xpath(".//text()").getall()))


def _date_param(day):
    return day.strftime("%d/%m/%Y")


def _to_mdy(value):
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", _clean(value))
    if not match:
        return ""
    day, month, year = (int(part) for part in match.groups())
    return f"{month}/{day}/{year}"


def _official_dates(value):
    date_match = _DETAIL_DATES_RE.search(value or "")
    if date_match:
        return _to_mdy(date_match.group(1)), _to_mdy(date_match.group(2))
    dates = _DETAIL_DATE_RE.findall(value or "")
    if not dates:
        return "", ""
    start = _to_mdy(dates[0])
    return start, _to_mdy(dates[1]) if len(dates) > 1 else start


def _tournament_id(value):
    for pattern in _TOURNAMENT_ID_PATTERNS:
        match = pattern.search(value or "")
        if match:
            return match.group(1)
    return ""


def _response_text(client, response, context):
    if response is None:
        return ""
    if not (200 <= response.status_code < 300):
        client.tele.record_error(f"TPPWB HTTP {response.status_code}: {context}")
        return ""
    return response.text


def _search_once(client, start_date, end_date, regions=REGIONS):
    response = client.post(
        SEARCH_URL,
        data={
            "Region": ",".join(regions),
            "SearchByGeoloc": "false",
            "Radius": "",
            "Latitude": "",
            "Longitude": "",
            "ClubId": "",
            "PeriodStartDate": _date_param(start_date),
            "PeriodEndDate": _date_param(end_date),
            "WishedCategory": "",
            "CategoryTypes": "",
            "SingleCategoryValue": "",
            "DoubleCategoryValue": "",
            "OrderBy": "DATEASC",
        },
        headers=_FORM_HEADERS,
    )
    text = _response_text(
        client,
        response,
        f"tournament search {_date_param(start_date)}-{_date_param(end_date)}",
    )
    if not text:
        return None

    ids = []
    seen = set()
    for card in Selector(text=text).css("dl.grid-data-item"):
        source = " ".join(
            card.xpath(".//@data-url | .//@href").getall() + [card.get()]
        )
        tournament_id = _tournament_id(source)
        if tournament_id and tournament_id not in seen:
            seen.add(tournament_id)
            ids.append(tournament_id)
    return ids


def _discover_tournaments(client, start_date, end_date, log):
    search_count = 0

    def collect(window_start, window_end, regions=REGIONS):
        nonlocal search_count
        search_count += 1
        ids = _search_once(client, window_start, window_end, regions)
        if ids is None:
            return {}
        if len(ids) < SEARCH_RESULT_LIMIT:
            return {tournament_id: None for tournament_id in ids}

        if window_start < window_end:
            midpoint = window_start + (window_end - window_start) // 2
            merged = {tournament_id: None for tournament_id in ids}
            merged.update(collect(window_start, midpoint))
            merged.update(collect(midpoint + timedelta(days=1), window_end))
            return merged

        if len(regions) > 1:
            merged = {tournament_id: None for tournament_id in ids}
            for region in regions:
                region_ids = _search_once(
                    client,
                    window_start,
                    window_end,
                    (region,),
                )
                search_count += 1
                if region_ids is None:
                    continue
                merged.update({tournament_id: None for tournament_id in region_ids})
                if len(region_ids) >= SEARCH_RESULT_LIMIT:
                    client.tele.record_error(
                        "TPPWB search remained capped at 100 tournaments for "
                        f"region {region} on {_date_param(window_start)}"
                    )
            return merged

        return {tournament_id: None for tournament_id in ids}

    tournaments = collect(start_date, end_date)
    ordered = sorted(tournaments, key=lambda value: int(value))
    log(
        "INFO",
        f"TPPWB discovery found {len(ordered)} tournament(s) in "
        f"{search_count} search request(s)",
    )
    return ordered


def _tournament_metadata(client, tournament_id):
    tournament_url = DETAIL_URL.format(tournament_id=tournament_id)
    response = client.get(tournament_url, headers=_HTML_HEADERS)
    text = _response_text(client, response, f"tournament detail {tournament_id}")
    if not text:
        return None

    selector = Selector(text=text)
    spans = selector.xpath(
        '(//div[contains(@class, "tournament-detail-club")]/div[1])[1]/span'
    )
    name = _join_text(spans[0]) if spans else ""
    period = _join_text(spans[1]) if len(spans) > 1 else ""
    start_date, end_date = _official_dates(period)
    if not (start_date and end_date):
        detail = selector.xpath(
            '(//div[contains(@class, "tournament-detail-club")]/div[1])[1]'
        )
        start_date, end_date = _official_dates(_join_text(detail))
    if not name or not (start_date and end_date):
        client.tele.record_error(
            f"TPPWB tournament detail {tournament_id} lacked name or official dates"
        )
        return None

    return {
        "tournament_id": tournament_id,
        "tournament_name": name,
        "tournament_url": tournament_url,
        "tournament_start_date": start_date,
        "tournament_end_date": end_date,
    }


def _published_draws(client, tournament_id):
    draw_select = None
    for attempt in range(2):
        response = client.get(
            CATEGORIES_URL,
            params={"idTournoi": tournament_id},
            headers=_HTML_HEADERS,
        )
        if response is None:
            return []
        if not (200 <= response.status_code < 300):
            _response_text(client, response, f"published draws {tournament_id}")
            return []
        text = response.text
        draw_select = Selector(text=text).css("#drawCategory") if text else None
        if draw_select:
            break
        if attempt:
            client.tele.record_error(
                f"TPPWB published draws response was malformed for {tournament_id}"
            )
            return []

    draws = []
    for option in draw_select.css("option"):
        raw_value = (option.attrib.get("value") or "").strip()
        if "|" not in raw_value:
            continue
        category_id, raw_types = raw_value.split("|", 1)
        category_id = category_id.strip()
        draw_types = [value.strip() for value in raw_types.split(",") if value.strip()]
        if category_id and draw_types:
            draws.append(
                {
                    "category_id": category_id,
                    "category_name": _join_text(option),
                    "draw_types": list(dict.fromkeys(draw_types)),
                }
            )
    return draws


def _draw_payload(client, tournament_id, category_id, draw_type):
    for attempt in range(2):
        response = client.post(
            DRAW_DATA_URL,
            data={
                "idTournoi": tournament_id,
                "idCategory": category_id,
                "drawType": draw_type,
                "selectedRoundIndex": "",
                "selectedRowIndex": "",
            },
            headers=_JSON_HEADERS,
        )
        if response is None:
            return None
        if not (200 <= response.status_code < 300):
            client.tele.record_error(
                f"TPPWB HTTP {response.status_code}: draw {tournament_id}/"
                f"{category_id}/{draw_type}"
            )
            return None
        try:
            outer = response.json()
            if not isinstance(outer, dict) or not {
                "drawData",
                "roundNames",
            }.issubset(outer):
                raise ValueError("missing drawData or roundNames")
            draw_data = outer["drawData"]
            round_names = outer["roundNames"]
            if draw_data is None:
                draw_data = []
            if round_names is None:
                round_names = []
            if isinstance(draw_data, str):
                draw_data = json.loads(draw_data)
            if isinstance(round_names, str):
                round_names = json.loads(round_names)
            if not isinstance(draw_data, list) or not isinstance(round_names, list):
                raise ValueError("draw data was not a list")
            if any(not isinstance(games, list) for games in draw_data):
                raise ValueError("draw response contained a malformed round")
        except (AttributeError, TypeError, ValueError) as exc:
            if not attempt:
                continue
            client.tele.record_error(
                f"TPPWB draw response was malformed for {tournament_id}/"
                f"{category_id}/{draw_type}",
                exc=exc,
            )
            return None
        if any(draw_data) or attempt:
            return draw_data, round_names
    return None


def _query_value(query, name):
    return _clean((query.get(name) or [""])[0])


def _player(side, *, partner=False):
    suffix = "2" if partner else ""
    raw_name = side.get("nameB" if partner else "name") or ""
    player_id = str(side.get("idB" if partner else "id") or "").strip()
    detail_url = _clean(side.get("urlPlayerDrawDetail") or "").replace("&amp;", "&")
    query = parse_qs(urlsplit(urljoin(BASE + "/", detail_url)).query)
    player_id = _query_value(query, f"PlayerAffiliationNumber{suffix}") or player_id
    last = _query_value(query, f"PlayerLastName{suffix}")
    first = _query_value(query, f"PlayerFirstName{suffix}")
    if last and first:
        name = f"{last}, {first}"
    elif last or first:
        name = last or first
    else:
        name = last_first(_clean(raw_name))
    return {"name": name, "id": player_id}


def _side_players(side):
    players = [_player(side)]
    if side.get("idB") or side.get("nameB"):
        players.append(_player(side, partner=True))
    return players


def _profile_gender(client, player_id):
    if not player_id:
        return ""
    response = client.get(
        PLAYER_URL.format(player_id=player_id),
        headers=_HTML_HEADERS,
    )
    if response is None:
        return None
    if not (200 <= response.status_code < 300):
        client.tele.record_error(
            f"TPPWB HTTP {response.status_code}: player profile {player_id}"
        )
        return None
    selector = Selector(text=response.text)
    sex = selector.xpath(
        '//dt[normalize-space(.)="Sexe:"]/following-sibling::dd[1]'
    )
    if not sex:
        client.log(
            "WARN",
            f"TPPWB player {player_id} has no public gender; using draw category",
        )
        return ""
    source = _field(sex, ".//@src").lower()
    if "female" in source:
        return "F"
    if "male" in source:
        return "M"
    return ""


class _GenderCache:
    def __init__(self):
        self._values = {}
        self._lookups = {}
        self._lock = threading.Lock()

    def resolve(self, client, player_id, fallback=""):
        if not player_id:
            return fallback
        with self._lock:
            if player_id in self._values:
                return self._values[player_id] or fallback
            lookup = self._lookups.setdefault(player_id, threading.Lock())
        with lookup:
            with self._lock:
                if player_id in self._values:
                    return self._values[player_id] or fallback
            value = _profile_gender(client, player_id)
            if value is not None:
                with self._lock:
                    self._values[player_id] = value
        return value or fallback


def _category_gender(category_name):
    value = _clean(category_name).casefold()
    if "messieur" in value or "garçon" in value or re.search(r"\bjg\b", value):
        return "M", "Male", "Men's"
    if "dame" in value or "fille" in value or re.search(r"\bjf\b", value):
        return "F", "Female", "Women's"
    if "mixte" in value or "mixed" in value:
        return "", "Mixed", "Mixed"
    return "", "", ""


def _outcome(winner, loser):
    values = {
        str(side.get("resultType") or "").strip().upper()
        for side in (winner, loser)
    }
    if "WO" in values:
        return "Walkover"
    if any(value.startswith("AB") for value in values):
        return "retired"
    return "Completed"


def _score(winner, loser, outcome):
    if outcome == "Walkover":
        return "W.O.;"
    winner_sets = str(winner.get("score") or "").split("-")
    loser_sets = str(loser.get("score") or "").split("-")
    parts = []
    for winner_games, loser_games in zip(winner_sets, loser_sets):
        winner_games = winner_games.strip()
        loser_games = loser_games.strip()
        if not (winner_games.isdigit() and loser_games.isdigit()):
            continue
        if winner_games == loser_games == "0":
            continue
        parts.append(f"{winner_games}-{loser_games}")
    score = ", ".join(parts)
    if outcome == "retired" and score:
        score += " ret."
    return f"{score};" if score else ""


def _match_row(
    client,
    game,
    metadata,
    category_name,
    round_name,
    gender_cache,
):
    if not isinstance(game, list) or len(game) != 2:
        return None
    sides = [side for side in game if isinstance(side, dict)]
    if len(sides) != 2 or any(
        str(side.get("id") or "").lower() == "virtual_final_team" for side in sides
    ):
        return None
    winners = [
        side for side in sides if str(side.get("statusWin") or "").upper() == "V"
    ]
    losers = [
        side for side in sides if str(side.get("statusWin") or "").upper() == "E"
    ]
    if len(winners) != 1 or len(losers) != 1:
        return None

    winner, loser = winners[0], losers[0]
    winner_players = _side_players(winner)
    loser_players = _side_players(loser)
    is_doubles = len(winner_players) > 1 or len(loser_players) > 1
    if not winner_players[0]["name"] or not loser_players[0]["name"]:
        return None

    fallback_gender, draw_gender, draw_prefix = _category_gender(category_name)
    for player in winner_players + loser_players:
        player["gender"] = gender_cache.resolve(
            client,
            player["id"],
            fallback_gender,
        )

    if not draw_prefix:
        known_genders = {
            player["gender"] for player in winner_players + loser_players if player["gender"]
        }
        if known_genders == {"M"}:
            draw_prefix, draw_gender = "Men's", "Male"
        elif known_genders == {"F"}:
            draw_prefix, draw_gender = "Women's", "Female"
        elif known_genders == {"M", "F"}:
            draw_prefix, draw_gender = "Mixed", "Mixed"

    team_type = "Doubles" if is_doubles else "Singles"
    draw_name = f"{draw_prefix} {team_type}".strip()
    outcome = _outcome(winner, loser)
    match_id = next(
        (
            str(side.get("matchId"))
            for side in sides
            if side.get("matchId") not in (None, "")
        ),
        "",
    )

    def at(players, index):
        if index < len(players):
            return players[index]
        return {"name": "", "id": "", "gender": ""}

    w1, w2 = at(winner_players, 0), at(winner_players, 1)
    l1, l2 = at(loser_players, 0), at(loser_players, 1)
    row = {column: "" for column in COLUMNS}
    row.update(
        {
            "match_id": match_id,
            "ball_type": "Yellow",
            "draw_name": draw_name,
            "draw_team_type": team_type,
            "tournament_name": metadata["tournament_name"],
            "date": metadata["tournament_start_date"],
            "round": round_name,
            "score": _score(winner, loser, outcome),
            "winner_1_name": w1["name"],
            "winner_1_gender": w1["gender"],
            "winner_1_third_party_id": w1["id"],
            "winner_1_country": "Belgium",
            "winner_2_name": w2["name"],
            "winner_2_gender": w2["gender"],
            "winner_2_third_party_id": w2["id"],
            "loser_1_name": l1["name"],
            "loser_1_gender": l1["gender"],
            "loser_1_third_party_id": l1["id"],
            "loser_1_country": "Belgium",
            "loser_2_name": l2["name"],
            "loser_2_gender": l2["gender"],
            "loser_2_third_party_id": l2["id"],
            "outcome": outcome,
            "id_type": "Belgium",
            "draw_gender": draw_gender,
            "tournament_country_code": "BEL",
            "tournament_import_source": IMPORT_SOURCE,
            "tournament_sanction_body": IMPORT_SOURCE,
            "winner_2_country": "Belgium" if w2["name"] else "",
            "loser_2_country": "Belgium" if l2["name"] else "",
            "tournament_event_type": "Tournament",
            "tournament_url": metadata["tournament_url"],
            "tournament_country": "Belgium",
            "tournament_start_date": metadata["tournament_start_date"],
            "tournament_end_date": metadata["tournament_end_date"],
        }
    )
    return row


def _rows_from_draw(
    client,
    payload,
    metadata,
    category_name,
    gender_cache,
):
    draw_data, round_names = payload
    rows = []
    for round_index, games in enumerate(draw_data):
        if not isinstance(games, list):
            continue
        round_name = (
            _clean(round_names[round_index]) if round_index < len(round_names) else ""
        )
        for game in games:
            row = _match_row(
                client,
                game,
                metadata,
                category_name,
                round_name,
                gender_cache,
            )
            if row:
                rows.append(row)
    return rows


def _scrape_tournament(client, tournament_id, gender_cache, log):
    metadata = _tournament_metadata(client, tournament_id)
    if metadata is None:
        return []
    published = _published_draws(client, tournament_id)
    rows = []
    for category in published:
        for draw_type in category["draw_types"]:
            payload = _draw_payload(
                client,
                tournament_id,
                category["category_id"],
                draw_type,
            )
            if payload is None:
                continue
            rows.extend(
                _rows_from_draw(
                    client,
                    payload,
                    metadata,
                    category["category_name"],
                    gender_cache,
                )
            )
    log(
        "INFO",
        f"{metadata['tournament_name']}: {len(rows)} played match(es) from "
        f"{sum(len(category['draw_types']) for category in published)} draw(s)",
    )
    return rows


def _dedup_key(row):
    return row.get("match_id") or (
        row.get("tournament_url", ""),
        row.get("draw_name", ""),
        row.get("round", ""),
        row.get("winner_1_third_party_id", ""),
        row.get("winner_2_third_party_id", ""),
        row.get("loser_1_third_party_id", ""),
        row.get("loser_2_third_party_id", ""),
        row.get("score", ""),
    )


def run(run_obj, log):
    tele = Telemetry()
    scraper = run_obj.scraper
    params = getattr(run_obj, "params", {}) or {}
    tournament_url = (params.get("tournament_url") or "").strip()
    start_date = run_obj.date_from
    end_date = run_obj.date_to
    tournament_id = _tournament_id(tournament_url) if tournament_url else ""
    if tournament_url and not tournament_id:
        message = "The Belgium tournament URL does not contain a valid tournament ID."
        tele.record_error(message)
        log("ERROR", message)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED
    if not tournament_url and not (start_date and end_date):
        message = "Belgium Results 2 needs a date range (date_from / date_to)."
        tele.record_error(message)
        log("ERROR", message)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED
    if not tournament_url and start_date > end_date:
        start_date, end_date = end_date, start_date

    if tournament_url:
        log("INFO", f"Belgium Results 2 starting - tournament {tournament_id}")
    else:
        log(
            "INFO",
            f"Belgium Results 2 starting - {_date_param(start_date)} to "
            f"{_date_param(end_date)}",
        )
    log("INFO", f"Concurrency: {scraper.worker_count} worker thread(s)")
    proxies = build_proxies(scraper, log)

    if tournament_url:
        tournament_ids = [tournament_id]
    else:
        with ScraperClient(
            log=log,
            tele=tele,
            proxies=proxies,
            allowed_hosts=ALLOWED_HOSTS,
        ) as discovery:
            tournament_ids = _discover_tournaments(
                discovery,
                start_date,
                end_date,
                log,
            )

    Run.objects.filter(pk=run_obj.pk).update(
        progress_total=len(tournament_ids),
        progress_done=0,
    )
    gender_cache = _GenderCache()

    def process(tournament_id):
        client = ScraperClient(
            log=log,
            tele=tele,
            proxies=proxies,
            allowed_hosts=ALLOWED_HOSTS,
        )
        try:
            return _scrape_tournament(client, tournament_id, gender_cache, log)
        except Exception as exc:  # noqa: BLE001 - isolate one tournament
            message = redact_secrets(
                f"TPPWB tournament {tournament_id} failed: "
                f"{exc.__class__.__name__}: {exc}"
            )
            tele.record_error(message, exc=exc)
            log("WARN", message)
            return []
        finally:
            Run.objects.filter(pk=run_obj.pk).update(
                progress_done=F("progress_done") + 1
            )
            client.close()

    tournament_rows = []
    if tournament_ids:
        with ThreadPoolExecutor(max_workers=scraper.worker_count) as executor:
            tournament_rows = list(executor.map(process, tournament_ids))

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    seen = set()
    row_count = 0
    for rows in tournament_rows:
        for row in rows:
            key = _dedup_key(row)
            if key in seen:
                continue
            seen.add(key)
            writer.writerow([sanitize_cell(row.get(column, "")) for column in COLUMNS])
            row_count += 1

    clean_empty = row_count == 0 and tele.error_count == 0
    if tele.error_count:
        status = Run.Status.PARTIAL if row_count else Run.Status.FAILED
    else:
        status = Run.Status.SUCCESS
    log(
        "INFO",
        f"Belgium Results 2 finished - status={status}, rows={row_count}, "
        f"requests={tele.request_count}, errors={tele.error_count}",
    )
    items_csv = buffer.getvalue() if row_count or clean_empty else ""
    return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
