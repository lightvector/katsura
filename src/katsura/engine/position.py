"""Build a self-contained :class:`AnalysisRequest` from the game model.

This runs on the GUI thread (it reads the SGF tree). The resulting request is an
immutable value object containing everything the engine worker needs to drive
the position to the target node *without* touching the tree again:

* an **anchor**: the deepest ancestor that is a "boundary" (the root, a node
  with setup edits, or a multi-stone-suicide move) — reproduced wholesale with
  ``set_position``;
* the **moves**: the pure-move suffix from the anchor down to the target, each a
  single ``play COLOR VERTEX`` (skipped-illegal moves contribute nothing, so the
  engine's move history omits them — exactly what KataGo's last-few-moves bias
  and superko want);
* the **target stones**: the full board at the target, used only as a
  last-resort ``set_position`` if the engine rejects a ``play`` (e.g. a ko/suicide
  illegal under *its* rules).

Because the worker diffs requests *by value* (anchor stones + move list), it can
do incremental ``undo``/``play`` when you scrub around inside one move sequence,
and it never depends on node identity (robust across edits and undo/redo).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..go.board import BLACK, EMPTY, MOVE_SUICIDE_MULTI, WHITE, Board
from ..model.replay import apply_node
from ..sgf.coords import Point
from .settings import DEFAULT_RULES, AnalysisSettings, clamp_komi, sgf_rules

GTP_COLOR = {BLACK: "B", WHITE: "W"}


def format_komi(komi: float | None) -> str | None:
    """Format a komi float for GTP (``None`` stays ``None`` = leave engine default)."""
    if komi is None:
        return None
    s = f"{komi:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def _stones_of(board: Board) -> tuple:
    """Canonical sorted tuple of ``(color, vertex)`` for every stone on ``board``."""
    from .coords import point_to_vertex
    out = []
    h = board.height
    for y in range(board.height):
        for x in range(board.width):
            c = board.get(x, y)
            if c != EMPTY:
                out.append((GTP_COLOR[c], point_to_vertex(Point(x, y), h)))
    return tuple(sorted(out))


@dataclass(frozen=True)
class AnalysisRequest:
    """Everything the engine worker needs to analyse one position."""

    seq: int
    width: int
    height: int
    rules: str | None
    komi: str | None
    color: str                       # "B" / "W" — side to move at the target
    anchor_stones: tuple             # sorted ((color, vertex), ...) for set_position
    moves: tuple                     # ((color, vertex), ...) plays anchor -> target
    target_stones: tuple             # full board at target (last-resort fallback)
    # Engine search parameters (default-valued so legacy callers/tests still work).
    wide_root_noise: float = 0.04
    playout_doubling_advantage: float = 0.0

    @property
    def position_key(self) -> tuple:
        """Identity of the *analysis* (board + history + side + all engine settings).

        Two requests with equal keys would produce the same search, so the
        controller can skip re-sending (e.g. on a comment edit that doesn't move)
        and cache results safely. Komi, rules, and the search parameters are all
        included so analyses run under different settings never collide.
        """
        return (self.width, self.height, self.rules, self.komi, self.color,
                self.anchor_stones, self.moves,
                self.wide_root_noise, self.playout_doubling_advantage)


def initial_settings(game) -> AnalysisSettings:
    """Derive a tab's starting :class:`AnalysisSettings` from its SGF.

    Komi is the SGF ``KM`` if it parses (else 7.0), snapped into the range
    KataGo accepts by :func:`clamp_komi` — a garbage or wildly out-of-range
    ``KM`` can never produce an unusable request. Rules come from the SGF
    ``RU`` whenever :func:`sgf_rules` recognises it, else the default. The
    search parameters start at their defaults.
    """
    k = game.get_komi()
    # A KM that parsed but isn't a real number (KM[nan], KM[inf]) is treated as
    # absent rather than clamped, so it falls back to the sensible default.
    komi = clamp_komi(k) if k is not None and math.isfinite(k) else 7.0
    rules = sgf_rules(game.get_info("RU")) or DEFAULT_RULES
    return AnalysisSettings(komi=komi, rules=rules)


def build_request(game, seq: int,
                  settings: AnalysisSettings | None = None,
                  to_move: int | None = None) -> AnalysisRequest:
    """Build an :class:`AnalysisRequest` for ``game``'s current node.

    ``settings`` supplies the engine komi/rules/search-params; when omitted they
    are derived from the SGF via :func:`initial_settings` (legacy behaviour).
    ``to_move`` overrides the side to analyse for (the GUI's transient
    Ctrl+Click player flip, which changes no SGF); default is the game's own
    side to move. It feeds ``color`` and hence ``position_key``, so analyses of
    the two sides of one board never mix — as if a ``PL`` property were set.
    """
    if settings is None:
        settings = initial_settings(game)
    from .coords import point_to_vertex

    w, h = game.width, game.height
    path = game.path_to_root(game.current)

    board = Board(w, h)
    boundary: list[bool] = []
    plays: list[tuple | None] = []
    boards: list[Board] = []

    for node in path:
        # The one implementation of MODEL.md's setup -> sweep -> move -> PL
        # (model/replay.py), shared with the editor's own replay so the engine
        # can never be driven to a position the board isn't showing. We ignore
        # its ``to_move`` (the side to analyse for is passed in) and read the
        # rest to decide boundaries and history.
        res = apply_node(node, board, BLACK, w, h)

        node_play: tuple | None = None
        is_boundary = node.parent is None or res.has_setup
        if res.has_move:
            if res.move_illegal:
                node_play = None                    # skipped — omit from history
            elif res.move_status == MOVE_SUICIDE_MULTI:
                is_boundary = True                  # engine rules may differ; set it
            else:
                node_play = (GTP_COLOR[res.move_color],
                             point_to_vertex(res.move_point, h))

        boundary.append(is_boundary)
        plays.append(node_play)
        boards.append(board.copy())

    anchor_idx = max(i for i in range(len(path)) if boundary[i])
    move_list = tuple(plays[i] for i in range(anchor_idx + 1, len(path))
                      if plays[i] is not None)

    return AnalysisRequest(
        seq=seq,
        width=w,
        height=h,
        rules=settings.rules.to_gtp(),
        komi=format_komi(settings.komi),
        color=GTP_COLOR[to_move if to_move is not None else game.to_move],
        anchor_stones=_stones_of(boards[anchor_idx]),
        moves=move_list,
        target_stones=_stones_of(boards[-1]),
        wide_root_noise=settings.wide_root_noise,
        playout_doubling_advantage=settings.playout_doubling_advantage,
    )
