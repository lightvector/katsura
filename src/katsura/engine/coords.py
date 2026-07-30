"""GTP vertex <-> board :class:`Point` conversion.

GTP names a point by a column letter (``A``..``Z`` skipping ``I``) and a row
number with ``1`` at the *bottom*. Our :class:`Point` uses ``x`` = column
(0-based) and ``y`` = row (0-based from the *top*), so for a board of height
``H`` the GTP row number is ``H - y``. This matches exactly what the board
widget draws for its coordinate labels.
"""

from __future__ import annotations

from typing import Optional

from ..sgf.coords import Point

# Western column letters, skipping 'I' — identical to the board view's labels
# and to GTP/KataGo's own column naming.
COL_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"
_COL_INDEX = {ch: i for i, ch in enumerate(COL_LETTERS)}


def column_label(x: int) -> str:
    """Display letter for column ``x``; ``"?"`` past GTP's 25 named columns.

    Boards may be up to 52 wide (SGF's limit), which GTP cannot name, so the
    *display* helpers degrade instead of raising the way :func:`point_to_vertex`
    must.
    """
    return COL_LETTERS[x] if 0 <= x < len(COL_LETTERS) else "?"


def point_label(p: Point, height: int) -> str:
    """A point named the way the board's coordinate labels read it (``D4``)."""
    return f"{column_label(p.x)}{height - p.y}"


def point_to_vertex(p: Optional[Point], height: int) -> str:
    """Encode a :class:`Point` (or ``None`` = pass) as a GTP vertex string."""
    if p is None:
        return "pass"
    if not (0 <= p.x < len(COL_LETTERS)):
        raise ValueError(f"column {p.x} is out of GTP's representable range")
    return f"{COL_LETTERS[p.x]}{height - p.y}"


def vertex_to_point(vertex: str, height: int) -> Optional[Point]:
    """Decode a GTP vertex string. ``pass``/``resign`` (any case) return ``None``."""
    v = vertex.strip().upper()
    if v in ("PASS", "RESIGN", ""):
        return None
    letter, num = v[0], v[1:]
    try:
        x = _COL_INDEX[letter]
        row = int(num)
    except (KeyError, ValueError):
        raise ValueError(f"invalid GTP vertex {vertex!r}") from None
    # Reject rows off the board rather than returning a negative y that would
    # index the flat board grid from the wrong end.
    if not 1 <= row <= height:
        raise ValueError(f"GTP vertex {vertex!r} is off a board of height {height}")
    return Point(x, height - row)
