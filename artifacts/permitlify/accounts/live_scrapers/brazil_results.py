"""Brazil Results (CBT / tenisintegrado.com.br) scraper.

Ports the production ``brazil_results`` spider onto MatchMiner's shared HTTP
client (:mod:`accounts.live_scrapers._http`) + telemetry. Input is a season
**year + month** (month ``0`` = whole year). The flow is:

1. discover the year's category pages, then the month tabs, then each month's
   tournament links (``tenisintegrado`` "new_torneio" listing);
2. for each tournament, walk its bracket panel (``torneio_painel_jogo``) across
   every category / parameter / round and parse each ``div.game`` block into a
   match row.

Player **gender is inferred from each player's name via Claude**
(:func:`accounts.live_scrapers._claude_gender.resolve_gender`, cached per
distinct name), restoring the gender the source's ``format_name_gender_claude``
call produced — the original discarded that gender and fell back to the draw
name, which is blank for age-category draws that carry no gender word. This is
**Claude-only with no fallback**: if no Anthropic key is configured (per-scraper
-> workspace/Settings -> env) the run **fails immediately** and asks for the key
rather than emitting genderless rows. The ``draw_gender`` field still reflects
the Portuguese draw name (``masculino``/``feminino``). Player names are emitted
as scraped, normalised to ``"Last, First"`` (the source's optional Claude name
pretty-formatting is not restored — only gender).

``run(run_obj, log)`` returns ``(items_csv, requests_csv, errors_csv,
row_count, status)``.
"""

import csv
import hashlib
import io
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urljoin

from django.db.models import F
from django.utils import timezone
from parsel import Selector

from accounts.models import Run

from ._claude_gender import resolve_claude_keys, resolve_gender
from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

BASE = "https://www.tenisintegrado.com.br"
INDEX2_URL = f"{BASE}/new_torneio/index2"
PAINEL_URL = f"{BASE}/torneio_painel_jogo"

# Items CSV columns — the production Brazil items schema (model field order,
# minus the internal spider_id / job_id). Title-cased header to match the
# framework's downloadable files (e.g. "Tournament Url").
COLUMNS = [
    "match_id", "ball_type", "id_type", "draw_bracket_value", "draw_name",
    "draw_team_type", "tournament_name", "date", "round", "score",
    "winner_1_name", "winner_1_gender", "winner_1_dob", "winner_1_third_party_id",
    "winner_1_city", "winner_1_state", "winner_1_country",
    "winner_2_name", "winner_2_gender", "winner_2_dob", "winner_2_third_party_id",
    "winner_2_city", "winner_2_state", "winner_2_country",
    "loser_1_name", "loser_1_gender", "loser_1_dob", "loser_1_third_party_id",
    "loser_1_city", "loser_1_state", "loser_1_country",
    "loser_2_name", "loser_2_gender", "loser_2_dob", "loser_2_third_party_id",
    "loser_2_city", "loser_2_state", "loser_2_country",
    "outcome", "draw_gender", "draw_bracket_type", "draw_type",
    "tournament_city", "tournament_state", "tournament_country_code",
    "tournament_host", "tournament_location_type", "tournament_surface",
    "tournament_event_category", "tournament_event_grade",
    "tournament_import_source", "tournament_sanction_body",
    "winner_2_college", "loser_2_college", "tournament_event_type",
    "winner_1_college", "loser_1_college",
    "tournament_url", "tournament_country", "tournament_start_date",
    "tournament_end_date",
]
HEADER = [c.replace("_", " ").title() for c in COLUMNS]


def _field(sel, xpath):
    """First xpath match, stripped, or ``""`` (mirrors fctcore.parse_field)."""
    value = sel.xpath(xpath).get()
    return value.strip() if value else ""


def _sha_id(name):
    """Stable synthetic id for a player without a profile link."""
    return hashlib.sha256((name or "").strip().lower().encode("utf-8")).hexdigest()


