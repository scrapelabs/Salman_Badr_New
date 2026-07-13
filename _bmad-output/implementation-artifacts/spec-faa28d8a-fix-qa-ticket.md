---
title: 'Fix QA ticket faa28d8a SportRadar competition metadata'
type: 'bugfix-design'
created: '2026-07-13'
status: 'approved-design'
review_loop_iteration: 0
baseline_commit: '99680a071c23ab0966ed18175f3dc88472ebedc7'
context: []
---

<frozen-after-approval reason="human-owned intent - do not modify unless human renegotiates">

## Intent

**Problem:** QA ticket `faa28d8a-2cbc-4619-9f32-9d04eca9ead7` reports three SportRadar output problems: tournament names include child draw metadata, draw names do not use the child competition suffix, and player-country cells can contain `Neutral` instead of usable country data.

**Approach:** For each unique Daily Summaries `competition.parent_id`, fetch and cache SportRadar Competition Info. Use the parent competition name as `tournament_name`, match the summary competition ID to a child, and remove only the exact parent-name prefix from the child name to produce `draw_name`. Treat case-insensitive `Neutral` country values as missing and emit an empty cell when no non-neutral country candidate exists.

## Boundaries And Constraints

**Always:** Keep the change scoped to the SportRadar scraper; cache Competition Info by parent ID for one run; preserve match rows when enrichment fails; retain current score, identity, DOB, category, deduplication, and CSV-column behavior; redact the API key as today.

**Ask First:** Changing QA ticket status, changing CSV columns, adding a persistent competition cache, introducing a second country data provider, or changing run-status semantics for non-fatal enrichment failures.

**Never:** Do not infer nationality from a player's name; do not hardcode competition/player IDs from the ticket; do not emit `Neutral` as a player country; do not drop a match solely because Competition Info is unavailable.

</frozen-after-approval>

## Investigation Evidence

- Current `sportradar._row_from_summary()` writes the child `competition.name` directly to `tournament_name` and prefers the first Daily Summaries group name for `draw_name`.
- Production run `2cb930ce` emitted examples such as tournament `WTA Iasi, Romania Women Singles`, draw `2026 Iasi, Romania, Qualifying`, and player country `Neutral`.
- Daily Summaries for child `sr:competition:43367` identifies parent `sr:competition:43365`.
- Competition Info for parent `sr:competition:43365` returns parent name `WTA Iasi, Romania` and child name `WTA Iasi, Romania Women Singles`; the required draw suffix is therefore `Women Singles`.
- SportRadar returns `Neutral` for affected players in both Daily Summaries and Competitor Profile, so no authoritative actual country is available from the configured provider. The approved behavior is an empty country cell.

## Design

### Components

- Add a Competition Info URL builder alongside `_daily_url()` and `_profile_url()`.
- Add a fetch/resolve helper that accepts the Daily Summaries competition object, calls Competition Info once per unique parent ID, caches success or failure, and returns resolved parent and child metadata.
- Pass resolved competition metadata into row mapping without changing the output schema.
- Add a small country-selection rule that ignores blank values and case-insensitive `Neutral` values before choosing the first available person or competitor country code/name.

### Data Flow

1. Fetch and paginate Daily Summaries exactly as today.
2. Apply the existing allowed-category filter before enrichment so excluded events do not trigger Competition Info calls.
3. Read the summary competition's `parent_id` and resolve it through the per-run cache.
4. If parent metadata is available, set `tournament_name` to its exact name.
5. Match the summary competition ID against `competition.children[*].competition.id`.
6. Build `draw_name` by removing the exact parent name only when it is a prefix of the matched child name, then strip surrounding whitespace and leading comma, hyphen, or colon separators. For example, `ATP Challenger Turin, Italy Men Singles` becomes `Men Singles`.
7. Use the matched child's `type` and `gender` for team type and draw/player gender fallbacks. Fall back to Daily Summaries values when child metadata is absent.
8. Select player country from existing person/competitor candidates while skipping `Neutral`; emit an empty string if no usable candidate remains.
9. Continue existing row deduplication and CSV writing.

### Failure Handling

- A missing `parent_id` performs no enrichment request and preserves existing Daily Summaries naming.
- A failed or malformed Competition Info response is cached as a failed lookup, reported through existing telemetry, and does not drop the match.
- If the parent resolves but the child is absent, use the verified parent tournament name. Derive the draw suffix from the Daily Summaries child name only when that name starts with the exact parent name; otherwise preserve the existing `_first_group_name(context) or competition.name` draw fallback.
- If stripping produces an empty draw name, preserve the same existing draw fallback.
- Non-fatal enrichment failures do not change current run-status semantics.

## I/O And Edge Cases

| Scenario | Input / State | Expected Output |
|----------|---------------|-----------------|
| Parent and child resolve | Parent `ATP Challenger Turin, Italy`; child `ATP Challenger Turin, Italy Men Singles` | Tournament `ATP Challenger Turin, Italy`; draw `Men Singles` |
| Doubles child resolves | Child suffix is `Women Doubles` | Draw `Women Doubles`; team type `Doubles`; draw gender `Female` |
| Repeated parent | Many matches share one parent ID | One Competition Info request for that parent in the run |
| No parent ID | Daily Summaries competition has no `parent_id` | Existing tournament/draw naming remains |
| Parent request fails | HTTP/JSON failure | Existing naming remains; match is retained; failure is cached and telemetered |
| Parent resolves, child missing | Parent name is known; child list lacks summary ID | Parent tournament name is used; exact-prefix Daily Summaries suffix or existing draw fallback is used |
| Neutral with no fallback | Country candidates are blank or `Neutral` | Player country is empty |
| Neutral then usable fallback | Person country is `Neutral`; competitor has a real code/name | Real fallback country is emitted |

## Test Design

- Add failing-first tests in `accounts/tests/test_sportradar.py` for Competition Info URL construction and API-key header handling.
- Prove parent/child mapping yields parent tournament names and suffix-only singles/doubles draw names.
- Prove repeated summaries sharing a parent issue one Competition Info request.
- Prove missing parent, failed response, malformed children, child mismatch, and empty suffix preserve safe fallbacks without dropping rows.
- Prove `Neutral`, including mixed case and surrounding whitespace, is skipped and produces a blank only when no usable fallback exists.
- Keep existing pagination, score, DOB, category, and doubles tests green.

## Verification

1. Run `python manage.py test accounts.tests.test_sportradar --keepdb`.
2. Run `python manage.py test --keepdb`.
3. Run one controlled single-day SportRadar job after automated tests pass.
4. Inspect the resulting CSV for exact parent tournament names, suffix-only draw names, blank former-`Neutral` countries, unchanged schema, and no API-key leakage.
5. Restart the local Waitress process after Python verification if it is running, then confirm local HTTP health.

## Non-Goals

- Resolving the real nationality of players SportRadar designates as neutral.
- Persisting competition metadata between runs.
- Reworking group/round fields outside the requested draw-name mapping.
- Updating the QA ticket workflow state.
