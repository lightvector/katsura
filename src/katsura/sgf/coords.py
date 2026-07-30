"""SGF coordinate handling.

SGF points are written as two letters: column first, then row, both using the
alphabet a..z then A..Z (so up to 52 columns/rows are representable). ``aa`` is
the top-left corner. A ``move`` value may also be empty (``B[]``) to denote a
pass.

Historically, on boards of size <= 19 the value ``tt`` (column 19, row 19,
0-indexed) was used to denote a pass, because it lies off such boards. We honour
that on *read* (see :func:`sgf_to_move`) but always *write* passes as ``[]``.
"""

from __future__ import annotations

from typing import NamedTuple
from collections.abc import Iterable

# The SGF coordinate alphabet: a..z (0..25) then A..Z (26..51).
_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LETTER_TO_INDEX = {ch: i for i, ch in enumerate(_LETTERS)}
MAX_COORD = len(_LETTERS)  # 52


class Point(NamedTuple):
    """A board position, 0-indexed. ``x`` is the column, ``y`` is the row."""

    x: int
    y: int


class SgfCoordError(ValueError):
    """Raised when an SGF coordinate token cannot be parsed."""


def _index_to_letter(i: int) -> str:
    if not 0 <= i < MAX_COORD:
        raise SgfCoordError(f"coordinate index {i} out of representable range 0..{MAX_COORD - 1}")
    return _LETTERS[i]


def _letter_to_index(ch: str) -> int:
    try:
        return _LETTER_TO_INDEX[ch]
    except KeyError:
        raise SgfCoordError(f"invalid SGF coordinate letter {ch!r}") from None


def point_to_sgf(p: Point) -> str:
    """Encode a :class:`Point` as a two-letter SGF coordinate string."""
    return _index_to_letter(p.x) + _index_to_letter(p.y)


def sgf_to_point(value: str) -> Point:
    """Decode a two-letter SGF coordinate string into a :class:`Point`.

    Raises :class:`SgfCoordError` on anything that is not exactly two valid
    coordinate letters.
    """
    if len(value) != 2:
        raise SgfCoordError(f"expected a 2-letter coordinate, got {value!r}")
    return Point(_letter_to_index(value[0]), _letter_to_index(value[1]))


def sgf_to_move(value: str, width: int, height: int) -> Point | None:
    """Decode a ``move`` value, returning ``None`` for a pass.

    An empty value is a pass. The legacy ``tt`` pass is recognised when the
    board is small enough (<= 19 in both dimensions) that ``tt`` is off-board.
    """
    if value == "":
        return None
    if value == "tt" and width <= 19 and height <= 19:
        return None
    return sgf_to_point(value)


def move_to_sgf(p: Point | None) -> str:
    """Encode a move (or a pass, when ``p`` is ``None``) as a move value."""
    if p is None:
        return ""
    return point_to_sgf(p)


def parse_point_list(values: Iterable[str]) -> list[Point]:
    """Expand a list of SGF point values into individual :class:`Point` objects.

    Each value is either a single point (``aa``) or a compressed rectangle
    (``aa:bc``) that expands to every point in the inclusive rectangle whose
    corners are the two given points. Order is preserved; rectangles expand in
    row-major order. Empty values are ignored (an empty point list such as
    ``VW[]`` carries meaning to the caller but contributes no points), and so
    are malformed values — this feeds replay and rendering of
    externally-authored SGF, which must tolerate garbage (the raw value stays
    in the tree untouched, so saving never loses or alters it).
    """
    out: list[Point] = []
    for value in values:
        if value == "":
            continue
        try:
            if ":" in value:
                a_str, b_str = value.split(":", 1)
                a = sgf_to_point(a_str)
                b = sgf_to_point(b_str)
                x0, x1 = sorted((a.x, b.x))
                y0, y1 = sorted((a.y, b.y))
                for y in range(y0, y1 + 1):
                    for x in range(x0, x1 + 1):
                        out.append(Point(x, y))
            else:
                out.append(sgf_to_point(value))
        except SgfCoordError:
            continue
    return out


def point_list_to_sgf(points: Iterable[Point], compress: bool = True) -> list[str]:
    """Encode points as SGF point-list values.

    With ``compress`` (the default) maximal rectangles are emitted as ``aa:bc``
    where it saves space; otherwise one value per point is produced. Duplicate
    points are collapsed. The result is sorted for deterministic output.
    """
    unique = sorted(set(points))
    if not unique:
        return []
    if not compress:
        return [point_to_sgf(p) for p in unique]

    remaining = set(unique)
    values: list[str] = []
    # Greedily peel off the largest axis-aligned rectangle anchored at the
    # top-left-most remaining point.
    while remaining:
        anchor = min(remaining)
        # Extend width along the anchor's row.
        w = 1
        while Point(anchor.x + w, anchor.y) in remaining:
            w += 1
        # Extend height as long as every cell of the candidate row is present.
        h = 1
        while all(Point(anchor.x + dx, anchor.y + h) in remaining for dx in range(w)):
            h += 1
        for dy in range(h):
            for dx in range(w):
                remaining.discard(Point(anchor.x + dx, anchor.y + dy))
        if w == 1 and h == 1:
            values.append(point_to_sgf(anchor))
        else:
            far = Point(anchor.x + w - 1, anchor.y + h - 1)
            values.append(f"{point_to_sgf(anchor)}:{point_to_sgf(far)}")
    return values
