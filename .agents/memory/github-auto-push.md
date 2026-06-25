---
name: GitHub auto-sync workflow (direct to main)
description: Keep scrapelabs/Salman_Badr_New in sync with the repl automatically by pushing checkpoints straight to main, given commit timing.
---

The user wants code synced to GitHub automatically with no asking (also in replit.md → User preferences). Repo: `scrapelabs/Salman_Badr_New`. Branch: `main`.

**Chosen workflow — push directly to `main`:** each turn, push the latest end-of-turn checkpoint commit straight to `origin/main`. Fast-forward/normal push only, never force-push. (The user previously trialed an ongoing-PR-into-main flow and explicitly switched back to direct-to-main.)

**Why the one-turn lag exists:** the agent cannot run `git commit` (blocked/destructive → would need a Project Task). The platform auto-creates a checkpoint commit only at end-of-turn ("Loop ended"). So the current turn's edits stay uncommitted while the turn runs and reach `main` on a later turn. The lag is expected, not a bug.

**Auth:** use the `github` connector token: `(await listConnections('github'))[0].settings.access_token`, inside code_execution via `child_process`. Embed the token in the remote URL as a single `execFileSync` arg (`https://x-access-token:<token>@github.com/...`). Scrub the token from any printed output; never persist it to `.git/config`. The inline `-c 'credential.helper=!f(){...}'` form fails git's `-c` parser — do not use it.

**Never force-push.** Fast-forward/normal only. If histories diverge or local is behind remote, stop and ask the user.

**Replit Git pane is connected but does NOT auto-push:** the user connected the workspace Git pane to GitHub, so a `subrepl-*` remote now points at `https://github.com/scrapelabs/Salman_Badr_New`. Despite this, Replit does **not** auto-push checkpoints to it (confirmed: a fresh checkpoint sat locally while GitHub main was still on the prior commit until the agent pushed). The native pane only pushes on a manual "Push updates" click. **Keep doing the per-turn API FF push** — do not assume the native connection handles syncing. Don't rely on the persisted remote name either; resolve the URL fresh and embed the connector token, since the `subrepl-*` remote URL has no auth.
