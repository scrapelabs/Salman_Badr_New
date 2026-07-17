# SportRadar Competition Metadata Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SportRadar CSV rows use the parent competition as the tournament, the child competition suffix as the draw, and an empty country cell instead of `Neutral`.

**Architecture:** Extend the existing SportRadar runner with pure row-normalization helpers plus a per-run Competition Info cache. Daily Summaries remain the source of matches, while Competition Info enriches only allowed-category rows and falls back to current naming without dropping data.

**Tech Stack:** Python 3.11, Django 5.2, `curl_cffi` through `ScraperClient`, Django `SimpleTestCase`, `unittest.mock`.

## Global Constraints

- Keep the change scoped to `artifacts/matchminer/accounts/live_scrapers/sportradar.py` and its tests.
- Preserve score, identity, DOB, category, deduplication, CSV columns, and current run-status semantics.
- Cache Competition Info successes and failures by parent ID for one run only.
- Never infer nationality or emit `Neutral`; use an empty cell when no non-neutral country candidate exists.
- Never drop a match solely because Competition Info is unavailable.
- Do not change QA ticket status.
- Stage and commit only files named by the active task; leave unrelated worktree changes untouched.

---

### Task 1: Normalize Enriched Row Fields

**Files:**
- Modify: `artifacts/matchminer/accounts/tests/test_sportradar.py:120-213`
- Modify: `artifacts/matchminer/accounts/live_scrapers/sportradar.py:123-143,304-431`

**Interfaces:**
- Produces: `_country_value(*values: str) -> str`
- Produces: `_draw_suffix(parent_name: str, child_name: str) -> str`
- Extends: `_row_from_summary(summary, *, client=None, api_key="", dob_cache=None, competition_metadata=None) -> dict | None`
- `competition_metadata` shape: a dictionary with `parent` and `child` keys, each containing a SportRadar competition dictionary.

- [ ] **Step 1: Add failing row-mapping regression tests**

Add these methods to `SportRadarTests` in `artifacts/matchminer/accounts/tests/test_sportradar.py`:

```python
    def test_enriched_row_uses_parent_tournament_and_child_draw_suffix(self):
        summary = singles_summary()
        home = summary["sport_event"]["competitors"][0]
        home.pop("country_code", None)
        home["country"] = " Neutral "
        competition_metadata = {
            "parent": {
                "id": "sr:competition:parent",
                "name": "UTR PTT Newport Beach",
            },
            "child": {
                "id": "sr:competition:1",
                "name": "UTR PTT Newport Beach Men Singles",
                "parent_id": "sr:competition:parent",
                "type": "singles",
                "gender": "men",
            },
        }

        row = sportradar._row_from_summary(
            summary,
            competition_metadata=competition_metadata,
        )

        self.assertEqual(row["tournament_name"], "UTR PTT Newport Beach")
        self.assertEqual(row["draw_name"], "Men Singles")
        self.assertEqual(row["draw_team_type"], "Singles")
        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["loser_1_country"], "")

    def test_country_value_skips_neutral_before_real_fallback(self):
        self.assertEqual(
            sportradar._country_value(" Neutral ", "", "RUS"),
            "RUS",
        )
        self.assertEqual(
            sportradar._country_value("neutral", None, "  "),
            "",
        )

    def test_enriched_row_uses_daily_child_and_keeps_empty_suffix_fallback(self):
        summary = singles_summary()
        competition = summary["sport_event"]["sport_event_context"]["competition"]
        competition["name"] = "UTR PTT Newport Beach Men Singles"
        metadata = {
            "parent": {
                "id": "sr:competition:parent",
                "name": "UTR PTT Newport Beach",
            },
            "child": {},
        }

        row = sportradar._row_from_summary(
            summary,
            competition_metadata=metadata,
        )

        self.assertEqual(row["tournament_name"], "UTR PTT Newport Beach")
        self.assertEqual(row["draw_name"], "Men Singles")

        competition["name"] = "UTR PTT Newport Beach"
        row = sportradar._row_from_summary(
            summary,
            competition_metadata=metadata,
        )

        self.assertEqual(row["draw_name"], "2026 Newport Beach Men 11, 13-16 Playoff")
```

In `test_doubles_row_uses_embedded_players`, replace the row construction with the following metadata-backed call and add the two assertions shown:

