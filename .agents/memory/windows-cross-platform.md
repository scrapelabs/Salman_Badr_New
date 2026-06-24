---
name: Windows + POSIX cross-platform constraints
description: This Django app runs on Replit (Linux) AND is run locally on Windows; rules to keep both working.
---

# Windows / POSIX cross-platform constraints (permitlify / MatchMiner)

This app runs on **two** targets: Replit (Linux) and the user's **local Windows + Python 3.10** box (the `bat_files/` flow). Code that is correct on Linux can hard-crash on Windows. Two classes of bug to never reintroduce:

## 1. Never let a console echo crash the worker

The `run_scrape` worker is launched as a subprocess with `stdout/stderr = DEVNULL`. On Windows that device (`nul`) is opened with the **legacy cp1252** codec, so `print()`-ing a log line that contains emoji (🚀 ❌ 🎾 …) raises `UnicodeEncodeError`.

**Why it was catastrophic:** `_RunLogger.__call__` writes the `RunLogLine` DB row *first*, then `print()`s. So the DB line appears, then the print raises — and when this happens inside the crash `except`, it kills the handler before it can log the traceback or save `FAILED`, leaving the run stuck `RUNNING` with no visible error.

**Rule:** the DB `RunLogLine` is the source of truth; the `print()` is only a dev echo. Always guard it (`try/except (UnicodeEncodeError, OSError, ValueError): pass`). Don't rely on it for anything. Make the first crash log line carry the real exception summary so the error is visible even if the live console stops polling at `done`.

## 2. Process launch/kill must branch on `os.name`

`os.killpg`, `signal.SIGKILL`, `os.setsid`/`start_new_session=True`, and `os.waitpid` are **POSIX-only** — they raise `AttributeError` / don't exist on Windows. The Stop button died with `module 'os' has no attribute 'killpg'`.

**Rule:** gate process-group handling behind `IS_WINDOWS = os.name == "nt"`.
- Launch: Windows → `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` (only reference this attr *inside* the Windows branch — it doesn't exist on POSIX), POSIX → `start_new_session=True`.
- Kill: Windows → `taskkill /F /T /PID <pid>` (treat returncode 0 and 128/"not found" as success; add a `timeout=` since it runs inside the Stop request), POSIX → `os.killpg`/`os.kill`/`os.waitpid` unchanged.

**How to apply:** any new use of `os`/`signal`/`subprocess` process primitives, or any new emoji-bearing worker log, must be checked against both targets before shipping.
