import csv
import io
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from parsel import Selector

from accounts.live_scrapers import _ts_tournament


class FakeSelectorClient:
    def __init__(self, pages):
        self.pages = pages

    def get_selector(self, url):
        html = self.pages.get(url)
        return Selector(text=html) if html is not None else None


class FakeResponse:
    status_code = 200
    text = ""


class FakeScraperClient(FakeSelectorClient):
    pages = {}

    def __init__(self, *args, **kwargs):
        super().__init__(self.pages)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        pass

    def get(self, *args, **kwargs):
        return FakeResponse()

    def post(self, *args, **kwargs):
        return FakeResponse()


class TSTournamentDateWindowTests(SimpleTestCase):
    def test_direct_url_discovery_reads_iso_tournament_dates(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Sweden Tournament",
            base="https://svtf.tournamentsoftware.com",
            country="Sweden",
            country_code="SWE",
            sanction_body="Sweden",
        )
        supplied_url = f"{cfg.base}/tournament/TOURNAMENT-ID"
        html = """
        <div class="page-head"><div class="media__content">
          <h2 class="media__title"><span class="nav-link">
            <span class="nav-link__value">Elite Hotels Next Gen Cup 2026</span>
          </span></h2>
          <small class="media__subheading"><span class="nav-link">
            <span class="nav-link__value">
              <svg><use href="/Content/icons/calendar.svg#icon"></use></svg>
              2 Jul to 5 Jul
            </span>
          </span></small>
        </div></div>
        <ul class="page-nav"><li class="page-nav__item">
          <a class="page-nav__link" href="/tournament/tournament-id">Overview</a>
        </li></ul>
        <ul>
          <li class="is-completed list__item is-started">
            <time datetime="2026-07-02T00:00:00.0000000+02:00"></time>
          </li>
          <li class="is-danger is-current list__item is-finished">
            <time datetime="2026-07-05T23:59:00.0000000+02:00"></time>
          </li>
        </ul>
        """

        tournaments = _ts_tournament._discover_one(
            FakeSelectorClient({supplied_url: html}),
            cfg,
            supplied_url,
            lambda *_args: None,
        )

        self.assertEqual(len(tournaments), 1)
        self.assertEqual(tournaments[0]["tournament_start_date"], "07/02/2026")
        self.assertEqual(tournaments[0]["tournament_end_date"], "07/05/2026")

    def test_date_in_window_keeps_rows_inside_requested_window(self):
        self.assertTrue(
            _ts_tournament._date_in_window(
                "06/26/2026", "2026-06-17", "2026-07-01"
            )
        )
        self.assertFalse(
            _ts_tournament._date_in_window(
                "07/03/2026", "2026-06-17", "2026-07-01"
            )
        )
        self.assertFalse(
            _ts_tournament._date_in_window(
                "06/14/2026", "2026-06-17", "2026-07-01"
            )
        )

    def test_blank_or_unparseable_row_date_is_kept(self):
        self.assertTrue(
            _ts_tournament._date_in_window("", "2026-06-17", "2026-07-01")
        )
        self.assertTrue(
            _ts_tournament._date_in_window(
                "unknown", "2026-06-17", "2026-07-01"
            )
        )

    def test_tournament_overlaps_requested_window(self):
        self.assertTrue(
            _ts_tournament._tournament_overlaps_window(
                {
                    "tournament_start_date": "06/26/2026",
                    "tournament_end_date": "06/28/2026",
                },
                "2026-06-17",
                "2026-07-01",
            )
        )

    def test_tournament_before_or_after_requested_window_is_skipped(self):
        self.assertFalse(
            _ts_tournament._tournament_overlaps_window(
                {
                    "tournament_start_date": "06/12/2026",
                    "tournament_end_date": "06/14/2026",
                },
                "2026-06-17",
                "2026-07-01",
            )
        )
        self.assertFalse(
            _ts_tournament._tournament_overlaps_window(
                {
                    "tournament_start_date": "07/03/2026",
                    "tournament_end_date": "07/04/2026",
                },
                "2026-06-17",
                "2026-07-01",
            )
        )


