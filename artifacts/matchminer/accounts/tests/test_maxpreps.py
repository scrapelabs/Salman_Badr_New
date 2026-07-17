from datetime import date

from django.test import SimpleTestCase, override_settings

from accounts.live_scrapers import maxpreps, registry
from accounts.views import validate_run_params


class MaxPrepsTests(SimpleTestCase):
    @override_settings(MAXPREPS_API_KEY="")
    def test_api_key_ignores_run_param_and_uses_default(self):
        self.assertEqual(
            maxpreps._api_key({"api_key": "not-the-maxpreps-feed-key"}),
            maxpreps.MAXPREPS_API_KEY,
        )

    @override_settings(MAXPREPS_API_KEY="SERVER-KEY")
    def test_api_key_prefers_server_setting_over_run_param(self):
        self.assertEqual(
            maxpreps._api_key({"api_key": "not-the-maxpreps-feed-key"}),
            "SERVER-KEY",
        )

    def test_run_params_do_not_accept_editable_api_key(self):
        spec = registry.get_spec("maxpreps")

        self.assertIsNotNone(spec)
        self.assertFalse(spec.feed_api_key)

        inputs = validate_run_params(
            spec,
            {
                "date_from": date(2026, 6, 10).isoformat(),
                "date_to": date(2026, 7, 1).isoformat(),
                "rank_type": "both",
                "api_key": "not-the-maxpreps-feed-key",
            },
        )

        self.assertNotIn("api_key", inputs.params)
