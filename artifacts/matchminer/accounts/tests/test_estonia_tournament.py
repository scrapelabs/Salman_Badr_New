from unittest.mock import patch

from django.test import SimpleTestCase

from accounts.live_scrapers import estonia_tournament


class _FakeSelector:
    def __init__(self, url):
        self.url = url


class EstoniaTournamentNameTests(SimpleTestCase):
    def test_clean_name_removes_promoted_player_note(self):
        self.assertEqual(
            estonia_tournament._clean_name(
                "Hvingija (Tõstetud 1. Etapiks Üles), Suzanna"
            ),
            "Hvingija, Suzanna",
        )
        self.assertEqual(
            estonia_tournament._clean_name(
                "Suzanna Hvingija (Tõstetud 1. Etapiks Üles)"
            ),
            "Suzanna Hvingija",
        )

    def test_promoted_duplicate_player_uses_real_same_name_id(self):
        real_url = (
            "https://etl.tournamentsoftware.com/sport/player.aspx"
            "?id=1D0E38CC-0CE1-4C83-9BCF-70D087C34683&player=205"
        )
        promoted_url = real_url.replace("player=205", "player=320")
        tournament = {
            "tournament_id": "1D0E38CC-0CE1-4C83-9BCF-70D087C34683",
            "tournament_name": "Eesti Klubide Karikavõistlused 2026 - Noorteliiga",
            "tournament_url": "https://etl.tournamentsoftware.com/sport/tournament?id=1D0E38CC-0CE1-4C83-9BCF-70D087C34683",
            "tournament_start_date": "04/04/2026",
            "tournament_end_date": "04/04/2026",
        }
        match = {
            "draw_name": "Noorteliiga – I – Group G",
            "draw_team_type": "Singles",
            "match_date": "04/04/2026",
            "match_round": "",
            "score": "0-6, 0-6;",
            "outcome": "Completed",
            "winner_1": {
                "name": "Hvingija (Tõstetud 1. Etapiks Üles), Suzanna",
                "profile_url": promoted_url,
            },
            "loser_1": {"name": "Niit, Enrico", "profile_url": real_url},
        }

        def fake_get_sel(_client, url, _cache):
            return _FakeSelector(url)

        def fake_player_id(_client, sel, _fallback_name, _cache):
            if "player=205" in sel.url:
                return "10057315"
            return "local_10357854892411419264"

        def fake_matches(sel):
            return [match] if "player=320" in sel.url else []

        with patch.object(estonia_tournament, "_tournament_player_links", return_value=[]), patch.object(
            estonia_tournament,
            "_sport_player_links",
            return_value=[
                ("Hvingija, Suzanna", real_url),
                ("Hvingija (Tõstetud 1. Etapiks Üles), Suzanna", promoted_url),
            ],
        ), patch.object(estonia_tournament, "_get_sel", side_effect=fake_get_sel), patch.object(
            estonia_tournament, "_player_id", side_effect=fake_player_id
        ), patch.object(estonia_tournament, "_player_dob", return_value=""), patch.object(
            estonia_tournament, "resolve_gender", return_value="F"
        ), patch.object(
            estonia_tournament, "_parse_matches_sport", side_effect=fake_matches
        ):
            rows = estonia_tournament._scrape_tournament(
                client=object(), tournament=tournament, claude_keys=[]
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["winner_1_name"], "Hvingija, Suzanna")
        self.assertEqual(rows[0]["winner_1_third_party_id"], "10057315")
