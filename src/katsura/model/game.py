"""The :class:`Game` model: a single SGF game tree plus a replayable board.

This is the object the GUI drives. It keeps a *current node* pointer, the board
position at that node, and whose turn it is, and offers navigation and editing
operations that keep the SGF tree and the board in sync.

The board at any node is computed by replaying from the root. Replaying is cheap
(a few hundred operations for a normal game), so navigation simply re-replays
along the path to the new current node rather than maintaining incremental
undo state.
"""

from __future__ import annotations

from .. import __version__
from ..go.board import (
    BLACK,
    EMPTY,
    ILLEGAL_OCCUPIED,
    ILLEGAL_OFF_BOARD,
    ILLEGAL_SUICIDE_SINGLE,
    MAX_BOARD_SIZE,
    MOVE_OK,
    MOVE_SUICIDE_MULTI,
    WHITE,
    Board,
)
from ..sgf.coords import (
    Point,
    SgfCoordError,
    move_to_sgf,
    parse_point_list,
    point_list_to_sgf,
    point_to_sgf,
    sgf_to_move,
    sgf_to_point,
)
from ..sgf.tree import SgfNode
from . import markup as M
from .replay import NodeApplication, apply_node, move_point_of

# What we stamp into a new game's ``AP`` (the SGF "application that wrote this
# file" property, conventionally ``name:version``). Derived from the package
# version so released files say which build produced them.
APPLICATION = f"Katsura:{__version__}"

# Whole-board geometry transforms: how a point moves, expressed in the board's
# *pre-transform* width and height, and whether the transform swaps the two.
_GEOMETRY_TRANSFORMS = {
    "rot_cw":  (lambda p, w, h: Point(h - 1 - p.y, p.x), True),
    "rot_ccw": (lambda p, w, h: Point(p.y, w - 1 - p.x), True),
    "flip_h":  (lambda p, w, h: Point(w - 1 - p.x, p.y), False),
    "flip_v":  (lambda p, w, h: Point(p.x, h - 1 - p.y), False),
}

# SGF properties whose value is a point list (each value a single point or a
# compressed ``aa:bc`` rectangle): setup stones, markers, territory, dim, view.
_POINT_LIST_PROPS = (
    M.ADD_BLACK, M.ADD_WHITE, M.ADD_EMPTY,
    *M.MARK_PROPS,
    "TB", "TW", "DD", "VW",
)
# Properties whose value is a composed ``point:point`` pair (line, arrow).
_COMPOSED_POINT_PROPS = ("AR", "LN")


def _node_points(node: SgfNode, width: int, height: int):
    """Yield every point carried by one node's operations (passes excluded).

    ``width``/``height`` are the dimensions the node's move values are
    expressed in (needed to recognise a legacy ``tt`` pass). Malformed values
    contribute nothing.
    """
    for prop in (M.BLACK_MOVE, M.WHITE_MOVE):
        mv = node.get_one(prop)
        if mv:
            try:
                pt = sgf_to_move(mv, width, height)
            except SgfCoordError:
                pt = None
            if pt is not None:
                yield pt
    for prop in _POINT_LIST_PROPS:
        yield from parse_point_list(node.get(prop))
    for v in node.get(M.LABEL_PROP):
        if ":" in v:
            try:
                yield sgf_to_point(v.split(":", 1)[0])
            except SgfCoordError:
                pass
    for prop in _COMPOSED_POINT_PROPS:
        for v in node.get(prop):
            if ":" in v:
                a_str, b_str = v.split(":", 1)
                try:
                    a, b = sgf_to_point(a_str), sgf_to_point(b_str)
                except SgfCoordError:
                    continue
                yield a
                yield b


