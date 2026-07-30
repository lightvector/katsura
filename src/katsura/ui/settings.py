"""Application preferences, backed by QSettings so they persist between runs."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings

from .. import APP, ORG  # re-exported: UI modules import them from here
from ..engine.settings import DEFAULT_INTERVAL_CS


@dataclass
class Prefs:
    """User-visible display/behaviour preferences."""

    show_coordinates: bool = True
    show_move_numbers: bool = False
    show_last_move_marker: bool = True
    show_variation_hints: bool = True  # mark children of the current node on board
    centered_tree: bool = False        # centre the golden line; splay branches up/down
    board_color: str = "#e8b964"
    background_color: str = "#3a3a3a"
    grid_color: str = "#101010"
    label_color: str = "#101010"
    default_board_size: int = 19
    # Interactive-play rule options (replay/loading stays fully tolerant; these
    # only restrict moves the user tries to *click*).
    forbid_multi_suicide: bool = False  # forbid multi-stone self-capture on play
    forbid_simple_ko: bool = True       # forbid an immediate simple-ko recapture
    page_step: int = 10  # moves per Page Up / Page Down
    # Live-analysis display.
    show_analysis_overlay: bool = True  # draw the candidate-move overlay on the board
    show_pv_on_hover: bool = True       # hovering a candidate previews its full PV

    # Session-only toggles, excluded from persistence: 'i' flips the overlay so
    # often mid-review that restoring a stale "off" across runs is surprising —
    # move-analysis info always starts shown. (Plain class attr, not a field.)
    _SESSION_ONLY = frozenset({"show_analysis_overlay"})
    # Persisted-settings schema (plain class attrs, not fields — annotating them
    # would make them settings). Bump _SCHEMA when a stored key's *meaning* or
    # default changes in a way that a value written by an older build would
    # misrepresent, and list the affected keys under the new version: those keys
    # are ignored on load, so the new default actually takes effect, until the
    # user sets them again. Every setting is written on any save, so a default
    # nobody chose is otherwise persisted and silently outlives its meaning.
    _SCHEMA = 2
    _RESET_AT = {
        # v2: `analysis_min_label_weight` used to be the *only* threshold and
        # defaulted to 0.2%; it now hides just the numbers and defaults to 1%,
        # with `analysis_min_weight` taking over hiding the move outright.
        2: ("analysis_min_label_weight",),
    }
    # Two weight thresholds, both as a fraction of the strongest move's weight,
    # both bypassed by the engine's own top choice (which is always drawn in
    # full). They are separate because clutter comes in two grades: a move too
    # weak to be worth *reading* still tells you the search looked there.
    analysis_min_weight: float = 0.002       # below this: no circle, no numbers
    analysis_min_label_weight: float = 0.01  # below this: circle only
    # How often the engine reports analysis updates (the kata-analyze interval),
    # in centiseconds. Lower = smoother updates but more I/O.
    analysis_interval_cs: int = DEFAULT_INTERVAL_CS

    @classmethod
    def _outdated(cls, s: QSettings) -> set:
        """Field names whose stored value predates a change in its meaning."""
        try:
            stored = int(s.value("prefs/_schema", 1))
        except (TypeError, ValueError):
            stored = 1
        return {name
                for version, names in cls._RESET_AT.items() if version > stored
                for name in names}

    def load(self) -> Prefs:
        s = QSettings(ORG, APP)
        outdated = self._outdated(s)
        for f in self.__dataclass_fields__.values():
            if f.name in self._SESSION_ONLY or f.name in outdated:
                continue
            key = f"prefs/{f.name}"
            if not s.contains(key):
                continue
            cur = getattr(self, f.name)
            val = s.value(key)
            if isinstance(cur, bool):
                # QSettings may return strings like "true"/"false".
                val = str(val).lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    continue
            elif isinstance(cur, float):
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
            setattr(self, f.name, val)
        return self

    def save(self) -> None:
        s = QSettings(ORG, APP)
        for name in self.__dataclass_fields__:
            if name in self._SESSION_ONLY:
                s.remove(f"prefs/{name}")   # drop any stale persisted value
                continue
            s.setValue(f"prefs/{name}", getattr(self, name))
        s.setValue("prefs/_schema", self._SCHEMA)
        s.sync()
