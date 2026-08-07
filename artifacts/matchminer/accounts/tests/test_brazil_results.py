import csv
import io
from types import SimpleNamespace
from unittest import mock

from parsel import Selector

from django.test import SimpleTestCase

from accounts.live_scrapers import brazil_results
from accounts.models import Run


BRAZIL_DOUBLES_PANEL = """
<div class="tournament-title">G2 - Copa Skop de Tenis</div>
<div class="tournament-local">Natal-RN</div>
<div class="tournament-period"><div class="info">09/06/2026 a 12/06/2026</div></div>
<div class="tournament-share"><input class="form-control" value="https://www.tenisintegrado.com.br/torneio_painel_jogo/index/22812"></div>
<input name="id_torneio" value="22812">
<h4>16 Anos Masculino Duplas</h4>
<div class="game">
  <div class="game-top"><span>1º Jogo - Quartas - 11/06/2026 10:30</span><span>(2520483)</span></div>
  <ul class="list-group">
    <li class="list-group-item">
      <div class="players">
        <div class="avatar-container"><span class="avatar-info"><a href="perfil2/index/418799">Leonardo Hora</a></span></div>
        <div class="avatar-container"><span class="avatar-info"><a href="perfil2/index/232359">Joao Duthevicz</a></span></div>
      </div>
      <div class="score pull-right"><div class="set">6</div><div class="set">4</div></div>
    </li>
    <li class="list-group-item">
      <div class="players">
        <div class="avatar-container"><span class="avatar-info"><a href="perfil2/index/274296">Leonardo Do Ceara</a></span></div>
        <div class="avatar-container"><span class="avatar-info"><a href="perfil2/index/423177">Theo Grimaldi</a></span></div>
      </div>
      <div class="score pull-right"><div class="set">3</div><div class="set">2</div></div>
    </li>
  </ul>
  <div class="game-bottom">VENCEDOR: Leonardo Hora - 2 X 0</div>
</div>
"""


class _Response:
    def __init__(self, text="", data=None):
        self.status_code = 200
        self.text = text
        self._data = data or {}

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self):
        self.posts = []
        self.tele = type("Telemetry", (), {"record_error": lambda *args, **kwargs: None})()
        self.log = lambda *args, **kwargs: None

    def post(self, url, data=None, headers=None):
        self.posts.append((url, data or {}))
        if url == brazil_results.GRUPOS_URL:
            return _Response(data={"233964": "#1-Chave principal"})
        return _Response(BRAZIL_DOUBLES_PANEL)


class BrazilResultsDoublesTests(SimpleTestCase):
    def test_nested_doubles_players_are_parsed_as_partners(self):
        rows = brazil_results._parse_games(
            "https://www.tenisintegrado.com.br/torneio_painel_jogo/index/22812",
            Selector(text=BRAZIL_DOUBLES_PANEL),
            client=None,
            claude_keys=[],
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["draw_team_type"], "Doubles")
        self.assertEqual(row["winner_1_name"], "Hora, Leonardo")
        self.assertEqual(row["winner_2_name"], "Duthevicz, Joao")
        self.assertEqual(row["loser_1_name"], "Ceara, Leonardo Do")
        self.assertEqual(row["loser_2_name"], "Grimaldi, Theo")
        self.assertEqual(row["score"], "6-3, 4-2;")

    def test_two_player_sides_are_doubles_even_when_draw_label_is_generic(self):
        rows = brazil_results._parse_games(
            "https://www.tenisintegrado.com.br/torneio_painel_jogo/index/22812",
            Selector(text=BRAZIL_DOUBLES_PANEL.replace("16 Anos Masculino Duplas", "Sub 16")),
            client=None,
            claude_keys=[],
        )

        self.assertEqual(rows[0]["draw_team_type"], "Doubles")

    def test_category_posts_to_tournament_scoped_panel_url(self):
        client = _FakeClient()
        initial = Selector(
            text="""
            <select id="id_categoria"><option value="18">16 Anos Masculino Duplas</option></select>
            <select id="id_periodo"><option selected="selected" value="23721">09/06/2026 - 12/06/2026</option></select>
            <input name="id_torneio" value="22812">
            """
        )

        rows = brazil_results._parse_category(
            client,
            "https://www.tenisintegrado.com.br/torneio_painel_jogo/index/22812",
            initial,
            claude_keys=[],
        )

        self.assertEqual(len(rows), 1)
        self.assertTrue(
            any(
                url == "https://www.tenisintegrado.com.br/torneio_painel_jogo/index/22812"
                for url, _data in client.posts
            )
        )

    def test_clean_window_without_published_matches_is_success(self):
        class RunClient:
            def __init__(self, **_kwargs):
                self.log = lambda *_args: None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

            def close(self):
                pass

        run_obj = SimpleNamespace(
            pk=1,
            scraper=SimpleNamespace(worker_count=1, proxy=None),
            params={"year": 2026, "month": 8},
            date_from=None,
        )
        logs = []

        with mock.patch.object(
            brazil_results, "resolve_claude_keys", return_value=["key"]
        ), mock.patch.object(
            brazil_results, "build_proxies", return_value=None
        ), mock.patch.object(
            brazil_results, "ScraperClient", RunClient
        ), mock.patch.object(
            brazil_results, "_discover_tournaments", return_value=["tournament"]
        ), mock.patch.object(
            brazil_results, "_scrape_tournament", return_value=[]
        ), mock.patch.object(
            brazil_results.Run.objects, "filter"
        ):
            items, _requests, errors, count, status = brazil_results.run(
                run_obj,
                lambda level, message: logs.append((level, message)),
            )

        self.assertEqual(status, Run.Status.SUCCESS)
        self.assertEqual(count, 0)
        self.assertEqual(errors, "")
        self.assertEqual(next(csv.reader(io.StringIO(items))), brazil_results.HEADER)
        self.assertTrue(any("No completed matches" in message for _, message in logs))
