import csv
import io
import json
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from parsel import Selector

from accounts.live_scrapers import belgium_results_2, registry
from accounts.models import Scraper
from accounts.views import validate_run_params


class _Telemetry:
    def __init__(self):
        self.errors = []

    def record_error(self, message, **kwargs):
        self.errors.append(message)


class _Response:
    def __init__(self, *, text="", payload=None, status_code=200):
        self.text = text
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class _Client:
    def __init__(self, *, gets=None, posts=None):
        self.gets = gets or {}
        self.posts = posts or {}
        self.requests = []
        self.tele = _Telemetry()

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return self.gets.get(url, _Response(status_code=404))

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self.posts.get(url, _Response(status_code=404))


class BelgiumResults2Tests(SimpleTestCase):
    def test_schema_matches_supplied_61_column_order(self):
        self.assertEqual(len(belgium_results_2.HEADER), 61)
        self.assertEqual(
            belgium_results_2.HEADER[:9],
            [
                "Match ID",
                "Ball Type",
                "Draw Bracket Value",
                "Draw Name",
                "Draw Team Type",
                "Tournament Name",
                "Date",
                "Round",
                "Score",
            ],
        )
        self.assertEqual(belgium_results_2.HEADER[32], "ID Type")
        self.assertEqual(
            belgium_results_2.HEADER[-7:],
            [
                "Winner 1 DOB",
                "Winner 2 DOB",
                "Loser 1 DOB",
                "Loser 2 DOB",
                "Tournament Country",
                "Tournament Start Date",
                "Tournament End Date",
            ],
        )

    def test_registry_accepts_date_range_or_tournament_url(self):
        spec = registry.get_spec("belgium_results_2")

        self.assertEqual(spec.input_kind, registry.INPUT_DATE_RANGE_OR_URL)
        self.assertEqual(
            spec.runner_path,
            "accounts.live_scrapers.belgium_results_2:run",
        )
        self.assertIs(spec.load_runner(), belgium_results_2.run)
        self.assertFalse(spec.uses_browser)
        self.assertEqual(spec.allowed_hosts, ("tennis.tppwb.be",))
        self.assertEqual(spec.model_upload_label, "")
        self.assertEqual(spec.model_filename, "")

        inputs = validate_run_params(
            spec,
            {"date_from": "2026-05-28", "date_to": "2026-06-07"},
        )
        self.assertEqual(inputs.date_from, date(2026, 5, 28))
        self.assertEqual(inputs.date_to, date(2026, 6, 7))
        self.assertEqual(
            inputs.params,
            {"date_from": "2026-05-28", "date_to": "2026-06-07"},
        )

        tournament_url = (
            "https://tennis.tppwb.be/MyAFT/Competitions/TournamentDetail/361658"
        )
        url_inputs = validate_run_params(spec, {"tournament_url": tournament_url})
        self.assertIsNone(url_inputs.date_from)
        self.assertIsNone(url_inputs.date_to)
        self.assertEqual(url_inputs.params, {"tournament_url": tournament_url})

    def test_search_posts_exact_date_range_and_extracts_ids(self):
        html = """
        <dl class="grid-data-item">
          <a data-url="/MyAFT/Tooltip/TournamentDetails/362068"></a>
        </dl>
        <dl class="grid-data-item">
          <a class="hamburger" data-url="/menu?IdTournoi=362419"></a>
        </dl>
        """
        client = _Client(
            posts={belgium_results_2.SEARCH_URL: _Response(text=html)}
        )

        ids = belgium_results_2._search_once(
            client,
            date(2026, 5, 28),
            date(2026, 6, 7),
        )

        self.assertEqual(ids, ["362068", "362419"])
        method, url, kwargs = client.requests[0]
        self.assertEqual((method, url), ("POST", belgium_results_2.SEARCH_URL))
        self.assertEqual(kwargs["data"]["Region"], "1,3,4,6")
        self.assertEqual(kwargs["data"]["PeriodStartDate"], "28/05/2026")
        self.assertEqual(kwargs["data"]["PeriodEndDate"], "07/06/2026")
        self.assertEqual(kwargs["data"]["OrderBy"], "DATEASC")

    def test_capped_search_keeps_parent_ids_when_a_split_fails(self):
        start = date(2026, 5, 28)
        end = date(2026, 5, 29)
        client = SimpleNamespace(tele=_Telemetry())

        def fake_search(_client, window_start, window_end, regions=belgium_results_2.REGIONS):
            if window_start == start and window_end == end:
                return [str(value) for value in range(100)]
            if window_start == start:
                return ["1", "100"]
            return None

        with mock.patch.object(
            belgium_results_2,
            "_search_once",
            side_effect=fake_search,
        ):
            ids = belgium_results_2._discover_tournaments(
                client,
                start,
                end,
                lambda *_args: None,
            )

        self.assertEqual(ids, [str(value) for value in range(101)])

    def test_detail_and_categories_use_official_metadata(self):
        detail_url = belgium_results_2.DETAIL_URL.format(tournament_id="362068")
        detail_html = """
        <div class="tournament-detail-club"><div>
          <span>T.C. GRAND OHEY (6031)</span>
          <span>Tournoi du 28/05/2026 au 07/06/2026</span>
        </div></div>
        """
        categories_html = """
        <select id="drawCategory">
          <option value="">Categorie</option>
          <option value="1028381|F,Q,S,SA,SB">Simples Messieurs 2 (B0-B-15)</option>
          <option value="1028382|">Unpublished</option>
        </select>
        """
        client = _Client(
            gets={
                detail_url: _Response(text=detail_html),
                belgium_results_2.CATEGORIES_URL: _Response(text=categories_html),
            }
        )

        metadata = belgium_results_2._tournament_metadata(client, "362068")
        draws = belgium_results_2._published_draws(client, "362068")

        self.assertEqual(metadata["tournament_name"], "T.C. GRAND OHEY (6031)")
        self.assertEqual(metadata["tournament_start_date"], "5/28/2026")
        self.assertEqual(metadata["tournament_end_date"], "6/7/2026")
        self.assertEqual(metadata["tournament_url"], detail_url)
        self.assertEqual(
            draws,
            [
                {
                    "category_id": "1028381",
                    "category_name": "Simples Messieurs 2 (B0-B-15)",
                    "draw_types": ["F", "Q", "S", "SA", "SB"],
                }
            ],
        )
        category_request = client.requests[1]
        self.assertEqual(category_request[2]["params"], {"idTournoi": "362068"})

    def test_one_day_tournament_uses_same_start_and_end_date(self):
        tournament_id = "362999"
        detail_url = belgium_results_2.DETAIL_URL.format(
            tournament_id=tournament_id
        )
        client = _Client(
            gets={
                detail_url: _Response(
                    text="""
                    <div class="tournament-detail-club"><div>
                      <span>One Day Open</span>
                      <span>Tournoi le 28/05/2026</span>
                    </div></div>
                    """
                )
            }
        )

        metadata = belgium_results_2._tournament_metadata(client, tournament_id)

        self.assertEqual(metadata["tournament_start_date"], "5/28/2026")
        self.assertEqual(metadata["tournament_end_date"], "5/28/2026")

    def test_draw_payload_is_double_decoded(self):
        draw_data = [[[{"name": "Winner"}, {"name": "Loser"}]]]
        payload = {
            "drawData": json.dumps(draw_data),
            "roundNames": json.dumps(["1/4 FINALE"]),
        }
        client = _Client(
            posts={belgium_results_2.DRAW_DATA_URL: _Response(payload=payload)}
        )

        result = belgium_results_2._draw_payload(
            client,
            "362068",
            "1028381",
            "F",
        )

        self.assertEqual(result, (draw_data, ["1/4 FINALE"]))
        data = client.requests[0][2]["data"]
        self.assertEqual(
            data,
            {
                "idTournoi": "362068",
                "idCategory": "1028381",
                "drawType": "F",
                "selectedRoundIndex": "",
                "selectedRowIndex": "",
            },
        )

    def test_published_draws_retries_blank_response(self):
        categories_html = """
        <select id="drawCategory">
          <option value="1028448|F">Simples Messieurs 2</option>
        </select>
        """
        client = _Client()
        client.get = mock.Mock(
            side_effect=[
                _Response(text="<html>Blank draw</html>"),
                _Response(text=categories_html),
            ]
        )

        draws = belgium_results_2._published_draws(client, "362072")

        self.assertEqual(len(draws), 1)
        self.assertEqual(draws[0]["category_id"], "1028448")
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(client.tele.errors, [])

    def test_draw_payload_retries_empty_response(self):
        completed = [[[{"statusWin": "V"}, {"statusWin": "E"}]]]
        client = _Client()
        client.post = mock.Mock(
            side_effect=[
                _Response(payload={"drawData": "[]", "roundNames": "[]"}),
                _Response(
                    payload={
                        "drawData": json.dumps(completed),
                        "roundNames": json.dumps(["FINALE"]),
                    }
                ),
            ]
        )

        payload = belgium_results_2._draw_payload(
            client,
            "362072",
            "1028448",
            "F",
        )

        self.assertEqual(payload, (completed, ["FINALE"]))
        self.assertEqual(client.post.call_count, 2)
        self.assertEqual(client.tele.errors, [])

    def test_malformed_category_and_draw_responses_record_errors(self):
        client = _Client(
            gets={
                belgium_results_2.CATEGORIES_URL: _Response(
                    text="<html>Maintenance</html>"
                )
            },
            posts={
                belgium_results_2.DRAW_DATA_URL: _Response(
                    payload={"drawData": {}, "roundNames": []}
                )
            },
        )

        self.assertEqual(belgium_results_2._published_draws(client, "362068"), [])
        self.assertIsNone(
            belgium_results_2._draw_payload(client, "362068", "1028381", "F")
        )
        self.assertEqual(len(client.tele.errors), 2)

    def test_gender_cache_retries_failed_profile_lookup(self):
        cache = belgium_results_2._GenderCache()
        client = _Client()

        with mock.patch.object(
            belgium_results_2,
            "_profile_gender",
            side_effect=[None, "M"],
        ) as profile_gender:
            self.assertEqual(cache.resolve(client, "6002148", "F"), "F")
            self.assertEqual(cache.resolve(client, "6002148", "F"), "M")
            self.assertEqual(cache.resolve(client, "6002148", "F"), "M")

        self.assertEqual(profile_gender.call_count, 2)

    def test_gender_cache_uses_fallback_once_for_limited_public_profile(self):
        profile_url = belgium_results_2.PLAYER_URL.format(player_id="3071830")
        client = _Client(gets={profile_url: _Response(text="<form>Connexion</form>")})
        client.log = mock.Mock()
        cache = belgium_results_2._GenderCache()

        self.assertEqual(cache.resolve(client, "3071830", "M"), "M")
        self.assertEqual(cache.resolve(client, "3071830", "M"), "M")

        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.tele.errors, [])
        client.log.assert_called_once()

    def test_completed_match_maps_names_profile_genders_and_spec_constants(self):
        metadata = {
            "tournament_name": "T.C. GRAND OHEY (6031)",
            "tournament_url": (
                "https://tennis.tppwb.be/MyAFT/Competitions/TournamentDetail/362068"
            ),
            "tournament_start_date": "5/28/2026",
            "tournament_end_date": "6/7/2026",
        }
        game = [
            {
                "name": "Alexandre CANTINEAU",
                "id": "6002148",
                "statusWin": "V",
                "score": "6-6-0",
                "resultType": "",
                "urlPlayerDrawDetail": (
                    "/MyAFT/Tooltip/PlayerDrawDetail?"
                    "PlayerAffiliationNumber=6002148&PlayerLastName=CANTINEAU"
                    "&PlayerFirstName=Alexandre"
                ),
            },
            {
                "name": "Denis SAMPAOLI",
                "id": "6024573",
                "statusWin": "E",
                "score": "2-1-0",
                "matchId": "7088863",
                "resultType": "",
                "urlPlayerDrawDetail": (
                    "/MyAFT/Tooltip/PlayerDrawDetail?"
                    "PlayerAffiliationNumber=6024573&PlayerLastName=SAMPAOLI"
                    "&PlayerFirstName=Denis"
                ),
            },
        ]
        male_profile = """
        <dl><dt>Sexe:</dt><dd><image src="/MyAFT/Content/Images/male.png"></image></dd></dl>
        """
        client = _Client(
            gets={
                belgium_results_2.PLAYER_URL.format(player_id="6002148"): _Response(
                    text=male_profile
                ),
                belgium_results_2.PLAYER_URL.format(player_id="6024573"): _Response(
                    text=male_profile
                ),
            }
        )

        row = belgium_results_2._match_row(
            client,
            game,
            metadata,
            "Open category",
            "1/4 FINALE",
            belgium_results_2._GenderCache(),
        )

        self.assertEqual(row["match_id"], "7088863")
        self.assertEqual(row["draw_name"], "Men's Singles")
        self.assertEqual(row["draw_team_type"], "Singles")
        self.assertEqual(row["tournament_name"], "T.C. GRAND OHEY (6031)")
        self.assertEqual(row["date"], "5/28/2026")
        self.assertEqual(row["round"], "1/4 FINALE")
        self.assertEqual(row["score"], "6-2, 6-1;")
        self.assertEqual(row["winner_1_name"], "CANTINEAU, Alexandre")
        self.assertEqual(row["winner_1_gender"], "M")
        self.assertEqual(row["winner_1_third_party_id"], "6002148")
        self.assertEqual(row["loser_1_name"], "SAMPAOLI, Denis")
        self.assertEqual(row["loser_1_gender"], "M")
        self.assertEqual(row["winner_1_country"], "Belgium")
        self.assertEqual(row["outcome"], "Completed")
        self.assertEqual(row["id_type"], "Belgium")
        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["tournament_host"], "")
        self.assertEqual(
            row["tournament_import_source"],
            "Association Francophone de Tennis - Belgium",
        )
        self.assertEqual(
            row["tournament_sanction_body"],
            "Association Francophone de Tennis - Belgium",
        )
        self.assertEqual(row["winner_1_dob"], "")
        self.assertEqual(row["tournament_start_date"], "5/28/2026")
        self.assertEqual(row["tournament_end_date"], "6/7/2026")

    def test_doubles_partners_and_countries_are_mapped(self):
        def side(prefix, status, score, match_id=""):
            return {
                "name": f"{prefix} One",
                "id": f"{prefix}1",
                "nameB": f"{prefix} Two",
                "idB": f"{prefix}2",
                "statusWin": status,
                "score": score,
                "matchId": match_id,
                "urlPlayerDrawDetail": (
                    "/MyAFT/Tooltip/PlayerDrawDetail?"
                    f"PlayerAffiliationNumber={prefix}1&PlayerLastName={prefix}ONE"
                    f"&PlayerFirstName=First&PlayerAffiliationNumber2={prefix}2"
                    f"&PlayerLastName2={prefix}TWO&PlayerFirstName2=Second"
                ),
            }

        gender_cache = SimpleNamespace(
            resolve=lambda _client, _player_id, fallback="": fallback
        )
        metadata = {
            "tournament_name": "Club",
            "tournament_url": "https://tennis.tppwb.be/detail",
            "tournament_start_date": "5/28/2026",
            "tournament_end_date": "6/7/2026",
        }

        row = belgium_results_2._match_row(
            _Client(),
            [side("W", "V", "6-6-0"), side("L", "E", "2-1-0", "match")],
            metadata,
            "Doubles Dames 2",
            "Finale",
            gender_cache,
        )

        self.assertEqual(row["draw_name"], "Women's Doubles")
        self.assertEqual(row["draw_team_type"], "Doubles")
        self.assertEqual(row["winner_2_name"], "WTWO, Second")
        self.assertEqual(row["winner_2_third_party_id"], "W2")
        self.assertEqual(row["winner_2_gender"], "F")
        self.assertEqual(row["winner_2_country"], "Belgium")
        self.assertEqual(row["loser_2_name"], "LTWO, Second")
        self.assertEqual(row["loser_2_country"], "Belgium")

    def test_walkover_and_retired_scores(self):
        walkover = ({"score": "--", "resultType": "WO"}, {"score": "--"})
        retired = (
            {"score": "5-0-0", "resultType": "Ab."},
            {"score": "3-0-0"},
        )

        self.assertEqual(belgium_results_2._outcome(*walkover), "Walkover")
        self.assertEqual(belgium_results_2._score(*walkover, "Walkover"), "W.O.;")
        self.assertEqual(belgium_results_2._outcome(*retired), "retired")
        self.assertEqual(
            belgium_results_2._score(*retired, "retired"),
            "5-3 ret.;",
        )

    def test_run_uses_only_tppwb_clients_and_writes_header_order(self):
        class RunClient:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.__class__.instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

            def close(self):
                pass

        row = {column: "" for column in belgium_results_2.COLUMNS}
        row.update(
            {
                "match_id": "7088863",
                "ball_type": "Yellow",
                "draw_name": "Men's Singles",
            }
        )
        run_obj = SimpleNamespace(
            pk=123,
            scraper=SimpleNamespace(worker_count=1, proxy=None),
            date_from=date(2026, 5, 28),
            date_to=date(2026, 6, 7),
        )
        RunClient.instances = []

        with mock.patch.object(
            belgium_results_2,
            "ScraperClient",
            RunClient,
        ), mock.patch.object(
            belgium_results_2,
            "build_proxies",
            return_value=None,
        ), mock.patch.object(
            belgium_results_2,
            "_discover_tournaments",
            return_value=["362068"],
        ), mock.patch.object(
            belgium_results_2,
            "_scrape_tournament",
            return_value=[row],
        ), mock.patch.object(
            belgium_results_2.Run.objects,
            "filter",
        ):
            items_csv, _requests, _errors, count, status = belgium_results_2.run(
                run_obj,
                lambda *_args: None,
            )

        rows = list(csv.reader(io.StringIO(items_csv)))
        self.assertEqual(status, belgium_results_2.Run.Status.SUCCESS)
        self.assertEqual(count, 1)
        self.assertEqual(rows[0], belgium_results_2.HEADER)
        self.assertEqual(rows[1][0], "7088863")
        self.assertEqual(rows[1][3], "Men's Singles")
        self.assertEqual(len(RunClient.instances), 2)
        self.assertTrue(
            all(
                instance.kwargs["allowed_hosts"] == ("tennis.tppwb.be",)
                for instance in RunClient.instances
            )
        )

    def test_run_scrapes_single_tournament_url_without_discovery(self):
        tournament_url = (
            "https://tennis.tppwb.be/MyAFT/Competitions/TournamentDetail/361658"
        )
        row = {column: "" for column in belgium_results_2.COLUMNS}
        row["match_id"] = "match-1"
        run_obj = SimpleNamespace(
            pk=124,
            scraper=SimpleNamespace(worker_count=1, proxy=None),
            params={"tournament_url": tournament_url},
            date_from=None,
            date_to=None,
        )
        client = mock.Mock()

        with mock.patch.object(
            belgium_results_2, "ScraperClient", return_value=client
        ), mock.patch.object(
            belgium_results_2, "build_proxies", return_value=None
        ), mock.patch.object(
            belgium_results_2, "_discover_tournaments"
        ) as discover, mock.patch.object(
            belgium_results_2, "_scrape_tournament", return_value=[row]
        ) as scrape, mock.patch.object(
            belgium_results_2.Run.objects, "filter"
        ):
            _items, _requests, _errors, count, status = belgium_results_2.run(
                run_obj,
                lambda *_args: None,
            )

        self.assertEqual(status, belgium_results_2.Run.Status.SUCCESS)
        self.assertEqual(count, 1)
        discover.assert_not_called()
        self.assertEqual(scrape.call_args.args[1], "361658")
        client.close.assert_called_once()


