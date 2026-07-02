---
name: MatchMiner live scrapers (catalogue + shared engines)
description: The catalogue is being expanded from the full source zip by porting whole families onto shared parameterised engines; unwired slugs fail honestly.
---

# Which tennis sources are wired (and the shared-engine strategy)

The catalogue is being expanded from the supplied source zip
(`attached_assets/scripts_*.zip`, ~38 spiders) by porting **families** onto a few
**shared, parameterised engines**, each driven by thin per-source config wrappers
(host/labels/constants only) + a `LIVE_SCRAPERS` registry entry + an idempotent
seed migration. Ports are **deterministic and AI-free**: the source AI was only
cosmetic gender/name guessing — gender left blank, names emitted as scraped
(cleaned of `[seed]`).

**Catalogue is now COMPLETE at 37 wired runners** (37 SPECs with a runner_path == 37
seeded `Scraper` rows). The last batch added the 4 originally-"HARD" sources as real
deterministic ports (no fabrication): **australia_tennis** (Azure Blob via a SAS URL,
`AUSTRALIA_TENNIS_SAS_URL`, 61-col), **poland_results** (portal.pzt.pl ASP.NET
WebForms, 61-col, no creds — open site, works direct), **usta_team_captains**
(TennisLink login `USTA_USERNAME`/`USTA_PASSWORD`, bespoke 15-col; the source's AI
name-split replaced by a deterministic "Last, First" parser), **college_dual_match**
(AI-CORE: real Claude extraction via `CLAUDE_KEYS` list + `OPENAI_API_KEY`, bespoke
23-col, prompt in `college_dual_match_prompt.txt`). All creds are `getattr`-read from
settings and **honest-fail** when unset (like ioncourt/prestosports/BJK-proxy).

Engines (all over the shared `_http.ScraperClient` + `telemetry.py`):
- **`_stadion.py`** — ITF/Stadion JSON API (`api.itf-production.sports-data.stadion.io`),
  `StadionConfig(draw_code, …)`. Wrappers: Billie Jean King Cup (`bjkc`), Davis Cup
  (`dc`). **Needs a residential proxy** (CloudFront 403s datacenter IPs); direct = honest fail.
- **`_ts_tournament.py`** — tournamentsoftware.com INDIVIDUAL tournaments. Has a
  **fixed-country** path and a **dynamic-country** sub-family (per-player nationality
  read from the profile flag; org labels from config). Works **direct (no proxy)**.
- **`_ts_league.py`** — tournamentsoftware.com **team leagues** (cookiewall →
  find/league/DoSearch → var DrawList → draw/<id> → team-match → div.match →
  player profiles). `TSLeagueConfig(label, base, country, country_code, sanction_body)`.
  Wrappers: croatia/denmark/sweden/hong_kong/finland. Finland's host is the federation's
  own `www.tennisassa.fi` (same platform). Direct, no proxy.