def _map_node_points(node: SgfNode, fn, width: int, height: int) -> int:
    """Apply the point map ``fn`` to every coordinate on a single node.

    ``fn`` maps a :class:`Point` to a new point, or to ``None`` to prune that
    operation: a pruned move loses its B/W property, a pruned list point or
    label disappears from its value list (an emptied list drops the property),
    and a line/arrow is dropped whole if either endpoint prunes.
    ``width``/``height`` are the dimensions the node's move values are
    currently expressed in (for pass detection); a rewritten pass is
    normalised to ``[]`` so it cannot be re-read as a move under other
    dimensions. Malformed values are left verbatim, as everywhere else.
    Returns the number of operations pruned.
    """
    pruned = 0
    # Moves: a single point (or a pass).
    for prop in (M.BLACK_MOVE, M.WHITE_MOVE):
        mv = node.get_one(prop)
        if mv is None:
            continue
        try:
            pt = sgf_to_move(mv, width, height)
        except SgfCoordError:
            continue
        if pt is None:
            if mv != "":
                node.set_one(prop, "")
            continue
        q = fn(pt)
        if q is None:
            node.remove(prop)
            pruned += 1
        else:
            node.set_one(prop, move_to_sgf(q))
    # Point lists. A present-but-empty value (e.g. ``VW[]``) is meaningful
    # and left untouched.
    for prop in _POINT_LIST_PROPS:
        vals = node.get(prop)
        if not vals or vals == [""]:
            continue
        pts = [fn(p) for p in parse_point_list(vals)]
        kept = [p for p in pts if p is not None]
        pruned += len(pts) - len(kept)
        node.set(prop, point_list_to_sgf(kept))
    # Labels: ``point:text``.
    labels = node.get(M.LABEL_PROP)
    if labels:
        out: list[str] = []
        for v in labels:
            if ":" not in v:
                continue
            pt_str, text = v.split(":", 1)
            try:
                q = fn(sgf_to_point(pt_str))
            except SgfCoordError:
                continue
            if q is None:
                pruned += 1
            else:
                out.append(f"{point_to_sgf(q)}:{text}")
        node.set(M.LABEL_PROP, out)
    # Composed point:point pairs (lines, arrows).
    for prop in _COMPOSED_POINT_PROPS:
        vals = node.get(prop)
        if not vals:
            continue
        out = []
        for v in vals:
            if ":" not in v:
                continue
            a_str, b_str = v.split(":", 1)
            try:
                a, b = fn(sgf_to_point(a_str)), fn(sgf_to_point(b_str))
            except SgfCoordError:
                continue
            if a is None or b is None:
                pruned += 1
            else:
                out.append(f"{point_to_sgf(a)}:{point_to_sgf(b)}")
        node.set(prop, out)
    return pruned


def fit_subtree_to_board(subtree: SgfNode, src_w: int, src_h: int,
                         dst_w: int, dst_h: int) -> int:
    """Refit a detached subtree's coordinates onto a differently-sized board.

    The bounding box of every operation in the subtree (moves, setup stones,
    marks, labels, territory/dim/view points, line/arrow endpoints) is
    anchored, per axis, to the source-board edge it lies nearer, and every
    coordinate is shifted so the box keeps the same offset from the matching
    corner of the destination board. Operations that land off the destination
    board are pruned per :func:`_map_node_points`. Points already off the
    *source* board (garbage in externally-authored SGF) are ignored when
    computing the box, then pruned like any other off-board result. Returns
    the number of pruned operations.
    """
    xs: list[int] = []
    ys: list[int] = []
    for node in subtree.walk():
        for p in _node_points(node, src_w, src_h):
            if 0 <= p.x < src_w and 0 <= p.y < src_h:
                xs.append(p.x)
                ys.append(p.y)
    # Ties anchor left/top (offset 0); anchoring right/bottom preserves the
    # box's distance to that edge across the size change.
    dx = dst_w - src_w if xs and min(xs) > (src_w - 1) - max(xs) else 0
    dy = dst_h - src_h if ys and min(ys) > (src_h - 1) - max(ys) else 0

    def fit(p: Point) -> Point | None:
        q = Point(p.x + dx, p.y + dy)
        return q if (0 <= q.x < dst_w and 0 <= q.y < dst_h) else None

    return sum(_map_node_points(node, fit, src_w, src_h)
               for node in subtree.walk())


def format_komi_value(value: float) -> str:
    """A komi as compact SGF/display text (no trailing zeros, no signed zero)."""
    s = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


class GameError(Exception):
    """Raised for illegal edits requested through the model (e.g. illegal move)."""


def parse_board_size(root: SgfNode) -> tuple[int, int]:
    """Read the board size from a root node's ``SZ`` property.

    Accepts ``SZ[19]`` (square) and ``SZ[w:h]`` (columns:rows). Defaults to
    19x19 when absent or unparseable, and clamps each dimension to what SGF
    coordinates can express (1..52) so a garbage ``SZ[100]`` degrades instead
    of crashing the load.
    """
    def clamp(n: int) -> int:
        return min(MAX_BOARD_SIZE, max(1, n))

    sz = root.get_one("SZ")
    if not sz:
        return 19, 19
    sz = sz.strip()
    try:
        if ":" in sz:
            w_str, h_str = sz.split(":", 1)
            return clamp(int(w_str)), clamp(int(h_str))
        n = clamp(int(sz))
        return n, n
    except ValueError:
        return 19, 19


