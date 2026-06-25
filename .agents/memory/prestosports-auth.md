---
name: PrestoSports login vs events authorization
description: PrestoSports login succeeds but the events endpoint can 401 "Access denied" — that's account authorization, not Cloudflare; a browser can't fix it.
---

# PrestoSports: login works, events can be authorization-denied (not Cloudflare)

When PrestoSports (`gameday-api.prestosports.com`) appears blocked, distinguish
**authentication** (login) from **authorization** (per-resource permission) before
reaching for browser automation.

Observed behaviour (diagnosed live with the project's `curl_cffi` Chrome-impersonation client):

- `POST /api/auth/token` → **200**, returns a valid AWS Cognito `idToken`
  (issuer `cognito-idp.us-west-2.amazonaws.com`). **No Cloudflare anywhere** in
  the flow — no JS challenge, no `cf-mitigated`, clean JSON throughout.
- With that `Bearer <idToken>`: generic endpoints (`/api/sports`,
  `/api/organizations`) and the **season objects** (`/api/seasons/{id}`) return
  **200** — the token is decoded and the account is recognised.
- BUT `/api/seasons/{id}/events` returns a clean JSON **401**
  `"Access denied for user: <account email>"` on **every** date window
  (in-season, out-of-season, full-season). The named-user message means the API
  authenticated the token and then denied the *resource*.

**Conclusion:** this is an **account-permissions** problem — the account can log
in and read season metadata but is not provisioned to read those NAIA tennis
seasons' match data. It is NOT bot detection / Cloudflare.

**Why patchright / undetected-browser + persistent profile does NOT help here:**
there is no anti-bot challenge to bypass; the API cleanly authenticates over plain
JSON and denies at the application authorization layer. A real browser cannot grant
an account permissions it does not have — it would hit the identical 401. Only a
properly-provisioned account (events/results read access) fixes it.

**How the scraper now reports it:** `_discover_events` distinguishes a genuine
empty window (HTTP 200, `totalElements=0`) from a 401/403 and returns an
`auth_denied` flag; `run()` then fails honestly with a clear errors-CSV/log
diagnostic ("authenticated but not authorised … not a Cloudflare block") instead
of a confusing empty FAILED. The diagnostic message intentionally omits the
account email (never leak credentials/usernames into the run log).

**Security note:** test creds were once supplied in a plaintext attachment under
`attached_assets/` (untracked but NOT gitignored → would be swept into the
end-of-turn commit and auto-pushed to GitHub). Delete such files before the turn
ends and advise rotating the exposed password; store real creds as
`PRESTOSPORTS_USERNAME`/`PRESTOSPORTS_PASSWORD` secrets, never in the repo.
