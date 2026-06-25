---
name: Replit container resource stats
description: How to read container-accurate CPU/memory/disk inside a Replit container (psutil pitfalls).
---

# Reading container resources on Replit

When building any real-time system-monitor / resource gauge inside a Replit container, plain psutil readings are misleading. Use these instead:

- **Memory:** prefer cgroup v2 — read `/sys/fs/cgroup/memory.max` (the limit; literal `max` means unlimited) and `/sys/fs/cgroup/memory.current` (usage). `psutil.virtual_memory()` reports the **host** total (e.g. tens of GB), not the container's limit (~16 GiB), so it overstates capacity and understates % used. Fall back to psutil only when the cgroup files are absent.
- **Disk:** `psutil.disk_usage('/')` returns **0** in the container. Use `psutil.disk_usage(str(settings.BASE_DIR))` (the workspace mount, `/home/runner/workspace`) for a real reading.
- **CPU:** `psutil.cpu_percent(interval=None)` is non-blocking but the **first** sample after process start reads `0.0`. Prime it once at import, and on a 0.0 reading take a short blocking sample (`interval=0.1`) so the first gauge isn't stuck at zero.

**Why:** these are container/cgroup realities, not bugs — discovered by experimentation (disk `/`=0, psutil memory = host total). 

**How to apply:** any new system-stats/telemetry code in a Replit container should follow the cgroup-v2-first / BASE_DIR-for-disk / prime-CPU pattern (see `accounts/system_stats.py` in the MatchMiner/permitlify artifact). Always degrade gracefully (wrap in try/except, return zeros) so a missing file never 500s the page.