```python
        competition_metadata = {
            "parent": {
                "id": "sr:competition:parent",
                "name": "UTR PTT Newport Beach",
            },
            "child": {
                "id": "sr:competition:1",
                "name": "UTR PTT Newport Beach Women Doubles",
                "parent_id": "sr:competition:parent",
                "type": "doubles",
                "gender": "women",
            },
        }

        row = sportradar._row_from_summary(
            summary,
            competition_metadata=competition_metadata,
        )

        self.assertEqual(row["tournament_name"], "UTR PTT Newport Beach")
        self.assertEqual(row["draw_name"], "Women Doubles")
```

- [ ] **Step 2: Run the new tests and verify the red state**

Run from `artifacts/matchminer`:

```powershell
& "..\..\.venv\Scripts\python.exe" manage.py test accounts.tests.test_sportradar.SportRadarTests.test_enriched_row_uses_parent_tournament_and_child_draw_suffix accounts.tests.test_sportradar.SportRadarTests.test_country_value_skips_neutral_before_real_fallback accounts.tests.test_sportradar.SportRadarTests.test_enriched_row_uses_daily_child_and_keeps_empty_suffix_fallback accounts.tests.test_sportradar.SportRadarTests.test_doubles_row_uses_embedded_players --keepdb
```

Expected: failure because `_country_value` and the `competition_metadata` argument do not exist yet.

- [ ] **Step 3: Add the pure normalization helpers**

Insert after `_gender_long()` in `artifacts/matchminer/accounts/live_scrapers/sportradar.py`:

```python
def _country_value(*values):
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text and text.lower() != "neutral":
            return text
    return ""


def _draw_suffix(parent_name, child_name):
    parent = (parent_name or "").strip()
    child = (child_name or "").strip()
    if not parent or not child.startswith(parent):
        return ""
    return child[len(parent):].lstrip(" \t,-:").strip()
```

- [ ] **Step 4: Use normalized country candidates**

Replace the `country` value in `_person_fields()` with:

```python
        "country": _country_value(
            person.get("country_code"),
            person.get("country"),
            fallback_competitor.get("country_code"),
            fallback_competitor.get("country"),
        ),
```

- [ ] **Step 5: Extend row mapping with optional parent/child metadata**

Change the function signature to:

```python
def _row_from_summary(
    summary,
    *,
    client=None,
    api_key="",
    dob_cache=None,
    competition_metadata=None,
):
```

Replace the existing competition/type/gender setup with:

```python
    competition = context.get("competition") or {}
    metadata = competition_metadata if isinstance(competition_metadata, dict) else {}
    parent_competition = metadata.get("parent") or {}
    child_competition = metadata.get("child") or {}
    competition_type = child_competition.get("type") or competition.get("type") or ""
    competition_gender = child_competition.get("gender") or competition.get("gender") or ""
    team_type = _team_type(competition_type)
    fallback_gender = _gender_short(competition_gender)
```

Replace the current `draw_name` assignment with:

```python
    fallback_draw_name = _first_group_name(context) or competition.get("name", "")
    parent_name = (parent_competition.get("name") or "").strip()
    child_name = (
        child_competition.get("name")
        or competition.get("name")
        or ""
    )
    draw_name = _draw_suffix(parent_name, child_name) or fallback_draw_name
    tournament_name = parent_name or competition.get("name", "")
```

Use the new variables in the returned row:

```python
        "draw_name": draw_name,
        "draw_team_type": team_type,
        "tournament_name": tournament_name,
```

Replace the `draw_gender` expression with:

```python
        "draw_gender": _gender_long(competition_gender),
```

- [ ] **Step 6: Run the SportRadar unit tests and verify green**

Run:

```powershell
& "..\..\.venv\Scripts\python.exe" manage.py test accounts.tests.test_sportradar --keepdb
```

Expected: all eight SportRadar tests pass with `OK`.

- [ ] **Step 7: Commit Task 1 only**

```powershell
git add -- "artifacts/matchminer/accounts/live_scrapers/sportradar.py" "artifacts/matchminer/accounts/tests/test_sportradar.py"
git diff --cached --check
git commit -m "Fix SportRadar competition row mapping"
```

---

### Task 2: Fetch And Cache Competition Info

**Files:**
- Modify: `artifacts/matchminer/accounts/tests/test_sportradar.py:1-37,92-250`
- Modify: `artifacts/matchminer/accounts/live_scrapers/sportradar.py:89-102,269-322,434-522`

**Interfaces:**
- Produces: `_competition_url(competition_id: str) -> str`
- Produces: `_competition_metadata(client, api_key: str, competition: dict, cache: dict) -> dict | None`
- Consumes: Task 1 `_row_from_summary(summary, *, client=None, api_key="", dob_cache=None, competition_metadata=metadata)`

