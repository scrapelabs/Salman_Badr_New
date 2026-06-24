---
name: GitHub auto-push workflow
description: Keep scrapelabs/Salman_Badr_New in sync with the repl automatically, given commit timing.
---

The user wants all code pushed to GitHub automatically with no asking (also recorded in replit.md → User preferences). Repo: `scrapelabs/Salman_Badr_New`, branch `main`.

**Why the lag exists:** the agent cannot run `git commit` (it's a blocked/destructive command → would need a Project Task). The platform auto-creates a checkpoint commit only at end-of-turn ("Loop ended"). So the current turn's edits stay uncommitted while the turn runs and cannot be pushed until a later turn.

**How to apply:** each turn, push the catch-up — fetch remote `main`, confirm the remote SHA is an ancestor of local HEAD (fast-forward), then push `HEAD:main`. That sends the prior turn's checkpoint. The current turn's work lands on the next push. The one-turn lag is expected, not a bug.

**Auth:** use the `github` connector token: `(await listConnections('github'))[0].settings.access_token`, inside code_execution via `child_process`. Push with the token embedded in the remote URL as a single `execFileSync` arg (`https://x-access-token:<token>@github.com/...`), and scrub the token from any printed output. Never persist the token to `.git/config`. The inline `-c 'credential.helper=!f(){...}'` form fails git's `-c` parser — do not use it.

**Never force-push.** Fast-forward only. If histories diverge or local is behind remote, stop and ask the user.
