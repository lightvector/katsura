"""Per-tab engine *analysis* settings: komi, rules, and search parameters.

These are deliberately distinct from the SGF's own ``KM`` / ``RU`` properties.
The engine komi is *initialised* from the SGF komi when a tab loads (or 7.0 if
the SGF doesn't specify one), then edited independently on the toolbar; it is
never unspecified and is always snapped to a value KataGo will accept (see
:func:`clamp_komi`). The rules are likewise *seeded* from the SGF ``RU`` via
:func:`sgf_rules` (a tolerant, heuristic mapping — see below) and then edited
independently. The two search parameters (``analysisWideRootNoise`` and
``playoutDoublingAdvantage``) are engine-only.

Every one of these settings feeds into the analysis cache key
(:meth:`AnalysisRequest.position_key`), so searches run under different settings
never get mixed up with each other.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Optional

KO_RULES = ("SIMPLE", "POSITIONAL", "SITUATIONAL")
SCORING_RULES = ("AREA", "TERRITORY")
TAX_RULES = ("NONE", "SEKI", "ALL")
WHITE_HANDICAP_BONUS_RULES = ("0", "N-1", "N")

# KataGo accepts komi in ±400 as of v1.17.0 (±150 before that). Sending a komi
# an older engine rejects costs nothing but a failed `komi` command (the worker
# keeps its previous value and logs it), so we offer the wider modern range.
KOMI_LIMIT = 400.0
PDA_LIMIT = 3.0
# How often a running search reports (the kata-analyze interval, centiseconds).
# The live value is Prefs.analysis_interval_cs; this is the one default every
# layer falls back to, rather than each picking its own.
DEFAULT_INTERVAL_CS = 25
# The discrete values analysisWideRootNoise steps through (0..1 is the range).
WIDE_ROOT_NOISE_STEPS = (0.0, 0.01, 0.04, 0.10, 0.25, 0.50, 1.0)


@dataclass(frozen=True)
class KataRules:
    """The full KataGo ruleset to analyse under (see ``kata-set-rules``)."""

    ko: str = "POSITIONAL"            # SIMPLE | POSITIONAL | SITUATIONAL
    scoring: str = "AREA"            # AREA | TERRITORY
    tax: str = "NONE"               # NONE | SEKI | ALL
    suicide: bool = True            # multi-stone self-capture legal?
    has_button: bool = False        # button Go (area scoring only)
    white_handicap_bonus: str = "0"  # "0" | "N-1" | "N" bonus points in handicap games
    friendly_pass_ok: bool = True   # False: capture all dead stones before passing

    def normalized(self) -> "KataRules":
        """Button only has meaning under area scoring; drop it otherwise."""
        if self.scoring != "AREA" and self.has_button:
            return replace(self, has_button=False)
        return self

    def to_dict(self) -> dict:
        r = self.normalized()
        return {
            "ko": r.ko, "scoring": r.scoring, "tax": r.tax,
            "suicide": r.suicide, "hasButton": r.has_button,
            "whiteHandicapBonus": r.white_handicap_bonus,
            "friendlyPassOk": r.friendly_pass_ok,
        }

    def to_gtp(self) -> str:
        """Compact JSON for ``kata-set-rules`` (no spaces — a single GTP token)."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


# Every ruleset we know by name. Most come straight from the kata-set-rules
# table in docs/katago_GTP_Extensions.md (keys match its shorthand strings);
# the rest are rulesets an SGF may name that KataGo has no shorthand for, so we
# spell their fields out here. Nothing sends these names to the engine — rules
# always go over the wire as the full JSON of KataRules.to_gtp() — so the two
# kinds need no separate tables: a name is a name.
PRESETS: dict[str, KataRules] = {
    "tromp-taylor": KataRules("POSITIONAL", "AREA", "NONE", True, False, "0", False),
    "chinese": KataRules("SIMPLE", "AREA", "NONE", False, False, "N", True),
    "chinese-ogs": KataRules("POSITIONAL", "AREA", "NONE", False, False, "N", True),
    "chinese-kgs": KataRules("POSITIONAL", "AREA", "NONE", False, False, "N", True),
    "japanese": KataRules("SIMPLE", "TERRITORY", "SEKI", False, False, "0", True),
    "korean": KataRules("SIMPLE", "TERRITORY", "SEKI", False, False, "0", True),
    "stone-scoring": KataRules("SIMPLE", "AREA", "ALL", False, False, "0", True),
    "aga": KataRules("SITUATIONAL", "AREA", "NONE", False, False, "N-1", True),
    "bga": KataRules("SITUATIONAL", "AREA", "NONE", False, False, "N-1", True),
    "new-zealand": KataRules("SITUATIONAL", "AREA", "NONE", True, False, "0", True),
    "aga-button": KataRules("SITUATIONAL", "AREA", "NONE", False, True, "N-1", True),
    # Ing / Goe (應氏規則), no KataGo shorthand: area scoring, self-capture
    # illegal, no white handicap bonus. Ing's "disturbing ko" provisions have no
    # exact KataGo equivalent; situational superko is the closest behaviour.
    "ing": KataRules("SITUATIONAL", "AREA", "NONE", False, False, "0", True),
}

