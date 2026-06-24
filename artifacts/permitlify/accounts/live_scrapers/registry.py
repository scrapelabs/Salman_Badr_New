"""Single source of truth for the scraper catalogue.

Each :class:`ScraperSpec` declares a scraper's slug, how its real-time start
form collects inputs (``input_kind``), where its runner lives, and (for URL
inputs) the host allowlist used as an SSRF guard. This registry is consumed by:

- ``accounts.management.commands.run_scrape`` (the worker) — to resolve and
  dispatch the runner;
- ``accounts.views`` — to drive the generic start form, validate inputs, and
  gate the trigger webhook;
- ``templates/scraper_detail.html`` (via the view context) — to render the
  right input fields and schedule docs.

Runners are referenced by ``"module.path:function"`` and imported **lazily**
(only inside the worker process, at dispatch time). That keeps importing this
module in the web process light, and means a broken scraper module can never
take down the site — it just fails that one run honestly.
"""

import importlib
from dataclasses import dataclass

# --- input kinds: what the real-time start form collects ------------------
INPUT_YEAR = "year"                            # a single season year
INPUT_YEAR_MONTH = "year_month"                # season year + month (0 = all)
INPUT_DATE_RANGE = "date_range"                # a from/to calendar window
INPUT_DATE_RANGE_OR_URL = "date_range_or_url"  # a tournament URL OR a date window

INPUT_KINDS = frozenset(
    {INPUT_YEAR, INPUT_YEAR_MONTH, INPUT_DATE_RANGE, INPUT_DATE_RANGE_OR_URL}
)


@dataclass(frozen=True)
class ScraperSpec:
    """How a scraper is launched and what inputs it takes."""

    slug: str
    input_kind: str = INPUT_YEAR
    runner_path: str = ""              # "accounts.live_scrapers.foo:run"
    allowed_hosts: tuple = ()          # host allowlist for URL inputs (SSRF guard)

    def load_runner(self):
        """Import and return the runner ``run(run_obj, log)``.

        Returns ``None`` when no runner is wired, so the worker can fail the run
        honestly instead of fabricating data. The import happens here (not at
        module load) to keep the web process light and isolate import failures
        to the run worker.
        """
        if not self.runner_path:
            return None
        module_path, _, func = self.runner_path.partition(":")
        module = importlib.import_module(module_path)
        return getattr(module, func)


SPECS = {
    "billiejeankingcup": ScraperSpec(
        slug="billiejeankingcup",
        input_kind=INPUT_YEAR,
        runner_path="accounts.live_scrapers.billiejeankingcup:run",
    ),
    "brazil_results": ScraperSpec(
        slug="brazil_results",
        input_kind=INPUT_YEAR_MONTH,
        runner_path="accounts.live_scrapers.brazil_results:run",
    ),
    "croatia_league": ScraperSpec(
        slug="croatia_league",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.croatia_league:run",
        allowed_hosts=("hts.tournamentsoftware.com",),
    ),
}

# Used for slugs without a registry entry so the UI / validation degrade
# gracefully (a plain year form) instead of erroring; the worker still fails
# such a run honestly because the spec carries no runner.
DEFAULT_SPEC = ScraperSpec(slug="", input_kind=INPUT_YEAR)


def get_spec(slug):
    """Return the :class:`ScraperSpec` for ``slug`` or ``None`` if unregistered."""
    return SPECS.get(slug)


def spec_for(slug):
    """Like :func:`get_spec` but never ``None`` (falls back to a year-input spec)."""
    return SPECS.get(slug) or DEFAULT_SPEC
