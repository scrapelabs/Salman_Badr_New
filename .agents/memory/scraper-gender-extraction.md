---
name: Gender extraction from per-match draw/competition name
description: How draw-name-driven scrapers derive player gender + the brazil datacenter-block validation gap.
---

Several scrapers carry NO per-player gender field upstream; gender is inferred from the
per-match **draw / competition name** (the round/event label on each match), not the player profile.

**Convention (two fields, two encodings):** per-player slots use the single-letter code
`winner_*_gender`/`loser_*_gender` = `"M"`/`"F"`/`""`; the row-level `draw_gender` label is the
spelled-out `"Male"`/`"Female"`/`""`. Keep them consistent (derive the label from the code).

**Shared helper** `accounts/live_scrapers/_gender.py::draw_gender_code(name)`:
- strips accents (NFKD) and matches gender tokens with **word boundaries** — required so `men`
  does not match inside `tournament`, etc.
- checks **FEMALE tokens before MALE**, because the female words are supersets of the male ones
  (`women`⊃`men`, `female`⊃`male`, Croatian `seniorke`⊃`senior`/`seniori`). Reverse the order and
  every women's draw is mislabeled male.
- tokens span EN (boys/girls/men/women), HR (Dječaci/Juniori/seniori/seniorska→M; Djevojčice/
  Juniorke/seniorke / "za seniorke"→F), PT/ES (masculino/feminino). Mixed/unknown → `""` (so
  mixed-doubles draws are correctly left blank, not force-tagged).

Assign the code to **every PRESENT player slot only** (skip absent doubles partners).

**Tennis Europe DOB:** not on the profile head — it lives on the Biography tab
(`<profile_url>/biography`, "Year of birth") → `1/1/YYYY`. Fetched only as a fallback when the
head DOB is empty (one extra request per such profile).

**Brazil validation gap:** `brazil_results` discovery upstream (`tenisintegrado.com.br`) can
**403-block datacenter IPs** (same family as the ITF/Stadion CloudFront block), so live discovery
returns **0 tournaments from Replit** — that is an infra block, NOT a code bug. When blocked,
validate the brazil gender logic by code inspection + unit test of the `"Male"→"M"`/`"Female"→"F"`
mapping; re-run live only with a residential proxy / allowed IP.
