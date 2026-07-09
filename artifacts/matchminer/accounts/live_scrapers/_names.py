"""Shared player-name formatting for the live scrapers.

Canonical output is ``"Lastname, Firstname"`` — the format the production
pipeline produced (originally via a Claude name formatter that this
deterministic port replaces). Sources that expose a player as a single
``"Firstname Lastname"`` string (e.g. the tournamentsoftware.com draws) must
pass it through :func:`last_first` so every scraper emits one consistent
ordering.

The transform is deterministic: a name that already contains a comma is assumed
to already be in ``"Last, First"`` form and is returned with only its spacing
normalised (never re-reversed); otherwise the final whitespace-delimited token
is treated as the surname and moved to the front. Single-token and empty names
are returned unchanged. Compound / particle surnames (van/de, Hispanic double
surnames) are not specially handled — matching the existing
brazil/uruguay/padelfip/usta heuristics.
"""

import re

_RE_WS = re.compile(r"\s+")


def last_first(raw):
    """Return ``raw`` reordered as ``"Lastname, Firstname"`` (see module doc)."""
    if not raw:
        return ""
    name = _RE_WS.sub(" ", str(raw).replace("\u00a0", " ")).strip()
    if not name:
        return ""
    if "," in name:
        last, _, first = name.partition(",")
        last, first = last.strip(), first.strip()
        return f"{last}, {first}" if first else last
    parts = name.split(" ")
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-1]}, {' '.join(parts[:-1])}"
