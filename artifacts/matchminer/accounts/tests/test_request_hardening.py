from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from accounts.views import LOGIN_ACCOUNT_FAILURE_LIMIT


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "request-hardening-tests",
        }
    }
)
class RequestHardeningTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_host_allowlist_is_not_wildcard(self):
        self.assertNotIn("*", settings.ALLOWED_HOSTS)
        self.assertIn("40.71.187.109", settings.ALLOWED_HOSTS)
        self.assertIn(".replit.dev", settings.ALLOWED_HOSTS)
        self.assertIn("testserver", settings.ALLOWED_HOSTS)

    def test_probe_paths_are_blocked_before_normal_routing(self):
        for path in ("/.env", "/wp-admin/install.php", "/vendor/phpunit/phpunit"):
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.content, b"")

    def test_encoded_traversal_is_blocked(self):
        response = self.client.get("/%2e%2e/%2e%2e/etc/passwd")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.content, b"")

    def test_overlong_request_target_is_blocked(self):
        response = self.client.get("/?" + ("x" * 9000))

        self.assertEqual(response.status_code, 414)
        self.assertEqual(response.content, b"")

    def test_unsupported_methods_are_rejected_early(self):
        response = self.client.generic("TRACE", "/")

        self.assertEqual(response.status_code, 405)

    def test_normal_login_page_still_loads(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "username")

    def test_repeated_login_failures_are_throttled(self):
        User = get_user_model()
        User.objects.create_user("operator", password="correct-password")

        for _ in range(LOGIN_ACCOUNT_FAILURE_LIMIT):
            response = self.client.post(
                "/",
                {"username": "operator", "password": "wrong-password"},
                REMOTE_ADDR="203.0.113.10",
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/",
            {"username": "operator", "password": "wrong-password"},
            REMOTE_ADDR="203.0.113.10",
        )

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many failed login attempts", status_code=429)

    def test_login_throttle_does_not_trust_spoofed_x_forwarded_for(self):
        User = get_user_model()
        User.objects.create_user("operator", password="correct-password")

        for idx in range(LOGIN_ACCOUNT_FAILURE_LIMIT):
            response = self.client.post(
                "/",
                {"username": "operator", "password": "wrong-password"},
                REMOTE_ADDR="203.0.113.10",
                HTTP_X_FORWARDED_FOR=f"198.51.100.{idx}",
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/",
            {"username": "operator", "password": "wrong-password"},
            REMOTE_ADDR="203.0.113.10",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many failed login attempts", status_code=429)

    @override_settings(TRUSTED_PROXY_IPS=["10.0.0.1"])
    def test_login_throttle_trusts_x_forwarded_for_from_configured_proxy(self):
        User = get_user_model()
        User.objects.create_user("operator", password="correct-password")

        for _ in range(LOGIN_ACCOUNT_FAILURE_LIMIT):
            response = self.client.post(
                "/",
                {"username": "operator", "password": "wrong-password"},
                REMOTE_ADDR="10.0.0.1",
                HTTP_X_FORWARDED_FOR="198.51.100.10",
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/",
            {"username": "operator", "password": "wrong-password"},
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="198.51.100.10",
        )

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many failed login attempts", status_code=429)
