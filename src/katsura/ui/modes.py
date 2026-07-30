"""Editing modes (tools) and their shift/ctrl modifier behaviours.

Each tool's four modifier slots (plain / shift / ctrl / shift+ctrl) are described
here so the main window can build tooltips and the board view can preview the
right thing. See ``docs/MODEL.md`` for the editing semantics.
"""

from __future__ import annotations

from enum import Enum

from ..model.markup import MarkType


class EditMode(Enum):
    PLAY = "play"               # alternating play
    PLAY_STONE = "play_stone"   # play a fixed colour (black; shift = white)
    SETUP = "setup"             # setup stones (black; shift = white; toggles)
    MARK_TRIANGLE = "mark_triangle"
    MARK_SQUARE = "mark_square"
    MARK_CIRCLE = "mark_circle"
    MARK_CROSS = "mark_cross"
    LABEL = "label"
    SELECT = "select"           # select board contents to cut/copy/move/paste


MODE_TO_MARK = {
    EditMode.MARK_TRIANGLE: MarkType.TRIANGLE,
    EditMode.MARK_SQUARE: MarkType.SQUARE,
    EditMode.MARK_CIRCLE: MarkType.CIRCLE,
    EditMode.MARK_CROSS: MarkType.CROSS,
}

# Toolbar labels and per-modifier help, shown in tooltips.
MODE_LABELS: dict[EditMode, str] = {
    EditMode.PLAY: "Play",
    EditMode.PLAY_STONE: "Play Stone",
    EditMode.SETUP: "Setup",
    EditMode.MARK_TRIANGLE: "Triangle",
    EditMode.MARK_SQUARE: "Square",
    EditMode.MARK_CIRCLE: "Circle",
    EditMode.MARK_CROSS: "Cross",
    EditMode.LABEL: "Label",
    EditMode.SELECT: "Select",
}

# The bare number keys '1'-'9' select tools, in the order the tools appear on
# the toolbar and in the Tools menu (which is also EditMode's own order).
MODE_KEY_ORDER: tuple[EditMode, ...] = (
    EditMode.PLAY, EditMode.PLAY_STONE, EditMode.SETUP,
    EditMode.MARK_TRIANGLE, EditMode.MARK_SQUARE, EditMode.MARK_CIRCLE,
    EditMode.MARK_CROSS, EditMode.LABEL, EditMode.SELECT,
)
MODE_HOTKEY: dict[EditMode, int] = {
    mode: i for i, mode in enumerate(MODE_KEY_ORDER, 1)
}

# Raw-NN symmetries in hotkey order: Shift+1…Shift+7 pick symmetries 1-7 and
# Shift+8 picks symmetry 0, so the Engine ▸ Raw NN View menu reads 1…8 down its
# shortcut column. The single source of truth for both the key dispatch
# (EditorTab.handle_key) and that menu.
RAW_NN_KEY_ORDER: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 0)

# Compact glyphs for the mark tools' toolbar buttons (set as QAction.iconText,
# which toolbar buttons display; menus keep the full MODE_LABELS word).
MODE_GLYPHS: dict[EditMode, str] = {
    EditMode.MARK_TRIANGLE: "△",
    EditMode.MARK_SQUARE: "□",
    EditMode.MARK_CIRCLE: "○",
    EditMode.MARK_CROSS: "✕",
}

MODE_HELP: dict[EditMode, list[str]] = {
    EditMode.PLAY: [
        "Click: play the side to move",
        "Shift+Click: pass",
        "Ctrl+Click: flip who plays next (GUI only, no SGF change)",
        "Shift+Ctrl+Click: set player-to-move (PL) in the SGF",
    ],
    EditMode.PLAY_STONE: [
        "Click: play a black stone",
        "Shift+Click: play a white stone",
    ],
    EditMode.SETUP: [
        "Click: add black (click black again to erase)",
        "Shift+Click: add white (click white again to erase)",
        "Ctrl+Click: same, but always record the edit (even if redundant)",
        "Shift+Ctrl+Click: white, always recorded",
    ],
    EditMode.MARK_TRIANGLE: ["Click: toggle a triangle"],
    EditMode.MARK_SQUARE: ["Click: toggle a square"],
    EditMode.MARK_CIRCLE: ["Click: toggle a circle"],
    EditMode.MARK_CROSS: ["Click: toggle a cross (X)"],
    EditMode.LABEL: [
        "Click: add next letter label (A, B, … Z, AA, …); click to remove",
        "Shift+Click: add next number label (1, 2, 3, …)",
        "Ctrl+Click: type a custom label",
    ],
    EditMode.SELECT: [
        "Click / drag: select a spot or rectangle (stones, marks, labels)",
        "Shift: add to selection · Ctrl: subtract",
        "Drag a selected area (no modifier): move the contents",
        "Ctrl+X cut · Ctrl+C copy → click to paste (r rotate, f flip, Esc cancel)",
    ],
}
