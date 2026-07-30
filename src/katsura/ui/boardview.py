"""The Go board widget: custom-painted, with mouse and keyboard input."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QBrush,
    QRadialGradient,
    QFont,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication, QWidget

from ..engine.coords import column_label
from ..go.board import BLACK, WHITE, EMPTY, opponent
from ..model import markup as M
from ..model.game import Game
from ..model.markup import MarkType
from ..sgf.coords import Point, sgf_to_move
from .modes import EditMode, MODE_TO_MARK
from .settings import Prefs

# Tools whose clicks "paint": a press starts a stroke that drags continuously.
_PAINT_MODES = {EditMode.SETUP, EditMode.MARK_TRIANGLE, EditMode.MARK_SQUARE,
                EditMode.MARK_CIRCLE, EditMode.MARK_CROSS}

# Colour scale for analysis candidate discs, indexed by a move's weight relative
# to the strongest move's weight (0..1). Piecewise-linear over (t, RGBA) points.
_WEIGHT_SCALE = [
    (0.00, (150, 10, 10, 15)),
    (0.30, (200, 40, 40, 240)),
    (0.40, (230, 60, 40, 255)),
    (0.53, (255, 100, 40, 255)),
    (0.80, (205, 220, 40, 255)),
    (0.92, (120, 235, 90, 255)),
    (1.00, (100, 250, 140, 255)),
]
# The order-0 (engine-preferred) move always gets this fill, regardless of weight.
_ORDER0_FILL = (100, 240, 255, 255)
# When the order-0 move is beaten, these mark it and its challengers.
_BEATEN_BORDER = (220, 40, 40)        # thin red on a beaten order-0 move
_BETTER_BORDER = (40, 110, 255)       # thin blue on a move that beats order-0
# How far a move must beat the order-0 move to be flagged (red on order-0, blue
# on the challenger). One margin per axis, in that axis's own units — a tenth of
# a percent of winrate, or a tenth of a point of score. Deliberately sensitive:
# the flag is meant to catch *any* real disagreement between the search's choice
# and its own numbers. (A single 0.1 used to serve both, which read as 10% on
# the winrate axis.)
_BETTER_WINRATE_MARGIN = 0.001        # winrate is a fraction, so 0.001 = 0.1%
_BETTER_SCORE_MARGIN = 0.1            # points


# Ownership heatmap scale: t in [0,1] maps Black-owned (dark) -> White-owned
# (light). t = (white_ownership + 1) / 2.
_OWNERSHIP_SCALE = [
    (0.00, (115, 10, 10, 255)),
    (0.22, (200, 60, 60, 255)),
    (0.36, (220, 120, 120, 255)),
    (0.50, (190, 205, 180, 255)),
    (0.64, (130, 130, 230, 255)),
    (0.80, (80, 80, 255, 255)),
    (1.00, (20, 20, 235, 255)),
]


def _interp_scale(pts, t: float) -> QColor:
    """Interpolate a piecewise-linear (t, RGBA) colour scale at ``t`` in [0, 1]."""
    t = max(0.0, min(1.0, t))
    for i in range(1, len(pts)):
        t1, c1 = pts[i]
        if t <= t1:
            t0, c0 = pts[i - 1]
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return QColor(*[round(c0[k] + (c1[k] - c0[k]) * f) for k in range(4)])
    return QColor(*pts[-1][1])


def _scale_color(t: float) -> QColor:
    """Interpolate the candidate weight colour scale at ``t`` in [0, 1]."""
    return _interp_scale(_WEIGHT_SCALE, t)


def _ownership_color(t: float) -> QColor:
    """Interpolate the ownership heatmap scale at ``t`` in [0, 1]."""
    return _interp_scale(_OWNERSHIP_SCALE, t)


def _fmt_side_score(points: float) -> str:
    """Points as a side-relative score, the way Go results read: ``B+3.5``."""
    return f"{'B' if points >= 0 else 'W'}+{abs(points):.1f}"


def _hoshi_points(n: int) -> list[int]:
    """Return the star-point line indices for an ``n``-line dimension.

    Boards of 9 lines or fewer use the third line (index 2); larger boards use
    the fourth line (index 3). The centre point is included for odd sizes.
    """
    if n < 5:
        return []
    edge = 2 if n <= 9 else 3
    center = (n - 1) // 2
    pts = {edge, n - 1 - edge}
    if n % 2 == 1:
        pts.add(center)
    return sorted(pts)


class BoardView(QWidget):
    """Renders the current position of a :class:`Game` and emits input events."""

    clicked = Signal(object, object)        # Point, Qt.KeyboardModifiers
    rightClicked = Signal(object)           # Point
    navigate = Signal(str)                  # navigation command name
    selectionRect = Signal(object, object, object)  # start Point, end Point, mods
    selectionMove = Signal(int, int)        # dx, dy (drag-move of selection)
    pasteAt = Signal(object)                # Point (paste buffer here)
    paintBegin = Signal(object, object)     # Point, mods (start a paint stroke)
    paintMove = Signal(object)              # Point (continue stroke)
    paintEnd = Signal()                     # end stroke
    analysisReadout = Signal(str)           # status-bar text for the hovered move

    def __init__(self, prefs: Prefs, parent=None):
        super().__init__(parent)
        self.prefs = prefs
        self.game: Game | None = None
        self._analysis = None                   # latest engine Analysis (or None)
        self._raw_nn = None                     # RawNN (kata-raw-nn) or None
        self._policy_mode = False               # 'p': show policy priors, not stats
        self._show_ownership = False            # 'o': ownership heatmap
        # Transient white-perspective ownership from the previous position, drawn
        # only while the current position has no result yet (anti-flicker). Owned
        # by the controller; no connection to the cache or the SGF model.
        self._stale_ownership = None
        self._last_readout = None               # last status-bar readout emitted
        self._hover: Point | None = None
        self.mode = None                        # set by the controller
        self.transient_to_move: int | None = None
        # Selection-tool state (driven by the controller for rendering).
        self.selection: set = set()            # selected Points
        self.paste_active = False
        self.paste_items: list = []
        self.paste_dims = (0, 0)
        self.paste_center = (0, 0)
        self._painting = False
        self._paint_last: Point | None = None
        self._sel_dragging = False
        self._sel_is_move = False
        self._sel_start: Point | None = None
        self._sel_cur: Point | None = None
        self._sel_mods = Qt.NoModifier
        self.setMinimumSize(200, 200)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        # Geometry computed during paint, reused for hit-testing.
        self._cell = 1.0
        self._origin = (0.0, 0.0)  # pixel of intersection (0,0)

    def set_mode(self, mode) -> None:
        self.mode = mode
        self.update()

    def set_selection(self, points) -> None:
        self.selection = set(points)
        self.update()

    def set_paste(self, active: bool, items=None, dims=(0, 0), center=(0, 0)) -> None:
        self.paste_active = active
        self.paste_items = items or []
        self.paste_dims = dims
        self.paste_center = center
        self.update()

    def set_game(self, game: Game | None) -> None:
        self.game = game
        self._hover = None
        self.update()

    def _to_move(self) -> int:
        """The side to play next: the transient (Ctrl+Click) override when set,
        else the game's own side to move. Engine analyses are requested for this
        side, so every overlay perspective flip must key off it too."""
        if self.transient_to_move is not None:
            return self.transient_to_move
        return self.game.to_move if self.game is not None else BLACK

    def set_analysis(self, analysis) -> None:
        """Set (or clear, with ``None``) the engine analysis overlay."""
        self._analysis = analysis
        self.update()
        self._emit_readout()

    def set_raw_nn(self, raw) -> None:
        """Set (or clear, with ``None``) the raw-NN evaluation to display."""
        self._raw_nn = raw
        self.update()
        self._emit_readout()

    def toggle_policy_mode(self) -> None:
        self._policy_mode = not self._policy_mode
        self.update()
        self._emit_readout()

    def toggle_ownership(self) -> None:
        self._show_ownership = not self._show_ownership
        self.update()
        self._emit_readout()        # the hovered point's ownership % appears/goes

    def ownership_shown(self) -> bool:
        return self._show_ownership

    def policy_mode_shown(self) -> bool:
        return self._policy_mode

    def current_ownership_white(self):
        """The white-perspective ownership currently drawable (or None)."""
        return self._ownership_white()

    def set_stale_ownership(self, vals) -> None:
        """Set/clear the transient prior ownership shown until a result arrives."""
        self._stale_ownership = list(vals) if vals else None
        if self._show_ownership:
            self.update()

    def has_raw_nn(self) -> bool:
        return self._raw_nn is not None

    def refresh_readout(self) -> None:
        self._emit_readout()

    # -- geometry ----------------------------------------------------------

    def _compute_geometry(self) -> None:
        g = self.game
        cols = g.width if g else 19
        rows = g.height if g else 19
        pad = 6.0
        lab_total = 1.7 if self.prefs.show_coordinates else 0.1
        lab = lab_total / 2.0
        w = self.width() - 2 * pad
        h = self.height() - 2 * pad
        cell = min(w / (cols + lab_total), h / (rows + lab_total))
        cell = max(cell, 4.0)
        # Centre the board within the widget.
        board_w = (cols + lab_total) * cell
        board_h = (rows + lab_total) * cell
        ox = (self.width() - board_w) / 2 + (lab + 0.5) * cell
        oy = (self.height() - board_h) / 2 + (lab + 0.5) * cell
        self._cell = cell
        self._lab = lab
        self._origin = (ox, oy)

    def _xy(self, i: int, j: int) -> QPointF:
        ox, oy = self._origin
        return QPointF(ox + i * self._cell, oy + j * self._cell)

    def _point_at(self, px: float, py: float) -> Point | None:
        g = self.game
        if g is None:
            return None
        ox, oy = self._origin
        i = round((px - ox) / self._cell)
        j = round((py - oy) / self._cell)
        if 0 <= i < g.width and 0 <= j < g.height:
            # Reject clicks too far from any intersection.
            cx, cy = ox + i * self._cell, oy + j * self._cell
            if math.hypot(px - cx, py - cy) <= self._cell * 0.5:
                return Point(i, j)
        return None

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event) -> None:
        self._compute_geometry()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.fillRect(self.rect(), QColor(self.prefs.background_color))
        g = self.game
        if g is None:
            return
        self._draw_board_surface(p)
        if self._ownership_active():
            self._draw_ownership_cells(p)        # heatmap under the grid/stones
        self._draw_grid(p)
        self._draw_hoshi(p)
        if self.prefs.show_coordinates:
            self._draw_coordinates(p)
        # "Show Move Analysis Info" gates the per-move discs/stats only; the
        # ownership heatmap is independent ('o'), so it stays visible to inspect.
        overlay_on = self._overlay_active()
        raw_on = self._raw_active()
        any_overlay = overlay_on or raw_on
        self._draw_stones(p)
        self._draw_ghosts(p)
        self._draw_edit_halos(p)
        if self.prefs.show_variation_hints and not any_overlay:
            self._draw_variation_hints(p)
        self._draw_markup(p)
        if self.prefs.show_last_move_marker:
            self._draw_last_move(p)
        if raw_on:
            self._draw_raw_policy(p)
        elif overlay_on and self._policy_mode:
            self._draw_policy(p)
        elif overlay_on:
            self._draw_analysis(p)
        self._draw_selection(p)
        self._draw_hover(p)
        self._draw_paste_preview(p)

    def _draw_board_surface(self, p: QPainter) -> None:
        g = self.game
        tl = self._xy(0, 0)
        br = self._xy(g.width - 1, g.height - 1)
        half = self._cell * (self._lab + 0.5)
        rect = QRectF(tl.x() - half, tl.y() - half,
                      (br.x() - tl.x()) + 2 * half, (br.y() - tl.y()) + 2 * half)
        p.fillRect(rect, QColor(self.prefs.board_color))

    def _draw_grid(self, p: QPainter) -> None:
        g = self.game
        pen = QPen(QColor(self.prefs.grid_color))
        pen.setWidthF(max(1.0, self._cell * 0.03))
        p.setPen(pen)
        for i in range(g.width):
            a = self._xy(i, 0)
            b = self._xy(i, g.height - 1)
            p.drawLine(a, b)
        for j in range(g.height):
            a = self._xy(0, j)
            b = self._xy(g.width - 1, j)
            p.drawLine(a, b)

    def _draw_hoshi(self, p: QPainter) -> None:
        g = self.game
        xs = _hoshi_points(g.width)
        ys = _hoshi_points(g.height)
        r = max(1.8, self._cell * 0.09)
        p.setBrush(QBrush(QColor(self.prefs.grid_color)))
        p.setPen(Qt.NoPen)
        for i in xs:
            for j in ys:
                c = self._xy(i, j)
                p.drawEllipse(c, r, r)

    def _draw_coordinates(self, p: QPainter) -> None:
        g = self.game
        p.setPen(QPen(QColor(self.prefs.grid_color)))
        font = QFont()
        font.setPointSizeF(max(6.0, self._cell * 0.32))
        p.setFont(font)
        off = self._cell * (self._lab + 0.5) * 0.62
        for i in range(g.width):
            letter = column_label(i)
            top = self._xy(i, 0)
            self._centered_text(p, top.x(), top.y() - off, letter)
            bot = self._xy(i, g.height - 1)
            self._centered_text(p, bot.x(), bot.y() + off, letter)
        for j in range(g.height):
            num = str(g.height - j)  # row 1 at the bottom (Western convention)
            left = self._xy(0, j)
            self._centered_text(p, left.x() - off, left.y(), num)
            right = self._xy(g.width - 1, j)
            self._centered_text(p, right.x() + off, right.y(), num)

    def _centered_text(self, p: QPainter, cx: float, cy: float, text: str) -> None:
        fm = p.fontMetrics()
        w = fm.horizontalAdvance(text)
        h = fm.height()
        p.drawText(QPointF(cx - w / 2, cy + h / 2 - fm.descent()), text)

    def _stone_radius(self) -> float:
        return self._cell * 0.47

    def _draw_stone(self, p: QPainter, i: int, j: int, color: int, alpha: int = 255) -> None:
        c = self._xy(i, j)
        r = self._stone_radius()
        if color == BLACK:
            base = QColor(30, 30, 30, alpha)
            hi = QColor(110, 110, 110, alpha)
        else:
            base = QColor(225, 225, 225, alpha)
            hi = QColor(255, 255, 255, alpha)
        grad = QRadialGradient(c.x() - r * 0.35, c.y() - r * 0.35, r * 1.6)
        grad.setColorAt(0.0, hi)
        grad.setColorAt(1.0, base)
        p.setBrush(QBrush(grad))
        if color == WHITE:
            p.setPen(QPen(QColor(80, 80, 80, alpha), max(1.0, r * 0.04)))
        else:
            p.setPen(Qt.NoPen)
        p.drawEllipse(c, r, r)

    def _draw_stones(self, p: QPainter) -> None:
        g = self.game
        board = g.board
        show_numbers = self.prefs.show_move_numbers
        numbers = self._move_numbers() if show_numbers else {}
        for j in range(g.height):
            for i in range(g.width):
                col = board.get(i, j)
                if col == EMPTY:
                    continue
                self._draw_stone(p, i, j, col)
                if show_numbers and (i, j) in numbers:
                    self._draw_stone_number(p, i, j, numbers[(i, j)], col)

    def _move_numbers(self) -> dict[tuple[int, int], int]:
        """Map intersections to move numbers along the path to the current node."""
        g = self.game
        result: dict[tuple[int, int], int] = {}
        path = g.path_to_root(g.current)
        n = 0
        for node in path:
            for prop in (M.BLACK_MOVE, M.WHITE_MOVE):
                mv = node.get_one(prop)
                if mv is not None:
                    n += 1
                    pt = sgf_to_move(mv, g.width, g.height)
                    if pt is not None:
                        result[(pt.x, pt.y)] = n
        return result

    def _draw_stone_number(self, p: QPainter, i: int, j: int, num: int, color: int) -> None:
        c = self._xy(i, j)
        p.setPen(QPen(QColor(255, 255, 255) if color == BLACK else QColor(20, 20, 20)))
        font = QFont()
        font.setPointSizeF(max(5.0, self._cell * (0.30 if num < 100 else 0.24)))
        font.setBold(True)
        p.setFont(font)
        self._centered_text(p, c.x(), c.y(), str(num))

    def _draw_last_move(self, p: QPainter) -> None:
        g = self.game
        pt = g.last_move_point
        if pt is None:
            return
        c = self._xy(pt.x, pt.y)
        if g.last_move_illegal:
            # The move was skipped; mark the attempted point with a red ring + X
            # so it is clearly visible that an illegal move was not played.
            r = self._stone_radius() * 0.7
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(220, 40, 40), max(1.8, self._cell * 0.06)))
            p.drawEllipse(c, r, r)
            d = r * 0.7
            p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
            p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
            return
        col = g.board.get(pt.x, pt.y)
        if col == EMPTY:
            return
        r = self._stone_radius() * 0.45
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(220, 40, 40) if col == BLACK else QColor(200, 30, 30),
                      max(1.5, self._cell * 0.05)))
        p.drawEllipse(c, r, r)

    def _draw_ghosts(self, p: QPainter) -> None:
        """Draw setup stones the legality sweep removed as faint, half-transparent
        stones.

        A zero-liberty setup stone is empty on the legal board but still *present*
        in the SGF setup layer; drawing it at ~50% opacity keeps it distinct from
        a truly empty point (and from a live stone). See ``docs/MODEL.md``.
        """
        g = self.game
        for pt, color in g.setup_ghosts.items():
            self._draw_stone(p, pt.x, pt.y, color, alpha=128)

    def _draw_edit_halos(self, p: QPainter) -> None:
        """Mark every point that carries an AB/AW/AE specifier on this node.

        The decision keys off the *specifier*, not the legal board: AB/AW (a
        stone is set) get a gold ring framing the real or ghost stone; AE (a
        point is emptied) gets a translucent gold disc. Drawn for *all* recorded
        specifiers, including redundant (forced) ones, so the node's explicit
        edits are always visible.
        """
        g = self.game
        setups = g.get_setup_points()
        if not setups:
            return
        sr = self._stone_radius()
        for pt, spec in setups.items():
            c = self._xy(pt.x, pt.y)
            if spec != EMPTY:
                p.setPen(QPen(QColor(255, 205, 40), max(1.8, self._cell * 0.07)))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, sr * 1.06, sr * 1.06)
            else:
                p.setPen(QPen(QColor(255, 205, 40, 200), max(1.2, self._cell * 0.04)))
                p.setBrush(QColor(255, 215, 90, 80))
                p.drawEllipse(c, sr * 0.7, sr * 0.7)

    def _draw_variation_hints(self, p: QPainter) -> None:
        """Mark on the board where the current node's child moves are."""
        g = self.game
        if len(g.current.children) < 1:
            return
        r = self._stone_radius() * 0.5
        for child in g.current.children:
            for prop in (M.BLACK_MOVE, M.WHITE_MOVE):
                mv = child.get_one(prop)
                if mv is None:
                    continue
                pt = sgf_to_move(mv, g.width, g.height)
                if pt is None or g.board.get(pt.x, pt.y) != EMPTY:
                    continue
                c = self._xy(pt.x, pt.y)
                p.setBrush(QColor(60, 90, 200, 90))
                p.setPen(Qt.NoPen)
                p.drawEllipse(c, r, r)

    # -- engine analysis overlay ------------------------------------------

    def _overlay_active(self) -> bool:
        """True when there is an analysis AND the overlay is enabled in prefs."""
        return self._analysis is not None and self.prefs.show_analysis_overlay

    def _raw_active(self) -> bool:
        """True when a raw-NN evaluation is displayable (same pref gate)."""
        return self._raw_nn is not None and self.prefs.show_analysis_overlay

    # -- ownership + policy / raw-nn overlays ------------------------------

    def _ownership_white(self):
        """Per-point ownership in White's perspective ([-1,1], row-major), or None.

        Uses the raw-NN ``whiteOwnership`` in raw mode, else the search's
        ``ownership`` (current-player perspective) converted to White's.
        """
        if self._raw_nn is not None:
            return self._raw_nn.ownership
        a = self._analysis
        if a is not None and a.ownership:
            black_to_move = self.game is not None and self._to_move() == BLACK
            return [-v for v in a.ownership] if black_to_move else a.ownership
        # No live result yet: fall back to the prior position's ownership, if the
        # controller recorded one, so the heatmap doesn't flicker off and back on.
        return self._stale_ownership

    def _ownership_active(self) -> bool:
        return self._show_ownership and self._ownership_white() is not None

    def _draw_ownership_cells(self, p: QPainter) -> None:
        own = self._ownership_white()
        if not own:
            return
        g = self.game
        p.setPen(Qt.NoPen)
        for j in range(g.height):
            for i in range(g.width):
                idx = j * g.width + i
                if idx >= len(own):
                    continue
                # t = (white_ownership + 1) / 2: 0 = Black owns, 1 = White owns.
                p.setBrush(_ownership_color((own[idx] + 1.0) / 2.0))
                p.drawRect(self._cell_rect(i, j))

    def _policy_disc_color(self, ratio: float) -> QColor:
        # Same softened mapping as the candidate discs, keyed by prior/max-prior.
        return _scale_color(1.0 - (1.0 - math.sqrt(max(0.0, ratio))) ** 2)

    def _draw_prior_disc(self, p: QPainter, i: int, j: int, value: float,
                         ratio: float) -> None:
        c = self._xy(i, j)
        fill = self._policy_disc_color(ratio)
        p.setBrush(QBrush(fill))
        edge = fill.darker(125)
        edge.setAlpha(fill.alpha())
        p.setPen(QPen(edge, max(1.0, self._cell * 0.022)))
        p.drawEllipse(c, self._stone_radius(), self._stone_radius())
        ink_a = round(0.2 * 255 + 0.8 * fill.alpha())
        self._draw_prior_label(p, c, value, QColor(0, 0, 0, ink_a))

    def _draw_prior_label(self, p: QPainter, c: QPointF, prior: float,
                          ink: QColor) -> None:
        pct = prior * 100.0
        text = f"{pct:.1f}" if pct >= 1.0 else f"{pct:.2f}"
        p.setPen(QPen(ink))
        f = QFont()
        f.setPointSizeF(max(5.0, self._cell * 0.22))
        f.setBold(True)
        p.setFont(f)
        self._centered_text(p, c.x(), c.y(), text)

    def _draw_policy(self, p: QPainter) -> None:
        """Policy-prior view ('p'): each searched move shows only its prior."""
        a = self._analysis
        g = self.game
        if not a or not a.moves:
            return
        max_p = max((m.prior for m in a.moves), default=0.0)
        for m in a.moves:
            pt = m.point
            if pt is None or not g.board.in_bounds(pt.x, pt.y):
                continue
            if g.board.get(pt.x, pt.y) != EMPTY:
                continue
            ratio = (m.prior / max_p) if max_p > 0 else 0.0
            self._draw_prior_disc(p, pt.x, pt.y, m.prior, ratio)

    def _draw_raw_policy(self, p: QPainter) -> None:
        """Raw-NN policy view (1-8): the raw policy on every legal empty point."""
        raw = self._raw_nn
        g = self.game
        pol = raw.policy
        if not pol:
            return
        max_p = max((v for v in pol if v == v), default=0.0)   # ignore NaN
        if max_p <= 0:
            return
        for j in range(g.height):
            for i in range(g.width):
                idx = j * g.width + i
                if idx >= len(pol):
                    continue
                v = pol[idx]
                if v != v or g.board.get(i, j) != EMPTY:        # NaN = illegal
                    continue
                self._draw_prior_disc(p, i, j, v, v / max_p)

    # -- status-bar readout ------------------------------------------------

    def _black_wr_lead(self, m) -> tuple[float, float]:
        """A move's winrate/lead from Black's perspective."""
        black_to_move = self.game is not None and self._to_move() == BLACK
        wr = m.winrate if black_to_move else 1.0 - m.winrate
        lead = m.score_lead if black_to_move else -m.score_lead
        return wr, lead

    def _black_selfplay_score(self, m) -> float:
        """A move's predicted final score from Black's perspective (points)."""
        black_to_move = self.game is not None and self._to_move() == BLACK
        return m.score_selfplay if black_to_move else -m.score_selfplay

    def _emit_readout(self) -> None:
        text = self._readout_text()
        if text != self._last_readout:
            self._last_readout = text
            self.analysisReadout.emit(text)

    def _readout_text(self) -> str:
        """Compose the status-bar readout: the analysis stats (when move info is
        shown) plus the hovered point's ownership (whenever the heatmap is on —
        that overlay is independent of the 'i' toggle)."""
        parts = []
        if self._raw_active():
            parts.append(self._fmt_raw_readout(self._raw_nn))
        elif self._overlay_active():
            stats = self._fmt_analysis_readout(self._analysis)
            if stats:
                parts.append(stats)
        own = self._fmt_ownership_readout()
        if own:
            parts.append(own)
        return "    ".join(parts)

    def _fmt_analysis_readout(self, a) -> str:
        hov = self._candidate_at(self._hover)
        if hov is not None:
            return self._fmt_move_readout(hov)
        # Not hovering a move: show the pass evaluation if the engine reported it.
        pas = next((m for m in a.moves if m.point is None), None)
        if pas is None:
            return ""
        if self._policy_mode:
            return f"pass    policy {pas.prior * 100:.2f}%"
        return "    ".join(["pass"] + self._move_stat_parts(pas))

    def _fmt_ownership_readout(self) -> str:
        """Black's predicted share of the hovered point, 0–100% (heatmap only)."""
        if not self._show_ownership or self._hover is None or self.game is None:
            return ""
        own = self._ownership_white()
        if not own:
            return ""
        idx = self._hover.y * self.game.width + self._hover.x
        if idx >= len(own):
            return ""
        # White-perspective [-1, 1] -> Black's share of the point, as 0…100%.
        black = max(0.0, min(100.0, (1.0 - own[idx]) / 2.0 * 100.0))
        return f"ownership B {black:.1f}%"

    def _fmt_raw_readout(self, raw) -> str:
        parts = [
            f"raw-nn sym {raw.symmetry}",
            f"win B {raw.black_winrate * 100:.1f}%",
            f"no-result {raw.no_result * 100:.2f}%",
            f"lead {_fmt_side_score(raw.black_lead)}",
            f"score {_fmt_side_score(raw.black_score_selfplay)}"
            f" (std {raw.score_stdev:.1f})",
            f"varTimeLeft {raw.var_time_left:.1f}",
            f"stWinlossErr {raw.shortterm_winloss_error:.3f}",
            f"stScoreErr {raw.shortterm_score_error:.2f}",
            f"policyPass {raw.policy_pass * 100:.2f}%",
        ]
        return "    ".join(parts)

    def _move_stat_parts(self, m) -> list[str]:
        """One candidate's stats as readout fields (Black-perspective).

        Shared by the hovered move and the pass line, so both read identically.
        """
        wr, lead = self._black_wr_lead(m)
        parts = [
            f"win B {wr * 100:.1f}%",
            f"lead {_fmt_side_score(lead)}",
            f"score {_fmt_side_score(self._black_selfplay_score(m))}"
            f" (std {m.score_stdev:.1f})",
            f"visits {m.visits:,}",
            f"weight {m.weight:.0f}",
            f"edgeW {m.edge_weight:.0f}",
            f"policy {m.prior * 100:.2f}%",
        ]
        # Shown whenever the engine reported it at all, rounded like every other
        # field — no hidden threshold below which the number vanishes.
        if m.no_result_value is not None:
            parts.append(f"no-result {m.no_result_value * 100:.2f}%")
        return parts

    def _fmt_move_readout(self, m) -> str:
        return "    ".join([f"Stats for {m.move}"] + self._move_stat_parts(m))

    def _candidate_at(self, pt: Point | None):
        if pt is None or self._analysis is None:
            return None
        for m in self._analysis.moves:
            if m.point == pt:
                return m
        return None

    @staticmethod
    def _fmt_visits(v: int) -> str:
        if v >= 10000:
            return f"{round(v / 1000)}k"
        if v >= 1000:
            return f"{v / 1000:.1f}k"
        return str(v)

    def drawable_candidates(self) -> list[tuple]:
        """The candidates the overlay draws, as ``(move, weight ratio)``.

        A move is drawn only if the search gave it at least
        ``Prefs.analysis_min_weight`` of the top move's weight — below that it is
        left off the board entirely, circle and numbers alike. Between that and
        ``Prefs.analysis_min_label_weight`` it keeps its circle but loses its
        numbers (see :meth:`_labels_candidate`), so the board still shows *where*
        the search looked without a wall of unreadable text. The engine's own
        choice (``order == 0``) is always drawn in full, whatever its weight.
        Moves off the board or on an occupied point are never drawn.
        """
        a = self._analysis
        g = self.game
        if not a or not a.moves or g is None:
            return []
        max_w = max((m.edge_weight for m in a.moves), default=0.0)
        threshold = self.prefs.analysis_min_weight
        out = []
        for m in a.moves:
            pt = m.point
            if pt is None or not g.board.in_bounds(pt.x, pt.y):
                continue
            if g.board.get(pt.x, pt.y) != EMPTY:
                continue
            ratio = (m.edge_weight / max_w) if max_w > 0 else 0.0
            if m.order == 0 or ratio >= threshold:
                out.append((m, ratio))
        return out

    def _labels_candidate(self, m, ratio: float) -> bool:
        """Whether a drawn candidate also gets its winrate/lead/visits text."""
        return m.visits > 0 and (m.order == 0
                                 or ratio >= self.prefs.analysis_min_label_weight)

    def _draw_analysis(self, p: QPainter) -> None:
        a = self._analysis
        if not a or not a.moves:
            return
        best = next((m for m in a.moves if m.order == 0), None)

        # Compare candidates in the side-to-move's own terms (which move is
        # better): flag the order-0 move red if any move beats it by the margin
        # on winrate or score, and blue-border every move that beats it by the
        # margin on either axis.
        beaten = False
        challengers: set = set()
        if best is not None:
            for m in a.moves:
                if m is best:
                    continue
                if (m.winrate - best.winrate >= _BETTER_WINRATE_MARGIN
                        or m.score_lead - best.score_lead >= _BETTER_SCORE_MARGIN):
                    beaten = True
                    challengers.add(id(m))

        # The hovered candidate is redrawn below as a full-opacity stone + stats
        # (optionally with its PV), so skip its flat disc here.
        hov = self._candidate_at(self._hover)

        r = self._stone_radius()
        for m, ratio in self.drawable_candidates():
            if m is hov:
                continue
            pt = m.point
            c = self._xy(pt.x, pt.y)
            if m.order == 0:
                fill = QColor(*_ORDER0_FILL)
            else:
                # Soften the weight ratio with 1-(1-sqrt(x))^2 so weak moves lift
                # off the floor faster while strong moves stay distinct near 1.
                fill = _scale_color(1.0 - (1.0 - math.sqrt(ratio)) ** 2)
            p.setBrush(QBrush(fill))
            if m.order == 0 and beaten:
                p.setPen(QPen(QColor(*_BEATEN_BORDER),
                              max(1.4, self._cell * 0.05)))
            elif id(m) in challengers:
                p.setPen(QPen(QColor(*_BETTER_BORDER),
                              max(1.4, self._cell * 0.05)))
            else:
                # A very thin border in a slightly darkened version of the fill.
                edge = fill.darker(125)
                edge.setAlpha(fill.alpha())
                p.setPen(QPen(edge, max(1.0, self._cell * 0.022)))
            p.drawEllipse(c, r, r)
            # Black text, kept more opaque than the disc (0.2 + 0.8*disc-alpha)
            # so faint discs are still readable.
            if self._labels_candidate(m, ratio):
                ink_a = round(0.2 * 255 + 0.8 * fill.alpha())
                self._draw_candidate_text(p, c, m, QColor(0, 0, 0, ink_a))

        if hov is not None:
            # The full-opacity stone + stats always show on hover; the PV tail is
            # gated on the preference so it can be suppressed on its own.
            if self.prefs.show_pv_on_hover and hov.pv_points:
                self._draw_pv(p, hov)
            else:
                self._draw_hover_candidate(p, hov)

    def _draw_hover_candidate(self, p: QPainter, m) -> None:
        """Draw the hovered candidate as a full-opacity stone with its stats
        (no PV continuation)."""
        g = self.game
        pt = m.point
        if pt is None or not g.board.in_bounds(pt.x, pt.y):
            return
        color = self._to_move()
        self._draw_stone(p, pt.x, pt.y, color, alpha=255)
        c = self._xy(pt.x, pt.y)
        ink = QColor(245, 245, 245) if color == BLACK else QColor(20, 20, 20)
        self._draw_candidate_text(p, c, m, ink)

    def _draw_candidate_text(self, p: QPainter, c: QPointF, m, ink: QColor) -> None:
        # Winrate and lead are always shown from Black's perspective, regardless
        # of whose turn it is (KataGo reports them for the side to move).
        wr, lead = self._black_wr_lead(m)

        p.setPen(QPen(ink))

        wtext = f"{wr * 100:.1f}"
        ltext = f"{lead:+.1f}"
        vtext = self._fmt_visits(m.visits)
        if self._cell > 30:
            f1 = QFont()
            f1.setPointSizeF(max(5.0, self._cell * 0.21))
            f1.setBold(True)
            p.setFont(f1)
            self._centered_text(p, c.x(), c.y() - self._cell * 0.25, wtext)
            f2 = QFont()
            f2.setPointSizeF(max(4.0, self._cell * 0.17))
            p.setFont(f2)
            # Nudge the lower two lines down ~1px (a circle has a touch more room
            # at the bottom than the top once the bold top line is placed).
            self._centered_text(p, c.x(), c.y() - self._cell * 0.01 + 1.0, ltext)
            self._centered_text(p, c.x(), c.y() + self._cell * 0.22 + 1.0, vtext)
        else:
            f1 = QFont()
            f1.setPointSizeF(max(5.0, self._cell * 0.22))
            f1.setBold(True)
            p.setFont(f1)
            self._centered_text(p, c.x(), c.y(), wtext)

    def _draw_pv(self, p: QPainter, m) -> None:
        """Preview a candidate's PV: the first stone shows full-opacity stats,
        the rest are move-numbered."""
        g = self.game
        color = self._to_move()
        seen: set = set()
        for idx, pt in enumerate(m.pv_points):
            if pt is None or not g.board.in_bounds(pt.x, pt.y):
                color = opponent(color)
                continue
            if pt in seen:
                color = opponent(color)
                continue
            seen.add(pt)
            self._draw_stone(p, pt.x, pt.y, color, alpha=255)
            c = self._xy(pt.x, pt.y)
            ink = QColor(245, 245, 245) if color == BLACK else QColor(20, 20, 20)
            if idx == 0:
                # The candidate move itself: full-opacity winrate/lead/visits
                # instead of a "1" (white text when the stone is black).
                self._draw_candidate_text(p, c, m, ink)
            else:
                p.setPen(QPen(ink))
                f = QFont()
                f.setPointSizeF(max(5.0, self._cell * (0.30 if idx + 1 < 100 else 0.24)))
                f.setBold(True)
                p.setFont(f)
                self._centered_text(p, c.x(), c.y(), str(idx + 1))
            color = opponent(color)

    def _draw_markup(self, p: QPainter) -> None:
        g = self.game
        marks = g.get_marks()
        for pt, prop in marks.items():
            self._draw_mark(p, pt, prop)
        for pt, text in g.get_labels().items():
            self._draw_label(p, pt, text)

    def _mark_color(self, pt: Point) -> QColor:
        col = self.game.board.get(pt.x, pt.y)
        if col == BLACK:
            return QColor(245, 245, 245)
        return QColor(20, 20, 20)

    def _draw_mark(self, p: QPainter, pt: Point, prop: str) -> None:
        c = self._xy(pt.x, pt.y)
        r = self._stone_radius() * 0.6
        pen = QPen(self._mark_color(pt), max(1.5, self._cell * 0.05))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if prop == MarkType.CIRCLE.value:
            p.drawEllipse(c, r, r)
        elif prop == MarkType.SQUARE.value:
            p.drawRect(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r))
        elif prop == MarkType.TRIANGLE.value:
            poly = QPolygonF([
                QPointF(c.x(), c.y() - r),
                QPointF(c.x() - r * 0.87, c.y() + r * 0.5),
                QPointF(c.x() + r * 0.87, c.y() + r * 0.5),
            ])
            p.drawPolygon(poly)
        elif prop == MarkType.CROSS.value:
            d = r * 0.8
            p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
            p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
        elif prop == MarkType.SELECTED.value:
            p.setBrush(QColor(120, 160, 255, 130))
            p.drawEllipse(c, r * 0.7, r * 0.7)

    def _draw_label(self, p: QPainter, pt: Point, text: str) -> None:
        c = self._xy(pt.x, pt.y)
        col = self.game.board.get(pt.x, pt.y)
        if col == EMPTY:
            # Draw a small board-coloured disc so the label is readable on grid.
            p.setBrush(QColor(self.prefs.board_color))
            p.setPen(Qt.NoPen)
            r = self._stone_radius() * 0.95
            p.drawEllipse(c, r, r)
            p.setPen(QPen(QColor(self.prefs.label_color)))
        else:
            p.setPen(QPen(QColor(245, 245, 245) if col == BLACK else QColor(20, 20, 20)))
        font = QFont()
        show = text if len(text) <= 3 else text[:3]
        font.setPointSizeF(max(5.0, self._cell * (0.34 if len(show) < 3 else 0.26)))
        font.setBold(True)
        p.setFont(font)
        self._centered_text(p, c.x(), c.y(), show)

    def _cell_rect(self, x: int, y: int) -> QRectF:
        c = self._xy(x, y)
        h = self._cell / 2
        return QRectF(c.x() - h, c.y() - h, self._cell, self._cell)

    def _draw_selection(self, p: QPainter) -> None:
        # Live rubber-band rectangle while dragging a new selection.
        if self._sel_dragging and not self._sel_is_move and self._sel_start and self._sel_cur:
            x0, x1 = sorted((self._sel_start.x, self._sel_cur.x))
            y0, y1 = sorted((self._sel_start.y, self._sel_cur.y))
            p.setPen(QPen(QColor(90, 160, 255), max(1.5, self._cell * 0.04)))
            p.setBrush(QColor(120, 170, 255, 50))
            tl = self._cell_rect(x0, y0)
            br = self._cell_rect(x1, y1)
            p.drawRect(QRectF(tl.left(), tl.top(),
                              br.right() - tl.left(), br.bottom() - tl.top()))

        if not self.selection:
            return
        moving = self._sel_dragging and self._sel_is_move and self._sel_start and self._sel_cur
        if moving:
            dx = self._sel_cur.x - self._sel_start.x
            dy = self._sel_cur.y - self._sel_start.y
            # Fade the original cells (what will move away)…
            for pt in self.selection:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(self.prefs.board_color))
                col = QColor(self.prefs.board_color)
                col.setAlpha(165)
                p.setBrush(col)
                p.drawRect(self._cell_rect(pt.x, pt.y))
            # …and draw a ghost of the actual contents at the destination.
            marks = self.game.get_marks()
            labels = self.game.get_labels()
            for pt in self.selection:
                x, y = pt.x + dx, pt.y + dy
                if not self.game.board.in_bounds(x, y):
                    continue
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(120, 170, 255, 70))
                p.drawRect(self._cell_rect(x, y))
                stone = self.game.board.get(pt.x, pt.y)
                if stone != EMPTY:
                    self._draw_stone(p, x, y, stone, alpha=150)
                if pt in marks:
                    self._draw_mark(p, Point(x, y), marks[pt])
                if pt in labels:
                    self._draw_label(p, Point(x, y), labels[pt])
            return
        p.setPen(QPen(QColor(90, 160, 255), max(1.2, self._cell * 0.035)))
        p.setBrush(QColor(120, 170, 255, 70))
        for pt in self.selection:
            if self.game and self.game.board.in_bounds(pt.x, pt.y):
                p.drawRect(self._cell_rect(pt.x, pt.y))

    def _draw_paste_preview(self, p: QPainter) -> None:
        if not self.paste_active or self._hover is None or not self.paste_items:
            return
        cx, cy = self.paste_center
        ax, ay = self._hover.x - cx, self._hover.y - cy
        for rx, ry, stone, mark, label in self.paste_items:
            x, y = ax + rx, ay + ry
            if not (self.game and self.game.board.in_bounds(x, y)):
                continue
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(120, 170, 255, 50))
            p.drawRect(self._cell_rect(x, y))
            if stone is not None:
                self._draw_stone(p, x, y, stone, alpha=130)
            if mark is not None:
                self._draw_mark(p, Point(x, y), mark)
            if label:
                self._draw_label(p, Point(x, y), label)

    def _draw_hover(self, p: QPainter) -> None:
        if self.paste_active:
            return
        if self._hover is None or self.game is None:
            return
        # Over an analysis candidate, the overlay pass already draws a stone there.
        if self._overlay_active() and self._candidate_at(self._hover) is not None:
            return
        pt = self._hover
        board = self.game.board
        occupied = board.get(pt.x, pt.y) != EMPTY
        shift = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
        mode = self.mode

        if mode in (None, EditMode.PLAY):
            if occupied:
                return
            self._draw_stone(p, pt.x, pt.y, self._to_move(), alpha=95)
        elif mode == EditMode.PLAY_STONE:
            if occupied:
                return
            self._draw_stone(p, pt.x, pt.y, WHITE if shift else BLACK, alpha=95)
        elif mode == EditMode.SETUP:
            tool_color = WHITE if shift else BLACK
            # Mirror the click decision (Game.setup_click_target): a stone of
            # either colour would be erased; only an empty point gets the tool
            # colour. So hovering any stone shows the erase ring.
            target = self.game.setup_click_target(pt, tool_color)
            if target == EMPTY:
                # Would erase: show a faint erase ring.
                c = self._xy(pt.x, pt.y)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(220, 60, 60, 160), max(1.5, self._cell * 0.05)))
                r = self._stone_radius() * 0.8
                p.drawEllipse(c, r, r)
            else:
                self._draw_stone(p, pt.x, pt.y, target, alpha=95)
        elif mode in MODE_TO_MARK:
            self._draw_mark(p, pt, MODE_TO_MARK[mode].value)
        elif mode == EditMode.LABEL:
            c = self._xy(pt.x, pt.y)
            p.setBrush(QColor(255, 255, 255, 70))
            p.setPen(Qt.NoPen)
            r = self._stone_radius() * 0.7
            p.drawEllipse(c, r, r)

    # -- input -------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if self.game is None:
            return
        self.setFocus()
        pt = self._point_at(event.position().x(), event.position().y())
        if pt is None:
            return
        # Paste mode: any left click pastes the buffer at the cursor.
        if self.paste_active:
            if event.button() == Qt.LeftButton:
                self.pasteAt.emit(pt)
            return
        if self.mode == EditMode.SELECT and event.button() == Qt.LeftButton:
            mods = event.modifiers()
            self._sel_start = pt
            self._sel_cur = pt
            self._sel_mods = mods
            no_mod = not (mods & (Qt.ShiftModifier | Qt.ControlModifier))
            self._sel_is_move = no_mod and pt in self.selection
            self._sel_dragging = True
            return
        # Paint-style tools: press starts a stroke that drags continuously.
        if self.mode in _PAINT_MODES and event.button() == Qt.LeftButton:
            self._painting = True
            self._paint_last = pt
            self.paintBegin.emit(pt, event.modifiers())
            return
        if event.button() == Qt.LeftButton:
            self.clicked.emit(pt, event.modifiers())
        elif event.button() == Qt.RightButton:
            self.rightClicked.emit(pt)

    def mouseMoveEvent(self, event) -> None:
        pt = self._point_at(event.position().x(), event.position().y())
        if self._painting:
            if pt is not None and pt != self._paint_last:
                self._paint_last = pt
                self.paintMove.emit(pt)
            return
        if self._sel_dragging:
            if pt is not None and pt != self._sel_cur:
                self._sel_cur = pt
                self.update()
            return
        if pt != self._hover:
            self._hover = pt
            self.update()
            self._emit_readout()

    def mouseReleaseEvent(self, event) -> None:
        if self._painting:
            self._painting = False
            self.paintEnd.emit()
            return
        if not self._sel_dragging:
            return
        self._sel_dragging = False
        start, cur = self._sel_start, self._sel_cur
        if start is None or cur is None:
            return
        if self._sel_is_move:
            dx, dy = cur.x - start.x, cur.y - start.y
            if dx or dy:
                self.selectionMove.emit(dx, dy)
        else:
            self.selectionRect.emit(start, cur, self._sel_mods)
        self.update()

    def leaveEvent(self, event) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
            self._emit_readout()

    def wheelEvent(self, event) -> None:
        # Scrolling over the board steps through history along the golden line.
        dy = event.angleDelta().y()
        if dy > 0:
            self.navigate.emit("back")
        elif dy < 0:
            self.navigate.emit("forward")
        event.accept()

    def keyPressEvent(self, event) -> None:
        # Navigation keys never reach here: MainWindow's application-level
        # filter dispatches them (EditorTab.handle_key) before any widget sees
        # them. Only the modifier refresh is ours.
        if event.key() in (Qt.Key_Shift, Qt.Key_Control) and self._hover is not None:
            self.update()      # the tool preview depends on the modifier
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() in (Qt.Key_Shift, Qt.Key_Control) and self._hover is not None:
            self.update()
        super().keyReleaseEvent(event)
