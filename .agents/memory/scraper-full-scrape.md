---
name: Stadion scraper full-season scrape
description: The ITF/Stadion team scraper must collect a whole season; never cap the per-run tie count.
---

# Stadion / ITF team-competition scraper: collect the whole season

**Rule:** never cap how many ties a run processes. A full season is hundreds of
ties → hundreds of item rows. An artificial per-run cap once truncated a full
season down to a handful of rows, which looked to the user like a "Replit server
limit" but was just the cap.

**Why:** the original production spider was concurrent and uncapped. A capped or
purely-sequential port either under-collects or runs long enough to risk the
stale-run reaper killing it mid-flight. Fetching ties concurrently restores the
full result and keeps a run short.

**How to apply:** if a run returns far fewer rows than expected, suspect a cap or
a discovery/parse bug — not the environment. Any concurrency here needs
thread-safe shared state (telemetry, the run logger, the CSV writer/counters);
the run logger's sequence number must stay monotonic/unique or the live-console
poller skips lines.