class TSTournamentGenderOutputTests(SimpleTestCase):
    def test_ireland_style_draw_codes_fill_row_gender_fields(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Ireland Tournament",
            base="https://ti.tournamentsoftware.com",
            country="Ireland",
            country_code="IRE",
            sanction_body="Ireland",
        )

        row = _ts_tournament._build_row(
            FakeSelectorClient({}),
            cfg,
            {
                "draw_name": "MS 4 (WTN 25.00 - 40.00)",
                "tournament_name": "Irish Open",
            },
            {
                "draw_team_type": "Singles",
                "outcome": "Completed",
                "score": "6-3, 6-4;",
                "winner_1": {"name": "Winner One", "profile_url": ""},
                "loser_1": {"name": "Loser Two", "profile_url": ""},
            },
        )

        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["winner_1_gender"], "M")
        self.assertEqual(row["loser_1_gender"], "M")

    def test_tournament_title_can_fill_generic_draw_gender(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Ireland Tournament",
            base="https://ti.tournamentsoftware.com",
            country="Ireland",
            country_code="IRE",
            sanction_body="Ireland",
        )

        row = _ts_tournament._build_row(
            FakeSelectorClient({}),
            cfg,
            {
                "draw_name": "Group Connors",
                "tournament_name": "MENS DOUBLES 2026",
            },
            {
                "draw_team_type": "Doubles",
                "outcome": "Completed",
                "score": "6-3, 6-4;",
                "winner_1": {"name": "Winner One", "profile_url": ""},
                "winner_2": {"name": "Winner Two", "profile_url": ""},
                "loser_1": {"name": "Loser One", "profile_url": ""},
                "loser_2": {"name": "Loser Two", "profile_url": ""},
            },
        )

        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["winner_1_gender"], "M")
        self.assertEqual(row["winner_2_gender"], "M")
        self.assertEqual(row["loser_1_gender"], "M")
        self.assertEqual(row["loser_2_gender"], "M")

    def test_draw_gender_wins_over_generic_tournament_title(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Ireland Tournament",
            base="https://ti.tournamentsoftware.com",
            country="Ireland",
            country_code="IRE",
            sanction_body="Ireland",
        )

        row = _ts_tournament._build_row(
            FakeSelectorClient({}),
            cfg,
            {
                "draw_name": "MS 4 (WTN 25.00 - 40.00)",
                "tournament_name": "Boys and Girls Championship",
            },
            {
                "draw_team_type": "Singles",
                "outcome": "Completed",
                "score": "6-3, 6-4;",
                "winner_1": {"name": "Winner One", "profile_url": ""},
                "loser_1": {"name": "Loser Two", "profile_url": ""},
            },
        )

        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["winner_1_gender"], "M")
        self.assertEqual(row["loser_1_gender"], "M")

    def test_ireland_name_gender_fallback_fills_ambiguous_single_gender_group(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Ireland Tournament",
            base="https://ti.tournamentsoftware.com",
            country="Ireland",
            country_code="IRE",
            sanction_body="Ireland",
            claude_gender_fallback=True,
        )

        with mock.patch(
            "accounts.live_scrapers._ts_tournament.resolve_gender",
            side_effect=["F", "F"],
        ):
            row = _ts_tournament._build_row(
                FakeSelectorClient({}),
                cfg,
                {
                    "draw_name": "BOX F",
                    "tournament_name": "DAVID LLOYD BELFAST SINGLES BOX LEAGUES",
                    "claude_keys": ["key"],
                },
                {
                    "draw_team_type": "Singles",
                    "outcome": "Completed",
                    "score": "6-3, 6-4;",
                    "winner_1": {"name": "Jenny Lyall", "profile_url": ""},
                    "loser_1": {"name": "Kathleen Diamond", "profile_url": ""},
                },
            )

        self.assertEqual(row["draw_gender"], "Female")
        self.assertEqual(row["winner_1_gender"], "F")
        self.assertEqual(row["loser_1_gender"], "F")

    def test_ireland_name_gender_fallback_labels_explicit_mixed_draw(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Ireland Tournament",
            base="https://ti.tournamentsoftware.com",
            country="Ireland",
            country_code="IRE",
            sanction_body="Ireland",
            claude_gender_fallback=True,
        )

        with mock.patch(
            "accounts.live_scrapers._ts_tournament.resolve_gender",
            side_effect=["M", "F", "M", "F"],
        ):
            row = _ts_tournament._build_row(
                FakeSelectorClient({}),
                cfg,
                {
                    "draw_name": "Division 1 Blue Group",
                    "tournament_name": "Cavan Mixed Doubles League 2026",
                    "claude_keys": ["key"],
                },
                {
                    "draw_team_type": "Doubles",
                    "outcome": "Completed",
                    "score": "6-3, 6-4;",
                    "winner_1": {"name": "Paul Blundell", "profile_url": ""},
                    "winner_2": {"name": "Jenny Lyall", "profile_url": ""},
                    "loser_1": {"name": "John Jamero", "profile_url": ""},
                    "loser_2": {"name": "Kathleen Diamond", "profile_url": ""},
                },
            )

        self.assertEqual(row["draw_gender"], "Mixed")
        self.assertEqual(row["winner_1_gender"], "M")
        self.assertEqual(row["winner_2_gender"], "F")
        self.assertEqual(row["loser_1_gender"], "M")
        self.assertEqual(row["loser_2_gender"], "F")

    def test_ireland_name_gender_fallback_labels_inferred_mixed_draw(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Ireland Tournament",
            base="https://ti.tournamentsoftware.com",
            country="Ireland",
            country_code="IRE",
            sanction_body="Ireland",
            claude_gender_fallback=True,
        )

        with mock.patch(
            "accounts.live_scrapers._ts_tournament.resolve_gender",
            side_effect=["M", "", "F", ""],
        ):
            row = _ts_tournament._build_row(
                FakeSelectorClient({}),
                cfg,
                {
                    "draw_name": "GRAND SLAM DOUBLES",
                    "tournament_name": "GRAND SLAM DOUBLES 2026",
                    "claude_keys": ["key"],
                },
                {
                    "draw_team_type": "Doubles",
                    "outcome": "Completed",
                    "score": "6-3, 6-4;",
                    "winner_1": {"name": "Paul Blundell", "profile_url": ""},
                    "winner_2": {"name": "James Kings-Tours", "profile_url": ""},
                    "loser_1": {"name": "Fiona Mcgoldrick", "profile_url": ""},
                    "loser_2": {"name": "Jayne Scott", "profile_url": ""},
                },
            )

        self.assertEqual(row["draw_gender"], "Mixed")
        self.assertEqual(row["winner_1_gender"], "M")
        self.assertEqual(row["loser_1_gender"], "F")

    def test_ireland_fallback_normalizes_group_gender_from_majority_evidence(self):
        rows = [
            {
                "tournament_url": "https://example.test/tournament/1",
                "tournament_name": "Kilkenny Junior Summer Matchplay 2026",
                "draw_name": "04.Saturday 16.00pm Mplay - Group B",
                "draw_team_type": "Singles",
                "draw_gender": "Male",
                "winner_1_name": "Carroll, Fearne O",
                "winner_1_gender": "",
                "loser_1_name": "Callan, Florence",
                "loser_1_gender": "M",
            },
            {
                "tournament_url": "https://example.test/tournament/1",
                "tournament_name": "Kilkenny Junior Summer Matchplay 2026",
                "draw_name": "04.Saturday 16.00pm Mplay - Group B",
                "draw_team_type": "Singles",
                "draw_gender": "Female",
                "winner_1_name": "Heslin, Aoife",
                "winner_1_gender": "F",
                "loser_1_name": "Carroll, Fearne O",
                "loser_1_gender": "",
            },
            {
                "tournament_url": "https://example.test/tournament/1",
                "tournament_name": "Kilkenny Junior Summer Matchplay 2026",
                "draw_name": "04.Saturday 16.00pm Mplay - Group B",
                "draw_team_type": "Singles",
                "draw_gender": "Female",
                "winner_1_name": "Morrissey, Caoimhe",
                "winner_1_gender": "F",
                "loser_1_name": "Carroll, Fearne O",
                "loser_1_gender": "",
            },
            {
                "tournament_url": "https://example.test/tournament/1",
                "tournament_name": "Kilkenny Junior Summer Matchplay 2026",
                "draw_name": "04.Saturday 16.00pm Mplay - Group B",
                "draw_team_type": "Singles",
                "draw_gender": "Female",
                "winner_1_name": "Ghradaigh, Muireann Ni",
                "winner_1_gender": "F",
                "loser_1_name": "Heslin, Aoife",
                "loser_1_gender": "F",
            },
        ]

        changed = _ts_tournament._normalize_claude_fallback_genders(rows)

        self.assertGreater(changed, 0)
        for row in rows:
            self.assertEqual(row["draw_gender"], "Female")
            self.assertEqual(row["winner_1_gender"], "F")
            self.assertEqual(row["loser_1_gender"], "F")

    def test_ireland_fallback_normalization_skips_explicit_mixed_draw(self):
        rows = [
            {
                "tournament_url": "https://example.test/tournament/1",
                "tournament_name": "Cavan Mixed Doubles League 2026",
                "draw_name": "Division 1 Blue Group",
                "draw_team_type": "Doubles",
                "draw_gender": "Mixed",
                "winner_1_name": "Paul Blundell",
                "winner_1_gender": "M",
                "winner_2_name": "Jenny Lyall",
                "winner_2_gender": "F",
                "loser_1_name": "Unknown Player",
                "loser_1_gender": "",
                "loser_2_name": "Kathleen Diamond",
                "loser_2_gender": "F",
            }
        ]

        changed = _ts_tournament._normalize_claude_fallback_genders(rows)

        self.assertEqual(changed, 0)
        self.assertEqual(rows[0]["draw_gender"], "Mixed")
        self.assertEqual(rows[0]["loser_1_gender"], "")