class BelgiumResults2DashboardTests(TestCase):
    def setUp(self):
        self.scraper = Scraper.objects.get(slug="belgium_results_2")
        self.user = get_user_model().objects.create_superuser(
            username="belgium-results-2-user",
            password="test-password",
        )
        self.client.force_login(self.user)

    def test_seeded_scraper_metadata_and_dashboard_visibility(self):
        self.assertEqual(self.scraper.code, "BE2")
        self.assertEqual(self.scraper.name, "Belgium Results 2")
        self.assertEqual(self.scraper.tour, "TPPWB")
        self.assertEqual(self.scraper.domain, "tennis.tppwb.be")
        self.assertEqual(self.scraper.threads, 5)
        self.assertEqual(self.scraper.max_tries, 4)
        self.assertIsNone(self.scraper.proxy_id)
        self.assertTrue(self.scraper.trigger_token)

        response = self.client.get(reverse("scrapers"))
        self.assertContains(response, 'data-slug="belgium_results_2"')
        self.assertContains(
            response,
            reverse("scraper_detail", args=["belgium_results_2"]),
        )
        self.assertContains(response, "Belgium Results 2")

    def test_detail_uses_date_range_or_url_form_without_model_upload(self):
        response = self.client.get(
            reverse("scraper_detail", args=[self.scraper.slug])
        )

        self.assertContains(response, 'name="date_from"')
        self.assertContains(response, 'name="date_to"')
        page = Selector(text=response.content.decode())
        self.assertTrue(page.css('form.rt-start [name="tournament_url"]'))

        settings_response = self.client.get(
            reverse("scraper_detail", args=[self.scraper.slug]) + "?tab=settings"
        )
        self.assertNotContains(settings_response, "Captcha solver model")
