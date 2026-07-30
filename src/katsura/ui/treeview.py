"""The graphical variation tree.

Columns are depth from the root. Two row-layout modes:

* **compact** (default): a greedy per-column packer. A node sits at its parent's
  row when free, otherwise just below the lowest node already placed in that
  column. An early variation therefore stays right under the main line and only
  drops lower if it would actually collide with something — it does not reserve
  space for a depth it may never reach.
* **centered**: the remembered ("golden") line is drawn straight along a centre
  row; branches that come *later* in an in-order traversal splay diagonally
  down, branches *earlier* splay diagonally up. This keeps the local branch
  structure of the current line readable even in exponentially large trees.

The canvas paints the whole tree inside a :class:`QScrollArea`. Up/Down move to a
node at the **same depth** in an adjacent line (so the jump is reversible and
never throws you far forward/back).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QScrollArea, QWidget

from ..model import markup as M
from ..model.game import Game
from ..sgf.tree import SgfNode

CELL = 26
RADIUS = 9
MARGIN = 16

CURRENT_HUE = QColor(180, 150, 255)   # lavender: the node actually being viewed
# The remembered line's gold is kept slightly dull so it never competes with
# the lavender current node for attention.
GOLDEN = QColor(232, 195, 88)
BACKDROP = QColor(45, 47, 52)         # tree canvas + viewport fill (uniform)


def _node_color(node: SgfNode):
    """Return a fill colour describing what kind of node this is."""
    if node.has(M.BLACK_MOVE):
        return QColor(30, 30, 30), True
    if node.has(M.WHITE_MOVE):
        return QColor(235, 235, 235), True
    if node.has(M.ADD_BLACK) or node.has(M.ADD_WHITE) or node.has(M.ADD_EMPTY):
        return QColor(90, 150, 90), False  # setup node
    return QColor(120, 130, 150), False    # empty / root / annotation node


class _TreeCanvas(QWidget):
    nodeSelected = Signal(object)  # SgfNode

    def __init__(self, parent=None):
        super().__init__(parent)
        self.game: Game | None = None
        self.centered = False
        self.marked_ids: set[int] = set()        # subtree marked for cut/copy
        self.marked_color: QColor | None = None
        self._pos: dict[int, tuple[int, int]] = {}   # id(node) -> (col, row) for *display*
        self._nodes: dict[int, SgfNode] = {}
        self._movenum: dict[int, int] = {}           # id(node) -> move number (0 if not a move)
        self._by_row: dict[int, list[tuple[int, SgfNode]]] = {}
        # (col, row) -> node for the *display* layout, for O(1) click hit-testing.
        self._cell_index: dict[tuple[int, int], SgfNode] = {}
        # Per-subtree display extent (max col, min row, max row) keyed by id(root),
        # so paint can prune whole off-screen branches; see _compute_subtree_bounds.
        self._bounds: dict[int, tuple[int, int, int]] = {}
        # Cached golden-line membership for the *display*, invalidated when the
        # current node or the layout changes, so paint need not rebuild it.
        self._remembered: set[int] = set()
        self._remembered_id: int | None = None
        # Signature of the golden line the centred layout was built for, so we can
        # skip a relayout when navigation stays on the same line.
        self._layout_golden_sig: tuple[int, ...] = ()
        # A stable, current-independent compact layout used ONLY for keyboard
        # navigation, so Up/Down behave the same regardless of the display mode.
        self._nav_pos: dict[int, tuple[int, int]] = {}
        self._nav_by_row: dict[int, list[tuple[int, SgfNode]]] = {}
        self._row_offset = 0
        self._pad_top = 0          # extra top padding used to vertically centre the spine
        self._content_h = 0
        self._cols = 1
        self._rows = 1
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

    def set_game(self, game: Game | None) -> None:
        self.game = game
        self._relayout()
        self.update()

    def set_centered(self, on: bool) -> None:
        self.centered = on
        self._relayout()
        self.update()

    def set_marked(self, ids: set[int], color: QColor | None) -> None:
        self.marked_ids = ids
        self.marked_color = color
        self.update()

    # -- layout ------------------------------------------------------------

    def _golden_nodes(self) -> list[SgfNode]:
        g = self.game
        spine: list[SgfNode] = []
        n: SgfNode | None = g.current
        ancestors = []
        while n is not None:
            ancestors.append(n)
            n = n.parent
        spine.extend(reversed(ancestors))
        n = g.current
        while True:
            nxt = g.preferred_child(n)
            if nxt is None:
                break
            spine.append(nxt)
            n = nxt
        return spine

    def _relayout(self) -> None:
        self._pos.clear()
        self._nodes.clear()
        self._by_row.clear()
        self._cell_index.clear()
        self._movenum.clear()
        self._nav_pos = {}
        self._nav_by_row = {}
        if self.game is None:
            self.resize(QSize(50, 50))
            return
        # Always build the stable compact layout as the navigation reference
        # (it is a pure function of the tree structure, never of the current
        # node), so keyboard Up/Down is consistent and reversible regardless of
        # whether the display is compact or centred.
        self._compute_compact(self._nav_pos)
        for nid, (c, r) in self._nav_pos.items():
            self._nav_by_row.setdefault(r, []).append((c, self._nodes[nid]))

        if self.centered:
            self._layout_centered()
        else:
            self._pos = dict(self._nav_pos)
        self._compute_move_numbers()
        rows = [r for _, r in self._pos.values()]
        cols = [c for c, _ in self._pos.values()]
        min_row = min(rows)
        self._row_offset = -min_row
        self._rows = max(rows) - min_row + 1
        self._cols = max(cols) + 1
        self._pad_top = 0
        for nid, (c, r) in self._pos.items():
            self._by_row.setdefault(r, []).append((c, self._nodes[nid]))
            self._cell_index[(c, r)] = self._nodes[nid]
        self._content_h = self._rows * CELL + 2 * MARGIN
        self.setMinimumSize(self._cols * CELL + 2 * MARGIN, self._content_h)
        self.resize(self.minimumSize())
        self._compute_subtree_bounds()
        self._remembered_id = None          # force the golden cache to rebuild
        self._layout_golden_sig = (
            tuple(id(n) for n in self._golden_nodes()) if self.centered else ())

    def spine_y(self) -> float:
        """Pixel y of the central (row-0) line in canvas coordinates."""
        return self._pad_top + MARGIN + self._row_offset * CELL + CELL / 2

    def apply_vcenter(self, viewport_h: int) -> float:
        """Pad the canvas so the spine can sit at the viewport's vertical centre.

        Returns the spine's y after padding. Keeps the central line at a fixed
        screen position regardless of how the tree grows above or below it.
        """
        base_spine = MARGIN + self._row_offset * CELL + CELL / 2
        above = base_spine
        below = self._content_h - base_spine
        pad_top = max(0, viewport_h // 2 - int(above))
        pad_bottom = max(0, viewport_h // 2 - int(below))
        self._pad_top = pad_top
        total_h = self._content_h + pad_top + pad_bottom
        self.setMinimumHeight(total_h)
        self.resize(self.width(), total_h)
        return self.spine_y()

    def _principal_path(self) -> list[SgfNode]:
        path: list[SgfNode] = []
        n: SgfNode | None = self.game.root
        while n is not None:
            path.append(n)
            n = n.children[0] if n.children else None
        return path

    def _compute_compact(self, target: dict) -> None:
        # The regular (and navigation) layout = the unified layout centred on the
        # principal variation (root via first children).
        self._layout(self._principal_path(), target)

    def _layout_centered(self) -> None:
        # The centred display = the same unified layout, centred on the golden
        # (active) variation instead of the principal one.
        self._layout(self._golden_nodes(), self._pos)

    def _layout(self, center_path: list[SgfNode], target: dict) -> None:
        """The single layout algorithm used by BOTH views.

        ``center_path`` (the "centre variation": principal for the regular view,
        golden for the centred view) is drawn straight on row 0. Everything else
        is a subtree hanging off a centre node, either *above* (it branches at a
        lower child index than the centre continuation — earlier in in-order) or
        *below* (higher index — later). We walk outward from the centre variation
        in in-order (backwards above, forwards below) and place each run at the
        row one step further out than the outermost row already consumed in any
        column the run spans, tracked by a per-column envelope. Above subtrees
        decompose into runs by the maximal-index child (the part nearest the
        centre line), below subtrees by the first (0-index) child. The two views
        are therefore identical whenever the golden line is the principal line.
        """
        below_env: dict[int, int] = {}
        above_env: dict[int, int] = {}
        for col, node in enumerate(center_path):
            self._nodes[id(node)] = node
            target[id(node)] = (col, 0)
            below_env[col] = 0
            above_env[col] = 0

        def place(node: SgfNode, col: int, direction: int) -> None:
            # Iterative DFS in exactly the old recursion's pre-order (the
            # envelopes are mutable state, so placement order is semantics):
            # a run is placed, then its branches deepest-run-node-first, each
            # fully explored before the next. Explicit stack because branch
            # nesting can track path depth in a dense tree (recursion limit).
            work = [(node, col)]
            while work:
                n0, c0 = work.pop()
                run: list[tuple[SgfNode, int]] = []
                n, c = n0, c0
                while n is not None:
                    self._nodes[id(n)] = n
                    run.append((n, c))
                    # Below follows the first child; above follows the last
                    # child (the part of the subtree nearest the centre line).
                    n = (n.children[0] if direction > 0 else n.children[-1]) \
                        if n.children else None
                    c += 1
                cols = [cc for _, cc in run]
                if direction > 0:
                    row = max((below_env.get(cc, 0) for cc in cols), default=0) + 1
                else:
                    row = min((above_env.get(cc, 0) for cc in cols), default=0) - 1
                for nn, cc in run:
                    target[id(nn)] = (cc, row)
                    (below_env if direction > 0 else above_env)[cc] = row
                # Branch into the run nodes' other children, deepest run node
                # first (in-order), nearest-the-run sibling first. Pushed in
                # reverse so the stack pops them in that order.
                branches = [(child, cc + 1)
                            for nn, cc in reversed(run)
                            for child in (nn.children[1:] if direction > 0
                                          else nn.children[-2::-1])]
                work.extend(reversed(branches))

        # Process centre nodes deepest-first (matches walking outward in in-order).
        for i in range(len(center_path) - 1, -1, -1):
            node = center_path[i]
            center_child = center_path[i + 1] if i + 1 < len(center_path) else None
            if center_child is not None:
                k = node.children.index(center_child)
                below_children = node.children[k + 1:]   # increasing index: nearest first
                above_children = node.children[:k]
            else:
                below_children = list(node.children)     # leaf continuations go below
                above_children = []
            for child in below_children:
                place(child, i + 1, +1)
            for child in reversed(above_children):       # decreasing index: nearest first
                place(child, i + 1, -1)

    def _compute_move_numbers(self) -> None:
        # Explicit stack (like _compute_subtree_bounds): a long game record
        # would overflow the recursion limit on load.
        stack: list[tuple[SgfNode, int]] = [(self.game.root, 0)]
        while stack:
            node, m = stack.pop()
            is_move = node.has(M.BLACK_MOVE) or node.has(M.WHITE_MOVE)
            nm = m + (1 if is_move else 0)
            self._movenum[id(node)] = nm if is_move else 0
            for ch in node.children:
                stack.append((ch, nm))

    def _compute_subtree_bounds(self) -> None:
        """Record each subtree's display extent, keyed by id(subtree root).

        Stored as ``(max_col, min_row, max_row)``; the min col is always the
        root's own col, since col == depth grows by one per child. Rows splay
        both ways, so we keep both ends. Because every connector joins a node to
        one of its children — both inside that node's subtree — a subtree's box
        bounds all of its nodes *and* all of its connectors. Paint can therefore
        skip an entire branch the instant its box is disjoint from the viewport,
        with no heuristic margin. Done once per relayout (post-order, explicit
        stack so a deep line can't overflow the recursion limit).
        """
        self._bounds = {}
        if self.game is None:
            return
        stack: list[tuple[SgfNode, bool]] = [(self.game.root, False)]
        while stack:
            node, processed = stack.pop()
            if not processed:
                stack.append((node, True))
                for ch in node.children:
                    stack.append((ch, False))
                continue
            col, row = self._pos[id(node)]
            max_c, min_r, max_r = col, row, row
            for ch in node.children:
                c_max_c, c_min_r, c_max_r = self._bounds[id(ch)]
                max_c = max(max_c, c_max_c)
                min_r = min(min_r, c_min_r)
                max_r = max(max_r, c_max_r)
            self._bounds[id(node)] = (max_c, min_r, max_r)

    def _cell_center(self, col: int, row: int) -> QPointF:
        r = row + self._row_offset
        return QPointF(MARGIN + col * CELL + CELL / 2,
                       self._pad_top + MARGIN + r * CELL + CELL / 2)

    def _subtree_rect(self, nid: int) -> QRectF:
        """Pixel bounding box of the subtree rooted at ``nid`` (see
        :meth:`_compute_subtree_bounds`). Uses whole cells, so it already covers
        every glyph and connector inside; padded by the stroke half-width so a
        box that merely grazes the viewport edge still tests as intersecting."""
        col = self._pos[nid][0]
        max_c, min_r, max_r = self._bounds[nid]
        top = self._pad_top + MARGIN
        x0 = MARGIN + col * CELL
        x1 = MARGIN + (max_c + 1) * CELL
        y0 = top + (min_r + self._row_offset) * CELL
        y1 = top + (max_r + self._row_offset + 1) * CELL
        return QRectF(x0 - 2, y0 - 2, (x1 - x0) + 4, (y1 - y0) + 4)

    def current_center(self) -> QPointF | None:
        if self.game is None:
            return None
        cr = self._pos.get(id(self.game.current))
        if cr is None:
            return None
        return self._cell_center(*cr)

    def _remembered_ids(self) -> set[int]:
        return {id(n) for n in self._golden_nodes()}

    def _remembered_set(self) -> set[int]:
        """The golden-line set, rebuilt only when the current node changes (or the
        layout was rebuilt, which resets ``_remembered_id``). Repaints from mere
        scrolling reuse it untouched."""
        cur = id(self.game.current)
        if self._remembered_id != cur:
            self._remembered = self._remembered_ids()
            self._remembered_id = cur
        return self._remembered

    def golden_layout_stale(self) -> bool:
        """Whether a centred relayout is actually needed for the current node.

        The centred layout is a pure function of the golden line, so navigating
        *along* that same line (its node set unchanged) needs only a repaint, not
        a full O(n) relayout. Always ``False`` in compact mode, whose layout does
        not depend on the current node at all."""
        if not self.centered or self.game is None:
            return False
        return tuple(id(n) for n in self._golden_nodes()) != self._layout_golden_sig

    # -- painting ----------------------------------------------------------

    def _collect_visible(
        self, vis: QRectF
    ) -> tuple[list[tuple[SgfNode, SgfNode, QPointF, QPointF]],
               list[tuple[int, SgfNode, QPointF]]]:
        """Cull the tree to the exposed rect ``vis``, returning what to paint.

        ``(connectors, nodes)`` where each connector is ``(parent, child, a, b)``
        (a/b being the cell centres) and each node is ``(id, node, centre)``. We
        descend the tree and skip any subtree whose box (see
        :meth:`_compute_subtree_bounds`) is disjoint from ``vis``, turning paint
        from O(all nodes) into O(visible). A connector is tested by its own
        two-cell box, *independently* of whether we recurse into the child: that
        box can straddle the viewport even when both endpoints are off-screen,
        but it is always contained in the parent's subtree box, so a connector
        that meets ``vis`` is never reached via a pruned parent. Pure (no
        painting), so the culling can be unit-tested against a brute-force scan.
        """
        half = CELL / 2 + 2          # half-cell + stroke: bounds circle, ring, badges
        conns: list[tuple[SgfNode, SgfNode, QPointF, QPointF]] = []
        nodes: list[tuple[int, SgfNode, QPointF]] = []
        stack: list[SgfNode] = [self.game.root]
        while stack:
            node = stack.pop()
            nid = id(node)
            pos = self._pos.get(nid)
            if pos is None:
                continue
            a = self._cell_center(*pos)
            if vis.intersects(QRectF(a.x() - half, a.y() - half, 2 * half, 2 * half)):
                nodes.append((nid, node, a))
            for ch in node.children:
                cid = id(ch)
                cpos = self._pos.get(cid)
                if cpos is None or cid not in self._bounds:
                    # A node added since the last layout (an edit always
                    # relayouts right after, so this is a single frame at
                    # worst). Skipping beats raising out of a paint event.
                    continue
                b = self._cell_center(*cpos)
                conn = QRectF(min(a.x(), b.x()) - 2, min(a.y(), b.y()) - 2,
                              abs(b.x() - a.x()) + 4, abs(b.y() - a.y()) + 4)
                if vis.intersects(conn):
                    conns.append((node, ch, a, b))
                if vis.intersects(self._subtree_rect(cid)):
                    stack.append(ch)
        return conns, nodes

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), BACKDROP)
        if self.game is None:
            return
        remembered = self._remembered_set()
        # Qt clips the paint event to the exposed region of this (scrolled) canvas,
        # so event.rect() is exactly the viewport area we must cover.
        conns, visible = self._collect_visible(QRectF(event.rect()))

        # Connectors first, so every node circle lands on top of them.
        lit_pen = QPen(GOLDEN, 2.6)
        dim_pen = QPen(QColor(150, 155, 165), 2.0)
        for node, ch, a, b in conns:
            lit = id(node) in remembered and id(ch) in remembered
            p.setPen(lit_pen if lit else dim_pen)
            if b.y() == a.y():
                p.drawLine(a, b)
            else:
                mid = QPointF(b.x() - CELL / 2, a.y())
                p.drawLine(a, mid)
                p.drawLine(mid, b)

        current_id = id(self.game.current)
        # A soft lavender halo behind the viewed node (under every circle), so
        # it stands out from the golden line at a glance. The centre is hidden
        # by the node itself; only the soft annulus around it shows.
        cur_pos = self._pos.get(current_id)
        if cur_pos is not None:
            c = self._cell_center(*cur_pos)
            halo_r = RADIUS * 2.0
            grad = QRadialGradient(c, halo_r)
            for stop, alpha in ((0.0, 110), (0.55, 45), (1.0, 0)):
                col = QColor(CURRENT_HUE)
                col.setAlpha(alpha)
                grad.setColorAt(stop, col)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawEllipse(c, halo_r, halo_r)
        num_font = QFont()
        num_font.setPointSizeF(6.5)
        for nid, node, c in visible:
            fill, _is_move = _node_color(node)
            # Cut/copy mark ring underneath.
            if nid in self.marked_ids and self.marked_color is not None:
                p.setPen(QPen(self.marked_color, 3.0))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, RADIUS + 3.5, RADIUS + 3.5)
            if nid == current_id:
                p.setPen(QPen(CURRENT_HUE, 3.4))
            elif nid in remembered:
                p.setPen(QPen(GOLDEN, 1.4))
            else:
                p.setPen(QPen(QColor(20, 20, 20), 1.0))
            p.setBrush(QBrush(fill))
            p.drawEllipse(c, RADIUS, RADIUS)
            mv = self._movenum.get(nid, 0)
            if mv:
                # Tiny move number on the stone-coloured node.
                light = fill.lightness() < 128
                p.setPen(QColor(235, 235, 235) if light else QColor(25, 25, 25))
                p.setFont(num_font)
                fm = p.fontMetrics()
                s = str(mv)
                p.drawText(QPointF(c.x() - fm.horizontalAdvance(s) / 2,
                                   c.y() + fm.ascent() / 2 - 0.5), s)
            elif nid == current_id:
                # Inner lavender dot so the viewed (non-move) node is unmistakable.
                p.setPen(Qt.NoPen)
                p.setBrush(CURRENT_HUE)
                p.drawEllipse(c, RADIUS * 0.42, RADIUS * 0.42)
            self._draw_node_badges(p, node, c)

    def _draw_node_badges(self, p: QPainter, node: SgfNode, c: QPointF) -> None:
        if node.has(M.COMMENT):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(220, 120, 60))
            p.drawEllipse(QPointF(c.x() + RADIUS * 0.7, c.y() - RADIUS * 0.7), 2.6, 2.6)
        pl = node.get_one(M.PLAYER)
        if pl is not None:
            v = pl.strip().upper()
            is_black = v in ("B", "1")
            p.setPen(QPen(QColor(120, 120, 120), 0.8))
            p.setBrush(QColor(20, 20, 20) if is_black else QColor(240, 240, 240))
            p.drawEllipse(QPointF(c.x() + RADIUS * 0.95, c.y() + RADIUS * 0.7), 3.2, 3.2)

    # -- input -------------------------------------------------------------

    def _node_at(self, x: float, y: float) -> SgfNode | None:
        # The hit radius (RADIUS + 4 = CELL/2) fits inside a single cell, so a
        # click can only land on the nearest cell's node — round to it and check
        # just that one, instead of scanning every node.
        col = round((x - MARGIN - CELL / 2) / CELL)
        row = round((y - self._pad_top - MARGIN - CELL / 2) / CELL) - self._row_offset
        node = self._cell_index.get((col, row))
        if node is None:
            return None
        center = self._cell_center(col, row)
        if (center.x() - x) ** 2 + (center.y() - y) ** 2 <= (RADIUS + 4) ** 2:
            return node
        return None

    def vertical_neighbor(self, direction: int) -> SgfNode | None:
        """Node at the SAME depth in the nearest line above/below.

        Uses the stable navigation layout (not the possibly-centred display
        layout), so the jump is a pure function of the SGF structure: it never
        moves you forward or backward in the game — only sideways — and Up after
        Down returns you where you started. Returns ``None`` if no adjacent line
        has a node at the current depth.
        """
        if self.game is None:
            return None
        cr = self._nav_pos.get(id(self.game.current))
        if cr is None:
            return None
        c, r = cr
        order = sorted((rr for rr in self._nav_by_row
                        if (rr < r if direction < 0 else rr > r)),
                       reverse=direction < 0)
        for rr in order:
            for col, node in self._nav_by_row[rr]:
                if col == c:
                    return node
        return None

    def mousePressEvent(self, event) -> None:
        self.setFocus()
        node = self._node_at(event.position().x(), event.position().y())
        if node is not None:
            self.nodeSelected.emit(node)

    # No keyPressEvent: navigation keys are dispatched by MainWindow's
    # application-level filter (EditorTab.handle_key) before any widget sees
    # them, so a handler here would be dead code pretending to be the second
    # half of the story.


class VariationTree(QScrollArea):
    """Scrollable container around the tree canvas."""

    nodeSelected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.canvas = _TreeCanvas()
        self.setWidget(self.canvas)
        self.setWidgetResizable(False)
        self.canvas.nodeSelected.connect(self.nodeSelected)
        self.setMinimumHeight(80)
        # Fill the whole viewport (not just the content-sized canvas) with the
        # canvas backdrop, so the panel is one uniform colour rather than fading
        # to the OS window colour past the drawn area.
        self.setStyleSheet(
            f"QScrollArea {{ border: none; }} "
            f"QScrollArea > QWidget > QWidget {{ background: {BACKDROP.name()}; }}")
        self.viewport().setStyleSheet(f"background: {BACKDROP.name()};")

    def set_game(self, game: Game | None) -> None:
        self.canvas.set_game(game)
        self.scroll_to_current()

    def set_centered(self, on: bool) -> None:
        self.canvas.set_centered(on)
        self.scroll_to_current()

    def set_marked(self, ids: set[int], color) -> None:
        self.canvas.set_marked(ids, color)

    def refresh(self) -> None:
        self.canvas.set_game(self.canvas.game)
        self.scroll_to_current()

    def vertical_neighbor(self, direction: int) -> SgfNode | None:
        return self.canvas.vertical_neighbor(direction)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.canvas.centered:
            self.scroll_to_current()

    def scroll_to_current(self) -> None:
        if self.canvas.centered and self.canvas.game is not None:
            vh = self.viewport().height()
            spine_y = self.canvas.apply_vcenter(vh)
            self.verticalScrollBar().setValue(int(spine_y - vh / 2))
            cc = self.canvas.current_center()
            if cc is not None:
                vw = self.viewport().width()
                self.horizontalScrollBar().setValue(int(cc.x() - vw / 2))
            return
        center = self.canvas.current_center()
        if center is not None:
            self.ensureVisible(int(center.x()), int(center.y()),
                               xmargin=CELL * 3, ymargin=CELL * 3)
