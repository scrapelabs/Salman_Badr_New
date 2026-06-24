---
name: MatchMiner run-worker process lifecycle
description: Non-obvious facts about the detached scrape-worker subprocess — killing/reaping it, the PID-clobber rule, and the OOM-kill symptom — when working on the Stop button, the stale-run reaper, or debugging stuck runs.
---

# MatchMiner scrape-worker subprocess lifecycle

The scrape worker (`manage.py run_scrape <uuid>`) runs as a **detached** subprocess
(`subprocess.Popen(..., start_new_session=True)`), so it's its own process-group
leader and survives the launching request/process.

## Killing & reaping (Stop button, reaper)
- Kill the whole group with `os.killpg(pid, SIGKILL)` (not `os.kill`), since the
  worker fans out a `ThreadPoolExecutor` — but threads share the PID, so the group
  kill is mainly future-proofing.
- A SIGKILLed child becomes a **zombie** until its parent reaps it. `os.kill(pid, 0)`
  (and `/proc/<pid>/stat`) report a zombie as **alive** — to verify death, reap first.
- Reaping needs a **short settle delay** (~0.2s) after the kill before
  `os.waitpid(pid, WNOHANG)` can collect it; reaping immediately returns `(0,0)`.
- **Across gunicorn workers you cannot reap another worker's child** (`waitpid` →
  `ChildProcessError`/ECHILD). Treat that as best-effort; the owning process reaps
  it via `subprocess._cleanup` on its next `Popen`. Dev `runserver` is single-process
  so reaping works there.
- Treat `ProcessLookupError` from the kill as **success** ("already dead"), not
  failure — otherwise you conflate "already gone" with "couldn't kill" and may
  wrongly leave a run RUNNING.

## PID-clobber rule (critical)
**Why:** the worker loads the `Run` row at startup; if its final `run.save()` is an
unrestricted full save, it writes back every in-memory field — clobbering `pid` (and
potentially undoing a STOPPED/FAILED status set concurrently by the Stop view or
reaper).
**How to apply:** the worker must (1) set `run.pid = os.getpid()` in its first save,
and (2) make its **final** save use explicit `update_fields` that **exclude
`pid`/`started_at`**. Any new cross-process write to a `Run` must respect this — never
add a bare `run.save()` to the worker's finish path.

## Debugging heuristic: stuck RUNNING + empty log
A run stuck `status=RUNNING` with **0 `RunLogLine`s and its worker PID gone** =
the worker was **SIGKILLed externally**, almost always the **OOM killer** (each scrape
holds a full season in memory via a `ThreadPoolExecutor`; running several 478-row
scrapes back-to-back triggers it). It is **not** a code bug — a clean failure writes
`status=FAILED` + a traceback log line. The 20-min stale-run reaper cleans it up.
Don't chase a phantom code bug; avoid hammering many concurrent full scrapes.