- **`_rankings.py`** — player **ranking snapshots**, a different output shape from the
  match-result scrapers: a 9-col schema (Birthdate, Gender, Player Id, Name, Nationality,
  Points, Rank, Rankdate, Ranktype), one row per ranked player across singles+doubles.
  Wrappers: `wtatennis` (WTA JSON API, gender F, direct — works) and `atptour` (2-stage:
  Cloudflare-gated rankings HTML discovery → hero JSON enrich, gender M, **needs a
  residential proxy** like Stadion; rankdate kept as the ISO snapshot, a faithful quirk vs
  WTA's m/d/Y). Uses the `rank_snapshot` input_kind (single date).
- **Standalone (own-parser) sources** — no shared engine; each its own module over
  `_http`+`telemetry`+the 61-col COLUMNS (or `_rankings` for padelfip): czech_scraper,
  uruguay_results, ioncourt, maxpreps, new_jersey_high_school, prestosports, padelfip,
  estonia_tournament.
  - **Creds-gated** (honest-fail until set; env vars, ioncourt pattern): ioncourt →
    `IONCOURT_PHONE`/`IONCOURT_PASSWORD`; prestosports →
    `PRESTOSPORTS_USERNAME`/`PRESTOSPORTS_PASSWORD`.
  - **Datacenter-blocked** (honest-fail here; work from a reachable network/proxy):
    new_jersey_high_school feed, estonia TS finder, atptour Cloudflare.
  - **padelfip** quirk: the FIP rankings API only serves the **current ISO week**;
    historical snapshot dates return [] → honest-fail with a diagnostic (faithful to source).

Source quirks worth remembering:
- **estonia_tournament**: its source uses a *dual parser* that doesn't fit the shared
  TS-tournament engine, so it's a bespoke standalone module (see above) and keeps a
  deterministic sha256 id fallback. **Exception to the AI-free rule:** per explicit
  user instruction it infers player gender from names via Claude
  (`_claude_gender.resolve_gender`) **only, with NO fallback**, and **honest-fails the
  whole run** (FAILED, 0 rows, error asks for the key) before any scraping if no
  Anthropic key resolves (per-scraper → Settings/workspace → env). Don't "helpfully"
  add a draw-name/heuristic gender fallback or re-drop the Claude call.
- **brazil_results** (standalone): same Claude-only per-player gender contract as
  estonia (infer from name via `_claude_gender.resolve_gender`, cached; NO fallback;
  honest-fail the run before any network if no Anthropic key resolves). The
  per-tournament `ScraperClient` + resolved `claude_keys` are threaded from `run()`
  down to `_MatchParser` (via a `process()` closure). Unlike estonia, the
  `draw_gender` **field** still uses the Portuguese draw-name word
  (`masculino`/`feminino`) — that's a real Brazil signal; only the per-player gender
  became Claude. The source already called Claude per player and **discarded** the
  gender (fell back to the draw name, blank for age-category draws) — this restores it.
  Don't re-add a draw-name per-player fallback or restore Claude name pretty-formatting.
- **Shared TS engines gender modes** (`_ts_tournament`/`_ts_league`): both support
  Claude name→gender via two config flags. `claude_gender` alone = **SOFT** (if no
  key: WARN + fall back to draw-name gender; currently unused). `claude_gender` +
  `claude_gender_required` = **HARD** (if no key: honest-fail the run + ask for the
  key before any network; used by **Finland** tournament & league, **Croatia**
  tournament & league, and **Tennis Europe**, matching Estonia's Claude-only
  contract). In the Claude branch per-player gender is **always Claude-only**; only
  the draw-level `draw_gender` may use an explicit draw-name word.
  **Why:** the sources inferred per-player gender from names via an LLM with no
  fallback; the user explicitly directed Croatia onto the same no-fallback contract
  (July 2026) after its soft mode masked missing genders. Don't flip any of them
  back to soft; keep SOFT only as an engine capability.
- **Tennis Europe ranking-tab DOB** (`_ts_tournament` `ranking_dob` flag): TE junior
  profiles hide DOB/YOB from both the profile head and Biography tab, so the source
  walked the site-wide ranking (`/ranking/` → first ranking → every "More" category
  list, `&ps=100`, pages via `page_caption` count/100) and recorded `1/1/<YOB>` keyed
  by **profile GUID** (ranking href `?id=<GUID>` ↔ player page `/player-profile/<guid>`,
  lowercase both sides). With the flag on, DOB comes from that registry **only** —
  unranked players stay blank (registry miss = blank, NO profile/Biography fallback,
  per user directive). Don't re-add per-profile DOB lookups for TE.
- **parsel `@xlink:href` trap**: an xpath using the `xlink:` prefix raises
  `ValueError: Undefined namespace prefix` on any page that doesn't declare the
  namespace (evaluated per-document, so it "works" on some pages and blows up runs
  on others — it broke single-URL discovery). Always use
  `@*[local-name()="href"]` for svg `<use>` hrefs.
- Asset hosts like `objects.fi` / `objs.fi` in league sources are CDNs — ignore for
  data crawling. `scripts.fi` in finland_league is only a python import path, not a host.

**How to apply:**
- The app **fails honestly** for any slug with no `LIVE_SCRAPERS` entry — never
  simulate/fabricate rows. Assigning a proxy doesn't make an unwired slug work; only
  a registry entry + runner does.
- To add a same-family source: write a thin wrapper (config only), add the registry
  SPEC (input_kind + allowed_hosts for URL inputs = SSRF guard), add an idempotent
  `get_or_create` seed migration, `migrate`, then validate with a bounded in-process
  smoke (see `verifying-background-scrape-runs`).
- **SSRF is enforced centrally in `_http.ScraperClient.request()`**, not just at the
  view layer. The client validates the **initial** target URL (not only redirect hops)
  via `assert_safe_url(url, allowed_hosts=self.allowed_hosts)` — http(s) only, no
  local/`.internal`/`.local` names, must resolve to a PUBLIC IP, optional host
  allowlist. A blocked URL is an honest fail (logs a redacted WARN + records an error
  CSV row + returns `None`), never an exception. **Why:** scrapers that discover
  second-stage links from external content (college_dual_match → Google Sheets /
  schedule pages / box-score PDFs) never pass through the view's `validate_run_params`
  guard, so without central validation a malicious sheet could point the fetcher at
  `169.254.169.254` / `127.0.0.1` / numeric-obfuscated loopback. Pass `allowed_hosts`
  to the client when inputs are URL-driven; leave it `None` to allow any public host.
- When resolving relative links discovered on a page, urljoin against the **full page
  URL** (`urljoin(current_page_url, href)`), never `scheme://host` — otherwise a
  relative href like `box.html` under `/teams/x/schedule/` silently flattens to the
  site root and the crawler under-collects (was a college_dual_match bug).
- Adding a **brand-new `input_kind`** (the start-form shape) touches ~7 spots in
  lockstep — miss one and the form/webhook/schedule silently desync: in
  `registry.py` (the constant + INPUT_KINDS set + the per-SPEC `input_kind`), and in
  `views.py` the per-kind defaults (real-time-tab context + `sched_defaults`),
  `validate_run_params`, `_trigger_example_json`, and `_github_workflow_yaml`, plus the
  `scraper_detail.html` start-form `elif` branch. After any of these, **restart** the
  `artifacts/permitlify: web` workflow (`--noreload`).
- Per-run telemetry (requests/errors CSVs) must stay byte-compatible with the
  production framework; see `telemetry.py`.
