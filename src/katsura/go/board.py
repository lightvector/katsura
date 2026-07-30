"""A Go board with capture logic and a single, permissive legality predicate.

The board supports rectangular (non-square) sizes and the kinds of "irregular"
positions that occur in SGF files. There is exactly one legality predicate (see
:meth:`Board.play_classified` and ``docs/MODEL.md``), used identically for
interactive play and for SGF replay:

* playing on an occupied point is illegal;
* single-stone suicide is illegal;
* multi-stone suicide is legal (the own group is removed);
* there is no ko / superko enforcement.

An *illegal* move is never half-applied: the board is left untouched and the
caller decides what to do (reject the click, or skip the move during replay).
"""

from __future__ import annotations

from collections.abc import Iterable

from ..sgf.coords import Point

# Intersection states. We reuse 0/1/2 for compactness and speed.
EMPTY = 0
BLACK = 1
WHITE = 2

Color = int  # one of BLACK / WHITE (EMPTY is not a player colour)

# The largest board representable in SGF coordinates (letters a..zA..Z).
MAX_BOARD_SIZE = 52

# Move-classification statuses returned by play_classified().
MOVE_PASS = "pass"
MOVE_OK = "ok"
MOVE_SUICIDE_MULTI = "suicide_multi"
ILLEGAL_OCCUPIED = "occupied"
ILLEGAL_SUICIDE_SINGLE = "suicide_single"
# Only reachable from an SGF with a coordinate outside its own SZ.
ILLEGAL_OFF_BOARD = "off_board"

APPLIED_STATUSES = frozenset({MOVE_PASS, MOVE_OK, MOVE_SUICIDE_MULTI})
ILLEGAL_STATUSES = frozenset(
    {ILLEGAL_OCCUPIED, ILLEGAL_SUICIDE_SINGLE, ILLEGAL_OFF_BOARD})


def opponent(color: Color) -> Color:
    return WHITE if color == BLACK else BLACK