class TSTournamentPlayerMatchTests(SimpleTestCase):
    def test_player_page_parses_normal_and_best_of_five_match_cards(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Sweden Tournament",
            base="https://svtf.tournamentsoftware.com",
            country="Sweden",
            country_code="SWE",
            sanction_body="Sweden",
        )
        player_url = f"{cfg.base}/player-profile/PLAYER"

        def card(css_class, winner, loser, sets):
            points = "".join(
                f'<div class="match__result"><ul class="points">'
                f"<li>{win}</li><li>{lose}</li></ul></div>"
                for win, lose in sets
            )
            return f"""
                <li class="match-group__item"><div class="{css_class}">
                  <div class="match__body">
                    <div class="match__row-wrapper"><div class="match__row has-won">
                      <a class="nav-link" href="/player-profile/{winner}">
                        <span class="nav-link__value">{winner}</span>
                      </a>
                    </div></div>
                    <div class="match__row-wrapper"><div class="match__row">
                      <a class="nav-link" href="/player-profile/{loser}">
                        <span class="nav-link__value">{loser}</span>
                      </a>
                    </div></div>
                    {points}
                  </div>
                </div></li>
            """

        html = (
            '<div class="module-container"><ul>'
            + card("match", "Winner One", "Loser One", [(6, 3), (6, 4)])
            + card(
                "best-of-5-or-more match",
                "Winner Two",
                "Loser Two",
                [(6, 4), (3, 6), (7, 5), (4, 6), (6, 2)],
            )
            + "</ul></div>"
        )

        with mock.patch.object(
            _ts_tournament,
            "_build_row",
            side_effect=lambda _client, _cfg, _ctx, match_data: match_data,
        ):
            rows = _ts_tournament._parse_player_matches(
                FakeSelectorClient({player_url: html}),
                cfg,
                {},
                player_url,
            )

        self.assertEqual(
            [row["score"] for row in rows],
            ["6-3, 6-4;", "6-4, 3-6, 7-5, 4-6, 6-2;"],
        )


