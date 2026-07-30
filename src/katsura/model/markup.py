"""Constants describing SGF markup so the model and GUI agree on names."""

from __future__ import annotations

from enum import Enum


class MarkType(str, Enum):
    """Point markers that occupy a single point (one of each per point)."""

    CIRCLE = "CR"
    SQUARE = "SQ"
    TRIANGLE = "TR"
    CROSS = "MA"  # 'X'
    SELECTED = "SL"


# Marker properties whose value is a point list.
MARK_PROPS = [m.value for m in MarkType]

# The label property (point ':' simpletext).
LABEL_PROP = "LB"

# Move properties.
BLACK_MOVE = "B"
WHITE_MOVE = "W"

# Setup properties.
ADD_BLACK = "AB"
ADD_WHITE = "AW"
ADD_EMPTY = "AE"

# Player-to-move.
PLAYER = "PL"

# The comment property.
COMMENT = "C"

# Node name.
NODE_NAME = "N"


def index_to_letters(n: int) -> str:
    """Spreadsheet-style label for index ``n`` (0->A, 25->Z, 26->AA, ...)."""
    s = ""
    n += 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def next_letter_label(used: set[str]) -> str:
    """The first letter label (A, B, ... Z, AA, ...) not already used."""
    i = 0
    while True:
        label = index_to_letters(i)
        if label not in used:
            return label
        i += 1


def next_number_label(used: set[str]) -> str:
    """The first positive-integer label (as text) not already used."""
    i = 1
    while str(i) in used:
        i += 1
    return str(i)