- [ ] **Step 1: Add test imports and a run-level fake client**

Replace the imports at the top of `artifacts/matchminer/accounts/tests/test_sportradar.py` with:

```python
import copy
import csv
import io
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from accounts.live_scrapers import registry, sportradar
from accounts.views import validate_run_params
```

Add after `FakeClient`:

```python
class FakeRunClient:
    payload = {}
    requests = []

    def __init__(self, *, tele, **_kwargs):
        self.tele = tele

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def get(self, url, **kwargs):
        self.__class__.requests.append({"url": url, **kwargs})
        return FakeResponse(self.__class__.payload)
```

- [ ] **Step 2: Add failing cache and run-integration tests**

Add these methods to `SportRadarTests`:

```python
    def test_competition_metadata_caches_missing_and_malformed_results(self):
        without_parent = singles_summary()["sport_event"]["sport_event_context"]["competition"]
        empty_client = FakeClient([])

        self.assertIsNone(
            sportradar._competition_metadata(
                empty_client,
                "SECRET",
                without_parent,
                {},
            )
        )
        self.assertEqual(empty_client.requests, [])

        with_parent = dict(without_parent, parent_id="sr:competition:parent")

        failed_client = FakeClient([FakeResponse({}, status_code=500)])
        failed_cache = {}
        self.assertIsNone(
            sportradar._competition_metadata(
                failed_client,
                "SECRET",
                with_parent,
                failed_cache,
            )
        )
        self.assertIsNone(
            sportradar._competition_metadata(
                failed_client,
                "SECRET",
                with_parent,
                failed_cache,
            )
        )
        self.assertEqual(len(failed_client.requests), 1)

        malformed_client = FakeClient([FakeResponse({"competition": []})])
        cache = {}

        self.assertIsNone(
            sportradar._competition_metadata(
                malformed_client,
                "SECRET",
                with_parent,
                cache,
            )
        )
        self.assertIsNone(
            sportradar._competition_metadata(
                malformed_client,
                "SECRET",
                with_parent,
                cache,
            )
        )
        self.assertEqual(len(malformed_client.requests), 1)
        self.assertTrue(
            any(
                "malformed" in message.lower()
                for _level, message, _exc in malformed_client.tele.errors
            )
        )

        malformed_children_client = FakeClient(
            [
                FakeResponse(
                    {
                        "competition": {
                            "id": "sr:competition:parent",
                            "name": "UTR PTT Newport Beach",
                            "children": {"unexpected": "shape"},
                        }
                    }
                )
            ]
        )
        metadata = sportradar._competition_metadata(
            malformed_children_client,
            "SECRET",
            with_parent,
            {},
        )
        self.assertEqual(metadata["parent"]["name"], "UTR PTT Newport Beach")
        self.assertEqual(metadata["child"], {})

        mismatched_child_client = FakeClient(
            [
                FakeResponse(
                    {
                        "competition": {
                            "id": "sr:competition:parent",
                            "name": "UTR PTT Newport Beach",
                            "children": [
                                {
                                    "competition": {
                                        "id": "sr:competition:other",
                                        "name": "UTR PTT Newport Beach Women Doubles",
                                    }
                                }
                            ],
                        }
                    }
                )
            ]
        )
        metadata = sportradar._competition_metadata(
            mismatched_child_client,
            "SECRET",
            with_parent,
            {},
        )
        self.assertEqual(metadata["parent"]["name"], "UTR PTT Newport Beach")
        self.assertEqual(metadata["child"], {})

    def test_run_fetches_parent_once_and_skips_disallowed_categories(self):
        first = singles_summary()
        context = first["sport_event"]["sport_event_context"]
        context["competition"].update(
            {
                "name": "UTR PTT Newport Beach Men Singles",
                "parent_id": "sr:competition:parent",
            }
        )
        home = first["sport_event"]["competitors"][0]
        home.pop("country_code", None)
        home["country"] = "Neutral"
        for competitor in first["sport_event"]["competitors"]:
            competitor["date_of_birth"] = "2000-01-01"

        second = copy.deepcopy(first)
        second["sport_event"]["id"] = "sr:sport_event:2"
        disallowed = singles_summary("sr:category:999")

        FakeRunClient.payload = {
            "competition": {
                "id": "sr:competition:parent",
                "name": "UTR PTT Newport Beach",
                "children": [
                    {
                        "competition": {
                            "id": "sr:competition:1",
                            "name": "UTR PTT Newport Beach Men Singles",
                            "parent_id": "sr:competition:parent",
                            "type": "singles",
                            "gender": "men",
                        }
                    }
                ],
            }
        }
        FakeRunClient.requests = []
        run_obj = SimpleNamespace(
            pk=123,
            scraper=SimpleNamespace(secret_value="SECRET"),
            params={},
            date_from=date(2026, 5, 17),
            date_to=date(2026, 5, 17),
        )
        progress_qs = mock.Mock()

        with mock.patch.object(sportradar, "ScraperClient", FakeRunClient), \
            mock.patch.object(sportradar, "build_proxies", return_value=None), \
            mock.patch.object(
                sportradar,
                "_fetch_daily_summaries",
                return_value=[first, second, disallowed],
            ), \
            mock.patch.object(
                sportradar.Run.objects,
                "filter",
                return_value=progress_qs,
            ):
            items_csv, _requests_csv, _errors_csv, row_count, status = sportradar.run(
                run_obj,
                lambda *_args: None,
            )

        rows = list(csv.DictReader(io.StringIO(items_csv)))
        self.assertEqual(status, sportradar.Run.Status.SUCCESS)
        self.assertEqual(row_count, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(FakeRunClient.requests), 1)
        self.assertEqual(
            FakeRunClient.requests[0]["url"],
            sportradar._competition_url("sr:competition:parent"),
        )
        self.assertEqual(
            FakeRunClient.requests[0]["headers"]["x-api-key"],
            "SECRET",
        )
        self.assertNotIn("SECRET", FakeRunClient.requests[0]["url"])
        self.assertTrue(
            all(row["Tournament Name"] == "UTR PTT Newport Beach" for row in rows)
        )
        self.assertTrue(all(row["Draw Name"] == "Men Singles" for row in rows))
        self.assertTrue(all(row["Loser 1 Country"] == "" for row in rows))
```

