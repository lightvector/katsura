"""An editor tab: board + variation tree + comment editor for one document."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QScrollArea,
    QPlainTextEdit,
    QLabel,
    QInputDialog,
    QMessageBox,
)

from ..go.board import BLACK, WHITE, opponent
from ..engine.coords import point_label
from ..engine.position import initial_settings
from ..engine.settings import clamp_komi
from ..model.game import Game, GameError, fit_subtree_to_board
from ..model import markup as M
from ..sgf.coords import Point
from .analysispanel import AnalysisInfoPanel
from .boardview import BoardView
from .collapsible import CollapsibleSection, PaneGrip
from .document import Document
from .enginecontrols import EngineSelectorButton
from .gameinfo import SgfInfoWidget
from .modes import EditMode, MODE_TO_MARK, RAW_NN_KEY_ORDER
from .selection import rotate_cw, flip_h
from .settings import Prefs
from .treeview import VariationTree


# With Shift held, most platforms report the *shifted* character rather than the
# digit key (Shift+1 -> Key_Exclam), so both spellings map back to the digit.
_SHIFTED_DIGIT_KEYS = {
    Qt.Key_Exclam: 1, Qt.Key_At: 2, Qt.Key_NumberSign: 3, Qt.Key_Dollar: 4,
    Qt.Key_Percent: 5, Qt.Key_AsciiCircum: 6, Qt.Key_Ampersand: 7,
    Qt.Key_Asterisk: 8, Qt.Key_ParenLeft: 9, Qt.Key_ParenRight: 0,
}


def _digit_of(key, shifted: bool = False) -> Optional[int]:
    """The digit ``key`` stands for (0-9), or ``None`` if it isn't one.

    The shifted-character spellings are consulted only when Shift is actually
    held, so a bare keypad ``*`` isn't read as an 8.
    """
    if Qt.Key_0 <= key <= Qt.Key_9:
        return key - Qt.Key_0
    return _SHIFTED_DIGIT_KEYS.get(key) if shifted else None


class CommentEdit(QPlainTextEdit):
    focusGained = Signal()
    focusLost = Signal()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.focusGained.emit()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focusLost.emit()


class EditorTab(QWidget):
    """Owns one :class:`Document` and the widgets that view/edit it."""

    changed = Signal()  # emitted when the model or position changes

    def __init__(self, window, document: Document, prefs: Prefs, parent=None):
        super().__init__(parent)
        # Not `self.window`: QWidget.window() is a method, and shadowing it
        # silently breaks any Qt-facing caller that expects the method.
        self.main_window = window
        self.document = document
        self.prefs = prefs
        # Per-tab engine analysis settings (komi/rules/search params), seeded
        # from the SGF on load and then edited independently from the toolbar.
        self.analysis_settings = initial_settings(self.game)
        self._loading = False
        self._comment_before: Optional[str] = None
        self._comment_snap = None      # pending undo snapshot of a comment session
        self._edit_snap = None         # pending undo snapshot of a speculative edit
        # Transient (not persisted to SGF) override of who plays the next move,
        # set by Ctrl+Click in Play mode. Cleared on any navigation.
        self.transient_color: Optional[int] = None
        # The subtree mark and the selection buffer live on the window so they
        # are shared across tabs (cross-SGF cut/copy/paste). Selection itself is
        # per-tab.
        self.selection: set = set()
        self.paste_active = False
        self.paste_items: list = []
        self.paste_dims = (0, 0)
        self.paste_center = (0, 0)
        # Paint-stroke state (setup/mark drag painting).
        self._paint_kind = None
        self._paint_mark_add = False
        self._paint_visited: set = set()

        self.setFocusPolicy(Qt.ClickFocus)

        self.board = BoardView(prefs)
        self.tree = VariationTree()
        self.comment = CommentEdit()
        self.comment.setPlaceholderText("Comment for this node…")
        self.comment.setFont(QFont("Sans", 10))

        self.info = QLabel()
        self.info.setObjectName("nodeInfo")
        self.info.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.analysis_panel = AnalysisInfoPanel()
        self.info_widget = SgfInfoWidget()
        self.info_widget.fieldEdited.connect(self.on_info_field_edited)

        # Collapsible right-hand panes, stacked top-to-bottom (no splitter).
        # Tree and Comments are growable (resizable via the grip below each);
        # Analysis Info and SGF Info are fixed-size (header + content).
        self.node_count_label = QLabel()
        self.node_count_label.setStyleSheet("color: #999;")
        self.sec_tree = CollapsibleSection(
            "Tree", self.tree, expanded=True, growable=True, open_height=300,
            accessory=self.node_count_label)
        self.engine_select = EngineSelectorButton()
        self.engine_select.engineSelected.connect(
            lambda i: self.main_window.select_engine_index(i))
        self.sec_analysis = CollapsibleSection(
            "Analysis Info", self.analysis_panel, expanded=True,
            accessory=self.engine_select)
        self.sec_sgf = CollapsibleSection(
            "SGF Info", self.info_widget, expanded=False)
        self.sec_comments = CollapsibleSection(
            "Comments", self.comment, expanded=True, growable=True, open_height=210)
        self.sections = (self.sec_tree, self.sec_analysis,
                         self.sec_sgf, self.sec_comments)

        stack = QWidget()
        slay = QVBoxLayout(stack)
        slay.setContentsMargins(0, 0, 0, 0)
        slay.setSpacing(0)
        for sec in self.sections:
            slay.addWidget(sec)
            if sec.growable:
                grip = PaneGrip(sec)
                grip.setVisible(sec.is_expanded())
                sec.grip = grip
                slay.addWidget(grip)
        slay.addStretch(1)        # leftover space collects at the bottom

        self.pane_scroll = QScrollArea()
        self.pane_scroll.setWidget(stack)
        self.pane_scroll.setWidgetResizable(True)
        self.pane_scroll.setFrameShape(QScrollArea.NoFrame)
        self.pane_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.addWidget(self.info)                  # node info: always visible
        rlay.addWidget(self.pane_scroll, 1)

        self.main_split = QSplitter(Qt.Horizontal)
        self.main_split.addWidget(self.board)
        self.main_split.addWidget(right)
        self.main_split.setStretchFactor(0, 3)
        self.main_split.setStretchFactor(1, 2)
        self.main_split.setSizes([700, 380])

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.main_split)

        # Wiring.
        self.board.clicked.connect(self.on_board_click)
        self.board.rightClicked.connect(self.on_board_right)
        self.board.navigate.connect(self.on_navigate)
        self.board.selectionRect.connect(self.on_selection_rect)
        self.board.selectionMove.connect(self.on_selection_move)
        self.board.pasteAt.connect(self.on_paste_at)
        self.board.paintBegin.connect(self.on_paint_begin)
        self.board.paintMove.connect(self.on_paint_move)
        self.board.paintEnd.connect(self.on_paint_end)
        self.board.analysisReadout.connect(self._on_board_readout)
        self.tree.nodeSelected.connect(self.on_tree_select)
        self.comment.textChanged.connect(self.on_comment_changed)
        self.comment.focusGained.connect(self.on_comment_focus_in)
        self.comment.focusLost.connect(self.on_comment_focus_out)

        self.sync_board_tool()
        self.tree.set_centered(prefs.centered_tree)
        self.board.set_game(self.game)
        self.tree.set_game(self.game)
        self.refresh()

    def mousePressEvent(self, event) -> None:
        # Clicking an empty part of the tab takes focus off the comment box so
        # the keyboard controls work again.
        self.board.setFocus()
        super().mousePressEvent(event)

    def _confirm(self, title: str, text: str) -> bool:
        """A Yes/No dialog centred on the main window (not the screen corner)."""
        box = QMessageBox(QMessageBox.Question, title, text,
                          QMessageBox.Yes | QMessageBox.No, self.main_window)
        box.setDefaultButton(QMessageBox.Yes)
        box.show()
        frame = box.frameGeometry()
        frame.moveCenter(self.main_window.frameGeometry().center())
        box.move(frame.topLeft())
        return box.exec() == QMessageBox.Yes

    # -- convenience -------------------------------------------------------

    @property
    def game(self) -> Game:
        return self.document.game

    def apply_prefs(self, prefs: Prefs) -> None:
        self.prefs = prefs
        self.board.prefs = prefs
        self.tree.set_centered(prefs.centered_tree)
        self.board.update()
        # The analysis readout is pref-gated too, so it has to be recomposed —
        # the View-menu toggles do this via MainWindow._set_view_pref.
        self.board.refresh_readout()

    def sync_board_tool(self) -> None:
        """Tell the board which tool/preview to show."""
        self.board.set_mode(self.main_window.mode)
        self.board.transient_to_move = self.transient_color
        self.board.update()

    def effective_to_move(self) -> int:
        return self.transient_color if self.transient_color is not None else self.game.to_move

    # -- refresh -----------------------------------------------------------

    def refresh(self) -> None:
        self.board.set_game(self.game)
        self.tree.set_game(self.game)
        self._load_comment()
        self.info_widget.load(self.game)
        self._update_info()
        self._update_node_count()
        self.changed.emit()

    def _update_node_count(self) -> None:
        n = sum(1 for _ in self.game.root.walk())
        self.node_count_label.setText(f"{n} node{'s' if n != 1 else ''}")

    def set_engine_choices(self, names: list[str], current: int) -> None:
        """Show the attached engines beside the Analysis Info header: the shown
        engine's name, as a drop-down selector when more than one is attached
        (hidden when none is)."""
        self.engine_select.set_engines(names, current)

    def _on_board_readout(self, text: str) -> None:
        """Forward the board's hover/analysis readout to the status bar."""
        if self.main_window.current_tab() is self:
            self.main_window.set_analysis_readout(text)

    def refresh_after_structural_change(self) -> None:
        self.tree.canvas.set_game(self.game)
        self.refresh()

    def _load_comment(self) -> None:
        self._loading = True
        self.comment.setPlainText(self.game.get_comment())
        self._loading = False

    def _update_info(self) -> None:
        g = self.game
        side = "Black" if self.effective_to_move() == BLACK else "White"
        override = " (override)" if self.transient_color is not None else ""
        last = ""
        if g.last_move_point is not None:
            # Same naming the board's coordinate labels (and GTP) use.
            tag = "illegal move" if g.last_move_illegal else "last"
            last = f"  {tag}: {point_label(g.last_move_point, g.height)}"
        elif g.current.has(M.BLACK_MOVE) or g.current.has(M.WHITE_MOVE):
            last = "  last: pass"
        self.info.setText(
            f"Move {g.move_number}   {side} to play{override}   "
            f"({g.width}×{g.height}){last}"
        )

    # -- navigation --------------------------------------------------------

    def on_navigate(self, cmd: str) -> None:
        g = self.game
        step = self.prefs.page_step
        # Lateral (up/down) movement is handled by the tree, which knows the
        # geometry; everything else maps to a Game navigation method.
        if cmd in ("prev_variation", "next_variation"):
            node = self.tree.vertical_neighbor(-1 if cmd == "prev_variation" else 1)
            if node is None:
                return          # no line that way: nothing moved, nothing to redo
            g.goto(node)
        else:
            actions = {
                "forward": lambda: g.forward(1),
                "back": lambda: g.back(1),
                "forward_page": lambda: g.forward(step),
                "back_page": lambda: g.back(step),
                "start": g.go_to_start,
                "end": g.go_to_end,
            }
            fn = actions.get(cmd)
            if fn is None:
                return
            fn()
        self.transient_color = None
        self._post_navigate()

    def _post_navigate(self) -> None:
        self._clear_selection()        # navigating clears a board selection
        self.sync_board_tool()
        self.board.set_game(self.game)
        self._load_comment()
        self._update_info()
        if self.tree.canvas.centered and self.tree.canvas.golden_layout_stale():
            # The centred layout is relative to the current line, so re-lay it out
            # only when navigation actually switched to a different line; moving
            # along the same line needs just a scroll + repaint.
            self.tree.refresh()
        else:
            self.tree.scroll_to_current()
            self.tree.canvas.update()
        self.changed.emit()

    def on_tree_select(self, node) -> None:
        self.game.goto(node)
        self.transient_color = None
        self._post_navigate()

    # -- keyboard dispatch (routed from the window's app-level event filter) --

    def handle_key(self, key, modifiers) -> bool:
        """Handle a key when a non-text widget has focus. Returns handled?"""
        shift = bool(modifiers & Qt.ShiftModifier)
        ctrl = bool(modifiers & Qt.ControlModifier)
        plain = not shift and not ctrl

        nav = {
            Qt.Key_Left: "back", Qt.Key_Right: "forward",
            Qt.Key_PageUp: "back_page", Qt.Key_PageDown: "forward_page",
            Qt.Key_Home: "start", Qt.Key_End: "end",
        }
        if plain and key in nav:
            self.on_navigate(nav[key])
            return True
        if ctrl and key in (Qt.Key_Left, Qt.Key_Right):
            delta = -1 if key == Qt.Key_Left else 1
            if shift:
                self.main_window.move_current_tab(delta)
            else:
                self.main_window.cycle_current_tab(delta)
            return True
        if plain and key == Qt.Key_Up:
            self.on_navigate("prev_variation")
            return True
        if plain and key == Qt.Key_Down:
            self.on_navigate("next_variation")
            return True
        if plain and key == Qt.Key_Space:
            self.main_window.toggle_analysis_via_key()
            return True
        if plain and key == Qt.Key_P:
            self.main_window.toggle_policy_view()
            return True
        if plain and key == Qt.Key_O:
            self.main_window.toggle_ownership_view()
            return True
        if plain and key == Qt.Key_I:
            self.main_window.toggle_overlay_view()
            return True
        if plain and (digit := _digit_of(key)) is not None:
            # Bare '1'-'9' pick a tool (Play, Play Stone, Setup, the four marks,
            # Label, Select) — see MODE_KEY_ORDER.
            if self.main_window.select_tool_by_number(digit):
                return True
        if shift and not ctrl and (digit := _digit_of(key, True)) is not None:
            # Shift+'1'-'8' show a raw-NN evaluation under RAW_NN_KEY_ORDER.
            if 1 <= digit <= len(RAW_NN_KEY_ORDER):
                self.main_window.show_raw_nn_view(RAW_NN_KEY_ORDER[digit - 1])
                return True
        if shift and not ctrl and key == Qt.Key_Up:
            self.shift_variation(-1)
            return True
        if shift and not ctrl and key == Qt.Key_Down:
            self.shift_variation(1)
            return True
        if self.paste_active and plain and key == Qt.Key_R:
            self.rotate_paste()
            return True
        if self.paste_active and plain and key == Qt.Key_F:
            self.flip_paste()
            return True
        if plain and key in (Qt.Key_Backspace, Qt.Key_Delete):
            if self.selection:
                self.selection_delete()
            else:
                self.delete_via_key()
            return True
        if plain and key == Qt.Key_Escape:
            if self.main_window.exit_raw_nn_view():
                return True
            return self.cancel_all()
        if ctrl and not shift and key == Qt.Key_X:
            self.cut()
            return True
        if ctrl and not shift and key == Qt.Key_C:
            self.copy()
            return True
        if ctrl and not shift and key == Qt.Key_V:
            self.paste()
            return True
        return False

    # -- board editing -----------------------------------------------------

    def _begin_edit(self) -> None:
        self._edit_snap = self.document.begin_edit()

    def _commit_edit_bare(self) -> None:
        """Record the pending edit in the undo history (no UI refresh)."""
        if self._edit_snap is not None:
            self.document.commit_edit(self._edit_snap)
            self._edit_snap = None

    def _commit_edit(self) -> None:
        self._commit_edit_bare()
        self._cancel_pending_if_edited()
        self.exit_paste_state()        # any edit ends a pending paste
        self.document.mark_dirty()
        self.transient_color = None
        self.refresh_after_structural_change()
        self.refresh_subtree_outline()
        self.board.set_selection(self.selection)
        self.sync_board_tool()
        self.main_window.update_undo_actions()

    def _discard_edit(self) -> None:
        """Drop a speculative begin_edit when the edit turns out to be a no-op."""
        self._edit_snap = None

    def on_board_click(self, pt: Point, modifiers) -> None:
        shift = bool(modifiers & Qt.ShiftModifier)
        ctrl = bool(modifiers & Qt.ControlModifier)
        mode = self.main_window.mode
        if mode == EditMode.PLAY:
            self._click_play(pt, shift, ctrl)
        elif mode == EditMode.PLAY_STONE:
            self._click_play_stone(pt, shift)
        elif mode == EditMode.SETUP:
            self._click_setup(pt, shift, ctrl)
        elif mode in MODE_TO_MARK:
            self._begin_edit()
            self.game.toggle_mark(pt, MODE_TO_MARK[mode])
            self._commit_edit()
        elif mode == EditMode.LABEL:
            self._click_label(pt, shift, ctrl)

    def _click_play(self, pt: Point, shift: bool, ctrl: bool) -> None:
        g = self.game
        if shift and ctrl:
            # Persist a player-to-move override in the SGF.
            self._begin_edit()
            g.set_player_to_move(opponent(self.effective_to_move()))
            self._commit_edit()
            return
        if ctrl:
            # Transient flip of who plays next; no SGF change, no move.
            self.transient_color = opponent(self.effective_to_move())
            self.sync_board_tool()
            self._update_info()
            self.changed.emit()
            return
        if shift:
            self._do_play(None, self.effective_to_move())  # pass
            return
        self._do_play(pt, self.effective_to_move())

    def _click_play_stone(self, pt: Point, shift: bool) -> None:
        self._do_play(pt, WHITE if shift else BLACK)

    def _do_play(self, pt: Optional[Point], color: int) -> None:
        g = self.game
        existing = g.find_child_move(pt, color)
        if existing is not None:
            g.goto(existing)
            self.transient_color = None
            self._post_navigate()
            return
        self._begin_edit()
        try:
            g.play(pt, color,
                   forbid_multi_suicide=self.prefs.forbid_multi_suicide,
                   forbid_ko=self.prefs.forbid_simple_ko)
        except GameError as e:
            self._discard_edit()
            self.main_window.flash(str(e))
            return
        self._commit_edit()

    def _click_setup(self, pt: Point, shift: bool, ctrl: bool) -> None:
        g = self.game
        tool_color = WHITE if shift else BLACK
        # A click toggles the point between holding a stone and empty: a stone of
        # *either* colour is erased first, and only an empty point receives the
        # tool colour (so black-on-white goes white -> empty -> black over two
        # clicks). Decided off the *setup layer* (pre-interpretation), so a
        # zero-liberty ghost counts as present. Ctrl = force edit: record verbatim
        # and resolve no captures (illegality is resolved only at interpretation).
        target = g.setup_click_target(pt, tool_color)
        self._begin_edit()
        g.set_setup_point(pt, target, force_redundant=ctrl)
        self._commit_edit()

    def _click_label(self, pt: Point, shift: bool, ctrl: bool) -> None:
        g = self.game
        labels = g.get_labels()
        if ctrl:
            cur = labels.get(pt, "")
            text, ok = QInputDialog.getText(self, "Label", "Label text:", text=cur)
            if not ok:
                return
            self._begin_edit()
            g.set_label(pt, text)
            self._commit_edit()
            return
        self._begin_edit()
        if pt in labels:
            g.set_label(pt, "")
        else:
            used = set(labels.values())
            if shift:
                nxt = M.next_number_label(used)
            else:
                nxt = M.next_letter_label(used)
            g.set_label(pt, nxt)
        self._commit_edit()

    def on_board_right(self, pt: Point) -> None:
        """Right-click: erase any markup/label at the point."""
        g = self.game
        marks = g.get_marks()
        labels = g.get_labels()
        if pt not in marks and pt not in labels:
            return
        self._begin_edit()
        if pt in labels:
            g.set_label(pt, "")
        for mt in MODE_TO_MARK.values():
            if marks.get(pt) == mt.value:
                g.toggle_mark(pt, mt)
        self._commit_edit()

    # -- paint strokes (setup / mark drag) --------------------------------

    def on_paint_begin(self, pt: Point, modifiers) -> None:
        g = self.game
        mode = self.main_window.mode
        shift = bool(modifiers & Qt.ShiftModifier)
        ctrl = bool(modifiers & Qt.ControlModifier)
        self._begin_edit()
        self._paint_visited = set()
        if mode == EditMode.SETUP:
            tool_color = WHITE if shift else BLACK
            # The first cell fixes the action for the whole stroke: erase if it
            # already holds a stone (of either colour), otherwise paint the tool
            # colour over every cell (overwriting stones the drag crosses). See
            # Game.setup_click_target / _click_setup.
            target = g.setup_click_target(pt, tool_color)
            self._paint_kind = ("setup", target, ctrl)
        else:
            mark = MODE_TO_MARK[mode]
            self._paint_mark_add = g.get_marks().get(pt) != mark.value
            self._paint_kind = ("mark", mark, False)
        self._paint_apply(pt)

    def on_paint_move(self, pt: Point) -> None:
        if self._paint_kind is not None:
            self._paint_apply(pt)

    def _paint_apply(self, pt: Point) -> None:
        if pt in self._paint_visited:
            return
        self._paint_visited.add(pt)
        g = self.game
        kind = self._paint_kind[0]
        if kind == "setup":
            _, color, ctrl = self._paint_kind
            g.set_setup_point(pt, color, force_redundant=ctrl)
        else:
            _, mark, _ = self._paint_kind
            if self._paint_mark_add:
                g.set_mark(pt, mark)
            else:
                g.clear_mark(pt)
        # Light refresh during the stroke (no undo/commit yet).
        self.board.set_game(self.game)
        self._update_info()

    def on_paint_end(self) -> None:
        if self._paint_kind is None:
            return
        self._paint_kind = None
        self._commit_edit()

    # -- structural ops ----------------------------------------------------

    def pass_move(self) -> None:
        self._do_play(None, self.effective_to_move())

    def insert_empty_node(self) -> None:
        self._begin_edit()
        self.game.insert_empty_node()
        self._commit_edit()

    def on_info_field_edited(self, prop: str, value: str) -> None:
        """An SGF Info field was committed (one undo step per field)."""
        if prop == "KM":
            self._set_sgf_komi(value)
            return
        if self.game.get_info(prop) == value:
            return
        snap = self.document.begin_edit()
        self.game.set_info(prop, value)
        self.document.commit_edit(snap)
        self.document.mark_dirty()
        self.changed.emit()
        self.main_window.update_undo_actions()

    def _set_sgf_komi(self, text: str) -> None:
        """Set (or clear) the SGF komi from the SGF Info field."""
        text = text.strip()
        if text == "":
            self.set_komi(None)
            return
        try:
            value = float(text.replace("子", "").replace("目", "").strip())
        except ValueError:
            return
        self.set_komi(value)

    def set_komi(self, value) -> None:
        """Set the SGF komi (undoable). Does NOT change the engine komi."""
        if self.game.get_komi() == value:
            return
        snap = self.document.begin_edit()
        self.game.set_komi(value)
        self.document.commit_edit(snap)
        self.document.mark_dirty()
        self.changed.emit()
        self.main_window.update_undo_actions()

    # -- engine analysis settings (toolbar; not part of the SGF/undo) -------

    def _update_settings(self, **changes) -> None:
        new = replace(self.analysis_settings, **changes)
        if new == self.analysis_settings:
            return
        self.analysis_settings = new
        self.main_window.refresh_engine_position()

    def set_engine_komi(self, value: float) -> None:
        self._update_settings(komi=clamp_komi(value))

    def set_engine_rules(self, rules) -> None:
        self._update_settings(rules=rules)

    def set_wide_root_noise(self, value: float) -> None:
        self._update_settings(wide_root_noise=value)

    def set_playout_doubling_advantage(self, value: float) -> None:
        self._update_settings(playout_doubling_advantage=value)

    def delete_via_key(self) -> None:
        """Delete the current node, prompting only when it would lose work.

        Confirmation is asked only if the node carries a comment or has children;
        a bare node (even with stones/markers/edits) is deleted immediately.
        """
        g = self.game
        node = g.current
        if node.parent is None:
            self.main_window.flash("Cannot delete the root node.")
            return
        needs_confirm = node.has(M.COMMENT) or bool(node.children)
        if needs_confirm and not self._confirm(
                "Delete node", "Delete this node and all its descendants?"):
            return
        self._begin_edit()
        try:
            g.delete_node()
        except GameError as e:
            self._discard_edit()
            self.main_window.flash(str(e))
            return
        self._commit_edit()

    def promote_current(self) -> None:
        """Ctrl+M: rotate the closest diverging ancestor of this line to the front."""
        self._begin_edit()
        if self.game.promote_closest():
            self._commit_edit()
        else:
            self._discard_edit()

    def shift_variation(self, direction: int) -> None:
        self._begin_edit()
        if self.game.shift_variation(direction):
            self._commit_edit()
        else:
            self._discard_edit()

    def set_player_to_move(self, color) -> None:
        self._begin_edit()
        self.game.set_player_to_move(color)
        self._commit_edit()

    # -- subtree cut / copy / paste ---------------------------------------

    def cut(self) -> None:
        if self.selection:
            self.selection_cut()
        else:
            self.mark_subtree("cut")

    def copy(self) -> None:
        if self.selection:
            self.selection_copy()
        else:
            self.mark_subtree("copy")

    def paste(self) -> None:
        if self.main_window.subtree_mark is not None:
            self.paste_subtree()
        elif self.main_window.selection_buffer is not None:
            self.enter_paste_state()

    def cancel_all(self) -> bool:
        if self.paste_active:
            self.exit_paste_state()
            self.main_window.flash("Cancelled")
            return True
        if self.main_window.subtree_mark is not None:
            self.main_window.clear_subtree_mark()
            self.main_window.flash("Cancelled")
            return True
        if self.selection:
            self._clear_selection()
            return True
        return False

    def mark_subtree(self, mode: str) -> None:
        node = self.game.current
        if node.parent is None:
            self.main_window.flash("Cannot cut/copy the root node.")
            return
        self._clear_selection()        # subtree marking and selection are exclusive
        self.exit_paste_state()
        self.main_window.set_subtree_mark(self, node, mode)
        self.main_window.flash(
            f"Marked subtree for {mode} — go to a target (any tab) and press "
            f"Ctrl+V (Esc to cancel)")

    def paste_subtree(self) -> None:
        mark = self.main_window.subtree_mark
        if mark is None:
            return
        src_tab, src, mode = mark
        target = self.game.current
        # Within the source tab, the target must be outside the marked subtree.
        if src_tab is self and not self.game.can_transplant(src, target):
            self.main_window.flash("Cannot paste here (target is inside the marked subtree).")
            return
        self._begin_edit()
        pruned = 0
        if src_tab is self:
            if mode == "cut":
                self.game.cut_subtree(src, target)
            else:
                self.game.copy_subtree(src, target)
        else:
            # Cross-SGF: always graft a clone into this tab (its own undo event).
            # Across board sizes, refit the clone: anchor its bounding box to
            # the nearest corner and prune whatever still lands off-board.
            clone = src.clone()
            src_game = src_tab.game
            if (src_game.width, src_game.height) != (self.game.width, self.game.height):
                pruned = fit_subtree_to_board(
                    clone, src_game.width, src_game.height,
                    self.game.width, self.game.height)
            self.game.attach_subtree(clone, target)
        self._commit_edit_bare()       # bare: keep a "copy" mark alive for re-paste
        self.document.mark_dirty()
        self.refresh_after_structural_change()
        self.sync_board_tool()
        self.main_window.update_undo_actions()
        if pruned:
            self.main_window.flash(
                f"Pasted onto a {self.game.width}x{self.game.height} board — "
                f"{pruned} off-board operation{'s' if pruned != 1 else ''} pruned.")
        # A cut is consumed: clear the mark and remove the source (the source
        # tab records that deletion as its own, independent undo event).
        if mode == "cut":
            self.main_window.clear_subtree_mark()
            if src_tab is not self:
                src_tab._delete_external_subtree(src)
        else:
            self.refresh_subtree_outline()

    def _delete_external_subtree(self, node) -> None:
        if node.parent is None:
            return
        snap = self.document.begin_edit()
        self.game.delete_node(node)
        self.document.commit_edit(snap)   # after the edit: redo lands where it left us
        self.document.mark_dirty()
        self.refresh_after_structural_change()
        self.refresh_subtree_outline()
        if self.main_window.current_tab() is self:
            self.main_window.update_undo_actions()

    def _owns_subtree_mark(self) -> bool:
        mark = self.main_window.subtree_mark
        return mark is not None and mark[0] is self

    def cancel_pending(self) -> bool:
        if self.main_window.subtree_mark is not None:
            self.main_window.clear_subtree_mark()
            return True
        return False

    def _cancel_pending_if_edited(self) -> None:
        """Drop a cut/copy mark if this tab's edit touched the marked subtree."""
        if self._owns_subtree_mark():
            _, node, _ = self.main_window.subtree_mark
            if self.game.subtree_contains(node, self.game.current):
                self.main_window.clear_subtree_mark()

    def refresh_subtree_outline(self) -> None:
        """Outline the marked subtree in the tree iff this tab owns the mark."""
        mark = self.main_window.subtree_mark
        if mark is not None and mark[0] is self:
            _, node, mode = mark
            ids = {id(n) for n in node.walk()}
            color = QColor(225, 70, 70) if mode == "cut" else QColor(80, 200, 110)
            self.tree.set_marked(ids, color)
        else:
            self.tree.set_marked(set(), None)

    # -- selection tool ----------------------------------------------------

    def _clear_selection(self) -> None:
        if self.selection:
            self.selection = set()
            self.board.set_selection(set())

    def on_tool_changed(self) -> None:
        """Called when the active tool changes: drop transient selection state."""
        self.exit_paste_state()
        if self.main_window.mode != EditMode.SELECT:
            self._clear_selection()

    def on_selection_rect(self, start: Point, end: Point, mods) -> None:
        self.cancel_pending()          # selecting clears a subtree mark
        self.exit_paste_state()
        x0, x1 = sorted((start.x, end.x))
        y0, y1 = sorted((start.y, end.y))
        rect = {Point(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
        if mods & Qt.ControlModifier:
            self.selection -= rect
        elif mods & Qt.ShiftModifier:
            self.selection |= rect
        else:
            self.selection = rect
        # Selecting a region with no stones/marks/labels is pointless — there is
        # nothing to move, cut, or copy — so dragging across empty board (or a
        # bare click) just clears the selection.
        if self.selection and not self.game.snapshot_points(self.selection)[0]:
            self.selection = set()
        self.board.set_selection(self.selection)
        self.changed.emit()

    def on_selection_move(self, dx: int, dy: int) -> None:
        if not self.selection:
            return
        g = self.game
        self._begin_edit()
        g.apply_move(self.selection, dx, dy)
        self.selection = {Point(p.x + dx, p.y + dy) for p in self.selection
                          if g.board.in_bounds(p.x + dx, p.y + dy)}
        self._commit_edit()
        self.board.set_selection(self.selection)

    def selection_cut(self) -> None:
        if not self.selection:
            return
        self.main_window.selection_buffer = self.game.snapshot_points(self.selection)
        self._begin_edit()
        self.game.apply_erase(self.selection)
        self._commit_edit()
        self._clear_selection()
        self.enter_paste_state()

    def transform_geometry(self, kind: str) -> None:
        """Rotate/flip the entire game tree (all moves, setup, marks, labels)."""
        self._begin_edit()
        self._clear_selection()
        self.game.transform_geometry(kind)
        self._commit_edit()

    def rotate_cw(self) -> None:
        self.transform_geometry("rot_cw")

    def rotate_ccw(self) -> None:
        self.transform_geometry("rot_ccw")

    def flip_horizontal(self) -> None:
        self.transform_geometry("flip_h")

    def flip_vertical(self) -> None:
        self.transform_geometry("flip_v")

    def selection_delete(self) -> None:
        if not self.selection:
            return
        self._begin_edit()
        self.game.apply_erase(self.selection)
        self._commit_edit()
        self._clear_selection()

    def selection_copy(self) -> None:
        if not self.selection:
            return
        self.main_window.selection_buffer = self.game.snapshot_points(self.selection)
        self._clear_selection()
        self.enter_paste_state()

    def _compute_paste_center(self) -> None:
        if not self.paste_items:
            self.paste_center = (0, 0)
            return
        xs = [it[0] for it in self.paste_items]
        ys = [it[1] for it in self.paste_items]
        self.paste_center = (round((min(xs) + max(xs)) / 2),
                             round((min(ys) + max(ys)) / 2))

    def _push_paste(self) -> None:
        self._compute_paste_center()
        self.board.set_paste(True, self.paste_items, self.paste_dims, self.paste_center)

    def enter_paste_state(self) -> None:
        buf = self.main_window.selection_buffer
        if not buf or not buf[0]:
            return
        items, dims = buf
        self.paste_items = list(items)
        self.paste_dims = dims
        self.paste_active = True
        self._push_paste()
        self.main_window.flash("Click to paste · r rotate · f flip · Esc cancel")

    def exit_paste_state(self) -> None:
        if self.paste_active:
            self.paste_active = False
            self.board.set_paste(False)

    def on_paste_at(self, pt: Point) -> None:
        if not self.paste_active or not self.paste_items:
            return
        cx, cy = self.paste_center
        anchor = Point(pt.x - cx, pt.y - cy)
        self._begin_edit()
        self.game.apply_paste(self.paste_items, anchor)
        self._commit_edit()           # this also exits the paste state

    def rotate_paste(self) -> None:
        if not self.paste_active:
            return
        self.paste_items, self.paste_dims = rotate_cw(self.paste_items, self.paste_dims)
        self._push_paste()

    def flip_paste(self) -> None:
        if not self.paste_active:
            return
        self.paste_items, self.paste_dims = flip_h(self.paste_items, self.paste_dims)
        self._push_paste()

    def _after_undo_redo(self) -> None:
        self.transient_color = None
        self.exit_paste_state()
        self._clear_selection()
        # Undo/redo reparses this tab's tree, so any subtree mark *it* owns now
        # references stale nodes and must be dropped. A mark owned by another tab
        # is unaffected and stays.
        if self._owns_subtree_mark():
            self.main_window.clear_subtree_mark()
        self.refresh_after_structural_change()
        self.refresh_subtree_outline()
        self.board.set_selection(self.selection)
        self.sync_board_tool()
        self.main_window.update_undo_actions()

    def undo(self) -> None:
        if self.document.undo():
            self._after_undo_redo()

    def redo(self) -> None:
        if self.document.redo():
            self._after_undo_redo()

    # -- comment editing ---------------------------------------------------

    def on_comment_changed(self) -> None:
        if self._loading:
            return
        self.game.set_comment(self.comment.toPlainText())
        self.document.mark_dirty()
        self.tree.canvas.update()
        self.changed.emit()

    def on_comment_focus_in(self) -> None:
        self._comment_before = self.game.get_comment()
        self._comment_snap = self.document.begin_edit()

    def on_comment_focus_out(self) -> None:
        # Commit the whole editing session as one undo step iff the text
        # actually changed; otherwise the snapshot is simply dropped (the redo
        # stack survives a click-in/click-out).
        if self._comment_snap is not None and \
                self._comment_before is not None and \
                self._comment_before != self.game.get_comment():
            self.document.commit_edit(self._comment_snap)
        self._comment_snap = None
        self._comment_before = None
        self.main_window.update_undo_actions()
