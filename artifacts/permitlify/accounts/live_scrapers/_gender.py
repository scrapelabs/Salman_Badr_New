"""Derive a player gender code from a draw / competition name.

tournamentsoftware.com (Tennis Europe, Croatia) and the Brazilian CBT site name
every draw after its gender + age + format, so the draw name a match belongs to
is the one reliable gender signal in the markup — the original pipeline guessed
gender with an LLM, which the deterministic port dropped. Examples:

- ``"BS16 - Boys Singles 16 Main Draw"``        (Tennis Europe, English)
- ``"Juniorke pojedinačno"`` / ``"Dječaci parovi"``  (Croatia individual, Croatian)
- ``"Prva liga za seniorke 2024"``              (Croatia league, Croatian)
- ``"Simples Masculino"``                       (Brazil, Portuguese)

:func:`draw_gender_code` returns ``"M"`` / ``"F"`` (the per-player schema code)
or ``""`` when the name carries no unambiguous single gender (mixed doubles,
generic "Parovi"/"Doubles", or an unrecognised language).

Matching notes:
- Names are accent-stripped + lower-cased first, so Croatian tokens are written
  in their ASCII form (``dječaci`` → ``djecaci``).
- Whole-word matching (``\b``) is required: the bare token ``men`` is a
  substring of ``tournament``, and ``seniorke`` (women) shares a prefix with
  ``seniorska`` (men).
- Female is matched before male because several pairs nest as substrings
  (``women`` ⊃ ``men``, ``female`` ⊃ ``male``).
"""

import re
import unicodedata

# Mixed draws can't be assigned a single gender → "". Croatian inflects the
# adjective ("mješoviti / mješovita / mješovito parovi", "miješani"), so the
# stem is matched with any suffix (``\w*``); the rest are whole words. Accent
# stripping first maps "mješovit" → "mjesovit", "miješan" → "mijesan".
_MIXED_EXACT = ("mixed", "mixto", "mixta", "mista", "mistas")
_MIXED_STEMS = ("mjesovit", "mijesan", "mjesan")

_FEMALE_TOKENS = (
    # English
    "girls", "women", "ladies", "female",
    # Croatian
    "djevojcice", "djevojke", "juniorke", "seniorke",
    "zenski", "zenska", "zensko", "zene",
    # Portuguese / Spanish
    "feminino", "feminina", "femenino",
)
_MALE_TOKENS = (
    # English
    "boys", "men", "gentlemen", "male",
    # Croatian
    "djecaci", "juniori", "seniori", "seniorska", "seniorski", "seniorsko",
    "muski", "muska", "musko", "muskarci",
    # Portuguese / Spanish
    "masculino", "masculina",
)


def _compile(tokens):
    return re.compile(r"\b(?:" + "|".join(tokens) + r")\b")


_MIXED_RE = re.compile(
    r"\b(?:"
    + "|".join(list(_MIXED_EXACT) + [s + r"\w*" for s in _MIXED_STEMS])
    + r")\b"
)
_FEMALE_RE = _compile(_FEMALE_TOKENS)
_MALE_RE = _compile(_MALE_TOKENS)


def _normalize(name):
    """Accent-strip + lower-case so Croatian tokens match in ASCII form."""
    decomposed = unicodedata.normalize("NFKD", str(name or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower()


def draw_gender_code(name):
    """Return ``"M"`` / ``"F"`` / ``""`` for a draw / competition ``name``."""
    low = _normalize(name)
    if not low:
        return ""
    if _MIXED_RE.search(low):
        return ""
    if _FEMALE_RE.search(low):
        return "F"
    if _MALE_RE.search(low):
        return "M"
    return ""


def is_mixed_draw(name):
    """True when a draw / competition ``name`` explicitly denotes a mixed event.

    Lets callers distinguish a genuinely mixed draw (keep the draw-level gender
    blank — the two sides differ) from a name that simply carries no gender word
    (where a per-player fallback is appropriate).
    """
    low = _normalize(name)
    return bool(low and _MIXED_RE.search(low))
