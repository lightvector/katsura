"""The main application window: menus, toolbar, tabs, and the controller glue."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QEvent, QSettings
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QTabWidget,
    QFileDialog,
    QMessageBox,
    QMenu,
    QToolBar,
    QToolButton,
    QApplication,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QAbstractSpinBox,
    QComboBox,
)

from ..go.board import BLACK, WHITE
from ..engine.config import EngineConfig, load_engines, save_engines
from ..engine.controller import AnalysisController
from ..engine.settings import clamp_komi, preset_name
from .document import Document
from .editortab import EditorTab
from .dialogs import NewGameDialog, PreferencesDialog, RulesDialog
from .enginecontrols import KomiSpinBox, WideRootNoiseSpinBox, PdaSpinBox
from .enginedialog import EngineManagerDialog
from .console import GtpConsole
from .modes import (
    EditMode, MODE_LABELS, MODE_GLYPHS, MODE_HELP, MODE_HOTKEY, MODE_KEY_ORDER,
    RAW_NN_KEY_ORDER,
)
from .settings import Prefs, ORG, APP

# Editing/navigation keys are dispatched at the application level (so they work
# no matter which non-text widget has focus) via EditorTab.handle_key. Text
# widgets are exempted so typing/clipboard/cursor keys keep working there.
_TEXT_WIDGETS = (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox)

SGF_FILTER = "SGF files (*.sgf);;All files (*)"
OPEN_FILTER = "SGF files (*.sgf *.sgfs);;All files (*)"

# The rules dialog is reachable from two places; they say the same thing.
_RULES_TIP = "The ruleset the engine analyses under — click to change it"


def hint_key(action: QAction, *keys) -> QAction:
    """Show ``keys`` in ``action``'s menu shortcut column *without binding them*.

    The single-key commands here (Backspace/Delete, the arrows, i/p/o, Space,
    the tool digits) are dispatched by the application-level key filter
    (:meth:`EditorTab.handle_key`), which is where the context-dependent
    behaviour lives — Backspace erases a selection or deletes a node, a digit is
    a tool only while a tab is open, and text widgets are exempted wholesale. A
    real shortcut would be a second, competing activation path for the same key,
    so these must stay inert.

    Qt has no display-only shortcut, but ``Qt.WidgetShortcut`` on an action
    whose only holder is a (normally unfocused) menu never activates, while
    menus still render it right-justified exactly like a real ``Ctrl+Z``. That
    uniformity is the point: every hotkey reminder in every menu sits in the
    same column, in Qt's native spelling, instead of some being parenthesised
    into the label text.
    """
    action.setShortcuts([QKeySequence(k) for k in keys])
    action.setShortcutContext(Qt.WidgetShortcut)
    return action


class MainWindow(QMainWindow):
    def __init__(self, prefs: Prefs | None = None, initial_tab: bool = True):
        super().__init__()
        self.prefs = prefs or Prefs().load()
        self.mode = EditMode.PLAY
        self._engine_loading = False
        # Cross-tab clipboards (shared across all open SGFs in this window):
        # a board-content buffer and at most one marked subtree at a time.
        self.selection_buffer = None       # (items, dims)
        self.subtree_mark = None           # (EditorTab, SgfNode, 'cut'|'copy')
        self.setWindowTitle("Katsura")
        self.resize(1180, 820)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        # Live-analysis engines (attached on demand from saved commands).
        # Any number may be attached at once — one controller per engine — but
        # exactly one is the *shown* (current) engine: only it analyses live
        # and drives the panel/board overlays; the others are held paused.
        self.engines: list[EngineConfig] = load_engines()
        self.engine_controllers: list[AnalysisController] = []
        self._current_engine: AnalysisController | None = None
        self.gtp_console: GtpConsole | None = None
        # When an engine dies, the console pops showing the dead engine's
        # transcript under a pseudo-entry "<name> (stopped)" until the user
        # picks a live engine (or reopens/closes the console).
        self._console_dead: str | None = None

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self.statusBar().showMessage("Ready")

        # Application-level key handling so navigation keys work regardless of
        # which (non-text) widget has focus.
        QApplication.instance().installEventFilter(self)

        if initial_tab:
            self.new_game(self.prefs.default_board_size)

    # -- tab access --------------------------------------------------------

    def current_tab(self) -> EditorTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, EditorTab) else None

    def _add_tab(self, document: Document) -> EditorTab:
        tab = EditorTab(self, document, self.prefs)
        tab.changed.connect(self._on_tab_state_changed)
        idx = self.tabs.addTab(tab, document.title)
        self.tabs.setCurrentIndex(idx)
        self.update_undo_actions()
        self._update_engine_selectors()
        return tab

    def cycle_current_tab(self, delta: int) -> None:
        """Select the next/previous tab, wrapping around (Ctrl+Left/Right)."""
        n = self.tabs.count()
        if n > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + delta) % n)

    def move_current_tab(self, delta: int) -> None:
        """Swap the current tab with its neighbor, keeping it selected
        (Ctrl+Shift+Left/Right)."""
        idx = self.tabs.currentIndex()
        target = idx + delta
        if 0 <= idx and 0 <= target < self.tabs.count():
            self.tabs.tabBar().moveTab(idx, target)

    # -- engine access -----------------------------------------------------

    @property
    def engine_controller(self) -> AnalysisController | None:
        """The controller of the *shown* engine (None when nothing is attached)."""
        return self._current_engine

    def refresh_engine_position(self) -> None:
        """Ask the shown engine (if any) to re-sync to the current position."""
        if self._current_engine is not None:
            self._current_engine.refresh_position()

    # -- actions / menus ---------------------------------------------------

    def _build_actions(self) -> None:
        self.act_new = QAction("&New…", self, shortcut=QKeySequence.New,
                               triggered=self.on_new)
        self.act_open = QAction("&Open…", self, shortcut=QKeySequence.Open,
                                triggered=self.on_open)
        self.act_save = QAction("&Save", self, shortcut=QKeySequence.Save,
                                triggered=self.on_save)
        self.act_save_as = QAction("Save &As…", self, shortcut=QKeySequence.SaveAs,
                                   triggered=self.on_save_as)
        self.act_close_tab = QAction("&Close Tab", self, shortcut=QKeySequence.Close,
                                     triggered=lambda: self.close_tab(self.tabs.currentIndex()))
        self.act_quit = QAction("&Quit", self, shortcut=QKeySequence.Quit,
                                triggered=self.close)

        self.act_undo = QAction("&Undo", self, shortcut=QKeySequence.Undo,
                                triggered=lambda: self._tab_call("undo"))
        self.act_redo = QAction("&Redo", self, shortcut=QKeySequence.Redo,
                                triggered=lambda: self._tab_call("redo"))
        self.act_pass = QAction("&Pass", self, shortcut="Ctrl+P",
                                triggered=lambda: self._tab_call("pass_move"))
        self.act_insert_empty = QAction("&Insert Empty Node", self,
                                        triggered=lambda: self._tab_call("insert_empty_node"))
        # Delete is dispatched from the app-level key filter (it erases a board
        # selection when there is one, else deletes the node); hint_key only
        # *shows* the key. Same for the two Shift+arrow items.
        self.act_delete = hint_key(
            QAction("&Delete Node", self,
                    triggered=lambda: self._tab_call("delete_via_key")),
            Qt.Key_Backspace, Qt.Key_Delete)
        self.act_promote = QAction("Make &Main Variation", self, shortcut="Ctrl+M",
                                   triggered=lambda: self._tab_call("promote_current"))
        self.act_shift_up = hint_key(
            QAction("Shift Variation Up", self,
                    triggered=lambda: self._tab_call2("shift_variation", -1)),
            Qt.ShiftModifier | Qt.Key_Up)
        self.act_shift_down = hint_key(
            QAction("Shift Variation Down", self,
                    triggered=lambda: self._tab_call2("shift_variation", 1)),
            Qt.ShiftModifier | Qt.Key_Down)
        self.act_rotate_cw = QAction("Rotate Whole Board 90° &Clockwise", self,
                                     triggered=lambda: self._tab_call("rotate_cw"))
        self.act_rotate_ccw = QAction("Rotate Whole Board 90° Counter-clock&wise", self,
                                      triggered=lambda: self._tab_call("rotate_ccw"))
        self.act_flip_h = QAction("Flip Whole Board &Horizontally", self,
                                  triggered=lambda: self._tab_call("flip_horizontal"))
        self.act_flip_v = QAction("Flip Whole Board &Vertically", self,
                                  triggered=lambda: self._tab_call("flip_vertical"))
        self.act_pl_black = QAction("Set Player: Black", self,
                                    triggered=lambda: self._set_pl(BLACK))
        self.act_pl_white = QAction("Set Player: White", self,
                                    triggered=lambda: self._set_pl(WHITE))
        self.act_pl_clear = QAction("Clear Player-to-move", self,
                                    triggered=lambda: self._set_pl(None))

        # Navigation (the bare keys are handled by the app-level filter, so the
        # menu shortcuts are display-only). The labels plus the hotkey tip double
        # as the toolbar buttons' tooltips, keyed by command in _nav_labels.
        self.nav_actions = []
        self._nav_labels: dict[str, str] = {}
        for label, cmd, key, glyph in [
            ("Forward", "forward", Qt.Key_Right, "→"),
            ("Back", "back", Qt.Key_Left, "←"),
            ("Forward 10", "forward_page", Qt.Key_PageDown, "PgDn"),
            ("Back 10", "back_page", Qt.Key_PageUp, "PgUp"),
            ("To Start", "start", Qt.Key_Home, "Home"),
            ("To End", "end", Qt.Key_End, "End"),
            ("Next Variation", "next_variation", Qt.Key_Down, "↓"),
            ("Previous Variation", "prev_variation", Qt.Key_Up, "↑"),
        ]:
            a = hint_key(
                QAction(label, self,
                        triggered=lambda _=False, c=cmd: self._navigate(c)), key)
            self.nav_actions.append(a)
            # The toolbar buttons are bare glyphs, so their tooltip carries the
            # wording *and* the key (as an arrow, which reads better than Qt's
            # "Right" in free text).
            self._nav_labels[cmd] = f"{label} ({glyph})"

        # Edit modes (mutually exclusive). The bare number keys that select them
        # go through the app-level filter, so their menu shortcuts are
        # display-only too. Tooltips describe the shift/ctrl slots.
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_actions: dict[EditMode, QAction] = {}
        for mode in EditMode:
            a = QAction(MODE_LABELS[mode], self, checkable=True)
            # Toolbar buttons show the compact glyph (mark tools) or the plain
            # word; the Tools menu uses text(), which carries the mnemonic and
            # the shortcut column.
            a.setIconText(MODE_GLYPHS.get(mode, MODE_LABELS[mode]))
            key = MODE_HOTKEY.get(mode)
            head = MODE_LABELS[mode]
            if key is not None:
                hint_key(a, getattr(Qt, f"Key_{key}"))
                head += f" ({key})"
            tip = head + "\n" + "\n".join(MODE_HELP[mode])
            a.setToolTip(tip)
            a.triggered.connect(lambda _=False, m=mode: self.set_mode(m))
            self.mode_group.addAction(a)
            self.mode_actions[mode] = a
        self.mode_actions[EditMode.PLAY].setChecked(True)

        # View toggles.
        self.act_coords = self._view_toggle("Show Coordinates", "show_coordinates")
        self.act_numbers = self._view_toggle("Show Move Numbers", "show_move_numbers")
        self.act_lastmove = self._view_toggle("Mark Last Move", "show_last_move_marker")
        self.act_hints = self._view_toggle("Show Variation Hints", "show_variation_hints")
        self.act_centered = QAction("Centre Current Line in Tree", self, checkable=True)
        self.act_centered.setChecked(self.prefs.centered_tree)
        self.act_centered.toggled.connect(self._set_centered_tree)
        self.act_overlay = hint_key(
            self._view_toggle("Show Move Analysis Info", "show_analysis_overlay"),
            Qt.Key_I)
        self.act_pv_hover = self._view_toggle("Show PV on Hover", "show_pv_on_hover")
        # 'p'/'o' togglers, surfaced in the menu for discoverability. Unlike the
        # pref-backed toggles above these are per-board display state, so their
        # checkmarks are refreshed when the View menu opens (_sync_view_menu).
        self.act_policy = hint_key(
            QAction("Show Policy Priors", self, checkable=True,
                    triggered=self.toggle_policy_view), Qt.Key_P)
        self.act_ownership = hint_key(
            QAction("Show Ownership Heatmap", self, checkable=True,
                    triggered=self.toggle_ownership_view), Qt.Key_O)

        self.act_prefs = QAction("&Preferences…", self, triggered=self.on_preferences)
        self.act_about = QAction("&About", self, triggered=self.on_about)

        # Engine / analysis.
        self.act_manage_engines = QAction("&Manage Engines…", self,
                                          triggered=self.on_manage_engines)
        # Not checkable: the label itself says which way it will go, which reads
        # far more clearly than a checkmark on a noun ("Live Analysis").
        self.act_live_analysis = hint_key(
            QAction("Start &Live Analysis", self,
                    triggered=self.on_live_analysis_triggered), Qt.Key_Space)
        self.act_live_analysis.setEnabled(False)
        self.act_console = QAction("GTP &Console…", self, triggered=self.on_gtp_console)
        self.act_console.setEnabled(False)
        self.act_engine_rules = QAction("&Rules…", self, triggered=self._on_edit_rules)
        self.act_engine_rules.setToolTip(_RULES_TIP)
        self.act_clear_gui_cache = QAction(
            "Clear Cached Anal&ysis (GUI)", self,
            triggered=self.on_clear_gui_cache)
        self.act_clear_gui_cache.setToolTip(
            "Drop the analyses this window has kept for visited positions, so "
            "they are searched again from scratch")
        self.act_clear_gui_cache.setEnabled(False)
        self.act_clear_engine_cache = QAction(
            "Clear Cached Analysis (&Engine)", self,
            triggered=self.on_clear_engine_cache)
        self.act_clear_engine_cache.setToolTip(
            "Clear the engine's own search tree and neural-net cache. The "
            "search is halted — press Space to resume")
        self.act_clear_engine_cache.setEnabled(False)
        self.attach_menu = None  # built in _build_menus
        self.detach_menu = None  # built in _build_menus (one entry per engine)
        self.raw_nn_menu = None  # built in _build_menus (one entry per symmetry)

    def _view_toggle(self, label: str, attr: str) -> QAction:
        a = QAction(label, self, checkable=True)
        a.setChecked(getattr(self.prefs, attr))
        a.toggled.connect(lambda on, k=attr: self._set_view_pref(k, on))
        return a

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        for a in [self.act_new, self.act_open, self.act_save, self.act_save_as]:
            m_file.addAction(a)
        m_file.addSeparator()
        m_file.addAction(self.act_close_tab)
        m_file.addAction(self.act_quit)

        m_edit = mb.addMenu("&Edit")
        for a in [self.act_undo, self.act_redo]:
            m_edit.addAction(a)
        m_edit.addSeparator()
        for a in [self.act_pass, self.act_insert_empty, self.act_delete]:
            m_edit.addAction(a)
        m_edit.addSeparator()
        for a in [self.act_promote, self.act_shift_up, self.act_shift_down]:
            m_edit.addAction(a)
        m_edit.addSeparator()
        for a in [self.act_rotate_cw, self.act_rotate_ccw,
                  self.act_flip_h, self.act_flip_v]:
            m_edit.addAction(a)
        m_edit.addSeparator()
        for a in [self.act_pl_black, self.act_pl_white, self.act_pl_clear]:
            m_edit.addAction(a)

        m_nav = mb.addMenu("&Navigate")
        for a in self.nav_actions:
            m_nav.addAction(a)

        m_tools = mb.addMenu("&Tools")
        for a in self.mode_actions.values():
            m_tools.addAction(a)

        m_view = mb.addMenu("&View")
        m_view.aboutToShow.connect(self._sync_view_menu)
        for a in [self.act_coords, self.act_numbers, self.act_lastmove, self.act_hints,
                  self.act_centered]:
            m_view.addAction(a)
        m_view.addSeparator()
        for a in [self.act_overlay, self.act_policy, self.act_ownership,
                  self.act_pv_hover]:
            m_view.addAction(a)
        m_view.addSeparator()
        m_view.addAction(self.act_prefs)

        m_engine = mb.addMenu("E&ngine")
        self.attach_menu = m_engine.addMenu("&Attach Engine")
        self.attach_menu.aboutToShow.connect(self._rebuild_attach_menu)
        self.detach_menu = m_engine.addMenu("&Detach Engine")
        self.detach_menu.aboutToShow.connect(self._rebuild_detach_menu)
        m_engine.addSeparator()
        m_engine.addAction(self.act_live_analysis)
        self.raw_nn_menu = m_engine.addMenu("Raw &NN View")
        self._build_raw_nn_menu()
        m_engine.addAction(self.act_engine_rules)
        m_engine.addAction(self.act_console)
        m_engine.addSeparator()
        m_engine.addAction(self.act_clear_gui_cache)
        m_engine.addAction(self.act_clear_engine_cache)
        m_engine.addSeparator()
        m_engine.addAction(self.act_manage_engines)

        m_help = mb.addMenu("&Help")
        m_help.addAction(self.act_about)

        # Qt drops action tooltips in menus unless a menu opts in. Several
        # actions here carry a real explanation (the tools, the engine
        # commands), which was simply never reachable from the menu bar.
        for menu in mb.findChildren(QMenu):
            menu.setToolTipsVisible(True)

    def _build_raw_nn_menu(self) -> None:
        """One entry per raw-NN symmetry, in Shift+1…Shift+8 order.

        Enabled/disabled with the rest of the engine actions; the submenu makes
        the feature discoverable without having to know the hotkeys.
        """
        self.raw_nn_menu.setEnabled(False)      # needs an attached engine
        for i, sym in enumerate(RAW_NN_KEY_ORDER, 1):
            act = hint_key(
                QAction(f"Symmetry {sym}", self.raw_nn_menu,
                        triggered=lambda _=False, s=sym: self.show_raw_nn_view(s)),
                Qt.ShiftModifier | getattr(Qt, f"Key_{i}"))
            act.setToolTip(
                f"The net's evaluation of this position with no search, under "
                f"board symmetry {sym}. Esc or navigating exits.")
            self.raw_nn_menu.addAction(act)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Tools")
        tb.setMovable(False)
        self.addToolBar(tb)
        for mode in [EditMode.PLAY, EditMode.PLAY_STONE, EditMode.SETUP]:
            tb.addAction(self.mode_actions[mode])
        tb.addSeparator()
        for mode in [EditMode.MARK_TRIANGLE, EditMode.MARK_SQUARE,
                     EditMode.MARK_CIRCLE, EditMode.MARK_CROSS, EditMode.LABEL]:
            tb.addAction(self.mode_actions[mode])
        tb.addSeparator()
        tb.addAction(self.mode_actions[EditMode.SELECT])

        # Passing is common enough in review to deserve a button, but it is a
        # command, not a mode: it acts immediately instead of changing what a
        # click does. Its own toolbar keeps it visibly out of the tool row, and
        # separated the same way Navigate and Engine are — by whatever the
        # platform style draws between toolbars, rather than by a hand-made
        # rule that matches nothing else in the row.
        cmdtb = QToolBar("Commands")
        cmdtb.setMovable(False)
        self.addToolBar(cmdtb)
        self.act_pass.setIconText("Pass")
        self.act_pass.setToolTip(
            "Pass for the side to move (Ctrl+P, or Shift+Click with the Play "
            "tool)")
        cmdtb.addAction(self.act_pass)

        navtb = QToolBar("Navigate")
        navtb.setMovable(False)
        self.addToolBar(navtb)
        labels = {"start": "|◀", "back_page": "◀◀", "back": "◀",
                  "forward": "▶", "forward_page": "▶▶", "end": "▶|"}
        for cmd, text in labels.items():
            a = QAction(text, self,
                        triggered=lambda _=False, c=cmd: self._navigate(c))
            # Same wording as the Navigate menu item, hotkey tip included —
            # the glyph alone makes a poor tooltip.
            a.setToolTip(self._nav_labels[cmd])
            navtb.addAction(a)

        # Engine analysis settings for the current tab (komi/rules/search params).
        # These are distinct from the SGF's own komi/RU (the SGF komi lives in the
        # SGF Info pane); each feeds the analysis cache key.
        gametb = QToolBar("Engine")
        gametb.setMovable(False)
        self.addToolBar(gametb)

        # Each setting's label shares its spin box's tooltip (the class-level
        # TOOLTIP), so hovering either explains what the setting does.
        self.komi_label = QLabel(" Komi ")
        self.komi_label.setToolTip(KomiSpinBox.TOOLTIP)
        gametb.addWidget(self.komi_label)
        self.komi_spin = KomiSpinBox()
        self.komi_spin.valueChanged.connect(self._on_engine_komi_changed)
        gametb.addWidget(self.komi_spin)

        self.rules_button = QToolButton()
        self.rules_button.setText("Rules…")
        self.rules_button.setToolTip(_RULES_TIP)
        self.rules_button.clicked.connect(self._on_edit_rules)
        gametb.addWidget(self.rules_button)

        self.wrn_label = QLabel(" WRN ")
        self.wrn_label.setToolTip(WideRootNoiseSpinBox.TOOLTIP)
        gametb.addWidget(self.wrn_label)
        self.wrn_spin = WideRootNoiseSpinBox()
        self.wrn_spin.valueChanged.connect(self._on_wide_root_noise_changed)
        gametb.addWidget(self.wrn_spin)

        self.pda_label = QLabel(" PDA(W) ")
        self.pda_label.setToolTip(PdaSpinBox.TOOLTIP)
        gametb.addWidget(self.pda_label)
        self.pda_spin = PdaSpinBox()
        self.pda_spin.valueChanged.connect(self._on_pda_changed)
        gametb.addWidget(self.pda_spin)

        # The live-analysis state lives on the status bar (right side), so flashes
        # in the message area never clobber it. The stats moved to the per-tab
        # Analysis Info pane.
        self.engine_status = QLabel("Engine: off")
        self.statusBar().addPermanentWidget(self.engine_status)

    # -- command plumbing --------------------------------------------------

    def _tab_call(self, method: str) -> None:
        tab = self.current_tab()
        if tab is not None:
            getattr(tab, method)()

    def _tab_call2(self, method: str, arg) -> None:
        tab = self.current_tab()
        if tab is not None:
            getattr(tab, method)(arg)

    def _navigate(self, cmd: str) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.on_navigate(cmd)

    def _set_pl(self, color) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.set_player_to_move(color)

    def select_tool_by_number(self, n: int) -> bool:
        """Keys '1'-'9': pick the nth tool in toolbar order. Returns handled?"""
        if 1 <= n <= len(MODE_KEY_ORDER):
            self.set_mode(MODE_KEY_ORDER[n - 1])
            return True
        return False

    def set_mode(self, mode: EditMode) -> None:
        self.mode = mode
        self.mode_actions[mode].setChecked(True)
        tab = self.current_tab()
        if tab is not None:
            tab.on_tool_changed()
            tab.sync_board_tool()
        self.statusBar().showMessage(f"Tool: {MODE_LABELS[mode]}", 2000)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.KeyPress and self.isActiveWindow():
            focus = QApplication.focusWidget()
            if not isinstance(focus, _TEXT_WIDGETS):
                tab = self.current_tab()
                if tab is not None and tab.handle_key(event.key(), event.modifiers()):
                    return True
        elif event.type() == QEvent.MouseButtonPress and self.isActiveWindow():
            # Clicking inert chrome (toolbar gaps, labels, pane backgrounds) drops
            # keyboard focus from a text field back to the board, so navigation
            # hotkeys resume. Clicks on focusable widgets are left alone.
            self._release_focus_on_inert_click(event)
        return super().eventFilter(obj, event)

    def _release_focus_on_inert_click(self, event) -> None:
        focus = QApplication.focusWidget()
        if not isinstance(focus, _TEXT_WIDGETS):
            return
        # Resolve the deepest widget *under the cursor* (the leaf), not the
        # event's delivery target: a press can propagate up to an inert parent,
        # and a click that landed on a focusable widget must not count as inert.
        gp = event.globalPosition().toPoint()
        target = self.childAt(self.mapFromGlobal(gp)) or QApplication.widgetAt(gp)
        if not self._is_inert_target(target):
            return
        tab = self.current_tab()
        if tab is not None:
            tab.board.setFocus()
        else:
            focus.clearFocus()

    def _is_inert_target(self, w) -> bool:
        """True if ``w`` and all its ancestors decline focus (inert chrome).

        Events delivered to a focusable widget's non-focusable child (e.g. a
        text editor's viewport) are NOT inert, because an ancestor accepts
        focus — so a click into such a widget still focuses it.
        """
        while w is not None:
            if not isinstance(w, QWidget):
                return False
            if w.focusPolicy() != Qt.NoFocus:
                return False
            w = w.parentWidget()
        return True

    def _set_view_pref(self, attr: str, value: bool) -> None:
        setattr(self.prefs, attr, value)
        self.prefs.save()
        tab = self.current_tab()
        if tab is not None:
            tab.board.update()
            tab.board.refresh_readout()

    def _sync_view_menu(self) -> None:
        """Refresh the 'p'/'o' checkmarks from the current tab's board state
        (they are per-board toggles, not prefs) whenever the View menu opens."""
        tab = self.current_tab()
        self.act_policy.setChecked(
            tab is not None and tab.board.policy_mode_shown())
        self.act_ownership.setChecked(
            tab is not None and tab.board.ownership_shown())

    def _set_centered_tree(self, on: bool) -> None:
        self.prefs.centered_tree = on
        self.prefs.save()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, EditorTab):
                w.tree.set_centered(on)

    def _refresh_engine_controls(self) -> None:
        """Sync the toolbar engine settings to the current tab (or disable)."""
        tab = self.current_tab()
        self._engine_loading = True
        widgets = (self.komi_spin, self.rules_button, self.wrn_spin, self.pda_spin)
        if tab is not None:
            s = tab.analysis_settings
            for w in widgets:
                w.setEnabled(True)
            self.komi_spin.setValue(s.komi)
            self.wrn_spin.setValue(s.wide_root_noise)
            self.pda_spin.setValue(s.playout_doubling_advantage)
            self._update_rules_button(s.rules)
        else:
            for w in widgets:
                w.setEnabled(False)
        self._engine_loading = False

    def _update_rules_button(self, rules) -> None:
        # A recognised ruleset names itself; anything else is just "Rules" — the
        # button says what it configures rather than labelling the state
        # "Custom…", which read like an unchosen option.
        name = preset_name(rules)
        self.rules_button.setText(name.replace("-", " ").title()
                                  if name else "Rules")

    def _on_engine_komi_changed(self, value: float) -> None:
        if self._engine_loading:
            return
        tab = self.current_tab()
        if tab is None:
            return
        # clamp_komi snaps to a value the engine will accept (half-integer, in
        # range); write it back so the box never shows something else.
        komi = clamp_komi(value)
        if komi != value:
            self._engine_loading = True
            self.komi_spin.setValue(komi)
            self._engine_loading = False
        tab.set_engine_komi(komi)

    def _on_wide_root_noise_changed(self, value: float) -> None:
        if self._engine_loading:
            return
        tab = self.current_tab()
        if tab is not None:
            tab.set_wide_root_noise(value)

    def _on_pda_changed(self, value: float) -> None:
        if self._engine_loading:
            return
        tab = self.current_tab()
        if tab is not None:
            tab.set_playout_doubling_advantage(value)

    def _on_edit_rules(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        dlg = RulesDialog(tab.analysis_settings.rules, self)
        if dlg.exec():
            rules = dlg.result_rules()
            tab.set_engine_rules(rules)
            self._update_rules_button(rules)

    # -- engine / analysis -------------------------------------------------

    def on_manage_engines(self) -> None:
        dlg = EngineManagerDialog(self, self.engines)
        if dlg.exec() == EngineManagerDialog.Accepted:
            self.engines = dlg.engines
            save_engines(self.engines)

    def _rebuild_attach_menu(self) -> None:
        self.attach_menu.clear()
        if not self.engines:
            act = self.attach_menu.addAction("(no engines — Manage Engines…)")
            act.setEnabled(False)
            return
        attached = {(c.config.name, c.config.command)
                    for c in self.engine_controllers if c.config is not None}
        for cfg in self.engines:
            if (cfg.name, cfg.command) in attached:
                act = self.attach_menu.addAction(f"{cfg.name} (attached)")
                act.setEnabled(False)
            else:
                self.attach_menu.addAction(
                    cfg.name, lambda _=False, c=cfg: self.attach_engine(c))

    def _rebuild_detach_menu(self) -> None:
        self.detach_menu.clear()
        if not self.engine_controllers:
            self.detach_menu.addAction("(no engine attached)").setEnabled(False)
            return
        for ctrl in list(self.engine_controllers):
            self.detach_menu.addAction(
                ctrl.display_name,
                lambda _=False, c=ctrl: self.detach_engine(c))

    def _controller_for(self, config: EngineConfig) -> AnalysisController | None:
        return next((c for c in self.engine_controllers if c.config == config),
                    None)

    def attach_engine(self, config: EngineConfig) -> None:
        """Attach (start) a saved engine alongside any already attached.

        An engine that is already attached is just re-selected. A newly
        attached engine becomes the shown one by default.
        """
        existing = self._controller_for(config)
        if existing is not None:
            self.select_engine(existing)
            return
        ctrl = AnalysisController(
            self, interval_cs=self.prefs.analysis_interval_cs)
        ctrl.stateChanged.connect(self._on_engine_state)
        ctrl.analysisUpdated.connect(self._on_analysis_update)
        ctrl.engineDied.connect(self._on_engine_died)
        ctrl.consoleResponse.connect(self._on_console_response)
        ctrl.set_active(False)          # activated by select_engine below
        self.engine_controllers.append(ctrl)
        ctrl.attach(config)
        if ctrl.engine is None:
            # Start failed synchronously; _on_engine_died already cleaned up.
            return
        self.select_engine(ctrl)
        self.flash(f"Attaching engine: {config.name}")

    def detach_engine(self, ctrl: AnalysisController) -> None:
        """Stop one attached engine. If it was the shown one, fall back to the
        most recently attached remaining engine."""
        if ctrl not in self.engine_controllers:
            return
        self.engine_controllers.remove(ctrl)
        replacement = self._current_engine
        if ctrl is self._current_engine:
            ctrl.set_active(False)      # halts its search, stops board access
            self._current_engine = None
            replacement = (self.engine_controllers[-1]
                           if self.engine_controllers else None)
        ctrl.detach()
        ctrl.deleteLater()
        if replacement is not None:
            self.select_engine(replacement)
        else:
            self._update_engine_ui()
        if not self.engine_controllers and self.gtp_console is not None:
            self.gtp_console.close()

    def select_engine(self, ctrl: AnalysisController | None) -> None:
        """Make ``ctrl`` the shown engine: its analysis drives the panel, the
        board overlays, and the console. Every other attached engine is held
        open but never analysing (switching away auto-halts its search)."""
        if ctrl is not self._current_engine:
            old = self._current_engine
            self._current_engine = ctrl
            if old is not None:
                old.set_active(False)
            if ctrl is not None:
                if self._console_open():
                    ctrl.begin_console()    # console pause follows the selection
                ctrl.set_active(True)
        self._console_dead = None
        self._update_engine_ui()
        self._refresh_console_view()

    def select_engine_index(self, i: int) -> None:
        """Selector callback (Analysis Info header / console drop-down)."""
        if 0 <= i < len(self.engine_controllers):
            self.select_engine(self.engine_controllers[i])

    def _console_open(self) -> bool:
        return self.gtp_console is not None and self.gtp_console.isVisible()

    def _update_engine_ui(self) -> None:
        """Sync menus/toggles/status/selectors to the attached-engine set."""
        c = self._current_engine
        has = c is not None
        self.act_live_analysis.setEnabled(has)
        self.act_console.setEnabled(has)
        self.raw_nn_menu.setEnabled(has)
        self.act_clear_gui_cache.setEnabled(has)
        self.act_clear_engine_cache.setEnabled(has)
        if not has:
            # Nothing attached (or shown): no controller will repaint, so clear
            # every tab's analysis display outright.
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, EditorTab):
                    w.board.set_analysis(None)
                    w.board.set_raw_nn(None)
                    w.board.set_stale_ownership(None)
                    w.analysis_panel.set_stats(None)
        self._update_engine_status()

    def _on_engine_died(self, reason: str) -> None:
        """An engine vanished (bad command, crash, dropped ssh, …). Drop it from
        the attached set — falling back to another engine if it was the shown
        one — and pop the console with the reason and its captured output."""
        ctrl = self.sender()
        if ctrl not in self.engine_controllers:
            return
        name = ctrl.display_name or "engine"
        self._console_line(ctrl, f"*** Engine stopped: {reason} ***")
        dead_lines = list(ctrl.console_transcript)
        self.engine_controllers.remove(ctrl)
        replacement = self._current_engine
        if ctrl is self._current_engine:
            self._current_engine = None
            replacement = (self.engine_controllers[-1]
                           if self.engine_controllers else None)
        ctrl.deleteLater()
        if replacement is not None:
            self.select_engine(replacement)
        else:
            self._update_engine_ui()
        self.flash(f"Engine '{name}' stopped: {reason}")
        console = self._ensure_console()
        self._console_dead = name
        self._update_engine_selectors()
        console.set_transcript(dead_lines)
        console.show()
        console.raise_()
        console.activateWindow()
        # The console just became visible: the shown engine (if any) follows
        # the usual console-pause rule.
        cur = self._current_engine
        if cur is not None and not cur.is_console_paused():
            cur.begin_console()
            self._update_engine_status()

    def on_clear_gui_cache(self) -> None:
        """Forget the GUI's cached analyses for the shown engine (all
        positions); the current position re-analyses freshly if live."""
        c = self._current_engine
        if c is None:
            return
        c.clear_cache()
        self.flash(f"Cleared the GUI's cached analysis for {c.display_name}")

    def on_clear_engine_cache(self) -> None:
        """Halt any ongoing analysis and send ``clear_cache`` to the shown
        engine (clears its search tree + NN cache; press Space to resume)."""
        c = self._current_engine
        if c is None or c.engine is None:
            return
        if c.is_enabled():
            # Halts the running search (and stays halted, like pressing Space).
            self.set_live_analysis(False)
        self._console_line(c, "> clear_cache")
        c.engine.send_console("clear_cache")
        self.flash(f"Sent clear_cache to {c.display_name} — analysis halted")

    def toggle_analysis_via_key(self) -> None:
        """Spacebar: start/pause/resume live analysis at the current position."""
        c = self._current_engine
        if c is None or not c.is_attached():
            self.flash("No engine attached — use Engine ▸ Attach.")
            return
        c.exit_raw_nn()                   # starting analysis exits raw view
        self.set_live_analysis(not c.is_enabled())

    def toggle_policy_view(self) -> None:
        """'p': toggle showing policy priors instead of move stats."""
        if self._current_engine is not None:
            self._current_engine.exit_raw_nn()
        tab = self.current_tab()
        if tab is not None:
            tab.board.toggle_policy_mode()

    def toggle_ownership_view(self) -> None:
        """'o': toggle the ownership heatmap (works in raw-NN mode too)."""
        tab = self.current_tab()
        if tab is not None:
            tab.board.toggle_ownership()

    def toggle_overlay_view(self) -> None:
        """'i': toggle 'Show Move Analysis Info' (per-move discs/stats; not
        ownership)."""
        self.act_overlay.toggle()         # -> _set_view_pref keeps menu in sync

    def show_raw_nn_view(self, symmetry: int) -> None:
        """Shift+1…Shift+8: display a raw-NN evaluation under one symmetry."""
        c = self._current_engine
        if c is None or not c.is_attached():
            self.flash("No engine attached — use Engine ▸ Attach.")
            return
        c.show_raw_nn(symmetry)
        self._update_engine_status()

    def exit_raw_nn_view(self) -> bool:
        c = self._current_engine
        was = c.exit_raw_nn() if c is not None else False
        if was:
            self._update_engine_status()
        return was

    def on_live_analysis_triggered(self) -> None:
        """Engine ▸ Start/Stop Live Analysis — identical to pressing Space."""
        self.toggle_analysis_via_key()

    def set_live_analysis(self, on: bool) -> None:
        """Enable/disable live analysis for the shown engine and resync the UI."""
        if self._current_engine is not None:
            self._current_engine.set_enabled(on)
        self._update_engine_status()        # the status-bar widget is enough; no flash

    def _update_live_analysis_action(self) -> None:
        """Label the menu item with what activating it will *do*, not with the
        state it reflects — "Live Analysis" plus a checkmark left it ambiguous."""
        c = self._current_engine
        running = c is not None and c.is_attached() and c.is_enabled()
        self.act_live_analysis.setText(
            "Stop &Live Analysis" if running else "Start &Live Analysis")
        self.act_live_analysis.setToolTip(
            "Halt the running search (the last analysis stays on the board)"
            if running else
            "Start analysing the current position continuously")

    def set_analysis_readout(self, text: str) -> None:
        """Show the board's hover/pass analysis readout in the status-bar message
        area (the permanent message; transient flashes briefly overlay it)."""
        self.statusBar().showMessage(text)

    def _update_engine_status(self) -> None:
        c = self._current_engine
        self._update_engine_selectors()
        self._update_live_analysis_action()
        if c is None or not c.is_attached():
            self.engine_status.setText("Engine: off")
            self.engine_status.setStyleSheet("color: #888;")
            return
        st = c.state
        if st == "starting":
            text, color = "Engine: starting…", "#b08000"
        elif st.startswith("error"):
            text, color = "Engine: error", "#cc3333"
        elif st == "stopped":
            text, color = "Engine: off", "#888888"
        elif c.is_raw_mode():
            text, color = "◈ Raw NN view — Esc or navigate to exit", "#3a7fd0"
        elif c.is_console_paused():
            text, color = "❚❚ Console (analysis paused)", "#888888"
        elif not c.is_enabled():
            text, color = "❚❚ Analysis paused — press Space to resume", "#888888"
        else:
            text, color = "● Analyzing — press Space to pause", "#2f9e2f"
        self.engine_status.setText(text)
        self.engine_status.setStyleSheet(
            f"color: {color}; font-weight: bold;" if color else "")

    def _update_engine_selectors(self) -> None:
        """Fan the attached-engine list + current selection out to every tab's
        Analysis Info header selector and to the console's drop-down (they all
        mirror the same window-wide selection)."""
        names = [c.display_name for c in self.engine_controllers]
        try:
            cur = self.engine_controllers.index(self._current_engine)
        except ValueError:
            cur = -1
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab):
                tab.set_engine_choices(names, cur)
        if self.gtp_console is not None:
            if self._console_dead is not None:
                names = names + [f"{self._console_dead} (stopped)"]
                cur = len(names) - 1
            self.gtp_console.set_engines(names, cur)

    def _ensure_console(self) -> GtpConsole:
        """Create the GTP console widget on first use (content is filled from
        the shown engine's transcript by _refresh_console_view)."""
        if self.gtp_console is None:
            self.gtp_console = GtpConsole(self)
            self.gtp_console.commandEntered.connect(self._console_command)
            self.gtp_console.engineSelected.connect(self.select_engine_index)
            self.gtp_console.closed.connect(self._console_closed)
        return self.gtp_console

    def _refresh_console_view(self) -> None:
        """Fill the console output with the shown engine's transcript."""
        if self.gtp_console is None or self._console_dead is not None:
            return
        c = self._current_engine
        self.gtp_console.set_transcript(
            list(c.console_transcript) if c is not None else [])

    def on_gtp_console(self) -> None:
        c = self._current_engine
        if c is None or not c.is_attached():
            return
        console = self._ensure_console()
        self._console_dead = None
        c.begin_console()
        self._update_engine_status()
        self._refresh_console_view()
        console.show()
        console.raise_()
        console.activateWindow()

    def _console_command(self, text: str) -> None:
        c = self._current_engine
        if c is None or c.engine is None:
            return
        if self._console_dead is not None:
            # Typing while a dead engine's snapshot is shown targets the shown
            # LIVE engine, so switch the view to it first.
            self._console_dead = None
            self._update_engine_selectors()
            self._refresh_console_view()
        self._console_line(c, f"> {text}")
        c.engine.send_console(text)

    def _console_closed(self) -> None:
        self._console_dead = None
        if self._current_engine is not None:
            self._current_engine.end_console()
        self._update_engine_status()

    def _console_line(self, ctrl, line: str) -> None:
        """Record a line in ``ctrl``'s console transcript, and show it live if
        the console is currently displaying that engine."""
        t = ctrl.console_transcript
        t.append(line)
        if len(t) > 1000:
            del t[:500]
        if (self._console_open() and ctrl is self._current_engine
                and self._console_dead is None):
            self.gtp_console.append_line(line)

    def on_engine_log(self, ctrl, line: str) -> None:
        self._console_line(ctrl, f"  [engine] {line}")

    def _on_engine_state(self, state: str) -> None:
        if self.sender() is not self._current_engine:
            return                        # a background engine's state change
        if state.startswith("error"):
            self.flash(state)
        self._update_engine_status()

    def _on_console_response(self, command: str, ok: bool, text: str) -> None:
        ctrl = self.sender()
        if ctrl is None:
            return
        prefix = "=" if ok else "?"
        self._console_line(ctrl, f"{prefix} {text}".rstrip())

    def _on_analysis_update(self, stats) -> None:
        """``stats`` is a PanelStats (Black-perspective) for the current position,
        or None. The controller has already done the perspective conversion and
        the parent-continuity fallback."""
        if self.sender() is not self._current_engine:
            return                        # emission from a deselected engine
        tab = self.current_tab()
        if tab is None:
            return
        tab.analysis_panel.set_stats(stats)

    def flash(self, message: str) -> None:
        self.statusBar().showMessage(message, 4000)

    def update_undo_actions(self) -> None:
        tab = self.current_tab()
        self.act_undo.setEnabled(bool(tab and tab.document.can_undo()))
        self.act_redo.setEnabled(bool(tab and tab.document.can_redo()))

    def set_subtree_mark(self, tab, node, mode: str) -> None:
        old = self.subtree_mark
        self.subtree_mark = (tab, node, mode)
        if old is not None and old[0] is not tab:
            old[0].refresh_subtree_outline()
        tab.refresh_subtree_outline()

    def clear_subtree_mark(self) -> None:
        old = self.subtree_mark
        self.subtree_mark = None
        if old is not None:
            old[0].refresh_subtree_outline()

    # -- file commands -----------------------------------------------------

    def new_game(self, size: int) -> EditorTab:
        return self._add_tab(Document.new(size))

    def on_new(self) -> None:
        dlg = NewGameDialog(self, self.prefs.default_board_size)
        if dlg.exec() == NewGameDialog.Accepted:
            w, h = dlg.result_size()
            self._add_tab(Document.new(w, h))

    def _last_dir(self) -> str:
        """The folder used by the previous open/save, remembered across runs."""
        return str(QSettings(ORG, APP).value("state/last_dir", "") or "")

    def _remember_dir(self, path: str) -> None:
        s = QSettings(ORG, APP)
        s.setValue("state/last_dir", os.path.dirname(os.path.abspath(path)))
        s.sync()

    def on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open SGF", self._last_dir(), OPEN_FILTER)
        if not path:
            return
        self.open_path(path)

    def open_path(self, path: str) -> None:
        if path.lower().endswith(".sgfs"):
            self._open_sgfs(path)
            return
        try:
            doc = Document.open(path)
        except Exception as e:  # noqa: BLE001 - surface any load error to the user
            QMessageBox.critical(self, "Open failed", f"Could not open file:\n{e}")
            return
        self._add_tab(doc)
        self._remember_dir(path)
        self.flash(f"Opened {os.path.basename(path)}")

    def _open_sgfs(self, path: str) -> None:
        """Open a ``.sgfs`` (one SGF per line) as one tab per game."""
        name = os.path.basename(path)
        try:
            docs, failed = Document.open_sgfs(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", f"Could not open file:\n{e}")
            return
        if not docs:
            msg = (f"None of the {failed} game lines in {name} could be parsed."
                   if failed else f"{name} contains no games.")
            QMessageBox.critical(self, "Open failed", msg)
            return
        if len(docs) > 1:
            res = QMessageBox.question(
                self, "Open multiple games",
                f"{name} contains {len(docs)} games.\n"
                f"Open all of them, each as its own tab?",
                QMessageBox.Yes | QMessageBox.No)
            if res != QMessageBox.Yes:
                return
        first = None
        for doc in docs:
            tab = self._add_tab(doc)
            first = first or tab
        self.tabs.setCurrentWidget(first)
        self._remember_dir(path)
        if failed:
            QMessageBox.warning(
                self, "Some games failed to parse",
                f"{failed} of the {failed + len(docs)} game lines in {name} "
                f"could not be parsed; opened the other {len(docs)}.")
        self.flash(f"Opened {len(docs)} game{'s' if len(docs) != 1 else ''} "
                   f"from {name}")

    def on_save(self) -> bool:
        tab = self.current_tab()
        if tab is None:
            return False
        if tab.document.path is None:
            return self.on_save_as()
        return self._do_save(tab, tab.document.path)

    def on_save_as(self) -> bool:
        tab = self.current_tab()
        if tab is None:
            return False
        start = tab.document.path or self._last_dir()
        path, _ = QFileDialog.getSaveFileName(self, "Save SGF", start, SGF_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".sgf"):
            path += ".sgf"
        return self._do_save(tab, path)

    def _do_save(self, tab: EditorTab, path: str) -> bool:
        try:
            tab.document.save(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", f"Could not save file:\n{e}")
            return False
        self._refresh_tab_title(tab)
        self._remember_dir(path)
        self.flash(f"Saved {os.path.basename(path)}")
        return True

    def close_tab(self, index: int) -> None:
        w = self.tabs.widget(index)
        if not isinstance(w, EditorTab):
            return
        if w.document.dirty and not self._confirm_discard(w):
            return
        # A window-level subtree mark owned by this tab would keep a reference
        # to the dead widget (crashing any later clear/paste) and, for a cut,
        # would mutate a document nobody can see. Drop it, without touching
        # the closing tab's widgets.
        if self.subtree_mark is not None and self.subtree_mark[0] is w:
            self.subtree_mark = None
        self.tabs.removeTab(index)
        w.deleteLater()

    def _confirm_discard(self, tab: EditorTab) -> bool:
        res = QMessageBox.question(
            self, "Unsaved changes",
            f"'{tab.document.title.lstrip('* ')}' has unsaved changes. Save before closing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if res == QMessageBox.Cancel:
            return False
        if res == QMessageBox.Save:
            self.tabs.setCurrentWidget(tab)
            return self.on_save()
        return True

    # -- preferences / about ----------------------------------------------

    def on_preferences(self) -> None:
        dlg = PreferencesDialog(self.prefs, self)
        if dlg.exec() == PreferencesDialog.Accepted:
            dlg.apply_to(self.prefs)
            self.prefs.save()
            self.act_coords.setChecked(self.prefs.show_coordinates)
            self.act_numbers.setChecked(self.prefs.show_move_numbers)
            self.act_lastmove.setChecked(self.prefs.show_last_move_marker)
            self.act_hints.setChecked(self.prefs.show_variation_hints)
            self.act_centered.setChecked(self.prefs.centered_tree)
            self.act_overlay.setChecked(self.prefs.show_analysis_overlay)
            self.act_pv_hover.setChecked(self.prefs.show_pv_on_hover)
            for ctrl in self.engine_controllers:
                ctrl.set_interval_cs(self.prefs.analysis_interval_cs)
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, EditorTab):
                    w.apply_prefs(self.prefs)

    def on_about(self) -> None:
        QMessageBox.about(
            self, "About Katsura",
            "<b>Katsura</b><br>A graphical SGF editor for Go.<br><br>"
            "Navigation: ←/→ move, ↑/↓ switch variation, PgUp/PgDn ±10 moves, "
            "Home/End start/end (when the board or tree has focus).<br><br>"
            "Tools: <b>1</b>–<b>9</b> select the tool (Play, Play Stone, Setup, "
            "the four marks, Label, Select).<br><br>"
            "Analysis: <b>Space</b> start/pause, <b>i</b> move analysis info, "
            "<b>p</b> policy priors, <b>o</b> ownership heatmap, "
            "<b>Shift+1</b>–<b>Shift+8</b> raw-NN under symmetries (Esc exits).")

    # -- signal handlers ---------------------------------------------------

    def _on_tab_changed(self, index: int) -> None:
        self.update_undo_actions()
        self._refresh_engine_controls()
        tab = self.current_tab()
        if tab is not None:
            tab.refresh_subtree_outline()
        self.refresh_engine_position()

    def _on_tab_state_changed(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            self._refresh_tab_title(tab)
            self.update_undo_actions()
            self._refresh_engine_controls()
        self.refresh_engine_position()

    def _refresh_tab_title(self, tab: EditorTab) -> None:
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, tab.document.title)

    # -- window close ------------------------------------------------------

    def closeEvent(self, event) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, EditorTab) and w.document.dirty:
                self.tabs.setCurrentIndex(i)
                if not self._confirm_discard(w):
                    event.ignore()
                    return
        for ctrl in list(self.engine_controllers):
            ctrl.detach()
        self.engine_controllers.clear()
        self._current_engine = None
        event.accept()
