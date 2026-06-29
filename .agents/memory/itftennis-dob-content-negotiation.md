---
name: itftennis DOB content-negotiation
description: Why all itftennis-family scrapers silently emitted blank player DOB, and the porting rule that prevents it.
---

# itftennis DOB came back blank because the Accept header was dropped in the port

The itftennis family's player DOB comes from `GetHeadToHeadPlayerDetails`, an
ASP.NET **content-negotiating** endpoint. The original reference scraper called
it via `requests.get` with an `Accept` header preferring `application/xml;q=0.9`,
so it returned an XML document and `DateOfBirth` parsed fine.

The Django port routes that lookup through the **browser in-page `fetch()`**
(`_browser.BrowserClient.get` → `_api` → `_fetch`), which sent the JS `fetch`
default `Accept: */*`. With `*/*`, ASP.NET returns **JSON**, so `etree.fromstring`
threw and `_extract_dob` (which swallows all exceptions) silently yielded `""` —
on **every** player, for **all** itftennis variants (they share `_itftennis.py`).
The drawsheet/filter/calendar calls were unaffected because they're parsed as
JSON anyway, so matches still collected — only DOB was blank.

**Why:** the same content-negotiating URL returns a different body shape based on
the request's `Accept`, and the two transports (`requests.get` vs in-page fetch)
have different default `Accept` values.

**How to apply:** when porting a scraper from `requests`/`curl_cffi` to the
browser in-page fetch, **carry the `Accept` header over explicitly** for any
endpoint whose parser assumes a specific format (XML especially). Don't rely on
the fetch default. Belt-and-suspenders: make the parser format-agnostic (try XML,
then JSON — incl. WCF DataContract `/Date(ms)/` epoch) so a silent format flip
can't blank a field again. `itf_juniors_tournament_software` is a *separate*
TournamentSoftware source that genuinely never had a DOB path — its blank DOB is
faithful, not this bug.