DEFAULT_RULES = PRESETS["japanese"]

# Presets offered as one-click buttons in the rules dialog (label, key).
PRESET_BUTTONS = (
    ("Japanese", "japanese"),
    ("Chinese", "chinese"),
    ("Stone scoring", "stone-scoring"),
    ("Tromp-Taylor", "tromp-taylor"),
)


def preset_name(rules: KataRules) -> Optional[str]:
    """The shorthand name of ``rules`` if it matches a known ruleset, else ``None``.

    Several names can describe the same ruleset (korean == japanese,
    chinese-ogs == chinese-kgs); the first in ``PRESETS`` order wins.
    """
    target = rules.normalized()
    for name, r in PRESETS.items():
        if r.normalized() == target:
            return name
    return None


def ruleset(name: str) -> Optional[KataRules]:
    """Look a ruleset up by shorthand name; ``None`` if unknown."""
    return PRESETS.get(name)


# -- SGF RU -> ruleset -----------------------------------------------------
#
# `RU` is free text, so recognising it is necessarily heuristic. Values are
# normalised (lower-cased, every non-alphanumeric run collapsed to a single
# space) and then matched in three passes, most specific first:
#
#   1. the whole normalised string against _RU_ALIASES ("new zealand");
#   2. the same with the spaces squeezed out, so "NewZealand" / "TrompTaylor" /
#      "chinese_ogs" all land on the same key;
#   3. an ordered probe list: multi-word needles are looked for as substrings,
#      single words as whole tokens (so the "tt" in "Ing SST" is not a hit, and
#      the "ing" in "stone scoring" is not either). First hit wins, which is
#      why the specific entries come before the generic ones and the bare
#      area/territory fallbacks come last.
#
# Anything still unrecognised yields None and leaves the tab on DEFAULT_RULES.

_RU_ALIASES: dict[str, str] = {
    "japanese": "japanese", "japan": "japanese", "jp": "japanese",
    "jpn": "japanese", "nihon": "japanese", "nihonkiin": "japanese",
    "nihonkiin japanese": "japanese",
    "chinese": "chinese", "china": "chinese", "cn": "chinese",
    "chn": "chinese", "zhongguo": "chinese",
    "chineseogs": "chinese-ogs", "ogs": "chinese-ogs",
    "chinesekgs": "chinese-kgs", "kgs": "chinese-kgs",
    "korean": "korean", "korea": "korean", "kr": "korean", "kor": "korean",
    "hanguk": "korean",
    "aga": "aga", "agabutton": "aga-button", "buttonaga": "aga-button",
    "bga": "bga", "british": "bga",
    "newzealand": "new-zealand", "nz": "new-zealand", "nzl": "new-zealand",
    "tromptaylor": "tromp-taylor", "tromp": "tromp-taylor",
    "tt": "tromp-taylor",
    "stonescoring": "stone-scoring", "stone": "stone-scoring",
    "ing": "ing", "goe": "ing", "ingsst": "ing", "inggoe": "ing",
    "ing sst": "ing",
}

