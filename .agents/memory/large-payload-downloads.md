---
name: Serving large Run CSV/log downloads
description: Why big Run payload columns must be streamed (and column-only fetched), not served via a plain whole-Run HttpResponse.
---

A `Run`'s payload columns (`csv_data`, `requests_csv`, `errors_csv`, `log_text`) are single Postgres `TextField`s and can be **100 MB+** for big jobs (the queue-driven `south_africa` run hit ~230k rows / 112 MB). Two compounding mistakes made those downloads fail (recurring failure on the user's self-hosted Waitress box talking to a REMOTE/networked Postgres):

1. `get_object_or_404(Run, ...)` loads the **whole Run** — pulling `csv_data` + `log_text` + `requests_csv` + `errors_csv` all at once over the network just to serve one of them.
2. `HttpResponse(body)` **buffers the entire body in memory twice** (str + encoded bytes) and the WSGI server can't send a single byte until it's fully built → blows the channel/inactivity timeout (and risks the mid-request connection drop) on a slow/remote link.

**Convention to keep:** serve any large Run payload via
- `Run.objects.only("started_at", <field>)` (fetch ONLY the timestamp for the filename + the one needed column), and
- `StreamingHttpResponse` over a chunk generator (`_iter_text_chunks`, 256 KB utf-8 slices).

**Why the streaming-DB-retry interaction is safe:** fetch the column value **inside the view** (so the DB read is wrapped by `DBReconnectMiddleware`'s GET-retry), then hand the already-in-memory string to the generator. The generator does NO DB access during iteration — middleware can't catch generator-time DB errors, so never lazy-load the column inside the generator.

**Other notes:**
- No `GZipMiddleware` is installed, so streaming responses aren't buffered/compressed by middleware; BlockProbes is path-only, WhiteNoise is static-only — none interfere.
- Skipping `Content-Length` (chunked transfer) is fine; computing it would require a full extra encode.
- Windows/Waitress prod raises `--channel-timeout` (default 120s) as insurance for slow large transfers; Replit dev uses `runserver` (no such limit).
- **Still not zero-copy:** the chosen column is materialized in Python once (fine ≤ a few hundred MB). The college export also fully materializes (`list(...)` + `to_csv`) before streaming. **If payloads grow far beyond ~100 MB, the real fix is to write run artifacts to files/object storage and serve via `FileResponse`/X-Sendfile**, not a bigger TextField.