- [ ] **Step 3: Run the new tests and verify the red state**

Run:

```powershell
& "..\..\.venv\Scripts\python.exe" manage.py test accounts.tests.test_sportradar.SportRadarTests.test_competition_metadata_caches_missing_and_malformed_results accounts.tests.test_sportradar.SportRadarTests.test_run_fetches_parent_once_and_skips_disallowed_categories --keepdb
```

Expected: failure because `_competition_metadata` and `_competition_url` do not exist and `run()` does not pass enrichment metadata.

- [ ] **Step 4: Add the Competition Info URL builder**

Insert after `_profile_url()` in `artifacts/matchminer/accounts/live_scrapers/sportradar.py`:

```python
def _competition_url(competition_id):
    cid = quote(competition_id or "", safe=":")
    return (
        f"{BASE_URL}/{_access_level()}/v3/{_language_code()}"
        f"/competitions/{cid}/info.json"
    )
```

- [ ] **Step 5: Add the cached Competition Info resolver**

Insert after `_fetch_json()`:

```python
def _competition_metadata(client, api_key, competition, cache):
    parent_id = (competition.get("parent_id") or "").strip()
    if not parent_id:
        return None

    if parent_id not in cache:
        data = _fetch_json(client, api_key, _competition_url(parent_id))
        parent = data.get("competition") if isinstance(data, dict) else None
        if not isinstance(parent, dict) or not (parent.get("name") or "").strip():
            if data is not None:
                client.tele.record_error(
                    f"SportRadar Competition Info payload was malformed for {parent_id}"
                )
            parent = None
        cache[parent_id] = parent

    parent = cache[parent_id]
    if parent is None:
        return None

    children = parent.get("children") or []
    if not isinstance(children, list):
        children = []
    child = None
    for entry in children:
        candidate = entry.get("competition") if isinstance(entry, dict) else None
        if isinstance(candidate, dict) and candidate.get("id") == competition.get("id"):
            child = candidate
            break
    return {"parent": parent, "child": child or {}}
```

- [ ] **Step 6: Wire enrichment into the run loop**

Initialize the cache beside `dob_cache`:

```python
    dob_cache = {}
    competition_cache = {}
```

At the start of the `for summary in summaries:` loop, before `_row_from_summary()`, add the allowed-category guard and resolver:

```python
                    context = summary.get("sport_event", {}).get("sport_event_context") or {}
                    if (context.get("category") or {}).get("id") not in ALLOWED_CATEGORY_IDS:
                        continue
                    competition_metadata = _competition_metadata(
                        client,
                        api_key,
                        context.get("competition") or {},
                        competition_cache,
                    )
```

Pass the result into row mapping:

```python
                    row = _row_from_summary(
                        summary,
                        client=client,
                        api_key=api_key,
                        dob_cache=dob_cache,
                        competition_metadata=competition_metadata,
                    )
```

