from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        # Start the in-app recurring scheduler. It's a no-op outside a
        # web-serving process (e.g. during ``migrate``, the ``run_scrape``
        # worker subprocess, or any other management command), so importing it
        # here is safe in every context.
        try:
            from . import scheduler

            scheduler.start()
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("accounts.scheduler").exception(
                "Failed to start the in-app scheduler."
            )
