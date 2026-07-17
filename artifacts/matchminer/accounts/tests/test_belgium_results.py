import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from accounts.live_scrapers import belgium_results, registry
from accounts.views import validate_run_params


TPPWB_URL = (
    "https://tennis.tppwb.be/MyAFT/Competitions/TournamentDraw"
    "?idTournoi=362070&idCategory=1028406&drawType=P"
    "&roundIndex=4&rowIndex=7"
)

TPPWB_PAGE = """
<h4>362070 [6000-CITADELLE] - Du 11/06/2026 au 21/06/2026 -
  Crit&eacute;rium adultes by Cornet
</h4>
<select id="drawCategory">
  <option value="1028406|F,Q,P" selected>Messieurs 1 (BC2*) (95-115)</option>
</select>
"""

COMPLETED_GAME = [
    {
        "name": "Lucas PEHARPRE",
        "id": "04081723",
        "nameB": "",
        "idB": "",
        "statusWin": "V",
        "score": "6-3-7",
        "resultType": "",
        "urlPlayerDrawDetail": (
            "/MyAFT/Tooltip/PlayerDrawDetail?PlayerAffiliationNumber=04081723"
            "&PlayerLastName=PEHARPRE&PlayerFirstName=Lucas"
        ),
    },
    {
        "name": "Thomas VAN DE VELDE",
        "id": "6044642",
        "nameB": "",
        "idB": "",
        "statusWin": "E",
        "matchId": "7104177",
        "score": "4-6-5",
        "resultType": "",
        "urlPlayerDrawDetail": (
            "/MyAFT/Tooltip/PlayerDrawDetail?PlayerAffiliationNumber=6044642"
            "&PlayerLastName=VAN%20DE%20VELDE&PlayerFirstName=Thomas"
        ),
    },
]


class _Response:
    def __init__(self, *, text="", payload=None, status_code=200):
        self.text = text
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class _DrawClient:
    def __init__(self):
        unplayed = [
            {"name": "Waiting One", "id": "1", "statusWin": "", "score": "0-0-0"},
            {"name": "Waiting Two", "id": "2", "statusWin": "", "score": "0-0-0"},
        ]
        virtual = [
            {"name": "Next draw", "id": "virtual_final_team", "statusWin": "V"},
            {"name": "Player", "id": "3", "statusWin": "E", "matchId": "skip"},
        ]
        self.payload = {
            "drawData": json.dumps([[COMPLETED_GAME, unplayed, virtual], [[COMPLETED_GAME[0]]]]),
            "roundNames": json.dumps(["1ER TOUR PQ", "PRE-QUALIFICATION"]),
        }
        self.posts = []

    def get(self, url, **kwargs):
        return _Response(text=TPPWB_PAGE)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response(payload=self.payload)


class _RunClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        pass


class BelgiumTPPWBTests(SimpleTestCase):
    def test_tppwb_url_is_accepted_for_belgium(self):
        with mock.patch("accounts.views._ssrf.assert_resolves_public"):
            inputs = validate_run_params(
                registry.SPECS["belgium_results"],
                {"tournament_url": TPPWB_URL},
            )

        self.assertEqual(inputs.params, {"tournament_url": TPPWB_URL})
        self.assertIn(
            "tennis.tppwb.be",
            registry.SPECS["belgium_results"].allowed_hosts,
        )

    def test_tppwb_draw_posts_expected_payload_and_maps_completed_match(self):
        client = _DrawClient()

        rows = belgium_results._scrape_tppwb_draw(
            client,
            TPPWB_URL,
            lambda *_args: None,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(client.posts[0][0], belgium_results.TPPWB_DRAW_ENDPOINT)
        self.assertEqual(
            client.posts[0][1]["data"],
            {
                "idTournoi": "362070",
                "idCategory": "1028406",
                "drawType": "P",
                "selectedRoundIndex": "4",
                "selectedRowIndex": "7",
            },
        )

        row = rows[0]
        self.assertEqual(row["match_id"], "7104177")
        self.assertEqual(row["score"], "6-4, 3-6, 7-5;")
        self.assertEqual(row["round"], "1ER TOUR PQ")
        self.assertEqual(row["winner_1_name"], "PEHARPRE, Lucas")
        self.assertEqual(row["winner_1_third_party_id"], "04081723")
        self.assertEqual(row["loser_1_name"], "VAN DE VELDE, Thomas")
        self.assertEqual(row["draw_name"], "Messieurs 1 (BC2*) (95-115)")
        self.assertEqual(row["draw_team_type"], "Singles")
        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["tournament_name"], "Crit\u00e9rium adultes by Cornet")
        self.assertEqual(row["tournament_host"], "6000-CITADELLE")
        self.assertEqual(row["date"], "6/11/2026")
        self.assertEqual(row["tournament_end_date"], "6/21/2026")
        self.assertEqual(row["tournament_sanction_body"], "Tennis Wallonie-Bruxelles")
        self.assertEqual(row["tournament_url"], TPPWB_URL)

    def test_tppwb_score_maps_walkover_and_abandonment(self):
        walkover = ({"score": "--", "resultType": "WO"}, {"score": "--", "resultType": "WO"})
        retired = (
            {"score": "5-0-0", "resultType": "Ab."},
            {"score": "3-0-0", "resultType": "Ab."},
        )

        self.assertEqual(belgium_results._tppwb_outcome(*walkover), "Walkover")
        self.assertEqual(belgium_results._tppwb_score(*walkover, "Walkover"), "W.O.;")
        self.assertEqual(belgium_results._tppwb_outcome(*retired), "retired")
        self.assertEqual(belgium_results._tppwb_score(*retired, "retired"), "5-3 ret.;")

    def test_tooltip_query_repairs_accented_player_name(self):
        player = belgium_results._tppwb_player(
            {
                "id": "0726430",
                "name": "Th\ufffdodore DEMANET",
                "urlPlayerDrawDetail": (
                    "/MyAFT/Tooltip/PlayerDrawDetail?PlayerLastName=DEMANET"
                    "&PlayerFirstName=Th%C3%A9odore"
                ),
            }
        )

        self.assertEqual(player, {"name": "DEMANET, Th\u00e9odore", "id": "0726430"})

    def test_tppwb_run_does_not_initialize_flemish_captcha(self):
        run_obj = SimpleNamespace(
            pk=123,
            scraper=SimpleNamespace(worker_count=1, proxy=None),
            params={"tournament_url": TPPWB_URL},
            date_from=None,
            date_to=None,
        )
        row = {
            "match_id": "7104177",
            "winner_1_name": "PEHARPRE, Lucas",
            "loser_1_name": "VAN DE VELDE, Thomas",
            "score": "6-4, 3-6, 7-5;",
        }

        with mock.patch.object(
            belgium_results, "build_proxies", return_value=None
        ), mock.patch.object(
            belgium_results, "ScraperClient", _RunClient
        ), mock.patch.object(
            belgium_results, "_scrape_tppwb_draw", return_value=[row]
        ) as scrape, mock.patch.object(
            belgium_results, "materialize_uploaded_model"
        ) as materialize, mock.patch.object(
            belgium_results, "CaptchaSolver"
        ) as solver, mock.patch.object(
            belgium_results.Run.objects, "filter"
        ):
            _csv, _requests, _errors, row_count, status = belgium_results.run(
                run_obj,
                lambda *_args: None,
            )

        self.assertEqual(row_count, 1)
        self.assertEqual(status, belgium_results.Run.Status.SUCCESS)
        scrape.assert_called_once()
        materialize.assert_not_called()
        solver.assert_not_called()
