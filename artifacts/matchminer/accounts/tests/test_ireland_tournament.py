from django.test import SimpleTestCase

from accounts.live_scrapers import _ts_tournament, ireland_tournament


class IrelandTournamentFilteringTests(SimpleTestCase):
    def test_ireland_excludes_handicap_events_by_name(self):
        kept = _ts_tournament._filter_tournaments(
            ireland_tournament.CONFIG,
            [
                {"tournament_name": "Ireland Junior Open"},
                {"tournament_name": "Senior Handicaps 2026 MEMBERS ONLY"},
                {"tournament_name": "St Mary's Club Handicap Championships 2026"},
                {"tournament_name": "Mixed CASE hAnDiCaP Invitational"},
            ],
            lambda *_args: None,
        )

        self.assertEqual(kept, [{"tournament_name": "Ireland Junior Open"}])
