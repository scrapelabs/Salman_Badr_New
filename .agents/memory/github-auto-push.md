---
name: GitHub auto-sync workflow (ongoing PR)
description: Keep scrapelabs/Salman_Badr_New in sync with the repl automatically via one ongoing pull request, given commit timing.
---

The user wants code synced to GitHub automatically with no asking (also in replit.md → User preferences). Repo: `scrapelabs/Salman_Badr_New`. Long-lived working branch: `agent/updates`. Base: `main`.

**Chosen workflow — ONE ongoing pull request (not direct-to-main):**
- Do **NOT** push directly to `main`. Push end-of-turn checkpoints to branch `agent/updates`, and keep a single open PR `agent/updates` → `main`. The user reviews and merges; `main` only advances when they merge.
- Each turn: fetch remote; push local `HEAD` to `agent/updates` (fast-forward/normal only, never force). If the PR doesn't exist yet, create it via the GitHub REST API (`POST /repos/{owner}/{repo}/pulls`). If it already exists, the push updates it automatically — don't open a second one.
- A PR can only be created once the branch is *ahead* of `main` (GitHub rejects a zero-diff PR). When everything is already merged, just ensure the branch exists; the PR opens on the next turn that has a commit ahead.

**Why the one-turn lag exists:** the agent cannot run `git commit` (blocked/destructive → would need a Project Task). The platform auto-creates a checkpoint commit only at end-of-turn ("Loop ended"). So the current turn's edits stay uncommitted while the turn runs and reach the branch/PR on a later turn. The lag is expected, not a bug.

**Auth:** use the `github` connector token: `(await listConnections('github'))[0].settings.access_token`, inside code_execution via `child_process`. For git push, embed the token in the remote URL as a single `execFileSync` arg (`https://x-access-token:<token>@github.com/...`). For REST calls, send header `Authorization: Bearer <token>`. Scrub the token from any printed output; never persist it to `.git/config`. The inline `-c 'credential.helper=!f(){...}'` form fails git's `-c` parser — do not use it.

**Never force-push.** Fast-forward/normal only. If histories diverge or local is behind remote, stop and ask the user.
