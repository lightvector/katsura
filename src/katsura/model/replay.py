"""Applying one SGF node to a board: the single implementation of the position
semantics specified in ``docs/MODEL.md``.

Two callers drive a board node by node — the editor's replay
(:meth:`katsura.model.game.Game._recompute`) and the engine's request builder
(:func:`katsura.engine.position.build_request`) — and they have to agree
*exactly*: if they drift, the engine analyses a position the board is not
showing, and nothing in either half would look wrong. So the algorithm lives
here once,

1. apply the node's ``AB``/``AW``/``AE`` setup atomically, then sweep dead
   groups (setup can leave zero-liberty stones, which cannot exist on a board);
2. play the node's move under the one legality predicate — an illegal move is
   *skipped*, leaving the board untouched, but still flips whose turn it is;
3. apply ``PL`` **after** the move, so it names who plays the *next* one;

and each caller reads what it needs off the returned :class:`NodeApplication`
(the editor wants ghosts and the last-move marker, the engine wants boundary
and history information).

Nothing here mutates anything but ``board``, so it is equally safe for the
displayed position and for throwaway queries like :meth:`Game.board_at`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..go.board import Board, BLACK, WHITE, EMPTY, ILLEGAL_STATUSES, opponent
from ..sgf.coords import Point, SgfCoordError, parse_point_list, sgf_to_move
from ..sgf.tree import SgfNode
from . import markup as M


def color_from_pl(value: str) -> Optional[int]:
    """The colour an SGF ``PL`` value names (``B``/``1``, ``W``/``2``), else None."""
    v = value.strip().upper()
    if v in ("B", "1"):
        return BLACK
    if v in ("W", "2"):
        return WHITE
    return None


@dataclass(frozen=True)
class NodeApplication:
    """What applying one node did (see :func:`apply_node`)."""

    to_move: int                       # side to play *after* this node (PL applied)
    has_setup: bool                    # the node carried AB/AW/AE
    move_color: Optional[int] = None   # None when the node records no move
    move_point: Optional[Point] = None  # None = pass (or a malformed value)
    move_status: Optional[str] = None  # Board.play_classified status, if a move
    # Setup stones the sweep removed that the move did not re-occupy, mapped to
    # the colour they were specified as. Only populated under ``track_ghosts``.
    ghosts: dict = field(default_factory=dict)

    @property
    def has_move(self) -> bool:
        return self.move_color is not None

    @property
    def move_illegal(self) -> bool:
        """True when the node's move was skipped as illegal (board untouched)."""
        return self.move_status in ILLEGAL_STATUSES


def move_point_of(value: str, width: int, height: int) -> Optional[Point]:
    """Tolerantly decode a ``B``/``W`` value; garbage reads as a pass.

    Externally-authored SGF carries malformed coordinates, and replay must not
    crash on them. The raw value stays in the tree untouched, so a save neither
    loses nor alters it.
    """
    try:
        return sgf_to_move(value, width, height)
    except SgfCoordError:
        return None


def apply_node(node: SgfNode, board: Board, to_move: int,
               width: int, height: int, *,
               track_ghosts: bool = False) -> NodeApplication:
    """Apply ``node`` to ``board`` and report what happened.

    ``width``/``height`` are the dimensions the node's values are expressed in
    (needed to recognise a legacy ``tt`` pass). With ``track_ghosts`` the result
    carries the zero-liberty setup stones the sweep removed — the faint stones
    the board view draws and the setup tool treats as present; callers that only
    need the position skip that bookkeeping.
    """
    # 1-2. Setup edits, applied atomically, then one dead-group sweep.
    ab = parse_point_list(node.get(M.ADD_BLACK))
    aw = parse_point_list(node.get(M.ADD_WHITE))
    ae = parse_point_list(node.get(M.ADD_EMPTY))
    has_setup = bool(ab or aw or ae)
    ghosts: dict[Point, int] = {}
    if has_setup:
        board.apply_setup(add_black=ab, add_white=aw, add_empty=ae)
        if track_ghosts:
            nominal = board.copy()              # colours before the sweep
            for p in board.remove_dead_groups():
                ghosts[p] = nominal.get_point(p)
        else:
            board.remove_dead_groups()

    # 3. The move, under the single legality predicate. Illegal moves are
    #    skipped (board untouched) but still flip whose turn it is.
    move_color: Optional[int] = None
    move_point: Optional[Point] = None
    move_status: Optional[str] = None
    for prop, color in ((M.BLACK_MOVE, BLACK), (M.WHITE_MOVE, WHITE)):
        mv = node.get_one(prop)
        if mv is None:
            continue
        move_color = color
        move_point = move_point_of(mv, width, height)
        move_status, _ = board.play_classified(color, move_point)
        to_move = opponent(color)
        break

    # 4. PL is applied AFTER the move, so it controls the *next* move's colour.
    pl = node.get_one(M.PLAYER)
    if pl is not None:
        c = color_from_pl(pl)
        if c is not None:
            to_move = c

    if ghosts:
        # Drop any removed point the move then played on (it shows a real stone,
        # not a ghost). The result is a pure function of root->node.
        ghosts = {p: c for p, c in ghosts.items() if board.get_point(p) == EMPTY}
    return NodeApplication(to_move, has_setup, move_color, move_point,
                           move_status, ghosts)
