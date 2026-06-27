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
INPUT_RANK_SNAPSHOT = "rank_snapshot"          # a single ranking-snapshot date

INPUT_KINDS = frozenset(
    {
        INPUT_YEAR,
        INPUT_YEAR_MONTH,
        INPUT_DATE_RANGE,
        INPUT_DATE_RANGE_OR_URL,
        INPUT_RANK_SNAPSHOT,
    }
)


@dataclass(frozen=True)
class ScraperSpec:
    """How a scraper is launched and what inputs it takes."""

    slug: str
    input_kind: str = INPUT_YEAR
    runner_path: str = ""              # "accounts.live_scrapers.foo:run"
    allowed_hosts: tuple = ()          # host allowlist for URL inputs (SSRF guard)
    uses_browser: bool = False         # drives a headless-Chrome pool (resource-heavy)
    needs_claude: bool = False         # AI scraper: surface a Claude API key field
    url_required: bool = False         # URL input is mandatory (no date-only mode)
    needs_login: bool = False          # login scraper: surface username/password fields
    login_label: str = ""              # label for the login fields (e.g. "USTA TennisLink")
    login_user_label: str = "username" # noun for the identifier field (e.g. "phone")
    accepts_sheet: bool = False        # URL input may be a Google Sheet of Team/Link rows
    secret_label: str = ""             # label for a single masked secret config field
    secret_env_var: str = ""           # env var the runner falls back to (for help text)
    feed_api_key: bool = False         # surface a run-time feed API-key field
    feed_api_key_default: str = ""     # prefilled / fallback feed API key
    feed_gender: bool = False          # surface a boys/girls/both gender selector
    has_match_store: bool = False      # persists matches to CollegeMatch + a "Match database" tab
    model_upload_label: str = ""       # surface a model-file upload field on the Settings tab
    model_filename: str = ""           # canonical on-disk filename the uploaded model is saved as

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
    "davis_cup": ScraperSpec(
        slug="davis_cup",
        input_kind=INPUT_YEAR,
        runner_path="accounts.live_scrapers.davis_cup:run",
    ),
    "brazil_results": ScraperSpec(
        slug="brazil_results",
        input_kind=INPUT_YEAR_MONTH,
        runner_path="accounts.live_scrapers.brazil_results:run",
    ),
    "uruguay_results": ScraperSpec(
        slug="uruguay_results",
        input_kind=INPUT_YEAR_MONTH,
        runner_path="accounts.live_scrapers.uruguay_results:run",
    ),
    "croatia_league": ScraperSpec(
        slug="croatia_league",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.croatia_league:run",
        allowed_hosts=("hts.tournamentsoftware.com",),
    ),
    "denmark_league": ScraperSpec(
        slug="denmark_league",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.denmark_league:run",
        allowed_hosts=("dtf.tournamentsoftware.com",),
    ),
    "sweden_league": ScraperSpec(
        slug="sweden_league",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.sweden_league:run",
        allowed_hosts=("svtf.tournamentsoftware.com",),
    ),
    "hong_kong_league": ScraperSpec(
        slug="hong_kong_league",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.hong_kong_league:run",
        allowed_hosts=("hkta.tournamentsoftware.com",),
    ),
    "finland_league": ScraperSpec(
        slug="finland_league",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.finland_league:run",
        allowed_hosts=("www.tennisassa.fi", "tennisassa.fi"),
    ),
    # --- tournamentsoftware.com individual tournaments (shared engine) ----
    "croatia_tournament": ScraperSpec(
        slug="croatia_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.croatia_tournament:run",
        allowed_hosts=("hts.tournamentsoftware.com",),
    ),
    "denmark_tournament": ScraperSpec(
        slug="denmark_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.denmark_tournament:run",
        allowed_hosts=("dtf.tournamentsoftware.com",),
    ),
    "sweden_tournament": ScraperSpec(
        slug="sweden_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.sweden_tournament:run",
        allowed_hosts=("svtf.tournamentsoftware.com",),
    ),
    "hong_kong_tournament": ScraperSpec(
        slug="hong_kong_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.hong_kong_tournament:run",
        allowed_hosts=("hkta.tournamentsoftware.com",),
    ),
    "finland_tournament": ScraperSpec(
        slug="finland_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.finland_tournament:run",
        allowed_hosts=("www.tennisassa.fi", "tennisassa.fi"),
    ),
    "ireland_tournament": ScraperSpec(
        slug="ireland_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.ireland_tournament:run",
        allowed_hosts=("ti.tournamentsoftware.com",),
    ),
    "luxembourg_tournament": ScraperSpec(
        slug="luxembourg_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.luxembourg_tournament:run",
        allowed_hosts=("flt.tournamentsoftware.com",),
    ),
    # --- dynamic-country tournamentsoftware.com sites (shared engine) -----
    # One host aggregates tournaments from many countries; country is read
    # per-tournament and per-player rather than being a federation constant.
    "glta_tournament": ScraperSpec(
        slug="glta_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.glta_tournament:run",
        allowed_hosts=("glta.tournamentsoftware.com",),
    ),
    "tennis_europe": ScraperSpec(
        slug="tennis_europe",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.tennis_europe:run",
        allowed_hosts=("te.tournamentsoftware.com",),
    ),
    "cosat_tournament": ScraperSpec(
        slug="cosat_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.cosat_tournament:run",
        allowed_hosts=("cosat.tournamentsoftware.com",),
    ),
    "itf_juniors_tournament_software": ScraperSpec(
        slug="itf_juniors_tournament_software",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.itf_juniors_tournament_software:run",
        allowed_hosts=("itfjuniors.tournamentsoftware.com",),
    ),
    # --- itftennis.com circuits (shared engine, parameterised by circuit) -
    "itftennis_juniors": ScraperSpec(
        slug="itftennis_juniors",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.itftennis_juniors:run",
        allowed_hosts=("www.itftennis.com",),
        uses_browser=True,
    ),
    "itftennis_masters": ScraperSpec(
        slug="itftennis_masters",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.itftennis_masters:run",
        allowed_hosts=("www.itftennis.com",),
        uses_browser=True,
    ),
    "itftennis_mens": ScraperSpec(
        slug="itftennis_mens",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.itftennis_mens:run",
        allowed_hosts=("www.itftennis.com",),
        uses_browser=True,
    ),
    "itftennis_womens": ScraperSpec(
        slug="itftennis_womens",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.itftennis_womens:run",
        allowed_hosts=("www.itftennis.com",),
        uses_browser=True,
    ),
    # --- ioncourt.com JSON API (college dual matches) ---------------------
    # A pure date-range scraper (no tournament URL). No host allowlist: it
    # only ever calls its own hard-coded api.ioncourt.com endpoints.
    "ioncourt": ScraperSpec(
        slug="ioncourt",
        input_kind=INPUT_DATE_RANGE,
        runner_path="accounts.live_scrapers.ioncourt:run",
        needs_login=True,
        login_label="Ioncourt",
        login_user_label="phone",
    ),
    # --- prestosports.com JSON+XML API (college dual matches) -------------
    # Date-range scraper over its own hard-coded gameday-api.prestosports.com
    # endpoints (no URL input / host allowlist). Needs login credentials
    # (PRESTOSPORTS_USERNAME / PRESTOSPORTS_PASSWORD); without them the run
    # fails honestly, like ioncourt without its credentials.
    "prestosports": ScraperSpec(
        slug="prestosports",
        input_kind=INPUT_DATE_RANGE,
        runner_path="accounts.live_scrapers.prestosports:run",
        needs_login=True,
        login_label="PrestoSports",
    ),
    # --- cesky-tenis.cz (Czech national tennis) standalone HTML scraper ----
    # A date-range OR single-tournament-URL scraper; the seed URL is validated
    # against the cesky-tenis.cz allowlist at the view layer.
    "czech_scraper": ScraperSpec(
        slug="czech_scraper",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.czech_scraper:run",
        allowed_hosts=("cesky-tenis.cz",),
    ),
    "belgium_results": ScraperSpec(
        slug="belgium_results",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.belgium_results:run",
        allowed_hosts=("www.tennisenpadelvlaanderen.be", "tennisenpadelvlaanderen.be"),
        model_upload_label="Captcha solver model (.keras)",
        model_filename="captcha_model.keras",
    ),
    # --- US high-school feed APIs (date-range; own hard-coded hosts) -------
    # Vendor feed APIs keyed by a feed api_key (overridable via settings).
    # No URL input / host allowlist — each calls only its own host.
    "maxpreps": ScraperSpec(
        slug="maxpreps",
        input_kind=INPUT_DATE_RANGE,
        runner_path="accounts.live_scrapers.maxpreps:run",
    ),
    "new_jersey_high_school": ScraperSpec(
        slug="new_jersey_high_school",
        input_kind=INPUT_DATE_RANGE,
        runner_path="accounts.live_scrapers.new_jersey_high_school:run",
        feed_api_key=True,
        feed_api_key_default="4f59cee1-3db0-4128-84ba-bd7995dadd95",
        feed_gender=True,
    ),
    # --- Estonia (tennis.ee discovery + etl.tournamentsoftware.com) -------
    # Bespoke multi-stage scraper (NOT the shared TS engine). Date-range OR a
    # single tournament URL; the seed URL is validated against the
    # etl.tournamentsoftware.com allowlist at the view layer.
    "estonia_tournament": ScraperSpec(
        slug="estonia_tournament",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.estonia_tournament:run",
        allowed_hosts=("etl.tournamentsoftware.com",),
    ),
    # --- player-ranking snapshots (singles + doubles in one run) ----------
    # Not match results: a single snapshot date yields a 9-column ranking
    # table. Each only calls its own hard-coded host, so no URL input / host
    # allowlist is needed. atptour sits behind Cloudflare — without a residential
    # proxy that clears it, the run fails honestly (like the Stadion scrapers).
    "wtatennis": ScraperSpec(
        slug="wtatennis",
        input_kind=INPUT_RANK_SNAPSHOT,
        runner_path="accounts.live_scrapers.wtatennis:run",
    ),
    "atptour": ScraperSpec(
        slug="atptour",
        input_kind=INPUT_RANK_SNAPSHOT,
        runner_path="accounts.live_scrapers.atptour:run",
    ),
    # padelfip: FIP world padel rankings (www.padelfip.com WordPress API).
    # A rank-snapshot scraper like wtatennis; only calls its own host.
    "padelfip": ScraperSpec(
        slug="padelfip",
        input_kind=INPUT_RANK_SNAPSHOT,
        runner_path="accounts.live_scrapers.padelfip:run",
    ),
    # --- Tennis Australia (Azure Blob match feed) -------------------------
    # Date-range scraper reading match JSON from an Azure Blob container via a
    # SAS URL (settings.AUSTRALIA_TENNIS_SAS_URL — a credential). No URL input
    # / host allowlist: it only calls its own SAS-signed container. Without the
    # SAS URL the run fails honestly, like the Stadion scrapers without a proxy.
    "australia_tennis": ScraperSpec(
        slug="australia_tennis",
        input_kind=INPUT_DATE_RANGE,
        runner_path="accounts.live_scrapers.australia_tennis:run",
        secret_label="Azure Blob SAS URL",
        secret_env_var="AUSTRALIA_TENNIS_SAS_URL",
    ),
    # --- Polish Tennis (PZT) results — portal.pzt.pl ASP.NET HTML ---------
    # Date-range OR a single tournament URL; the seed URL is validated against
    # the portal.pzt.pl allowlist at the view layer. Fully deterministic.
    "poland_results": ScraperSpec(
        slug="poland_results",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.poland_results:run",
        allowed_hosts=("portal.pzt.pl",),
    ),
    # --- USTA League Team Captains (tennislink.usta.com) ------------------
    # A championship-year scraper (INPUT_YEAR) that logs into USTA TennisLink
    # (settings.USTA_USERNAME / USTA_PASSWORD) and walks the team rosters.
    # Bespoke 15-column captains schema. Without credentials it fails honestly.
    "usta_team_captains": ScraperSpec(
        slug="usta_team_captains",
        input_kind=INPUT_YEAR,
        runner_path="accounts.live_scrapers.usta_team_captains:run",
        needs_login=True,
        login_label="USTA TennisLink",
    ),
    # --- College Dual Match (AI) — Claude box-score extraction -----------
    # An AI-core scraper: given a box-score / schedule / Google-Sheet URL, it
    # extracts matches with Anthropic Claude (settings.CLAUDE_KEYS). URL input
    # with an open allowlist (arbitrary athletics sites); the view still applies
    # the SSRF public-IP guard. Without Claude keys the run fails honestly.
    "college_dual_match": ScraperSpec(
        slug="college_dual_match",
        input_kind=INPUT_DATE_RANGE_OR_URL,
        runner_path="accounts.live_scrapers.college_dual_match:run",
        needs_claude=True,
        url_required=True,
        accepts_sheet=True,
        has_match_store=True,
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