class IllegalMove(Exception):
    """Raised when a move is rejected under the active rules."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Board:
    """A mutable Go board.

    Stones are stored in a flat list indexed by ``y * width + x``. Coordinates
    are 0-indexed :class:`Point` objects (or raw x/y ints in the fast paths).
    """

    __slots__ = ("width", "height", "_grid")

    def __init__(self, width: int = 19, height: int | None = None):
        if height is None:
            height = width
        if not (1 <= width <= MAX_BOARD_SIZE and 1 <= height <= MAX_BOARD_SIZE):
            raise ValueError(
                f"board size {width}x{height} out of range 1..{MAX_BOARD_SIZE}")
        self.width = width
        self.height = height
        self._grid = [EMPTY] * (width * height)

    # -- basic access ------------------------------------------------------

    def copy(self) -> Board:
        b = Board.__new__(Board)
        b.width = self.width
        b.height = self.height
        b._grid = list(self._grid)
        return b

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def _idx(self, x: int, y: int) -> int:
        return y * self.width + x

    def get(self, x: int, y: int) -> int:
        return self._grid[y * self.width + x]

    def get_point(self, p: Point) -> int:
        return self._grid[p.y * self.width + p.x]

    def set(self, x: int, y: int, color: int) -> None:
        self._grid[y * self.width + x] = color

    def set_point(self, p: Point, color: int) -> None:
        self._grid[p.y * self.width + p.x] = color

    def clear(self) -> None:
        for i in range(len(self._grid)):
            self._grid[i] = EMPTY

    def neighbors(self, x: int, y: int) -> Iterable[tuple[int, int]]:
        if x > 0:
            yield x - 1, y
        if x < self.width - 1:
            yield x + 1, y
        if y > 0:
            yield x, y - 1
        if y < self.height - 1:
            yield x, y + 1

    # -- group / liberty analysis -----------------------------------------

    def group_and_liberties(self, x: int, y: int) -> tuple[list[int], int]:
        """Flood-fill the group at (x, y); return (stone indices, liberty count).

        Returns ``([], 0)`` for an empty point.
        """
        color = self._grid[self._idx(x, y)]
        if color == EMPTY:
            return [], 0
        grid = self._grid
        w = self.width
        start = y * w + x
        seen = {start}
        stack = [start]
        stones: list[int] = []
        liberties: set[int] = set()
        while stack:
            idx = stack.pop()
            stones.append(idx)
            cx = idx % w
            cy = idx // w
            for nx, ny in self.neighbors(cx, cy):
                nidx = ny * w + nx
                v = grid[nidx]
                if v == EMPTY:
                    liberties.add(nidx)
                elif v == color and nidx not in seen:
                    seen.add(nidx)
                    stack.append(nidx)
        return stones, len(liberties)

    def _remove_indices(self, indices: Iterable[int]) -> None:
        grid = self._grid
        for idx in indices:
            grid[idx] = EMPTY

    # -- move playing ------------------------------------------------------

    def _capture_dead_opponents(self, x: int, y: int, color: int) -> list[int]:
        """Remove opponent groups adjacent to (x, y) that have no liberties.

        Returns the removed stone indices. Mutates the board.
        """
        opp = opponent(color)
        captured: list[int] = []
        checked: set[int] = set()
        for nx, ny in self.neighbors(x, y):
            nidx = ny * self.width + nx
            if self._grid[nidx] == opp and nidx not in checked:
                stones, libs = self.group_and_liberties(nx, ny)
                checked.update(stones)
                if libs == 0:
                    captured.extend(stones)
        if captured:
            self._remove_indices(captured)
        return captured

    def play_classified(self, color: Color, p: Point | None) -> tuple[str, list[Point]]:
        """Classify and (if legal) apply a move under the single legality predicate.

        Returns ``(status, captured_points)`` where ``status`` is one of the
        module-level constants. For ``MOVE_PASS``/``MOVE_OK``/``MOVE_SUICIDE_MULTI``
        the move has been applied to the board and ``captured_points`` lists the
        removed stones (own group included for multi-stone suicide). For any
        status in ``ILLEGAL_STATUSES`` the board is **unchanged**.

        No ko/superko enforcement is performed. See ``docs/MODEL.md``.
        """
        if p is None:
            return MOVE_PASS, []
        x, y = p.x, p.y
        if not self.in_bounds(x, y):
            return ILLEGAL_OFF_BOARD, []
        idx = self._idx(x, y)
        if self._grid[idx] != EMPTY:
            return ILLEGAL_OCCUPIED, []

        # Simulate on a copy so an illegal move never leaves a partial mutation.
        trial = self.copy()
        trial._grid[idx] = color
        cap = trial._capture_dead_opponents(x, y, color)
        own_stones, own_libs = trial.group_and_liberties(x, y)
        w = self.width
        if own_libs == 0:
            if len(own_stones) == 1:
                return ILLEGAL_SUICIDE_SINGLE, []
            trial._remove_indices(own_stones)
            self._grid = trial._grid
            removed = cap + own_stones
            return MOVE_SUICIDE_MULTI, [Point(i % w, i // w) for i in removed]
        self._grid = trial._grid
        return MOVE_OK, [Point(i % w, i // w) for i in cap]

    def play(self, color: Color, p: Point | None) -> list[Point]:
        """Apply a move, raising :class:`IllegalMove` if it is illegal.

        Thin wrapper over :meth:`play_classified` for callers/tests that prefer
        exceptions. Multi-stone suicide is applied; occupied and single-stone
        suicide raise.
        """
        status, captured = self.play_classified(color, p)
        if status in ILLEGAL_STATUSES:
            raise IllegalMove(status)
        return captured

    # -- setup edits -------------------------------------------------------

    def apply_setup(
        self,
        add_black: Iterable[Point] = (),
        add_white: Iterable[Point] = (),
        add_empty: Iterable[Point] = (),
    ) -> None:
        """Apply AB/AW/AE-style setup edits directly (no capture semantics).

        Setup stones in SGF are placed without triggering captures. Validity
        (every group having a liberty) is the loader's responsibility via
        :meth:`remove_dead_groups` once all of a node's edits/moves are applied.

        Out-of-board points are silently ignored: externally-authored SGF may
        carry them, and the flat grid would otherwise wrap (or raise) — an
        ``AB[jj]`` on a 9x9 must not place a stone somewhere else.
        """
        for p in add_empty:
            if self.in_bounds(p.x, p.y):
                self.set_point(p, EMPTY)
        for p in add_black:
            if self.in_bounds(p.x, p.y):
                self.set_point(p, BLACK)
        for p in add_white:
            if self.in_bounds(p.x, p.y):
                self.set_point(p, WHITE)

    def resolve_setup_capture(self, p: Point, color: Color) -> list[Point]:
        """Resolve the captures implied by a setup stone of ``color`` at ``p``.

        *Setup stone wins* (the same order a real move uses): first remove every
        opponent group orthogonally adjacent to ``p`` that has no liberties, then
        — if the placed stone's own group then still has no liberties — remove
        that group too. Only groups adjacent to (or including) ``p`` are
        considered; dead groups elsewhere on the board are left untouched. The
        board is mutated and the removed points are returned. Assumes ``p``
        currently holds ``color``.

        Unlike :meth:`play_classified` there is no "illegal" verdict: a setup
        stone that cannot live is simply removed (single- and multi-stone
        self-capture alike). This is the in-board half of the setup tool's
        capture resolution; the model bakes the returned points into AE edits.
        """
        removed = self._capture_dead_opponents(p.x, p.y, color)
        own_stones, own_libs = self.group_and_liberties(p.x, p.y)
        if own_libs == 0:
            self._remove_indices(own_stones)
            removed = removed + own_stones
        w = self.width
        return [Point(i % w, i // w) for i in removed]

    def remove_dead_groups(self) -> list[Point]:
        """Remove every group that has no liberties; return the removed points.

        Used to coerce a tolerantly-loaded position into a valid one when an SGF
        leaves stones with zero liberties (which cannot exist on a real board).
        """
        removed: list[int] = []
        seen: set[int] = set()
        w = self.width
        for idx in range(len(self._grid)):
            if self._grid[idx] == EMPTY or idx in seen:
                continue
            stones, libs = self.group_and_liberties(idx % w, idx // w)
            seen.update(stones)
            if libs == 0:
                removed.extend(stones)
        if removed:
            self._remove_indices(removed)
        return [Point(i % w, i // w) for i in removed]

    # -- debugging ---------------------------------------------------------

    def ascii(self) -> str:  # pragma: no cover - debugging aid
        chars = {EMPTY: ".", BLACK: "X", WHITE: "O"}
        rows = []
        for y in range(self.height):
            rows.append(" ".join(chars[self.get(x, y)] for x in range(self.width)))
        return "\n".join(rows)