- [ ] **Step 7: Run all SportRadar tests and verify green**

Run:

```powershell
& "..\..\.venv\Scripts\python.exe" manage.py test accounts.tests.test_sportradar --keepdb
```

Expected: all ten SportRadar tests pass with `OK`.

- [ ] **Step 8: Commit Task 2 only**

```powershell
git add -- "artifacts/matchminer/accounts/live_scrapers/sportradar.py" "artifacts/matchminer/accounts/tests/test_sportradar.py"
git diff --cached --check
git commit -m "Enrich SportRadar rows from competition info"
```

---

### Task 3: Full And Live Verification

**Files:**
- Verify only: `artifacts/matchminer/accounts/live_scrapers/sportradar.py`
- Verify only: `artifacts/matchminer/accounts/tests/test_sportradar.py`

**Interfaces:**
- Consumes: the completed SportRadar runner from Tasks 1 and 2.
- Produces: automated-suite evidence, one controlled live-run result, and a healthy restarted local server when one was already in use.

- [ ] **Step 1: Run the full Django suite**

Run from `artifacts/matchminer`:

```powershell
& "..\..\.venv\Scripts\python.exe" manage.py test --keepdb
```

Expected: the suite finishes with `OK`; no existing tests regress.

- [ ] **Step 2: Run one controlled live SportRadar day**

Run:

```powershell
& "..\..\.venv\Scripts\python.exe" manage.py scrape_now sportradar --date-from 2026-07-13 --date-to 2026-07-13
```

Expected: the job finishes as `success` or `partial`, produces rows, and does not expose the API key in terminal output.

- [ ] **Step 3: Validate the persisted live CSV and secret redaction**

Run:

```powershell
& "..\..\.venv\Scripts\python.exe" manage.py shell -c "import csv,io; from accounts.models import Run; r=Run.objects.filter(scraper__slug='sportradar').order_by('-started_at').first(); rows=list(csv.DictReader(io.StringIO(r.csv_data))); country_cols=['Winner 1 Country','Winner 2 Country','Loser 1 Country','Loser 2 Country']; key=(r.scraper.secret_value or '').strip(); assert rows, 'live run produced no rows'; assert all((row.get(c) or '').strip().lower() != 'neutral' for row in rows for c in country_cols), 'Neutral country remains'; assert any(row.get('Tournament Name') == 'WTA Iasi, Romania' and row.get('Draw Name') == 'Women Singles' for row in rows), 'expected parent/child mapping missing'; combined='\n'.join([r.log_text,r.requests_csv,r.errors_csv]); assert not key or key not in combined, 'API key leaked'; print({'run':r.short_id,'status':r.status,'rows':len(rows),'neutral_values':0,'mapping_verified':True,'key_redacted':True})"
```

Expected: one summary dictionary with `neutral_values: 0`, `mapping_verified: True`, and `key_redacted: True`.

- [ ] **Step 4: Restart local Waitress if this project already has a running instance**

Run from the workspace root in an elevated PowerShell session:

```powershell
$project = "C:\Users\vmadmin\Desktop\Salman_Bader_New"
$servers = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*waitress*matchminer.wsgi:application*" -and
    $_.ExecutablePath -eq "$project\.venv\Scripts\python.exe"
}
if ($servers) {
    $servers | ForEach-Object { Stop-Process -Id $_.ProcessId }
    Start-Process -FilePath "$project\.venv\Scripts\python.exe" `
        -ArgumentList @("-m", "waitress", "--listen=0.0.0.0:80", "--threads=16", "--channel-timeout=1200", "matchminer.wsgi:application") `
        -WorkingDirectory "$project\artifacts\matchminer"
}
```

Expected: if Waitress was running, exactly this project's process is replaced. If it was not running, no process is started and no restart is needed for the CLI verification.

- [ ] **Step 5: Confirm local HTTP health when Waitress was restarted**

Run:

```powershell
$response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1/" -TimeoutSec 30
if ($response.StatusCode -ne 200) { throw "Local health check failed" }
$response.StatusCode
```

Expected: `200`. Skip this step only when Step 4 found no running Waitress instance.

- [ ] **Step 6: Inspect final scope**

Run from the workspace root:

```powershell
git status --short
git diff HEAD~2..HEAD -- "artifacts/matchminer/accounts/live_scrapers/sportradar.py" "artifacts/matchminer/accounts/tests/test_sportradar.py"
```

Expected: the two implementation commits contain only the SportRadar runner and tests; unrelated pre-existing worktree changes remain unmodified.