def _year_month(run_obj):
    """Resolve (year, month) from the run params, falling back to the window."""
    params = run_obj.params or {}
    year = params.get("year")
    if year is None:
        year = run_obj.date_from.year if run_obj.date_from else timezone.localdate().year
    month = params.get("month")
    if month is None:
        month = run_obj.date_from.month if run_obj.date_from else 0
    return int(year), int(month)


# ======================================================================
# Match-block parser (ported from the production Parser; logger / DB / Claude
# dependencies removed — pure HTML -> dict).
# ======================================================================
class _MatchParser:
    BALL_TYPE = "Yellow"
    COUNTRY = "Brasil"
    COUNTRY_CODE = "BRA"
    ID_TYPE = "Brasil"
    IMPORT_SOURCE = "CBT"
    SANCTION_BODY = "CBT"
    EVENT_TYPE = "Tournament"

    _RE_GAME_TOP = re.compile(
        r"^\s*\d+\s*º?\s*Jogo\s*-\s*(?P<round>[^-]+?)\s*-\s*"
        r"(?P<date>\d{2}/\d{2}/\d{4})(?:\s+\d{1,2}:\d{2})?\s*$",
        re.IGNORECASE,
    )
    _RE_GAME_TOP_SHORT = re.compile(
        r"^\s*(?P<round>\d+\s*º?\s*Rodada|Final|Semi[-\s]?final|"
        r"Quartas?(?:\s+de\s+final)?|Oitavas?(?:\s+de\s+final)?|"
        r"16[ªa]s?|32[ªa]s?|64[ªa]s?|Repescagem|Quali(?:fica[çc][ãa]o)?)"
        r"\s*-?\s*$",
        re.IGNORECASE,
    )
    _RE_DATE_IN_HEADER = re.compile(r"(\d{2}/\d{2}/\d{4})")
    _RE_BYE = re.compile(r"^\s*BYE\s*$", re.IGNORECASE)

    _WINNER_TOKEN = r"(?:vencedor(?:\(a\)|a)?|ganhador(?:\(a\)|a)?|camp[eê]ao|champion)"
    _RE_WINNER_LINE = re.compile(
        rf"{_WINNER_TOKEN}\s*[:\-]\s*(?P<name>[^\n\r]+?)(?:\s+-\s+(?P<tail>[^\n\r]+))?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    _RE_WINNER_TOKEN_ONLY = re.compile(rf"\b{_WINNER_TOKEN}\b", re.IGNORECASE)

    _RE_WO = re.compile(r"\b(w\.?o\.?|walkover)\b", re.IGNORECASE)
    _RE_RETIRED = re.compile(
        r"\b(des(?:ist[eê]ncia|istiu)?|abandono|retirou|retirado|ret\.?)\b",
        re.IGNORECASE,
    )

    _RE_PROFILE_ID = re.compile(r"perfil2/index/(\d+)")
    _RE_MATCH_ID = re.compile(r"\((\d+)\)")
    _RE_PERIOD = re.compile(r"(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})")
    _RE_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")

    def __init__(self, selector, client=None, claude_keys=None):
        # ``client`` / ``claude_keys`` drive per-player gender via Claude; the
        # run honest-fails before any parsing when no key is configured, so by
        # the time rows are built they are always present.
        self._client = client
        self._claude_keys = claude_keys or []
        self._extract_tournament(selector)
        self._extract_draw(selector)

    def _gender_for(self, name):
        """Infer one player's gender (``"M"``/``"F"``/``""``) from their name via
        Claude, cached per distinct name (see :mod:`._claude_gender`)."""
        if not name or self._client is None or not self._claude_keys:
            return ""
        return resolve_gender(self._client, self._claude_keys, name)

    # ---------- helpers ----------
    @staticmethod
    def _clean(text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _join_text(cls, sel):
        return cls._clean(" ".join(sel.xpath(".//text()").getall()))

    @classmethod
    def _format_date(cls, d):
        d = cls._clean(d)
        if not d:
            return ""
        try:
            dt = datetime.strptime(d, "%d/%m/%Y")
        except ValueError:
            return d
        return dt.strftime("%m/%d/%Y")

    @classmethod
    def _strip_markers(cls, name):
        return cls._RE_TRAILING_PAREN.sub("", cls._clean(name)).strip()

    @classmethod
    def _name_tokens(cls, name):
        if not name:
            return set()
        s = cls._strip_markers(name)
        try:
            s = unicodedata.normalize("NFKD", s)
            s = "".join(ch for ch in s if not unicodedata.combining(ch))
        except Exception:  # noqa: BLE001
            pass
        s = s.lower()
        toks = re.findall(r"[a-z0-9]+", s)
        return {t for t in toks if len(t) >= 2 and not t.isdigit()}

    @classmethod
    def _names_match(cls, target_tokens, candidate_name):
        if not target_tokens:
            return False
        cand = cls._name_tokens(candidate_name)
        if not cand:
            return False
        if target_tokens.issubset(cand) or cand.issubset(target_tokens):
            return True
        return len(target_tokens & cand) >= 2

    @classmethod
    def _last_first(cls, full_name):
        name = cls._strip_markers(full_name)
        if not name:
            return ""
        parts = name.split()
        if len(parts) == 1:
            return parts[0].rstrip(",").strip()
        return f"{parts[-1]}, {' '.join(parts[:-1])}".rstrip(",").strip()

    @classmethod
    def _profile_id_from_href(cls, href):
        if not href:
            return ""
        m = cls._RE_PROFILE_ID.search(href)
        return m.group(1) if m else ""

    @staticmethod
    def _set_games(cell):
        if cell is None:
            return None
        s = str(cell).strip()
        if not s:
            return None
        m = re.match(r"\s*(\d{1,2})\s*[-x\u00d7]\s*\d{1,2}", s)
        if m:
            return int(m.group(1))
        m = re.match(r"\s*(\d{1,2})", s)
        if m:
            return int(m.group(1))
        return None

    # ---------- tournament / draw context ----------
    def _extract_tournament(self, selector):
        title_sel = selector.xpath(".//div[@class='tournament-title']")
        self.tournament_name = self._join_text(title_sel) if title_sel else ""

        local_parts = selector.xpath(
            ".//div[@class='tournament-local']/descendant-or-self::text()"
        ).getall()
        local_text = self._clean(" ".join(local_parts))
        self.tournament_city = (
            local_text.split("-", 1)[0].strip() if "-" in local_text else local_text
        )

        self.tournament_start_date = ""
        self.tournament_end_date = ""
        for info_text in selector.xpath(
            ".//div[@class='tournament-period']//div[@class='info']/text()"
        ).getall():
            m = self._RE_PERIOD.match(self._clean(info_text))
            if m:
                self.tournament_start_date = self._format_date(m.group(1))
                self.tournament_end_date = self._format_date(m.group(2))
                break

        share_url = selector.xpath(
            ".//div[@class='tournament-share']//input[@class='form-control']/@value"
        ).get()
        if share_url:
            self.tournament_url = share_url.strip()
        else:
            id_torneio = selector.xpath(".//input[@name='id_torneio']/@value").get()
            if id_torneio:
                self.tournament_url = (
                    f"{BASE}/torneio_painel_info/index/{id_torneio.strip()}"
                )
            else:
                self.tournament_url = ""

        self.tournament_state = ""
        self.tournament_country = self.COUNTRY
        self.tournament_country_code = self.COUNTRY_CODE
        self.tournament_host = ""
        self.tournament_location_type = ""
        self.tournament_surface = ""
        self.tournament_event_category = ""
        self.tournament_event_grade = ""
        self.tournament_event_type = self.EVENT_TYPE
        self.tournament_import_source = self.IMPORT_SOURCE
        self.tournament_sanction_body = self.SANCTION_BODY

    def _extract_draw(self, selector):
        name = ""
        for txt in selector.xpath(
            ".//h4/text() | .//h3/text() | .//h2/text()"
        ).getall():
            txt = self._clean(txt)
            if "Simples" in txt or "Duplas" in txt:
                name = txt
                break
        if not name:
            opt = selector.xpath(
                ".//select[@id='id_categoria']//option[@selected]/text()"
            ).get()
            if opt:
                name = self._clean(opt)

        self.draw_name = name
        low = name.lower()
        self.draw_gender = (
            "Male" if "masculino" in low else ("Female" if "feminino" in low else "")
        )
        self.draw_team_type = (
            "Doubles" if "duplas" in low else ("Singles" if "simples" in low else "")
        )
        self.draw_bracket_value = ""
        self.draw_bracket_type = ""
        self.draw_type = ""

    # ---------- per-row extraction ----------
    def _players_in_row(self, li_sel):
        players = []
        for ac in li_sel.xpath('./div[@class="avatar-container"]'):
            player_name = (
                self._join_text(ac.xpath('./span[@class="avatar-info"]/a'))
                or self._join_text(ac.xpath("./a"))
                or self._join_text(ac)
            )
            if self._RE_BYE.match(player_name or ""):
                players.append(("__BYE__", ""))
                continue
            player_link = ac.xpath(
                ".//span[contains(@class,'avatar-info')]/a/@href"
            ).get()
            third_party_id = self._profile_id_from_href(player_link)
            if not third_party_id and player_name:
                third_party_id = _sha_id(player_name)
            if player_name:
                players.append((player_name, third_party_id))
        return players

    def _row_score(self, li_sel):
        score_sel = li_sel.xpath(".//div[@class='score pull-right']")
        if not score_sel:
            return []
        if score_sel.xpath(".//div[@class='wo text-danger']") or self._RE_WO.search(
            self._join_text(score_sel)
        ):
            return ["W.O."]
        sets = [self._join_text(s) for s in score_sel.xpath(".//div[@class='set']")]
        return [s for s in sets if s]

    @staticmethod
    def _row_has_winner_marker(li_sel):
        cls = (li_sel.attrib.get("class") or "").lower()
        if any(tok in cls for tok in ("winner", "vencedor", "is-winner", "champ")):
            return True
        icon_classes = " ".join(li_sel.xpath(".//@class").getall()).lower()
        if any(
            tok in icon_classes
            for tok in ("fa-trophy", "fa-check", "win-icon", "vencedor")
        ):
            return True
        return False

    @staticmethod
    def _build_score(winner_sets, loser_sets, outcome):
        if outcome == "Walkover":
            return "W.O.;"
        pairs = []
        for i in range(max(len(winner_sets), len(loser_sets))):
            w = winner_sets[i] if i < len(winner_sets) else ""
            l = loser_sets[i] if i < len(loser_sets) else ""
            if (not w and not l) or "W.O." in (w, l):
                continue
            pairs.append(f"{w}-{l}")
        score = ", ".join(pairs)
        if outcome == "retired" and score:
            score += " ret."
        return f"{score};" if score else ";"

    @classmethod
    def _outcome_from_block(cls, block_text):
        if cls._RE_WO.search(block_text):
            return "Walkover"
        if cls._RE_RETIRED.search(block_text):
            return "retired"
        return "Completed"

    def _winner_name_from_text(self, block_text):
        m = self._RE_WINNER_LINE.search(block_text)
        if m:
            return self._strip_markers(m.group("name"))
        return None

    def _winner_row_index_from_markup(self, rows):
        marked = [i for i, r in enumerate(rows) if self._row_has_winner_marker(r)]
        if len(marked) == 1:
            return marked[0]
        return None

    def _winner_row_index_from_score(self, row_a_sets, row_b_sets):
        a_wins = b_wins = 0
        for i in range(max(len(row_a_sets), len(row_b_sets))):
            a = row_a_sets[i] if i < len(row_a_sets) else ""
            b = row_b_sets[i] if i < len(row_b_sets) else ""
            ai = self._set_games(a)
            bi = self._set_games(b)
            if ai is None or bi is None:
                continue
            if ai > bi:
                a_wins += 1
            elif bi > ai:
                b_wins += 1
        if a_wins == b_wins:
            return None
        return 0 if a_wins > b_wins else 1

    # ---------- one match ----------
    def parse_match(self, game_sel):
        match_data = {}
        try:
            spans = game_sel.xpath('./div[@class="game-top"]/span')
            header_text = self._clean(self._join_text(spans[0])) if spans else ""
            match_id = ""
            if len(spans) >= 2:
                m = self._RE_MATCH_ID.search(self._join_text(spans[1]))
                if m:
                    match_id = m.group(1)

            round_ = ""
            date = ""
            m = self._RE_GAME_TOP.match(header_text)
            if m:
                round_ = self._clean(m.group("round"))
                date = self._format_date(m.group("date"))
            else:
                m2 = self._RE_GAME_TOP_SHORT.match(header_text)
                if m2:
                    round_ = self._clean(m2.group("round"))
                dm = self._RE_DATE_IN_HEADER.search(header_text)
                if dm:
                    date = self._format_date(dm.group(1))
            if not date and getattr(self, "tournament_start_date", ""):
                date = self.tournament_start_date

            rows = game_sel.xpath(
                './ul[@class="list-group"]/li[@class="list-group-item"]'
            )
            if len(rows) < 2:
                return None

            row_a_players = self._players_in_row(rows[0])
            row_b_players = self._players_in_row(rows[1])
            row_a_sets = self._row_score(rows[0])
            row_b_sets = self._row_score(rows[1])

            if not row_a_players or not row_b_players:
                return None
            if any(p[0] == "__BYE__" for p in row_a_players) or any(
                p[0] == "__BYE__" for p in row_b_players
            ):
                return None

            block_text = self._join_text(
                game_sel.xpath(".//div[@class='game-bottom']")
            ) or self._join_text(game_sel)
            outcome = self._outcome_from_block(block_text)

            winner_idx = None
            winner_name_raw = self._winner_name_from_text(block_text)
            if winner_name_raw:
                target_tokens = self._name_tokens(winner_name_raw)
                for idx, players in enumerate((row_a_players, row_b_players)):
                    if any(self._names_match(target_tokens, p[0]) for p in players):
                        winner_idx = idx
                        break

            if winner_idx is None:
                wi = self._winner_row_index_from_markup(rows)
                if wi is not None:
                    winner_idx = wi

            if winner_idx is None:
                wi = self._winner_row_index_from_score(row_a_sets, row_b_sets)
                if wi is not None:
                    winner_idx = wi

            if winner_idx is None and outcome != "Completed":
                if row_a_sets and not row_b_sets:
                    winner_idx = 0
                elif row_b_sets and not row_a_sets:
                    winner_idx = 1

            if winner_idx is None:
                return None

            if winner_idx == 0:
                winners, losers = row_a_players, row_b_players
                winner_sets, loser_sets = row_a_sets, row_b_sets
            else:
                winners, losers = row_b_players, row_a_players
                winner_sets, loser_sets = row_b_sets, row_a_sets

            score = self._build_score(winner_sets, loser_sets, outcome)
            # Per-player gender is inferred from each player's name via Claude
            # (cached); the draw_gender field below still reflects the Portuguese
            # draw name ("masculino"/"feminino").
            winner_1_name = self._last_first(winners[0][0]) if winners else ""
            winner_1_third_party_id = winners[0][1] if winners else ""
            winner_1_country = self.COUNTRY if winner_1_name else ""
            winner_1_gender = self._gender_for(winner_1_name)
            if len(winners) >= 2:
                winner_2_name = self._last_first(winners[1][0])
                winner_2_third_party_id = winners[1][1]
                winner_2_gender = self._gender_for(winner_2_name)
                winner_2_country = self.COUNTRY
            else:
                winner_2_name = ""
                winner_2_third_party_id = ""
                winner_2_gender = ""
                winner_2_country = ""

            loser_1_name = self._last_first(losers[0][0]) if losers else ""
            loser_1_third_party_id = losers[0][1] if losers else ""
            loser_1_country = self.COUNTRY if loser_1_name else ""
            loser_1_gender = self._gender_for(loser_1_name)
            if len(losers) >= 2:
                loser_2_name = self._last_first(losers[1][0])
                loser_2_third_party_id = losers[1][1]
                loser_2_gender = self._gender_for(loser_2_name)
                loser_2_country = self.COUNTRY
            else:
                loser_2_name = ""
                loser_2_third_party_id = ""
                loser_2_gender = ""
                loser_2_country = ""

            match_data = {
                "match_id": match_id,
                "ball_type": self.BALL_TYPE,
                "id_type": self.ID_TYPE,
                "draw_bracket_value": self.draw_bracket_value,
                "draw_name": self.draw_name,
                "draw_team_type": self.draw_team_type,
                "tournament_name": self.tournament_name,
                "date": date,
                "round": round_,
                "score": score,
                "winner_1_name": winner_1_name,
                "winner_1_gender": winner_1_gender,
                "winner_1_dob": "",
                "winner_1_third_party_id": winner_1_third_party_id,
                "winner_1_city": "",
                "winner_1_state": "",
                "winner_1_country": winner_1_country,
                "winner_2_name": winner_2_name,
                "winner_2_gender": winner_2_gender,
                "winner_2_dob": "",
                "winner_2_third_party_id": winner_2_third_party_id,
                "winner_2_city": "",
                "winner_2_state": "",
                "winner_2_country": winner_2_country,
                "loser_1_name": loser_1_name,
                "loser_1_gender": loser_1_gender,
                "loser_1_dob": "",
                "loser_1_third_party_id": loser_1_third_party_id,
                "loser_1_city": "",
                "loser_1_state": "",
                "loser_1_country": loser_1_country,
                "loser_2_name": loser_2_name,
                "loser_2_gender": loser_2_gender,
                "loser_2_dob": "",
                "loser_2_third_party_id": loser_2_third_party_id,
                "loser_2_city": "",
                "loser_2_state": "",
                "loser_2_country": loser_2_country,
                "outcome": outcome,
                "draw_gender": self.draw_gender,
                "draw_bracket_type": self.draw_bracket_type,
                "draw_type": self.draw_type,
                "tournament_city": self.tournament_city,
                "tournament_state": self.tournament_state,
                "tournament_country_code": self.tournament_country_code,
                "tournament_host": self.tournament_host,
                "tournament_location_type": self.tournament_location_type,
                "tournament_surface": self.tournament_surface,
                "tournament_event_category": self.tournament_event_category,
                "tournament_event_grade": self.tournament_event_grade,
                "tournament_import_source": self.tournament_import_source,
                "tournament_sanction_body": self.tournament_sanction_body,
                "winner_2_college": "",
                "loser_2_college": "",
                "tournament_event_type": self.tournament_event_type,
                "winner_1_college": "",
                "loser_1_college": "",
                "tournament_url": self.tournament_url,
                "tournament_country": self.tournament_country,
                "tournament_start_date": self.tournament_start_date,
                "tournament_end_date": self.tournament_end_date,
            }
        except Exception:  # noqa: BLE001 - one bad block must not kill the row loop
            return None
        return match_data


# ======================================================================
# Discovery + per-tournament scraping
# ======================================================================
def _discover_tournaments(client, year, month, log):
    """Return the list of tournament URLs for ``year`` / ``month`` (0 = all)."""
    data = {
        "busca": "",
        "ano": str(year),
        "id_pais": "0",
        "id_uf": "0",
        "id_tab": "2",
        "id_depto": "2",
        "mes": "1",
    }
    resp = client.post(
        INDEX2_URL, files={k: (None, v) for k, v in data.items()}
    )
    if resp is None or not (200 <= resp.status_code < 300):
        log("WARN", "\u26a0\ufe0f Could not load the tournament index page")
        return []

    sel = Selector(text=resp.text)
    options = []
    for d1 in sel.xpath(
        '//ul[contains(@class,"nav-tabs")]//li[a[contains(text(), "Juvenil")]]'
        '/ancestor::ul[contains(@class,"nav-tabs")]//li[not(@class="disabled")]/a'
    ):
        href = d1.xpath("./@href").get()
        if href:
            options.append(urljoin(BASE + "/", href))
    log("INFO", f"\U0001f5c2\ufe0f {len(options)} category page(s) found")

    tournaments = []
    seen = set()
    for opt in options:
        msel = client.get_selector(opt)
        if msel is None:
            continue
        for d1 in msel.xpath(
            '//ul[contains(@class,"nav-tabs")]//li[a[contains(., "Fev")]]'
            '/ancestor::ul[contains(@class,"nav-tabs")]'
            '//li[not(@class="disabled")]/a[not(@class="dropdown-toggle")]'
        ):
            href = d1.xpath("./@href").get()
            if not href:
                continue
            if month != 0 and not href.endswith(f"/{month}"):
                continue
            month_url = urljoin(BASE + "/", href)
            psel = client.get_selector(month_url)
            if psel is None:
                continue
            for a in psel.xpath(
                '//table[@id="tournaments"]//tr[not(th)]/td[5]/a[@class="td-link"]/@href'
            ).getall():
                turl = a.strip()
                if turl and turl not in seen:
                    seen.add(turl)
                    tournaments.append(turl)
    return tournaments


def _parse_games(tournament_url, sel, client=None, claude_keys=None):
    parser = _MatchParser(sel, client=client, claude_keys=claude_keys)
    out = []
    for game_sel in sel.xpath('//div[@class="game"]'):
        md = parser.parse_match(game_sel)
        if md:
            if not md.get("tournament_url"):
                md["tournament_url"] = tournament_url
            out.append(md)
    return out


def _parse_category(client, tournament_url, sel, claude_keys=None):
    """Walk every category / parameter / round panel; return match rows."""
    out = []
    id_categorias = sel.xpath('//select[@id="id_categoria"]/option/@value').getall()
    id_parametros = sel.xpath('//select[@id="id_parametro"]/option/@value').getall()
    id_periodo = _field(sel, '//select[@id="id_periodo"]/option[@selected="selected"]/@value')
    nr_rodada = _field(sel, '//select[@id="round-selector"]/option[1]/@value')
    id_torneio = _field(sel, '//input[@name="id_torneio"]/@value')
    id_categoria_ant = _field(sel, '//input[@name="id_categoria_ant"]/@value')

    headers = {"Referer": tournament_url}

    for id_categoria in id_categorias:
        for id_parametro in id_parametros:
            data = {
                "id_categoria": id_categoria,
                "id_parametro": id_parametro,
                "id_periodo": id_periodo,
                "nr_rodada": nr_rodada,
                "id_torneio": id_torneio,
                "id_categoria_ant": id_categoria_ant,
            }
            if not nr_rodada:
                data.pop("nr_rodada", None)
            if not id_categoria_ant:
                data.pop("id_categoria_ant", None)

            resp = client.post(PAINEL_URL, data=data, headers=headers)
            if resp is None or not (200 <= resp.status_code < 300):
                continue
            psel = Selector(text=resp.text)
            nr_rodadas = psel.xpath('//select[@id="round-selector"]/option/@value').getall()
            id_periodo = _field(psel, '//select[@id="id_periodo"]/option[@selected="selected"]/@value')
            nr_rodada = _field(psel, '//select[@id="round-selector"]/option[1]/@value')
            id_torneio = _field(psel, '//input[@name="id_torneio"]/@value')
            id_categoria_ant = _field(psel, '//input[@name="id_categoria_ant"]/@value')

            if nr_rodadas:
                for nr in nr_rodadas:
                    data2 = {
                        "id_categoria": id_categoria,
                        "id_parametro": id_parametro,
                        "id_periodo": id_periodo,
                        "nr_rodada": nr,
                        "id_torneio": id_torneio,
                        "id_categoria_ant": id_categoria_ant,
                    }
                    if not nr:
                        data2.pop("nr_rodada", None)
                    if not id_categoria_ant:
                        data2.pop("id_categoria_ant", None)
                    resp2 = client.post(PAINEL_URL, data=data2, headers=headers)
                    if resp2 is not None and 200 <= resp2.status_code < 300:
                        out.extend(
                            _parse_games(
                                tournament_url,
                                Selector(text=resp2.text),
                                client=client,
                                claude_keys=claude_keys,
                            )
                        )
            else:
                out.extend(
                    _parse_games(
                        tournament_url, psel, client=client, claude_keys=claude_keys
                    )
                )
    return out


def _scrape_tournament(client, tournament_url, claude_keys=None):
    """Return all match rows for one tournament URL."""
    warm = client.get(tournament_url, headers={"Referer": BASE + "/"})
    if warm is None or not (200 <= warm.status_code < 300):
        return []
    m = re.search(r"/index/(\d+)", tournament_url or "")
    tournament_id = m.group(1) if m else ""
    panel_url = (
        f"{PAINEL_URL}/index/{tournament_id}" if tournament_id else PAINEL_URL
    )
    sel = client.get_selector(
        panel_url, headers={"Referer": tournament_url, "priority": "u=0, i"}
    )
    if sel is None:
        return []
    return _parse_category(client, tournament_url, sel, claude_keys=claude_keys)


def run(run_obj, log):
    """Execute the Brazil Results scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    year, month = _year_month(run_obj)
    log(
        "INFO",
        f"\U0001f3be Brazil Results (CBT) starting \u2014 year={year}, "
        f"month={month or 'all'}",
    )
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)

    # Gender is inferred from player names via Claude, with no fallback: without
    # a key, fail the run and ask for one rather than emitting genderless rows.
    claude_keys = resolve_claude_keys(scraper)
    if not claude_keys:
        msg = (
            "Anthropic API key required \u2014 Brazil Results infers player "
            "gender from names via Claude and has no fallback. Add a key on the "
            "Settings page (workspace-wide) or this scraper's Settings tab, then "
            "re-run."
        )
        tele.record_error(msg)
        log("ERROR", "\U0001f6d1 " + msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED
    log("INFO", "\U0001f9e0 Gender: Claude name inference enabled (cached)")

    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering tournaments \u2500\u2500\u2500\u2500")
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        tournaments = _discover_tournaments(discovery, year, month, log)

    total = len(tournaments)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} tournament(s) discovered")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def process(tournament_url):
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            rows = _scrape_tournament(client, tournament_url, claude_keys=claude_keys)
            for row in rows:
                # Source-identified key: dedupes only true duplicates within a
                # tournament, without collapsing genuine rematches (same
                # players/score in a different draw, round or date). ``match_id``
                # is included when present but never relied on alone.
                key = (
                    tournament_url,
                    row.get("match_id", ""),
                    row.get("draw_name", ""),
                    row.get("round", ""),
                    row.get("date", ""),
                    row.get("winner_1_name", ""),
                    row.get("loser_1_name", ""),
                    row.get("winner_2_name", ""),
                    row.get("loser_2_name", ""),
                    row.get("score", ""),
                )
                with lock:
                    if key in seen:
                        continue
                    seen.add(key)
                    writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
                    counter["rows"] += 1
                log(
                    "INFO",
                    f"   \U0001f3c6 {row.get('draw_team_type', '')}: "
                    f"{row.get('winner_1_name') or '?'} def. "
                    f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                    f"@ {row.get('tournament_name') or 'Brazil'}",
                )
        except Exception as exc:  # noqa: BLE001 - a bad tournament can't kill the run
            tele.record_error(
                redact_secrets(f"Tournament {tournament_url} failed: {exc}"), exc=exc
            )
            log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f tournament failed: {exc.__class__.__name__}: {exc}"
                ),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(
                progress_done=F("progress_done") + 1
            )
            client.close()

    if tournaments:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping tournaments \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, tournaments))

    row_count = counter["rows"]
    log("INFO", "\u2500\u2500\u2500\u2500 summary \u2500\u2500\u2500\u2500")
    log("INFO", f"\U0001f4be Writing {row_count} row(s) to CSV")
    log(
        "INFO",
        f"\U0001f4ca Telemetry: {tele.request_count} request(s), "
        f"{tele.error_count} error(s)",
    )
    status = Run.Status.SUCCESS if row_count else Run.Status.FAILED
    icon = "\U0001f3c1" if status == Run.Status.SUCCESS else "\U0001f6d1"
    log("INFO", f"{icon} Run finished \u2014 status={status}, rows={row_count}")
    items_csv = buf.getvalue() if row_count else ""
    return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
