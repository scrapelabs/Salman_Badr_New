from django.test import SimpleTestCase

from accounts.live_scrapers import _ts_tournament


class TSTournamentDateWindowTests(SimpleTestCase):
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