class Game:
    """A single SGF game tree with a current position."""

    def __init__(self, root: SgfNode, *, tolerant: bool = True):
        self.root = root
        self.tolerant = tolerant
        self.width, self.height = parse_board_size(root)
        self.current: SgfNode = root
        self.board = Board(self.width, self.height)
        self.to_move: int = BLACK
        self.move_number: int = 0
        # The current node's move point (for last-move display) and whether that
        # move was skipped as illegal (for the red "illegal move" marker).
        self.last_move_point: Point | None = None
        self.last_move_illegal: bool = False
        # Setup stones at the *current* node that the legality sweep removed
        # (zero-liberty) and the move did not re-occupy, mapped to their nominal
        # colour. Purely derived from the SGF by ``_recompute``; the GUI draws
        # them as faint "ghost" stones and the setup tool treats them as present.
        self.setup_ghosts: dict[Point, int] = {}
        self._recompute()

    # -- construction ------------------------------------------------------

    @classmethod
    def new(cls, width: int = 19, height: int | None = None,
            application: str = APPLICATION) -> Game:
        """Create a fresh, empty game with a populated root node."""
        if height is None:
            height = width
        root = SgfNode()
        root.set_one("GM", "1")
        root.set_one("FF", "4")
        root.set_one("CA", "UTF-8")
        root.set_one("AP", application)
        root.set_one("SZ", str(width) if width == height else f"{width}:{height}")
        return cls(root)

    # -- replay / position computation ------------------------------------

    def path_to_root(self, node: SgfNode) -> list[SgfNode]:
        chain: list[SgfNode] = []
        n: SgfNode | None = node
        while n is not None:
            chain.append(n)
            n = n.parent
        chain.reverse()
        return chain

    def _apply_node(self, node: SgfNode, board: Board, to_move: int,
                    track_ghosts: bool = False) -> NodeApplication:
        """Apply one node to ``board`` (see :func:`katsura.model.replay.apply_node`).

        Mutates only ``board`` — never ``self`` — so queries like
        :meth:`board_at` can replay arbitrary nodes without disturbing the
        *displayed* state (last-move marker, ghosts).
        """
        return apply_node(node, board, to_move, self.width, self.height,
                          track_ghosts=track_ghosts)

    def _recompute(self) -> None:
        """Replay from root to ``current``, refreshing board/to_move/move_number."""
        path = self.path_to_root(self.current)
        board = Board(self.width, self.height)
        to_move = BLACK
        move_number = 0

        # Handicap convention: setup-only root with no PL implies White to move.
        root = self.root
        if (root.has(M.ADD_BLACK) and not root.has(M.PLAYER)
                and not root.has(M.BLACK_MOVE) and not root.has(M.WHITE_MOVE)):
            to_move = WHITE

        res: NodeApplication | None = None
        for node in path:
            res = self._apply_node(
                node, board, to_move, track_ghosts=(node is self.current))
            to_move = res.to_move
            move_number += 1 if res.has_move else 0
        # The current node is the last in ``path``; its move info wins.
        self.board = board
        self.to_move = to_move
        self.move_number = move_number
        self.last_move_point = res.move_point if res.has_move else None
        self.last_move_illegal = res.move_illegal if res.has_move else False
        self.setup_ghosts = res.ghosts

    def board_at(self, node: SgfNode) -> Board:
        """Return a fresh board reflecting the position at ``node``.

        A pure query: replaying via :meth:`_apply_node` touches no display
        state, so this is safe mid-edit (e.g. the ko check in :meth:`play`).
        """
        path = self.path_to_root(node)
        board = Board(self.width, self.height)
        to_move = BLACK
        for n in path:
            to_move = self._apply_node(n, board, to_move).to_move
        return board

    # -- navigation --------------------------------------------------------

    def goto(self, node: SgfNode) -> None:
        self.current = node
        self._remember_path(node)
        self._recompute()

    def _remember_path(self, node: SgfNode) -> None:
        """Record, for each ancestor edge on the path to ``node``, which child
        was taken — so a later ``forward`` returns to this same variation."""
        n = node
        while n.parent is not None:
            n.parent.remembered_child = n
            n = n.parent

    def preferred_child(self, node: SgfNode) -> SgfNode | None:
        """The remembered (or first) child of ``node``, or ``None`` if a leaf.

        A remembered child that is no longer a child of this node (deleted, or
        moved elsewhere by a cut) is ignored.
        """
        if not node.children:
            return None
        remembered = node.remembered_child
        if remembered is not None and any(c is remembered for c in node.children):
            return remembered
        return node.children[0]

    def go_to_start(self) -> None:
        self.goto(self.root)

    def go_to_end(self) -> None:
        node = self.current
        while node.children:
            nxt = self.preferred_child(node)
            if nxt is None:
                break
            node = nxt
        self.goto(node)

    def forward(self, n: int = 1) -> bool:
        """Advance ``n`` moves along the remembered (else first-child) line."""
        node = self.current
        moved = False
        for _ in range(n):
            nxt = self.preferred_child(node)
            if nxt is None:
                break
            node = nxt
            moved = True
        if moved:
            self.goto(node)
        return moved

    def back(self, n: int = 1) -> bool:
        node = self.current
        moved = False
        for _ in range(n):
            if node.parent is None:
                break
            node = node.parent
            moved = True
        if moved:
            self.goto(node)
        return moved

    def has_next(self) -> bool:
        return bool(self.current.children)

    def has_prev(self) -> bool:
        return self.current.parent is not None

    def sibling_index(self, node: SgfNode | None = None) -> int:
        node = node or self.current
        if node.parent is None:
            return 0
        return node.parent.children.index(node)

    # -- move editing ------------------------------------------------------

    def _color_name(self, color: int) -> str:
        return M.BLACK_MOVE if color == BLACK else M.WHITE_MOVE

    def _move_point(self, value: str) -> Point | None:
        """Tolerantly decode a B/W move value; a malformed value reads as a pass."""
        return move_point_of(value, self.width, self.height)

    def find_child_move(self, point: Point | None, color: int) -> SgfNode | None:
        """Return the child node recording exactly this move, if one exists."""
        prop = self._color_name(color)
        for child in self.current.children:
            cv = child.get_one(prop)
            if cv is not None and self._move_point(cv) == point:
                return child
        return None

    def play(self, point: Point | None, color: int | None = None, *,
             forbid_multi_suicide: bool = False,
             forbid_ko: bool = False) -> SgfNode:
        """Play a move at ``point`` (``None`` = pass) for ``color`` (default: side to move).

        If a child of the current node already records exactly this move, we
        navigate into it (no duplicate is created). Otherwise the move is
        validated. The base predicate (see ``docs/MODEL.md``) always rejects
        occupied points and single-stone suicide. The two optional flags add
        *interactive-only* restrictions that SGF replay never applies:

        * ``forbid_multi_suicide`` — reject multi-stone self-capture too;
        * ``forbid_ko`` — reject an immediate simple-ko recapture, i.e. a move
          that captures exactly one stone and recreates the whole-board position
          that existed before the opponent's last move.

        A new child node is created and becomes current.
        """
        if color is None:
            color = self.to_move
        prop = self._color_name(color)

        existing = self.find_child_move(point, color)
        if existing is not None:
            self.goto(existing)
            return existing

        if point is not None:
            trial = self.board.copy()
            status, captured = trial.play_classified(color, point)
            if status == ILLEGAL_OCCUPIED:
                raise GameError("point is already occupied")
            if status == ILLEGAL_OFF_BOARD:
                raise GameError("point is off the board")
            if status == ILLEGAL_SUICIDE_SINGLE:
                raise GameError("single-stone suicide is not allowed")
            if forbid_multi_suicide and status == MOVE_SUICIDE_MULTI:
                raise GameError("multi-stone self-capture is not allowed")
            if (forbid_ko and status == MOVE_OK and len(captured) == 1
                    and self.current.parent is not None):
                # Simple ko: a single-stone recapture that restores the position
                # immediately before the opponent's last move (two plies back).
                prev = self.board_at(self.current.parent)
                if trial._grid == prev._grid:
                    raise GameError("illegal ko recapture")

        node = SgfNode()
        node.set_one(prop, move_to_sgf(point))
        self.current.add_child(node)
        self.goto(node)
        return node

    def pass_move(self, color: int | None = None) -> SgfNode:
        return self.play(None, color)

    # -- setup editing -----------------------------------------------------

    def _ensure_setup_node(self) -> SgfNode:
        """Return a node suitable for setup edits, creating a child if needed.

        Setup belongs on a node that has no move. If the current node already
        records a move, a fresh child node is created for the setup edits.
        """
        cur = self.current
        if cur.has(M.BLACK_MOVE) or cur.has(M.WHITE_MOVE):
            node = SgfNode()
            cur.add_child(node)
            self.goto(node)
            return node
        return cur

    def _parent_color(self, node: SgfNode, point: Point) -> int:
        parent_board = (self.board_at(node.parent) if node.parent is not None
                        else Board(self.width, self.height))
        return parent_board.get_point(point)

    def _node_nominal_board(self, node: SgfNode) -> Board:
        """The *nominal* (pre-legality) position at ``node``.

        The interpreted parent position with this node's AB/AW/AE overlaid, but
        *without* the dead-group sweep — so zero-liberty setup stones are still
        present. This is the layer the setup tool edits and reasons about (which
        groups a placed stone captures, and whether a click sets or erases).
        """
        board = (self.board_at(node.parent) if node.parent is not None
                 else Board(self.width, self.height))
        board.apply_setup(
            add_black=parse_point_list(node.get(M.ADD_BLACK)),
            add_white=parse_point_list(node.get(M.ADD_WHITE)),
            add_empty=parse_point_list(node.get(M.ADD_EMPTY)),
        )
        return board

    def setup_layer_color(self, point: Point) -> int:
        """Colour the setup tool sees at ``point`` (the pre-interpretation layer).

        Zero-liberty setup stones that interpretation removes from the *display*
        are treated as present here, so the tool's set/erase toggle (and the
        hover preview) act on what the SGF setup specifies rather than on the
        legality-resolved board. See ``docs/MODEL.md``.

        When the current node has a move, setup edits spawn a fresh child whose
        nominal layer equals the interpreted current position, so the displayed
        board is the right answer there (a fresh child has no ghosts of its own).
        """
        cur = self.current
        if not (cur.has(M.BLACK_MOVE) or cur.has(M.WHITE_MOVE)):
            ghost = self.setup_ghosts.get(point)
            if ghost is not None:
                return ghost
        return self.board.get_point(point)

    def setup_click_target(self, point: Point, tool_color: int) -> int:
        """The colour a setup click (or paint-stroke start) on ``point`` lays down.

        The tool toggles a point between *holding a stone* and *empty*: if the
        setup layer already holds a stone there — of **either** colour — the click
        erases it (returns ``EMPTY``); only an empty point receives ``tool_color``.
        So successive clicks cycle stone → empty → tool colour (a black click on a
        white stone first clears it; the next click places black).

        Read off the setup layer (`setup_layer_color`), so a zero-liberty ghost
        counts as a stone here too. A paint stroke calls this once for its first
        cell to fix whether the whole drag places ``tool_color`` or erases; the
        rest of the stroke then applies that fixed action (so a placing stroke
        overwrites stones it crosses, an erasing stroke clears them).
        """
        return EMPTY if self.setup_layer_color(point) != EMPTY else tool_color

    def _resolve_setup_captures(self, node: SgfNode, point: Point,
                                color: int) -> None:
        """Bake into ``node`` the captures implied by placing ``color`` at ``point``.

        Mirrors the move-capture rule (*setup stone wins*): in the node's nominal
        position, remove opponent groups adjacent to ``point`` with no liberties,
        then the placed stone's own group if it then has none. Each removed point
        is written back as a minimal-diff edit, so it becomes ``AE`` or simply
        loses its ``AB``/``AW`` (restoring the parent's value) — the captures end
        up *in the SGF* rather than being re-derived only at interpretation time.

        Only groups adjacent to (or including) ``point`` are touched; illegal
        setup elsewhere is left for interpretation to resolve. A no-op when
        erasing (``color == EMPTY``), since removing a stone never captures.
        """
        if color == EMPTY:
            return
        nominal = self._node_nominal_board(node)
        if nominal.get_point(point) != color:
            return                          # defensive: the edit didn't place it
        for p in nominal.resolve_setup_capture(point, color):
            self._setup_point_on(node, p, EMPTY, force_redundant=False)

    def set_setup_point(self, point: Point, color: int, *,
                        force_redundant: bool = False,
                        resolve_captures: bool | None = None) -> None:
        """Record a single setup edit at ``point`` to ``color`` (BLACK/WHITE/EMPTY).

        The point is placed in exactly one of AB/AW/AE (or none). With
        ``force_redundant=False`` (normal edits) the specifier is recorded only
        when ``color`` differs from the parent position's colour there, so
        redundant specifiers never accumulate. With ``force_redundant=True``
        (ctrl edits) the specifier is always recorded — even when redundant —
        which matters for future copy/paste of nodes between branches. See
        ``docs/MODEL.md``.

        ``resolve_captures`` (default: the opposite of ``force_redundant``)
        controls whether the captures the edit implies are baked into further
        edits, *setup stone wins*, on the groups adjacent to ``point`` (see
        :meth:`_resolve_setup_captures`). Normal clicks resolve; ctrl (force)
        clicks do not — they leave any illegality for interpretation, which the
        GUI then shows as ghost stones.
        """
        if resolve_captures is None:
            resolve_captures = not force_redundant
        node = self._ensure_setup_node()
        self._setup_point_on(node, point, color, force_redundant)
        if resolve_captures:
            self._resolve_setup_captures(node, point, color)
        self._recompute()

    def _setup_point_on(self, node: SgfNode, point: Point, color: int,
                        force_redundant: bool) -> None:
        """Record a setup edit at ``point`` on ``node`` (no recompute)."""
        parent_color = self._parent_color(node, point)
        ab = set(parse_point_list(node.get(M.ADD_BLACK)))
        aw = set(parse_point_list(node.get(M.ADD_WHITE)))
        ae = set(parse_point_list(node.get(M.ADD_EMPTY)))
        for s in (ab, aw, ae):
            s.discard(point)
        if force_redundant or color != parent_color:
            if color == BLACK:
                ab.add(point)
            elif color == WHITE:
                aw.add(point)
            else:
                ae.add(point)
        self._set_pointlist(node, M.ADD_BLACK, sorted(ab))
        self._set_pointlist(node, M.ADD_WHITE, sorted(aw))
        self._set_pointlist(node, M.ADD_EMPTY, sorted(ae))

    def get_setup_points(self, node: SgfNode | None = None) -> dict[Point, int]:
        """Map every point with an AB/AW/AE specifier on ``node`` to its colour.

        Includes redundant (forced) specifiers, so the GUI can halo all of them.
        """
        node = node or self.current
        out: dict[Point, int] = {}
        for p in parse_point_list(node.get(M.ADD_BLACK)):
            out[p] = BLACK
        for p in parse_point_list(node.get(M.ADD_WHITE)):
            out[p] = WHITE
        for p in parse_point_list(node.get(M.ADD_EMPTY)):
            out[p] = EMPTY
        return out

    @staticmethod
    def _set_pointlist(node: SgfNode, prop: str, points: list[Point]) -> None:
        if points:
            node.set(prop, point_list_to_sgf(points))
        else:
            node.remove(prop)

    # -- markup editing ----------------------------------------------------

    def toggle_mark(self, point: Point, mark: M.MarkType) -> None:
        """Toggle a single-point marker (circle/square/triangle/cross/selected)."""
        node = self.current
        prop = mark.value
        existing = set(parse_point_list(node.get(prop)))
        if point in existing:
            existing.discard(point)
        else:
            existing.add(point)
            # A point may carry only one of the geometric marks at a time;
            # clear the others at this point for clarity.
            for other in M.MARK_PROPS:
                if other != prop:
                    others = set(parse_point_list(node.get(other)))
                    if point in others:
                        others.discard(point)
                        self._set_pointlist(node, other, sorted(others))
        self._set_pointlist(node, prop, sorted(existing))

    def get_marks(self, node: SgfNode | None = None) -> dict[Point, str]:
        node = node or self.current
        marks: dict[Point, str] = {}
        for prop in M.MARK_PROPS:
            for p in parse_point_list(node.get(prop)):
                marks[p] = prop
        return marks

    def set_label(self, point: Point, text: str) -> None:
        """Set or clear a text label (LB) at ``point``."""
        node = self.current
        labels = self.get_labels(node)
        if text:
            labels[point] = text
        else:
            labels.pop(point, None)
        self._write_labels(node, labels)

    def get_labels(self, node: SgfNode | None = None) -> dict[Point, str]:
        node = node or self.current
        result: dict[Point, str] = {}
        for value in node.get(M.LABEL_PROP):
            if ":" not in value:
                continue
            pt_str, text = value.split(":", 1)
            try:
                result[sgf_to_point(pt_str)] = text
            except SgfCoordError:
                continue
        return result

    def _write_labels(self, node: SgfNode, labels: dict[Point, str]) -> None:
        if labels:
            values = [f"{point_to_sgf(p)}:{text}" for p, text in sorted(labels.items())]
            node.set(M.LABEL_PROP, values)
        else:
            node.remove(M.LABEL_PROP)

    def _set_mark_on(self, node: SgfNode, point: Point, prop: str) -> None:
        for other in M.MARK_PROPS:
            pts = set(parse_point_list(node.get(other)))
            if other == prop:
                pts.add(point)
            else:
                pts.discard(point)
            self._set_pointlist(node, other, sorted(pts))

    def _clear_marks_on(self, node: SgfNode, point: Point) -> None:
        for prop in M.MARK_PROPS:
            pts = set(parse_point_list(node.get(prop)))
            if point in pts:
                pts.discard(point)
                self._set_pointlist(node, prop, sorted(pts))

    def _set_label_on(self, node: SgfNode, point: Point, text: str | None) -> None:
        labels = self.get_labels(node)
        if text:
            labels[point] = text
        else:
            labels.pop(point, None)
        self._write_labels(node, labels)

    # -- root game info (player names, komi, rules, date, ...) ------------

    def get_info(self, prop: str) -> str:
        """Return a root-node simpletext property (empty string if absent)."""
        return self.root.get_one(prop) or ""

    def set_info(self, prop: str, value: str) -> None:
        """Set or clear a root-node simpletext property."""
        if value:
            self.root.set_one(prop, value)
        else:
            self.root.remove(prop)

    def get_komi(self) -> float | None:
        """Parse the root ``KM`` value as a float (``None`` if absent/unparseable).

        Tolerates the Chinese unit suffixes 子/目 sometimes seen in komi values.
        """
        v = self.root.get_one("KM")
        if v is None:
            return None
        s = v.strip().replace("子", "").replace("目", "").strip()
        try:
            return float(s)
        except ValueError:
            return None

    def set_komi(self, value: float | None) -> None:
        """Set or clear the root ``KM`` value, formatted without trailing zeros.

        Four decimals, not two: quarter- and eighth-point komi exist, and
        rounding the value the user typed would silently rewrite their SGF.
        """
        if value is None:
            self.root.remove("KM")
            return
        self.root.set_one("KM", format_komi_value(value))

    # -- selection compound edits (cut/copy/paste of board contents) -------

    def snapshot_points(self, points) -> tuple[list[tuple], tuple[int, int]]:
        """Capture the contents (stone/mark/label) of ``points`` on the current node.

        Returns ``(items, (w, h))`` where each item is
        ``(rx, ry, stone_or_None, mark_prop_or_None, label_or_None)`` relative to
        the selection's top-left corner, and (w, h) is its bounding-box size.
        """
        pts = list(points)
        if not pts:
            return [], (0, 0)
        minx = min(p.x for p in pts)
        miny = min(p.y for p in pts)
        maxx = max(p.x for p in pts)
        maxy = max(p.y for p in pts)
        marks = self.get_marks()
        labels = self.get_labels()
        items: list[tuple] = []
        for p in pts:
            stone = self.board.get(p.x, p.y)
            stone = stone if stone != EMPTY else None
            mark = marks.get(p)
            label = labels.get(p)
            if stone is None and mark is None and label is None:
                continue
            items.append((p.x - minx, p.y - miny, stone, mark, label))
        return items, (maxx - minx + 1, maxy - miny + 1)

    def _in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def _erase_quiet(self, points) -> None:
        cur = self.current
        for p in points:
            self._clear_marks_on(cur, p)
            self._set_label_on(cur, p, None)
        stone_pts = [p for p in points if self.board.get(p.x, p.y) != EMPTY]
        if stone_pts:
            node = self._ensure_setup_node_quiet()
            for p in stone_pts:
                self._setup_point_on(node, p, EMPTY, False)

    def _paste_quiet(self, items, anchor: Point) -> None:
        need_setup = any(it[2] is not None for it in items)
        setup_node = self._ensure_setup_node_quiet() if need_setup else self.current
        mark_node = self.current  # equals setup_node after a quiet ensure
        for rx, ry, stone, mark, label in items:
            x, y = anchor.x + rx, anchor.y + ry
            if not self._in_bounds(x, y):
                continue
            pt = Point(x, y)
            if stone is not None:
                self._setup_point_on(setup_node, pt, stone, False)
            if mark is not None:
                self._set_mark_on(mark_node, pt, mark)
            if label is not None:
                self._set_label_on(mark_node, pt, label)

    def _ensure_setup_node_quiet(self) -> SgfNode:
        cur = self.current
        if cur.has(M.BLACK_MOVE) or cur.has(M.WHITE_MOVE):
            node = SgfNode()
            cur.add_child(node)
            self.current = node
            return node
        return cur

    def apply_erase(self, points) -> None:
        self._erase_quiet(points)
        self._recompute()

    def apply_paste(self, items, anchor: Point) -> None:
        self._paste_quiet(items, anchor)
        self._recompute()

    def apply_move(self, points, dx: int, dy: int) -> None:
        if not points:
            return
        items, _ = self.snapshot_points(points)
        self._erase_quiet(points)
        minx = min(p.x for p in points)
        miny = min(p.y for p in points)
        self._paste_quiet(items, Point(minx + dx, miny + dy))
        self._recompute()

    # -- whole-tree geometric transforms ----------------------------------

    def transform_geometry(self, kind: str) -> None:
        """Geometrically transform every coordinate in the whole game tree.

        ``kind`` is one of ``rot_cw``, ``rot_ccw``, ``flip_h``, ``flip_v``.
        Rotations swap the board's width and height; flips keep them. Every
        node's moves, setup stones, markers, labels, and line/arrow/territory/
        dim/view annotations are remapped, the root ``SZ`` is updated when the
        dimensions change, and the current position is recomputed.
        """
        try:
            remap, swaps_axes = _GEOMETRY_TRANSFORMS[kind]
        except KeyError:
            raise ValueError(f"unknown transform {kind!r}") from None
        w, h = self.width, self.height
        nw, nh = (h, w) if swaps_axes else (w, h)

        def fn(p: Point) -> Point:
            return remap(p, w, h)

        # Remap every coordinate-bearing property on every node. ``fn`` uses the
        # *old* dimensions, so this must run before width/height are updated.
        for node in self.root.walk():
            _map_node_points(node, fn, w, h)

        if (nw, nh) != (w, h):
            self.root.set_one("SZ", str(nw) if nw == nh else f"{nw}:{nh}")
        self.width, self.height = nw, nh
        self.board = Board(nw, nh)
        self._recompute()

    # -- comments / player-to-move ----------------------------------------

    def get_comment(self, node: SgfNode | None = None) -> str:
        node = node or self.current
        return node.get_one(M.COMMENT) or ""

    def set_comment(self, text: str) -> None:
        if text:
            self.current.set_one(M.COMMENT, text)
        else:
            self.current.remove(M.COMMENT)

    def set_player_to_move(self, color: int | None) -> None:
        """Set or clear the PL property on the current node."""
        if color is None:
            self.current.remove(M.PLAYER)
        else:
            self.current.set_one(M.PLAYER, M.BLACK_MOVE if color == BLACK else M.WHITE_MOVE)
        self._recompute()

    # -- tree edits --------------------------------------------------------

    def delete_node(self, node: SgfNode | None = None) -> None:
        """Delete ``node`` and its subtree. The root cannot be deleted."""
        node = node or self.current
        if node.parent is None:
            raise GameError("cannot delete the root node")
        parent = node.parent
        node.detach()
        self.goto(parent)

    def insert_empty_node(self) -> SgfNode:
        """Insert a child node that edits nothing, and navigate into it."""
        node = SgfNode()
        self.current.add_child(node)
        self.goto(node)
        return node

    def shift_variation(self, direction: int) -> bool:
        """Reorder siblings to move the current line up/down by one position.

        Walks up from the current node to the closest ancestor edge whose
        child-on-the-path can move in ``direction`` (-1 up, +1 down) and swaps it
        with the neighbouring sibling. Repeated shift-up eventually makes the
        current line the global main line. Returns whether anything changed.
        """
        node = self.current
        while node.parent is not None:
            sibs = node.parent.children
            idx = sibs.index(node)
            new_idx = idx + direction
            if 0 <= new_idx < len(sibs):
                sibs[idx], sibs[new_idx] = sibs[new_idx], sibs[idx]
                self._remember_path(self.current)
                self._recompute()
                return True
            node = node.parent
        return False

    def promote_closest(self) -> bool:
        """Rotate the closest off-main ancestor edge of the current line to index 0.

        Unlike :meth:`shift_variation` (which moves by one), this moves the
        nearest diverging branch straight to the front. Returns whether anything
        changed.
        """
        node = self.current
        while node.parent is not None:
            sibs = node.parent.children
            idx = sibs.index(node)
            if idx > 0:
                sibs.pop(idx)
                sibs.insert(0, node)
                self._remember_path(self.current)
                self._recompute()
                return True
            node = node.parent
        return False

    # -- subtree cut / copy / paste ---------------------------------------

    def subtree_contains(self, root: SgfNode, target: SgfNode) -> bool:
        return any(n is target for n in root.walk())

    def can_transplant(self, source: SgfNode, target: SgfNode) -> bool:
        """A subtree may be pasted only outside itself (and not the root source)."""
        if source.parent is None:
            return False
        return not self.subtree_contains(source, target)

    def cut_subtree(self, source: SgfNode, target: SgfNode) -> SgfNode:
        """Move ``source`` (and its subtree) to be a new child of ``target``.

        Does not change the current node (the caller stays on the target).
        """
        if not self.can_transplant(source, target):
            raise GameError("cannot move a subtree into itself or the root")
        source.detach()
        target.add_child(source)
        self._recompute()
        return source

    def copy_subtree(self, source: SgfNode, target: SgfNode) -> SgfNode:
        """Deep-copy ``source`` as a new child of ``target`` (current unchanged)."""
        if self.subtree_contains(source, target):
            raise GameError("cannot copy a subtree into itself")
        clone = source.clone()
        target.add_child(clone)
        self._recompute()
        return clone

    def attach_subtree(self, subtree: SgfNode, target: SgfNode) -> SgfNode:
        """Attach an externally-supplied subtree (already detached) under target."""
        target.add_child(subtree)
        self._recompute()
        return subtree

    # -- single mark set/clear (for paint-style dragging) -----------------

    def set_mark(self, point: Point, mark: M.MarkType) -> None:
        self._set_mark_on(self.current, point, mark.value)
        self._recompute()

    def clear_mark(self, point: Point) -> None:
        self._clear_marks_on(self.current, point)
        self._recompute()