class TSTournamentTeamMatchTests(SimpleTestCase):
    def test_opt_in_date_range_discovers_team_matches_from_tournament_and_draw_pages(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Tennis Europe",
            base="https://te.tournamentsoftware.com",
            country="",
            country_code="",
            sanction_body="",
            dynamic_country=True,
            id_type_label="Europe",
            org_label="Tennis Europe",
        )
        object.__setattr__(cfg, "discover_team_matches", True)
        tournament_url = (
            "https://te.tournamentsoftware.com/sport/tournament"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E"
        )
        legacy_matches_url = (
            "https://te.tournamentsoftware.com/sport/legacymatches.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&d=20260624"
        )
        match_url = (
            "https://te.tournamentsoftware.com/sport/teammatch.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&match=18"
        )
        outside_window_match_url = (
            "https://te.tournamentsoftware.com/sport/teammatch.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&match=19"
        )
        FakeScraperClient.pages = {
            tournament_url: f"""
            <a href=\"/sport/legacymatches.aspx?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;d=20260624\">Matches</a>
            <a href=\"/sport/teammatch.aspx?id=FFFFFFFF-50B1-461C-9ADF-A147ADBE272E&amp;match=20\">Foreign tournament</a>
            <a href=\"https://elsewhere.example/sport/teammatch.aspx?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;match=21\">Foreign host</a>
            """,
            legacy_matches_url: f"""
            <a href=\"teammatch.aspx?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;match=18\">Duplicate team match</a>
            <a href=\"teammatch.aspx?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;match=19\">Outside window</a>
            """,
        }
        parse_calls = []

        def fake_parse_team_match_page(client, cfg, ctx, url):
            parse_calls.append(url)
            return [
                {
                    "tournament_url": ctx.get("tournament_url", ""),
                    "tournament_name": ctx.get("tournament_name", ""),
                    "draw_name": "B14 - Main",
                    "draw_team_type": "Singles",
                    "round": "Final",
                    "date": (
                        "06/24/2026"
                        if url == match_url
                        else "07/05/2026"
                    ),
                    "score": "6-2, 6-4;",
                    "winner_1_name": "Two, Winner",
                    "loser_1_name": "One, Loser",
                }
            ]

        run_obj = SimpleNamespace(
            pk=123,
            scraper=SimpleNamespace(worker_count=1),
            params={},
            date_from=date(2026, 6, 17),
            date_to=date(2026, 7, 1),
        )
        progress_qs = mock.Mock()

        with mock.patch.object(_ts_tournament, "ScraperClient", FakeScraperClient), \
            mock.patch.object(_ts_tournament, "build_proxies", return_value={}), \
            mock.patch.object(
                _ts_tournament,
                "_discover_range",
                return_value=[
                    {
                        "tournament_id": "E371BB26-50B1-461C-9ADF-A147ADBE272E",
                        "tournament_name": "Team Cup",
                        "tournament_url": tournament_url,
                        "tournament_start_date": "06/24/2026",
                        "tournament_end_date": "07/05/2026",
                        "tournament_city": "Paris",
                        "tournament_country": "France",
                    }
                ],
            ), \
            mock.patch.object(
                _ts_tournament,
                "_parse_team_match_page",
                side_effect=fake_parse_team_match_page,
            ), \
            mock.patch.object(
                _ts_tournament.Run.objects, "filter", return_value=progress_qs
            ):
            items_csv, _requests_csv, _errors_csv, row_count, status = _ts_tournament.run(
                cfg, run_obj, lambda *_args: None
            )

        rows = list(csv.DictReader(io.StringIO(items_csv)))
        self.assertEqual(row_count, 1)
        self.assertEqual(status, _ts_tournament.Run.Status.SUCCESS)
        self.assertEqual(parse_calls, [match_url, outside_window_match_url])
        self.assertEqual(rows[0]["Tournament Url"], tournament_url)
        self.assertEqual(rows[0]["Date"], "06/24/2026")

    def test_opt_in_single_tournament_url_discovers_team_matches_once(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Tennis Europe",
            base="https://te.tournamentsoftware.com",
            country="",
            country_code="",
            sanction_body="",
            dynamic_country=True,
            id_type_label="Europe",
            org_label="Tennis Europe",
            discover_team_matches=True,
        )
        supplied_url = (
            "https://te.tournamentsoftware.com/sport/tournament"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E"
        )
        legacy_matches_url = (
            "https://te.tournamentsoftware.com/sport/legacymatches.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&d=20260624"
        )
        match_url = (
            "https://te.tournamentsoftware.com/sport/teammatch.aspx"
            "?match=18&id=E371BB26-50B1-461C-9ADF-A147ADBE272E"
        )
        FakeScraperClient.pages = {
            supplied_url: f"""
            <title>Tennis Europe - Team Cup - Organization</title>
            <h2>Tennis Europe - Team Cup - Organization</h2>
            <a href="/sport/legacymatches.aspx?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;d=20260624">Matches</a>
            """,
            legacy_matches_url: f"""
            <a href="teammatch.aspx?match=18&amp;id=E371BB26-50B1-461C-9ADF-A147ADBE272E">Team match</a>
            <a href="teammatch.aspx?match=18&amp;id=E371BB26-50B1-461C-9ADF-A147ADBE272E">Duplicate query order</a>
            """,
        }
        parse_calls = []

        def fake_parse_team_match_page(client, cfg, ctx, url):
            parse_calls.append(url)
            return [
                {
                    "tournament_url": ctx.get("tournament_url", ""),
                    "tournament_name": ctx.get("tournament_name", ""),
                    "draw_name": "B14 - Main",
                    "draw_team_type": "Singles",
                    "round": "Final",
                    "date": "07/05/2026",
                    "score": "6-2, 6-4;",
                    "winner_1_name": "Two, Winner",
                    "loser_1_name": "One, Loser",
                }
            ]

        run_obj = SimpleNamespace(
            pk=123,
            scraper=SimpleNamespace(worker_count=1),
            params={"tournament_url": supplied_url},
            date_from=None,
            date_to=None,
        )
        progress_qs = mock.Mock()

        with mock.patch.object(_ts_tournament, "ScraperClient", FakeScraperClient), \
            mock.patch.object(_ts_tournament, "build_proxies", return_value={}), \
            mock.patch.object(
                _ts_tournament,
                "_parse_team_match_page",
                side_effect=fake_parse_team_match_page,
            ), \
            mock.patch.object(
                _ts_tournament.Run.objects, "filter", return_value=progress_qs
            ):
            items_csv, _requests_csv, _errors_csv, row_count, status = _ts_tournament.run(
                cfg, run_obj, lambda *_args: None
            )

        rows = list(csv.DictReader(io.StringIO(items_csv)))
        self.assertEqual(row_count, 1)
        self.assertEqual(status, _ts_tournament.Run.Status.SUCCESS)
        self.assertEqual(parse_calls, [match_url])
        self.assertEqual(rows[0]["Tournament Url"], supplied_url)
        self.assertEqual(rows[0]["Date"], "07/05/2026")

    def test_direct_legacy_team_match_page_is_parsed(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Tennis Europe",
            base="https://te.tournamentsoftware.com",
            country="",
            country_code="",
            sanction_body="",
            dynamic_country=True,
            id_type_label="Europe",
            org_label="Tennis Europe",
            biography_dob=True,
            guid_third_party_id=True,
        )
        match_url = (
            "https://te.tournamentsoftware.com/sport/teammatch.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&match=18"
        )
        loser_url = (
            "https://te.tournamentsoftware.com/sport/player.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&player=1"
        )
        winner_url = (
            "https://te.tournamentsoftware.com/sport/player.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&player=2"
        )
        pages = {
            match_url: """
            <title>Tennis Europe - Team Cup - Matches</title>
            <div id="content">
              <table>
                <tr><th>Time:</th><td>Wed 24/06/2026</td></tr>
                <tr><th>Draw:</th><td><a>B14 - Main</a></td></tr>
              </table>
              <table class="ruler matches"><tbody>
                <tr>
                  <td>1</td><td>MS2</td>
                  <td align="right"><table><tr><td>
                    <a href="player.aspx?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;player=1">Loser One</a>
                  </td></tr></table></td>
                  <td>-</td>
                  <td><table><tr><td><strong>
                    <a href="player.aspx?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;player=2">Winner Two</a>
                  </strong></td></tr></table></td>
                  <td><span class="score"><span>2-6</span> <span>4-6</span></span></td>
                  <td>0-1</td><td></td><td></td><td>1h</td>
                </tr>
              </tbody></table>
            </div>
            """,
            loser_url: '<h2><a href="/player-profile/P1">Profile</a></h2>',
            winner_url: '<h2><a href="/player-profile/P2">Profile</a></h2>',
            "https://te.tournamentsoftware.com/player-profile/P1/biography": """
            <div class="page-head"><div class="media__img"><div class="profile-icon">
              <img class="profile-head__nat" title="France">
            </div></div></div><dl><dt>Year of birth</dt><dd>2012</dd></dl>
            """,
            "https://te.tournamentsoftware.com/player-profile/P2/biography": """
            <div class="page-head"><div class="media__img"><div class="profile-icon">
              <img class="profile-head__nat" title="Germany">
            </div></div></div><dl><dt>Year of birth</dt><dd>2011</dd></dl>
            """,
        }

        rows = _ts_tournament._parse_team_match_page(
            FakeSelectorClient(pages),
            cfg,
            {"tournament_url": match_url},
            match_url,
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tournament_name"], "Team Cup")
        self.assertEqual(row["draw_name"], "B14 - Main")
        self.assertEqual(row["date"], "06/24/2026")
        self.assertEqual(row["score"], "6-2, 6-4;")
        self.assertEqual(row["winner_1_name"], "Two, Winner")
        self.assertEqual(row["winner_1_third_party_id"], "P2")
        self.assertEqual(row["winner_1_dob"], "1/1/2011")
        self.assertEqual(row["winner_1_country"], "Germany")
        self.assertEqual(row["loser_1_name"], "One, Loser")
        self.assertEqual(row["loser_1_third_party_id"], "P1")
        self.assertEqual(row["loser_1_dob"], "1/1/2012")
        self.assertEqual(row["loser_1_country"], "France")

    def test_direct_team_match_page_is_parsed(self):
        cfg = _ts_tournament.TSTournamentConfig(
            label="Tennis Europe",
            base="https://te.tournamentsoftware.com",
            country="",
            country_code="",
            sanction_body="",
            dynamic_country=True,
            id_type_label="Europe",
            org_label="Tennis Europe",
            guid_third_party_id=True,
        )
        match_url = (
            "https://te.tournamentsoftware.com/sport/teammatch.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&match=18"
        )
        profile_one = "https://te.tournamentsoftware.com/player-profile/P1"
        profile_two = "https://te.tournamentsoftware.com/player-profile/P2"
        pages = {
            match_url: """
            <div id="js-league-team-match-index">
              <div class="team-match-header"><div class="module-container">
                <div class="text--center">Final • <time datetime="2026-07-05 10:30"></time>
                  <a class="nav-link"><span class="nav-link__value">Boys Team U14</span></a>
                </div>
              </div></div>
            </div>
            <div class="module-container"><ul><li class="match-group__item"><div class="match">
              <div class="match__body">
                <div class="match__row-wrapper"><div class="match__row has-won">
                  <a class="nav-link" href="/player-profile/P1"><span class="nav-link__value">Winner One</span></a>
                </div></div>
                <div class="match__row-wrapper"><div class="match__row">
                  <a class="nav-link" href="/player-profile/P2"><span class="nav-link__value">Loser Two</span></a>
                </div></div>
                <div class="match__result"><ul class="points"><li>6</li><li>3</li></ul></div>
                <div class="match__result"><ul class="points"><li>6</li><li>4</li></ul></div>
              </div>
            </div></li></ul></div>
            """,
            profile_one: """
            <div class="page-subhead"><div class="media__img"><div class="profile-icon">
              <img class="profile-head__nat" title="France">
            </div></div><div class="media__content"><h4 class="media__title">
              <a href="/player-profile/P1"></a>
            </h4></div></div>
            """,
            profile_two: """
            <div class="page-subhead"><div class="media__img"><div class="profile-icon">
              <img class="profile-head__nat" title="Germany">
            </div></div><div class="media__content"><h4 class="media__title">
              <a href="/player-profile/P2"></a>
            </h4></div></div>
            """,
        }

        rows = _ts_tournament._parse_team_match_page(
            FakeSelectorClient(pages),
            cfg,
            {"tournament_name": "Tennis Europe Team Event", "tournament_url": match_url},
            match_url,
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tournament_name"], "Tennis Europe Team Event")
        self.assertEqual(row["draw_name"], "Boys Team U14")
        self.assertEqual(row["date"], "07/05/2026")
        self.assertEqual(row["round"], "Final")
        self.assertEqual(row["score"], "6-3, 6-4;")
        self.assertEqual(row["winner_1_name"], "One, Winner")
        self.assertEqual(row["winner_1_third_party_id"], "P1")
        self.assertEqual(row["winner_1_country"], "France")
        self.assertEqual(row["loser_1_name"], "Two, Loser")
        self.assertEqual(row["loser_1_third_party_id"], "P2")
        self.assertEqual(row["loser_1_country"], "Germany")
        self.assertEqual(row["tournament_event_type"], "Tournament")

    def test_team_match_url_detection(self):
        self.assertTrue(
            _ts_tournament._is_team_match_url(
                "https://te.tournamentsoftware.com/sport/teammatch.aspx?id=abc&match=1"
            )
        )
        self.assertFalse(
            _ts_tournament._is_team_match_url(
                "https://te.tournamentsoftware.com/tournament/abc"
            )
        )
