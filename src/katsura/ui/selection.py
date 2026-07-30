"""Pure transforms for the selection paste buffer.

A buffer is ``(items, (w, h))`` where each item is
``(rx, ry, stone_or_None, mark_prop_or_None, label_or_None)`` with ``rx``/``ry``
relative to the buffer's top-left corner.
"""

from __future__ import annotations


def rotate_cw(items: list[tuple], dims: tuple[int, int]):
    """Rotate the buffer 90° clockwise."""
    w, h = dims
    out = [(h - 1 - ry, rx, st, mk, lb) for (rx, ry, st, mk, lb) in items]
    return out, (h, w)


def flip_h(items: list[tuple], dims: tuple[int, int]):
    """Flip the buffer horizontally (left/right)."""
    w, h = dims
    out = [(w - 1 - rx, ry, st, mk, lb) for (rx, ry, st, mk, lb) in items]
    return out, (w, h)