_RU_PROBES: tuple[tuple[str, str], ...] = (
    # Multi-word / compound names before the plain ones they contain.
    ("aga button", "aga-button"),
    ("button aga", "aga-button"),
    ("new zealand", "new-zealand"),
    ("tromp taylor", "tromp-taylor"),
    ("stone scoring", "stone-scoring"),
    ("chinese ogs", "chinese-ogs"),
    ("chinese kgs", "chinese-kgs"),
    ("nihon kiin", "japanese"),
    ("ing sst", "ing"),
    # Single tokens.
    ("japanese", "japanese"), ("japan", "japanese"), ("nihon", "japanese"),
    ("jp", "japanese"), ("jpn", "japanese"),
    ("chinese", "chinese"), ("china", "chinese"), ("zhongguo", "chinese"),
    ("cn", "chinese"), ("chn", "chinese"),
    ("korean", "korean"), ("korea", "korean"), ("hanguk", "korean"),
    ("kr", "korean"), ("kor", "korean"),
    ("aga", "aga"), ("bga", "bga"), ("british", "bga"),
    ("nz", "new-zealand"), ("nzl", "new-zealand"),
    ("tromp", "tromp-taylor"), ("tt", "tromp-taylor"),
    ("ing", "ing"), ("goe", "ing"),
    ("ogs", "chinese-ogs"), ("kgs", "chinese-kgs"),
    # Last resort: the scoring style alone still tells us the most important bit.
    ("area", "chinese"), ("territory", "japanese"),
)


def _normalize_ru(ru: str) -> tuple[str, list[str]]:
    """``(normalised phrase, tokens)`` for an SGF ``RU`` value."""
    flat = re.sub(r"[^a-z0-9]+", " ", str(ru).lower()).strip()
    return flat, flat.split()


def sgf_rules_name(ru: str) -> Optional[str]:
    """The shorthand ruleset name an SGF ``RU`` value names, or ``None``.

    Tolerant of case, punctuation, abbreviations and trailing junk (e.g.
    ``"Japanese"``, ``"JP"``, ``"AGA (Area)"``, ``"NewZealand"``, ``"Ing SST"``,
    ``"Chinese;Komi:7.5"``).
    """
    if not ru:
        return None
    flat, tokens = _normalize_ru(ru)
    if not tokens:
        return None
    for candidate in (flat, "".join(tokens)):
        hit = _RU_ALIASES.get(candidate)
        if hit is not None:
            return hit
    token_set = set(tokens)
    for needle, name in _RU_PROBES:
        if " " in needle:
            if needle in flat:
                return name
        elif needle in token_set:
            return name
    return None


def sgf_rules(ru: str) -> Optional[KataRules]:
    """The :class:`KataRules` an SGF ``RU`` value names, or ``None`` if unknown."""
    name = sgf_rules_name(ru)
    return ruleset(name) if name else None


def clamp_komi(value: float) -> float:
    """Snap ``value`` to the nearest komi KataGo will accept.

    KataGo requires an integer or half-integer komi inside ``±KOMI_LIMIT``, so
    every path into the setting (SGF ``KM``, the toolbar spin box, a typed
    entry) rounds to the nearest 0.5 and clamps. Non-numeric or non-finite
    input (``KM[nan]``, ``KM[1e999]``) degrades to 0 rather than propagating a
    value that would poison the request and the cache key.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    v = round(v * 2.0) / 2.0
    return max(-KOMI_LIMIT, min(KOMI_LIMIT, v)) + 0.0   # normalise -0.0 -> 0.0


def step_wide_root_noise(value: float, up: bool) -> float:
    """The next/previous discrete ``analysisWideRootNoise`` value."""
    steps = WIDE_ROOT_NOISE_STEPS
    if up:
        for s in steps:
            if s > value + 1e-9:
                return s
        return steps[-1]
    for s in reversed(steps):
        if s < value - 1e-9:
            return s
    return steps[0]


def step_pda(value: float, up: bool) -> float:
    """The next/previous *clean multiple of 0.5* for playoutDoublingAdvantage.

    Stepping snaps to the adjacent half-integer rather than merely adding 0.5,
    so an off-grid value rounds onto the grid in the chosen direction.
    """
    q = value / 0.5
    nxt = (math.floor(q + 1e-9) + 1) if up else (math.ceil(q - 1e-9) - 1)
    return max(-PDA_LIMIT, min(PDA_LIMIT, nxt * 0.5))


@dataclass(frozen=True)
class AnalysisSettings:
    """The engine analysis settings for one tab (all feed the cache key)."""

    komi: float = 7.0
    rules: KataRules = field(default_factory=lambda: DEFAULT_RULES)
    wide_root_noise: float = 0.04
    playout_doubling_advantage: float = 0.0
