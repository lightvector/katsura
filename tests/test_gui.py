"""Headless (offscreen) smoke tests for the Qt front end."""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from katsura.go.board import BLACK, WHITE
from katsura.sgf.coords import Point
from katsura.ui.document import Document
from katsura.ui.modes import EditMode


def P(x, y):
    return Point(x, y)


def click(tab, pt, mods=Qt.NoModifier):
    tab.on_board_click(pt, mods)


def _close(w):
    """Close a window without triggering the unsaved-changes modal in headless tests."""
    for i in range(w.tabs.count()):
        tab = w.tabs.widget(i)
        if hasattr(tab, "document"):
            tab.document.dirty = False
    w.close()


def test_mainwindow_constructs(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    assert w.current_tab() is not None
    _close(w)


def test_play_navigate_branch_save_load(qapp, tmp_path):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game

    for p in [P(3, 3), P(15, 15), P(2, 2)]:
        click(tab, p)
    assert g.move_number == 3
    assert tab.document.dirty

    tab.on_navigate("start")
    assert g.move_number == 0
    tab.on_navigate("forward_page")
    assert g.move_number == 3

    # Branch from move 1.
    tab.on_navigate("start")
    tab.on_navigate("forward")
    click(tab, P(16, 16))
    assert len(g.root.children[0].children) == 2

    # Save and reload round-trips the structure.
    path = tmp_path / "game.sgf"
    tab.document.save(str(path))
    assert not tab.document.dirty
    doc2 = Document.open(str(path))
    assert sum(1 for _ in doc2.game.root.walk()) == sum(1 for _ in g.root.walk())
    _close(w)


def test_setup_mark_label_modes(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game

    w.set_mode(EditMode.SETUP)
    click(tab, P(9, 9))
    assert g.board.get(9, 9) == BLACK

    w.set_mode(EditMode.MARK_TRIANGLE)
    click(tab, P(5, 5))
    assert g.get_marks()[P(5, 5)] == "TR"

    w.set_mode(EditMode.LABEL)
    click(tab, P(6, 6))
    assert g.get_labels()[P(6, 6)] == "A"

    # Right-click erases the label.
    tab.on_board_right(P(6, 6))
    assert P(6, 6) not in g.get_labels()
    _close(w)


def test_undo_redo(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game

    click(tab, P(3, 3))
    assert g.move_number == 1
    tab.undo()
    assert tab.game.move_number == 0
    tab.redo()
    assert tab.game.move_number == 1
    _close(w)


def test_redo_returns_to_where_each_edit_left_you(qapp):
    """Redo puts the cursor where the edit *ended*, not where you were when you
    happened to press undo.

    Edit A, navigate away, edit C, navigate away, then undo twice and redo
    twice: each redo must land on the node its own edit produced, since that is
    the only position that explains what was just re-applied.
    """
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game

    click(tab, P(3, 3))                       # edit A: creates a move off the root
    move_a = g.current.get_one("B")
    g.go_to_start(); tab._post_navigate()     # navigate to B (the root)

    click(tab, P(9, 9))                       # edit C: a sibling branch off root
    move_c = tab.game.current.get_one("B")
    # Navigate to D — somewhere unrelated to either edit (edit A's node).
    tab.game.goto(tab.game.root.children[0]); tab._post_navigate()
    assert tab.game.current.get_one("B") == move_a

    # Undo: each step lands just *before* the edit it reverted (unchanged).
    tab.undo()
    assert tab.game.current is tab.game.root           # where C started
    tab.undo()
    assert tab.game.current is tab.game.root           # where A started

    # Redo: each step lands where that edit left us — A's new node, then C's —
    # NOT at B and D, which is what the pre-fix behaviour did.
    tab.redo()
    assert tab.game.current.get_one("B") == move_a
    tab.redo()
    assert tab.game.current.get_one("B") == move_c
    _close(w)


def test_redo_after_delete_returns_to_the_surviving_node(qapp):
    """A deletion's "end position" is the parent it fell back to."""
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()

    click(tab, P(3, 3))
    click(tab, P(15, 15))
    parent = tab.game.current.parent
    tab.delete_via_key()                      # deletes move 2, lands on move 1
    assert tab.game.current is parent
    tab.game.go_to_start(); tab._post_navigate()      # roam elsewhere
    tab.undo()
    assert tab.game.move_number == 2                  # the delete is undone
    tab.game.go_to_start(); tab._post_navigate()      # roam again
    tab.redo()
    assert tab.game.move_number == 1                  # back at the delete's end
    _close(w)


def test_redo_survives_abandoned_edits(qapp):
    """A speculative edit that turns out to be a no-op must not clear redo."""
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()

    click(tab, P(3, 3)); click(tab, P(15, 15))
    tab.undo()
    assert tab.game.move_number == 1 and tab.document.can_redo()

    click(tab, P(3, 3))              # occupied -> GameError -> edit discarded
    assert tab.document.can_redo()
    tab.shift_variation(-1)          # no sibling to swap with -> no-op
    assert tab.document.can_redo()
    tab.promote_current()            # nothing to promote -> no-op
    assert tab.document.can_redo()
    tab.on_comment_focus_in()        # click into the comment box...
    tab.on_comment_focus_out()       # ...and out without typing
    assert tab.document.can_redo()

    tab.redo()
    assert tab.game.move_number == 2
    _close(w)


def test_comment_session_is_one_undo_step(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    click(tab, P(3, 3))
    tab.on_comment_focus_in()
    tab.comment.setPlainText("hel")
    tab.comment.setPlainText("hello")
    tab.on_comment_focus_out()
    assert tab.game.get_comment() == "hello"
    tab.undo()                       # one step reverts the whole session...
    assert tab.game.get_comment() == ""
    assert tab.game.move_number == 1  # ...but not the move before it
    tab.redo()
    assert tab.game.get_comment() == "hello"
    _close(w)


def test_paste_subtree_is_undoable(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    click(tab, P(3, 3)); click(tab, P(4, 4))
    tab.on_navigate("start"); tab.on_navigate("forward")
    tab.copy()                       # mark the move-1 subtree
    tab.on_navigate("start")
    n_before = sum(1 for _ in tab.game.root.walk())
    tab.paste()                      # graft a copy at the root
    assert sum(1 for _ in tab.game.root.walk()) > n_before
    tab.undo()
    assert sum(1 for _ in tab.game.root.walk()) == n_before
    _close(w)


def test_deep_dense_game_loads_without_recursion_crash(qapp):
    """Tree layout, move numbers, and subtree copy on a game whose depth (and
    branch nesting) far exceeds the Python recursion limit."""
    from katsura.model.game import Game
    from katsura.sgf.tree import SgfNode
    from katsura.ui.mainwindow import MainWindow

    root = SgfNode()
    root.set_one("SZ", "19")
    cur = root
    for k in range(1500):
        var = cur.add_child()                      # a variation at every level
        var.set_one("B" if k % 2 == 0 else "W", "ab")
        cur = cur.add_child()
        cur.set_one("B" if k % 2 == 0 else "W", "aa")
    game = Game(root)
    doc = Document(roots=[root], game=game)

    w = MainWindow()
    w._add_tab(doc)                                # relayout + move numbers
    tab = w.current_tab()
    assert tab.game is game
    # Subtree copy/paste exercises SgfNode.clone on the deep chain.
    n_top = root.children[-1]
    total = sum(1 for _ in n_top.walk())
    assert sum(1 for _ in n_top.clone().walk()) == total
    _close(w)


def test_illegal_move_flashes_no_crash(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    click(tab, P(3, 3))      # black
    tab.on_navigate("start")
    # Re-playing onto the same point from start is a different color (still legal
    # as a branch); instead set up an occupied-suicide rejection.
    tab.on_navigate("end")
    n_before = sum(1 for _ in tab.game.root.walk())
    click(tab, P(3, 3))      # occupied -> rejected, no new node
    assert sum(1 for _ in tab.game.root.walk()) == n_before
    _close(w)


def test_non_square_board_tab(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    w._add_tab(Document.new(2, 9))
    tab = w.current_tab()
    assert tab.game.width == 2 and tab.game.height == 9
    click(tab, P(0, 0))
    click(tab, P(1, 5))
    assert tab.game.move_number == 2
    _close(w)


def test_ctrl_arrows_switch_and_move_tabs(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()                       # starts with one tab
    t1 = w.current_tab()
    w._add_tab(Document.new(9))
    t2 = w.current_tab()
    w._add_tab(Document.new(13))
    t3 = w.current_tab()
    assert w.tabs.currentIndex() == 2
    # Ctrl+Right wraps from the last tab to the first; Ctrl+Left wraps back.
    assert t3.handle_key(Qt.Key_Right, Qt.ControlModifier)
    assert w.current_tab() is t1
    t1.handle_key(Qt.Key_Left, Qt.ControlModifier)
    assert w.current_tab() is t3
    # Ctrl+Shift+Left swaps the current tab leftward and keeps it selected.
    assert t3.handle_key(Qt.Key_Left, Qt.ShiftModifier | Qt.ControlModifier)
    assert w.current_tab() is t3
    assert w.tabs.currentIndex() == 1
    assert w.tabs.widget(2) is t2
    # Moving past either end is a no-op (no wrap).
    t3.handle_key(Qt.Key_Left, Qt.ShiftModifier | Qt.ControlModifier)
    t3.handle_key(Qt.Key_Left, Qt.ShiftModifier | Qt.ControlModifier)
    assert w.tabs.currentIndex() == 0 and w.current_tab() is t3
    _close(w)


def test_untitled_tabs_are_numbered(qapp):
    import re

    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    w._add_tab(Document.new(9))
    names = [w.tabs.tabText(i) for i in range(w.tabs.count())]
    assert all(re.fullmatch(r"Untitled\d+", n) for n in names)
    nums = [int(n[len("Untitled"):]) for n in names]
    assert nums[1] > nums[0]
    _close(w)


def test_open_sgfs_multi(qapp, tmp_path, monkeypatch):
    from katsura.ui import mainwindow as mw
    monkeypatch.setattr(mw.QMessageBox, "question",
                        lambda *a, **k: mw.QMessageBox.Yes)
    warnings = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda *a, **k: warnings.append(a))
    # Mixed line endings, a blank line, a garbage line, trailing newlines.
    path = tmp_path / "games.sgfs"
    path.write_bytes(
        b"(;GM[1]FF[4]SZ[19];B[pd])\r\n"
        b"\r\n"
        b"(;GM[1]FF[4]SZ[9];B[cc])\n"
        b"this line is garbage\n"
        b"\n")
    w = mw.MainWindow()
    n0 = w.tabs.count()
    w.open_path(str(path))
    assert w.tabs.count() == n0 + 2
    # First opened game is selected; titles are stem + game-line index.
    assert w.tabs.tabText(w.tabs.currentIndex()) == "games[1]"
    assert w.tabs.tabText(n0 + 1) == "games[2]"
    tab = w.current_tab()
    # Pathless + clean: Save behaves like Save As, closing doesn't prompt.
    assert tab.document.path is None and not tab.document.dirty
    assert tab.game.width == 19
    assert warnings                       # the garbage line was reported
    _close(w)


def test_open_sgfs_declined(qapp, tmp_path, monkeypatch):
    from katsura.ui import mainwindow as mw
    monkeypatch.setattr(mw.QMessageBox, "question",
                        lambda *a, **k: mw.QMessageBox.No)
    path = tmp_path / "two.sgfs"
    path.write_text("(;GM[1]FF[4]SZ[19])\n(;GM[1]FF[4]SZ[19])\n")
    w = mw.MainWindow()
    n0 = w.tabs.count()
    w.open_path(str(path))
    assert w.tabs.count() == n0            # declined: nothing opened
    _close(w)


def test_open_sgfs_single_game_no_prompt(qapp, tmp_path, monkeypatch):
    from katsura.ui import mainwindow as mw

    def boom(*a, **k):
        raise AssertionError("must not prompt for a single game")

    monkeypatch.setattr(mw.QMessageBox, "question", boom)
    path = tmp_path / "one.sgfs"
    path.write_text("(;GM[1]FF[4]SZ[13];B[cc])\n")
    w = mw.MainWindow()
    n0 = w.tabs.count()
    w.open_path(str(path))
    assert w.tabs.count() == n0 + 1
    assert w.current_tab().game.width == 13
    _close(w)


def test_open_sgfs_no_valid_games(qapp, tmp_path, monkeypatch):
    from katsura.ui import mainwindow as mw
    errors = []
    monkeypatch.setattr(mw.QMessageBox, "critical",
                        lambda *a, **k: errors.append(a))
    w = mw.MainWindow()
    n0 = w.tabs.count()
    bad = tmp_path / "bad.sgfs"
    bad.write_text("garbage\nmore garbage\n")
    w.open_path(str(bad))
    assert w.tabs.count() == n0 and len(errors) == 1
    empty = tmp_path / "empty.sgfs"
    empty.write_text("\n\n")
    w.open_path(str(empty))
    assert w.tabs.count() == n0 and len(errors) == 2
    _close(w)


def test_zero_tabs_supported(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow(initial_tab=False)      # CLI file-open path starts like this
    assert w.tabs.count() == 0 and w.current_tab() is None
    # Window-level operations are safe no-ops without tabs.
    w._navigate("forward")
    w.update_undo_actions()
    w.new_game(9)
    assert w.tabs.count() == 1
    # Closing the last tab leaves the window empty (no auto-reopen).
    w.current_tab().document.dirty = False
    w.close_tab(0)
    assert w.tabs.count() == 0 and w.current_tab() is None
    _close(w)


def test_play_modifiers(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    click(tab, P(3, 3))                     # black; now white to move
    assert g.to_move == WHITE
    # Ctrl: transient flip (no move, no SGF change).
    n_before = sum(1 for _ in g.root.walk())
    click(tab, P(9, 9), Qt.ControlModifier)
    assert tab.transient_color == BLACK
    assert sum(1 for _ in g.root.walk()) == n_before
    # Now a plain click plays the transient colour (black).
    click(tab, P(4, 4))
    assert g.board.get(4, 4) == BLACK
    # Shift+Ctrl writes a PL override.
    click(tab, P(0, 0), Qt.ShiftModifier | Qt.ControlModifier)
    assert g.current.has("PL")
    _close(w)


def test_play_stone_and_setup_tools(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.PLAY_STONE)
    click(tab, P(2, 2))
    click(tab, P(3, 3), Qt.ShiftModifier)
    assert g.board.get(2, 2) == BLACK and g.board.get(3, 3) == WHITE
    # Setup tool: toggle erase + forced redundant.
    w._add_tab(Document.new(19))
    t2 = w.current_tab()
    g2 = t2.game
    w.set_mode(EditMode.SETUP)
    click(t2, P(5, 5))
    assert g2.board.get(5, 5) == BLACK
    click(t2, P(5, 5))                       # toggle erase
    assert g2.board.get(5, 5) == EMPTY
    _close(w)


def test_setup_capture_baked_via_clicks(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.SETUP)
    click(tab, P(0, 0))                          # black corner
    click(tab, P(1, 0), Qt.ShiftModifier)        # white
    click(tab, P(0, 1), Qt.ShiftModifier)        # white -> captures the corner
    assert g.board.get(0, 0) == EMPTY
    assert not g.root.has("AB")                  # the capture is baked into the SGF
    assert P(0, 0) not in g.setup_ghosts
    # The whole edit (placement + baked capture) is one undo step.
    tab.undo()
    assert tab.game.board.get(0, 0) == BLACK and tab.game.root.get("AB") == ["aa"]
    _close(w)


def test_setup_force_ghost_then_toggle_off(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.SETUP)
    click(tab, P(0, 0))                                       # black corner
    sc = Qt.ShiftModifier | Qt.ControlModifier               # white, forced (no resolve)
    click(tab, P(1, 0), sc)
    click(tab, P(0, 1), sc)
    assert g.board.get(0, 0) == EMPTY
    assert g.setup_ghosts == {P(0, 0): BLACK}                # illegal setup kept as ghost
    # Clicking black on the ghost treats it as present and toggles it off.
    click(tab, P(0, 0))
    assert not g.root.has("AB")
    assert P(0, 0) not in g.setup_ghosts
    _close(w)


def test_setup_opposite_colour_click_erases_first(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.SETUP)
    click(tab, P(5, 5), Qt.ShiftModifier)        # a white stone
    assert g.board.get(5, 5) == WHITE
    click(tab, P(5, 5))                          # black tool: erases white first...
    assert g.board.get(5, 5) == EMPTY
    click(tab, P(5, 5))                          # ...and a second click places black
    assert g.board.get(5, 5) == BLACK
    _close(w)


def test_setup_drag_from_empty_overwrites_opposite(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.SETUP)
    click(tab, P(3, 2), Qt.ShiftModifier)        # a lone white stone in the path
    assert g.board.get(3, 2) == WHITE
    # A black stroke started on an empty cell is a *placing* stroke; crossing the
    # white stone overwrites it to black directly (start cell fixes the action).
    tab.on_paint_begin(P(2, 2), Qt.NoModifier)
    for x in range(3, 6):
        tab.on_paint_move(P(x, 2))
    tab.on_paint_end()
    assert all(g.board.get(x, 2) == BLACK for x in range(2, 6))
    _close(w)


def test_setup_drag_from_stone_erases_across_colours(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.SETUP)
    click(tab, P(2, 2))                          # black at the start cell
    click(tab, P(4, 2), Qt.ShiftModifier)        # white further along the row
    # A black stroke started on the black stone is an *erasing* stroke; it clears
    # every cell it crosses, white included.
    tab.on_paint_begin(P(2, 2), Qt.NoModifier)
    for x in range(3, 6):
        tab.on_paint_move(P(x, 2))
    tab.on_paint_end()
    assert all(g.board.get(x, 2) == EMPTY for x in range(2, 6))
    _close(w)


def test_board_renders_with_ghost(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.SETUP)
    click(tab, P(0, 0))                                      # black corner
    sc = Qt.ShiftModifier | Qt.ControlModifier
    click(tab, P(1, 0), sc); click(tab, P(0, 1), sc)        # forced white -> ghost
    assert g.setup_ghosts == {P(0, 0): BLACK}
    tab.board.resize(400, 400)
    pm = tab.board.grab()           # forces a paintEvent through the ghost/halo path
    assert not pm.isNull()
    _close(w)


def test_paint_stroke_resolves_captures(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.SETUP)
    click(tab, P(0, 0))                          # black corner
    # Paint a white stroke over the corner's two liberties.
    tab.on_paint_begin(P(1, 0), Qt.ShiftModifier)
    tab.on_paint_move(P(0, 1))
    tab.on_paint_end()
    assert g.board.get(0, 0) == EMPTY            # captured
    assert not g.root.has("AB")                  # and baked out of the SGF
    assert P(0, 0) not in g.setup_ghosts
    _close(w)


def test_label_letter_number(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    w.set_mode(EditMode.LABEL)
    click(tab, P(0, 0))
    click(tab, P(1, 0))
    assert g.get_labels()[P(0, 0)] == "A" and g.get_labels()[P(1, 0)] == "B"
    click(tab, P(2, 0), Qt.ShiftModifier)
    assert g.get_labels()[P(2, 0)] == "1"
    _close(w)


def test_selection_tool_copy_paste(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.SETUP)
    click(tab, P(3, 3))
    click(tab, P(4, 3), Qt.ShiftModifier)        # white
    w.set_mode(EditMode.SELECT)
    tab.on_selection_rect(P(3, 3), P(4, 3), Qt.NoModifier)
    assert len(tab.selection) == 2
    tab.selection_copy()
    assert tab.paste_active and w.selection_buffer is not None
    tab.rotate_paste(); tab.flip_paste()          # transforms don't crash
    n_before = sum(1 for y in range(19) for x in range(19) if tab.game.board.get(x, y))
    tab.on_paste_at(P(10, 10))
    assert tab.paste_active is False               # single paste exits state
    n_after = sum(1 for y in range(19) for x in range(19) if tab.game.board.get(x, y))
    assert n_after == n_before + 2
    _close(w)


def test_selection_cut_and_move_undo(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.SETUP)
    click(tab, P(8, 8))
    w.set_mode(EditMode.SELECT)
    tab.on_selection_rect(P(8, 8), P(8, 8), Qt.NoModifier)
    tab.on_selection_move(2, 0)
    assert tab.game.board.get(8, 8) == EMPTY and tab.game.board.get(10, 8) == BLACK
    tab.undo()
    assert tab.game.board.get(8, 8) == BLACK and tab.game.board.get(10, 8) == EMPTY
    # Cut buffers + erases; Esc leaves paste state.
    tab.on_selection_rect(P(8, 8), P(8, 8), Qt.NoModifier)
    tab.selection_cut()
    assert tab.game.board.get(8, 8) == EMPTY and tab.paste_active
    tab.cancel_all()
    assert tab.paste_active is False
    _close(w)


def test_rotate_whole_board_via_tab_and_undo(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.PLAY)
    click(tab, P(3, 2))                       # black stone at (3, 2)
    assert tab.game.board.get(3, 2) == BLACK
    tab.rotate_cw()                            # (x,y) -> (18-y, x): (3,2)->(16,3)
    assert tab.game.board.get(3, 2) == EMPTY
    assert tab.game.board.get(16, 3) == BLACK
    tab.undo()
    assert tab.game.board.get(3, 2) == BLACK and tab.game.board.get(16, 3) == EMPTY
    _close(w)


def test_delete_key_erases_selection(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.SETUP)
    click(tab, P(8, 8))
    w.set_mode(EditMode.SELECT)
    tab.on_selection_rect(P(8, 8), P(8, 8), Qt.NoModifier)
    tab.handle_key(Qt.Key_Delete, Qt.NoModifier)
    # Selected stone is erased without entering paste state, and selection clears.
    assert tab.game.board.get(8, 8) == EMPTY
    assert not tab.paste_active and not tab.selection
    tab.undo()
    assert tab.game.board.get(8, 8) == BLACK
    _close(w)


def test_switching_tool_clears_selection(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.SETUP)
    click(tab, P(3, 3))                          # content so the region selects
    w.set_mode(EditMode.SELECT)
    tab.on_selection_rect(P(2, 2), P(4, 4), Qt.NoModifier)
    assert tab.selection
    w.set_mode(EditMode.PLAY)
    assert not tab.selection
    _close(w)


def test_selecting_empty_region_clears_selection(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.SETUP)
    click(tab, P(3, 3))
    w.set_mode(EditMode.SELECT)
    tab.on_selection_rect(P(3, 3), P(3, 3), Qt.NoModifier)   # over the stone
    assert tab.selection
    # Dragging across empty board (or a bare click on nothing) deselects.
    tab.on_selection_rect(P(10, 10), P(12, 12), Qt.NoModifier)
    assert not tab.selection
    _close(w)


def test_paint_stroke_setup(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.SETUP)
    tab.on_paint_begin(P(2, 2), Qt.NoModifier)
    for x in range(3, 7):
        tab.on_paint_move(P(x, 2))
    tab.on_paint_end()
    assert all(tab.game.board.get(x, 2) == BLACK for x in range(2, 7))
    tab.undo()                                   # whole stroke is one undo step
    assert all(tab.game.board.get(x, 2) == EMPTY for x in range(2, 7))
    _close(w)


def test_paint_stroke_erases_when_started_on_stone(qapp):
    from katsura.go.board import EMPTY
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.SETUP)
    for x in range(2, 6):                         # lay a row
        tab.on_paint_begin(P(x, 2), Qt.NoModifier)
        tab.on_paint_end()
    tab.on_paint_begin(P(2, 2), Qt.NoModifier)    # start on a stone -> erase action
    for x in range(3, 6):
        tab.on_paint_move(P(x, 2))
    tab.on_paint_end()
    assert all(tab.game.board.get(x, 2) == EMPTY for x in range(2, 6))
    _close(w)


def test_paste_centers_on_cursor(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    w.set_mode(EditMode.SETUP)
    tab.on_paint_begin(P(10, 10), Qt.NoModifier)
    tab.on_paint_move(P(11, 10))
    tab.on_paint_move(P(12, 10))
    tab.on_paint_end()
    w.set_mode(EditMode.SELECT)
    tab.on_selection_rect(P(10, 10), P(12, 10), Qt.NoModifier)
    tab.selection_copy()
    assert tab.paste_center == (1, 0)             # middle of a 3-wide row
    tab.on_paste_at(P(5, 5))                       # cursor = centre
    assert all(tab.game.board.get(x, 5) == BLACK for x in (4, 5, 6))
    _close(w)


def test_navigation_clears_selection(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    click(tab, P(3, 3))                            # a move to navigate over
    w.set_mode(EditMode.SELECT)
    tab.on_selection_rect(P(0, 0), P(3, 3), Qt.NoModifier)   # includes the stone
    assert tab.selection
    tab.on_navigate("back")
    assert not tab.selection
    _close(w)


def test_cross_sgf_selection_paste(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    ta = w.current_tab()
    w.set_mode(EditMode.SETUP)
    ta.on_paint_begin(P(3, 3), Qt.NoModifier); ta.on_paint_end()
    w.set_mode(EditMode.SELECT)
    ta.on_selection_rect(P(3, 3), P(3, 3), Qt.NoModifier)
    ta.selection_copy()                         # buffer is shared on the window
    # New tab, paste from the shared buffer.
    w._add_tab(Document.new(19))
    tb = w.current_tab()
    assert tb is not ta
    tb.paste()                                  # ctrl+v -> enter paste state from buffer
    assert tb.paste_active
    tb.on_paste_at(P(10, 10))
    assert tb.game.board.get(10, 10) == BLACK
    # Independent undo: undoing in tb doesn't touch ta.
    tb.undo()
    assert tb.game.board.get(10, 10) != BLACK
    assert ta.game.board.get(3, 3) == BLACK
    _close(w)


def test_cross_sgf_subtree_copy(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    ta = w.current_tab()
    click(ta, P(3, 3)); click(ta, P(4, 4))      # a little line in tab A
    ta.on_navigate("start"); ta.on_navigate("forward")  # mark the move-1 subtree
    w.set_mode(EditMode.PLAY)
    ta.copy()                                   # subtree copy -> window.subtree_mark
    assert w.subtree_mark is not None and w.subtree_mark[0] is ta
    w._add_tab(Document.new(19))
    tb = w.current_tab()
    n_before = sum(1 for _ in tb.game.root.walk())
    tb.paste()                                  # graft the copied subtree into tab B
    assert sum(1 for _ in tb.game.root.walk()) > n_before
    assert w.subtree_mark is not None           # copy persists for repeat paste
    _close(w)


def test_cross_sgf_subtree_paste_refits_board_size(qapp):
    """Pasting across board sizes anchors the subtree's bounding box to its
    nearest corner and prunes anything that still lands off-board."""
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    ta = w.current_tab()
    click(ta, P(16, 16)); click(ta, P(15, 15))  # near the bottom-right of 19x19
    ta.on_navigate("start"); ta.on_navigate("forward")
    ta.copy()
    w._add_tab(Document.new(9))
    tb = w.current_tab()
    tb.paste()
    node = tb.game.root.children[-1]
    # Bottom-right anchor: (16,16) is 2 in from the 19x19 corner, so it lands
    # 2 in from the 9x9 corner at (6,6); (15,15) follows to (5,5).
    assert node.get_one("B") == "gg"
    assert node.children[0].get_one("W") == "ff"
    _close(w)


def test_cross_sgf_subtree_cut(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    ta = w.current_tab()
    click(ta, P(3, 3)); click(ta, P(4, 4))
    ta.on_navigate("start"); ta.on_navigate("forward")
    src_nodes = sum(1 for _ in ta.game.root.walk())
    ta.cut()                                    # subtree cut
    w._add_tab(Document.new(19))
    tb = w.current_tab()
    tb.paste()                                  # cut transplants across tabs
    assert sum(1 for _ in tb.game.root.walk()) > 1
    # The source tab lost the subtree, and the mark is consumed.
    assert sum(1 for _ in ta.game.root.walk()) < src_nodes
    assert w.subtree_mark is None
    _close(w)


def test_closing_marking_tab_drops_subtree_mark(qapp):
    """Closing the tab that owns the cross-tab subtree mark must clear it —
    a stale mark kept a reference to the deleted widget, so a later Esc or
    Ctrl+V crashed on the dead C++ object (or mutated an invisible document)."""
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    ta = w.current_tab()
    click(ta, P(3, 3)); click(ta, P(4, 4))
    ta.on_navigate("start"); ta.on_navigate("forward")
    ta.cut()                                     # tab A owns the mark
    assert w.subtree_mark is not None and w.subtree_mark[0] is ta
    w._add_tab(Document.new(19))
    tb = w.current_tab()
    ta.document.dirty = False
    w.close_tab(w.tabs.indexOf(ta))              # close the marking tab
    assert w.subtree_mark is None
    qapp.processEvents()                         # let deleteLater run
    n_before = sum(1 for _ in tb.game.root.walk())
    tb.paste()                                   # no mark: must be a no-op
    assert sum(1 for _ in tb.game.root.walk()) == n_before
    assert tb.cancel_all() is False              # Esc path: no crash, nothing to do
    _close(w)


def test_lateral_nav_independent_of_centered_display(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    # Three sibling variations off the root, each two moves deep.
    for first, second in [(P(0, 0), P(1, 1)), (P(3, 3), P(4, 4)), (P(5, 5), P(6, 6))]:
        g.go_to_start()
        tab._post_navigate()
        click(tab, first)
        click(tab, second)
    # Sit on the *middle* branch at depth 1.
    mid = g.root.children[1]
    g.goto(mid)
    tab._post_navigate()

    def neighbor(direction):
        return tab.tree.vertical_neighbor(direction)

    # The vertical neighbours must be identical whether or not the display is
    # centred, and Down then Up must return to the same node.
    w.act_centered.setChecked(False)
    down_compact = neighbor(1)
    up_compact = neighbor(-1)
    w.act_centered.setChecked(True)
    assert tab.tree.vertical_neighbor(1) is down_compact
    assert tab.tree.vertical_neighbor(-1) is up_compact
    # Reversibility from the middle node.
    g.goto(mid); tab._post_navigate()
    nb = tab.tree.vertical_neighbor(1)
    assert nb is not None
    g.goto(nb); tab._post_navigate()
    assert tab.tree.vertical_neighbor(-1) is mid
    _close(w)


def _build_worked_example(tab):
    """1A->2A->{3A,3B}, 1A->2B->{3C,3D}, root->1B->2C->3E (from the layout spec)."""
    g = tab.game
    def start():
        g.go_to_start(); tab._post_navigate()
    start()
    for p in [P(0, 0), P(1, 0), P(2, 0)]:    # 1A, 2A, 3A (main line)
        click(tab, p)
    start(); tab.on_navigate("forward"); tab.on_navigate("forward")
    click(tab, P(2, 1))                       # 3B under 2A
    start(); tab.on_navigate("forward")
    click(tab, P(1, 1)); click(tab, P(2, 2))  # 2B under 1A, then 3C
    g.goto(g.root.children[0].children[1]); tab._post_navigate()
    click(tab, P(2, 3))                       # 3D under 2B
    start()
    click(tab, P(3, 3)); click(tab, P(4, 4)); click(tab, P(5, 5))  # 1B->2C->3E
    return g


def test_compact_layout_matches_spec(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    _build_worked_example(tab)
    nav = tab.tree.canvas._nav_pos
    names = {"aa": "1A", "ba": "2A", "ca": "3A", "cb": "3B", "bb": "2B",
             "cc": "3C", "cd": "3D", "dd": "1B", "ee": "2C", "ff": "3E"}
    rows = {}
    for nid, (c, r) in nav.items():
        node = tab.tree.canvas._nodes[nid]
        mv = node.get_one("B") or node.get_one("W")
        if mv in names:
            rows[names[mv]] = (c, r)
    # The exact placement from the worked example.
    assert rows["2B"] == (2, 2)     # one below 3B so 3C can extend straight
    assert rows["3C"] == (3, 2)
    assert rows["3D"] == (3, 3)
    assert rows["1B"] == (1, 4)     # below everything in cols 1..3
    assert rows["2C"] == (2, 4)
    assert rows["3E"] == (3, 4)
    _close(w)


def test_centered_up_down_split(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    for p in [P(0, 0), P(1, 1), P(2, 2)]:     # three children of root
        click(tab, p)
        g.go_to_start(); tab._post_navigate()
    a, b, c = g.root.children
    g.goto(b); tab._post_navigate()            # golden child of root = b (index 1)
    w.act_centered.setChecked(True)
    tab.tree.refresh()
    pos = tab.tree.canvas._pos
    assert pos[id(a)][1] < 0                    # earlier sibling -> up
    assert pos[id(b)][1] == 0                   # on the spine
    assert pos[id(c)][1] > 0                    # later sibling -> down
    _close(w)


def test_regular_and_centered_match_when_golden_is_principal(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    g = _build_worked_example(tab)
    cv = tab.tree.canvas
    # Force the golden (preferred) line onto the principal line.
    node = g.root
    while node.children:
        node = node.children[0]
        g.goto(node); tab._post_navigate()
    g.goto(g.root.children[0].children[0]); tab._post_navigate()
    assert [id(n) for n in cv._golden_nodes()] == [id(n) for n in cv._principal_path()]
    reg = {}
    cv._layout(cv._principal_path(), reg)
    cen = {}
    cv._layout(cv._golden_nodes(), cen)
    assert reg == cen   # one algorithm, parameterized only by the centre line
    _close(w)


def test_tree_culling_matches_bruteforce(qapp):
    """Subtree-box pruning must draw exactly what an unculled full scan would.

    `_collect_visible` skips whole branches whose box misses the viewport; the
    brute-force reference scans *every* node/connector and keeps those whose own
    box meets it. For every tile across the whole tree the two sets must agree —
    so pruning never drops (nor adds) a node or connector that touches the rect.
    """
    from PySide6.QtCore import QRectF

    from katsura.ui.mainwindow import MainWindow
    from katsura.ui.treeview import CELL

    w = MainWindow()
    tab = w.current_tab()
    _build_worked_example(tab)
    cv = tab.tree.canvas
    half = CELL / 2 + 2

    def cell_box(nid):
        a = cv._cell_center(*cv._pos[nid])
        return QRectF(a.x() - half, a.y() - half, 2 * half, 2 * half)

    def conn_box(nid, cid):
        a = cv._cell_center(*cv._pos[nid])
        b = cv._cell_center(*cv._pos[cid])
        return QRectF(min(a.x(), b.x()) - 2, min(a.y(), b.y()) - 2,
                      abs(b.x() - a.x()) + 4, abs(b.y() - a.y()) + 4)

    def brute(vis):
        en = {nid for nid in cv._nodes if vis.intersects(cell_box(nid))}
        ec = {(nid, id(ch)) for nid, node in cv._nodes.items()
              for ch in node.children if vis.intersects(conn_box(nid, id(ch)))}
        return en, ec

    centers = [cv._cell_center(*p) for p in cv._pos.values()]
    x0 = min(c.x() for c in centers) - CELL
    x1 = max(c.x() for c in centers) + CELL
    y0 = min(c.y() for c in centers) - CELL
    y1 = max(c.y() for c in centers) + CELL

    tiles = 0
    step = CELL - 4                      # sub-cell tiles, so every box edge is split
    yy = y0
    while yy < y1:
        xx = x0
        while xx < x1:
            vis = QRectF(xx, yy, step, step)
            conns, nodes = cv._collect_visible(vis)
            got_n = {nid for nid, _, _ in nodes}
            got_c = {(id(n), id(ch)) for n, ch, _, _ in conns}
            exp_n, exp_c = brute(vis)
            assert got_n == exp_n, f"nodes differ at {xx},{yy}"
            assert got_c == exp_c, f"connectors differ at {xx},{yy}"
            tiles += 1
            xx += step
        yy += step
    assert tiles > 20                    # the grid actually exercised many windows
    _close(w)


def test_tree_node_hit_test_matches_bruteforce(qapp):
    """The O(1) click hit-test must agree with an exhaustive scan everywhere:
    each node's centre resolves to that node, and over a fine grid the result
    equals the (unique) node within the hit radius, else None."""
    from katsura.ui.mainwindow import MainWindow
    from katsura.ui.treeview import CELL, RADIUS

    w = MainWindow()
    tab = w.current_tab()
    _build_worked_example(tab)
    cv = tab.tree.canvas
    r2 = (RADIUS + 4) ** 2

    def brute(x, y):
        hits = [cv._nodes[nid] for nid, (c, r) in cv._pos.items()
                if (cv._cell_center(c, r).x() - x) ** 2
                + (cv._cell_center(c, r).y() - y) ** 2 <= r2]
        assert len(hits) <= 1            # hit radius fits in one cell: never ambiguous
        return hits[0] if hits else None

    # Every node's own centre hits exactly that node.
    for nid, (c, r) in cv._pos.items():
        ctr = cv._cell_center(c, r)
        assert cv._node_at(ctr.x(), ctr.y()) is cv._nodes[nid]

    centers = [cv._cell_center(*p) for p in cv._pos.values()]
    x0 = min(c.x() for c in centers) - CELL
    x1 = max(c.x() for c in centers) + CELL
    y0 = min(c.y() for c in centers) - CELL
    y1 = max(c.y() for c in centers) + CELL

    checked = 0
    y = y0 + 0.5                          # half-pixel offset dodges exact-boundary ties
    while y < y1:
        x = x0 + 0.5
        while x < x1:
            assert cv._node_at(x, y) is brute(x, y), f"hit-test differs at {x},{y}"
            checked += 1
            x += 5
        y += 5
    assert checked > 50
    _close(w)


def test_centered_relayout_only_when_line_changes(qapp):
    """Navigating along the golden line repaints without an O(n) relayout; only
    switching to a different line re-lays the centred tree out."""
    from katsura.ui.mainwindow import MainWindow

    w = MainWindow()
    tab = w.current_tab()
    g = tab.game
    # Two sibling lines off the root, each a few moves deep.
    for line in [[P(0, 0), P(1, 1), P(2, 2)], [P(3, 3), P(4, 4)]]:
        g.go_to_start(); tab._post_navigate()
        for p in line:
            click(tab, p)
    w.act_centered.setChecked(True)
    tab.tree.refresh()
    cv = tab.tree.canvas

    # Sit at the end of the first line, then walk back/forward along it: the
    # golden node set is unchanged, so no relayout is needed.
    g.go_to_end(); tab._post_navigate()
    assert not cv.golden_layout_stale()
    tab.on_navigate("back")
    assert not cv.golden_layout_stale()
    tab.on_navigate("forward")
    assert not cv.golden_layout_stale()

    # The walk above stayed on the second line (where the layout is centred).
    # Jumping to the *first* line changes the golden line -> relayout needed, and
    # after refresh the signature is current again.
    g.goto(g.root.children[0]); tab.sync_board_tool()
    assert cv.golden_layout_stale()
    tab._post_navigate()                 # performs the relayout
    assert not cv.golden_layout_stale()
    _close(w)


def test_sgf_komi_field(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    # SGF komi now lives in the SGF Info pane (the KM property), and may be blank.
    tab.on_info_field_edited("KM", "6.5")
    assert tab.game.get_komi() == 6.5 and tab.game.root.get_one("KM") == "6.5"
    tab.on_info_field_edited("KM", "")
    assert tab.game.get_komi() is None and not tab.game.root.has("KM")
    _close(w)


def test_engine_settings_toolbar(qapp):
    from katsura.engine.settings import PRESETS
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    # Compact labels; each label shares its spin box's explanatory tooltip.
    assert w.wrn_label.text().strip() == "WRN"
    assert w.wrn_label.toolTip() == w.wrn_spin.toolTip()
    assert "explore more moves" in w.wrn_spin.toolTip()
    assert w.pda_label.toolTip() == w.pda_spin.toolTip()
    assert "stronger KataGo" in w.pda_spin.toolTip()
    assert w.komi_label.toolTip() == w.komi_spin.toolTip() != ""
    # Engine komi is independent of the SGF komi and clamped to KataGo's
    # v1.17+ range; the spin box itself is corrected, not just the setting.
    w.komi_spin.setValue(6.5)
    assert tab.analysis_settings.komi == 6.5
    assert tab.game.get_komi() is None              # SGF komi untouched
    w.komi_spin.setValue(999)
    assert tab.analysis_settings.komi == 400.0
    assert w.komi_spin.value() == 400.0
    w.komi_spin.setValue(6.3)                       # snapped to a half-integer
    assert tab.analysis_settings.komi == 6.5
    assert w.komi_spin.value() == 6.5
    # analysisWideRootNoise steps through the discrete set.
    w.wrn_spin.setValue(0.04); w.wrn_spin.stepBy(1)
    assert tab.analysis_settings.wide_root_noise == 0.10
    w.wrn_spin.stepBy(-2)
    assert tab.analysis_settings.wide_root_noise == 0.01
    # PDA snaps to clean 0.5 multiples.
    w.pda_spin.setValue(0.0); w.pda_spin.stepBy(1)
    assert tab.analysis_settings.playout_doubling_advantage == 0.5
    # Rules edits feed into the request key.
    tab.set_engine_rules(PRESETS["chinese"])
    assert tab.analysis_settings.rules == PRESETS["chinese"]
    _close(w)


def test_rules_dialog_preset_button_selection(qapp):
    from katsura.engine.settings import PRESETS, KataRules
    from katsura.ui.dialogs import RulesDialog

    # Opening with rules that exactly match a preset pre-selects its button,
    # exactly as if it had just been clicked.
    dlg = RulesDialog(PRESETS["japanese"])
    assert dlg._preset_buttons["japanese"].isChecked()
    assert not any(btn.isChecked() for key, btn in dlg._preset_buttons.items()
                   if key != "japanese")

    # Editing a field away from the preset deselects it...
    dlg.tax.setCurrentIndex(dlg.tax.findData("NONE"))
    assert not dlg._preset_buttons["japanese"].isChecked()
    # ...and editing back re-selects it.
    dlg.tax.setCurrentIndex(dlg.tax.findData("SEKI"))
    assert dlg._preset_buttons["japanese"].isChecked()

    # Clicking a preset button applies it and shows it selected.
    dlg._preset_buttons["tromp-taylor"].click()
    assert dlg._preset_buttons["tromp-taylor"].isChecked()
    assert not dlg._preset_buttons["japanese"].isChecked()
    assert dlg.result_rules() == PRESETS["tromp-taylor"]

    # whiteHandicapBonus and friendlyPassOk are exposed and round-trip; rules
    # differing from a preset only in one of them select nothing.
    almost_japanese = KataRules("SIMPLE", "TERRITORY", "SEKI", False, False,
                                "0", False)                # japanese w/ fpo=False
    dlg2 = RulesDialog(almost_japanese)
    assert not dlg2.friendly_pass.isChecked()
    assert dlg2.result_rules() == almost_japanese
    assert not any(btn.isChecked() for btn in dlg2._preset_buttons.values())
    dlg2.friendly_pass.setChecked(True)                    # now exactly japanese
    assert dlg2._preset_buttons["japanese"].isChecked()
    dlg2.whb.setCurrentIndex(dlg2.whb.findData("N"))       # differs again
    assert not dlg2._preset_buttons["japanese"].isChecked()
    assert dlg2.result_rules().white_handicap_bonus == "N"


def test_show_overlay_always_starts_selected(qapp, tmp_path, monkeypatch):
    """show_analysis_overlay is session-only: a persisted 'off' must never make
    move-analysis info start hidden on the next run."""
    from PySide6.QtCore import QSettings

    import katsura.ui.settings as S

    ini = str(tmp_path / "prefs.ini")
    monkeypatch.setattr(
        S, "QSettings", lambda org, app: QSettings(ini, QSettings.IniFormat))
    p = S.Prefs()
    p.show_analysis_overlay = False
    p.show_move_numbers = True                 # control: a persisted pref
    p.save()
    p2 = S.Prefs().load()
    assert p2.show_analysis_overlay is True    # always starts shown
    assert p2.show_move_numbers is True        # normal prefs still round-trip


def test_settings_never_touch_the_real_store(qapp, settings_dir):
    """Guards the conftest redirect: tests save real Prefs, and writing them to
    the store the installed app reads would overwrite the developer's own."""
    from PySide6.QtCore import QSettings

    from katsura import APP, ORG

    assert QSettings(ORG, APP).fileName().startswith(str(settings_dir))


def test_view_menu_ownership_policy_toggles_and_mark_glyphs(qapp):
    from katsura.ui.mainwindow import MainWindow
    from katsura.ui.modes import EditMode
    w = MainWindow()
    tab = w.current_tab()

    # Mark-tool toolbar buttons are compact glyphs; the Tools menu keeps words.
    tri = w.mode_actions[EditMode.MARK_TRIANGLE]
    assert tri.iconText() == "△" and tri.text() == "Triangle"
    assert w.mode_actions[EditMode.MARK_CROSS].iconText() == "✕"

    # The View-menu 'o'/'p' togglers mirror the per-board display state.
    w._sync_view_menu()
    assert not w.act_ownership.isChecked() and not w.act_policy.isChecked()
    w.act_ownership.trigger()                     # toggle via the menu
    assert tab.board.ownership_shown()
    tab.board.toggle_policy_mode()                # toggle via the 'p' key path
    w._sync_view_menu()                           # menu opening re-syncs
    assert w.act_policy.isChecked() and w.act_ownership.isChecked()
    w.act_policy.trigger()
    assert not tab.board.policy_mode_shown()
    _close(w)


def test_number_keys_select_tools(qapp):
    """Bare '1'-'9' pick a tool, in toolbar order."""
    from katsura.ui.mainwindow import MainWindow
    from katsura.ui.modes import MODE_KEY_ORDER, EditMode
    w = MainWindow()
    tab = w.current_tab()

    for i, mode in enumerate(MODE_KEY_ORDER, 1):
        key = getattr(Qt, f"Key_{i}")
        assert tab.handle_key(key, Qt.NoModifier)
        assert w.mode == mode
        assert w.mode_actions[mode].isChecked()
    # The order is the toolbar/menu order, and every tool is reachable.
    assert MODE_KEY_ORDER[0] is EditMode.PLAY
    assert MODE_KEY_ORDER[-1] is EditMode.SELECT
    assert set(MODE_KEY_ORDER) == set(EditMode)
    # '0' is not a tool key and must not be swallowed.
    assert not tab.handle_key(Qt.Key_0, Qt.NoModifier)
    _close(w)


def test_shift_number_keys_show_raw_nn(qapp):
    """Raw-NN moved to Shift+digit (the bare digits now belong to the tools)."""
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    seen = []
    w.show_raw_nn_view = lambda sym: seen.append(sym)

    # Shift+1..7 -> symmetries 1..7, Shift+8 -> symmetry 0.
    for i in range(1, 9):
        assert tab.handle_key(getattr(Qt, f"Key_{i}"), Qt.ShiftModifier)
    assert seen == [1, 2, 3, 4, 5, 6, 7, 0]

    # Platforms that report the shifted *character* instead of the digit key
    # (Shift+1 -> '!') land on the same symmetries.
    seen.clear()
    for key in (Qt.Key_Exclam, Qt.Key_At, Qt.Key_Asterisk):
        assert tab.handle_key(key, Qt.ShiftModifier)
    assert seen == [1, 2, 0]

    # Shift+9 is not a symmetry, and a bare digit selects a tool instead.
    seen.clear()
    tab.handle_key(Qt.Key_9, Qt.ShiftModifier)
    tab.handle_key(Qt.Key_2, Qt.NoModifier)
    assert seen == []
    _close(w)


def test_menu_hotkey_hints_are_uniform_and_inert(qapp):
    """Every menu hotkey reminder sits in Qt's shortcut column — none is
    parenthesised into the label — and the display-only ones never fire."""
    from katsura.ui.mainwindow import MainWindow
    from katsura.ui.modes import EditMode
    w = MainWindow()

    hinted = [
        (w.act_delete, "Backspace"), (w.act_shift_up, "Shift+Up"),
        (w.act_shift_down, "Shift+Down"), (w.act_overlay, "I"),
        (w.act_policy, "P"), (w.act_ownership, "O"),
        (w.act_live_analysis, "Space"),
        (w.mode_actions[EditMode.PLAY], "1"),
        (w.mode_actions[EditMode.SELECT], "9"),
        (w.nav_actions[0], "Right"),
    ]
    for act, key in hinted:
        assert act.shortcut().toString() == key, act.text()
        # Display-only: bound to the (unfocused) menu, so it cannot steal the
        # key from the comment box the way a WindowShortcut would.
        assert act.shortcutContext() == Qt.WidgetShortcut, act.text()
        assert "(" not in act.text(), act.text()

    # Real (functional) shortcuts render in the same column.
    assert w.act_undo.shortcut().toString() == "Ctrl+Z"
    assert w.act_pass.shortcut().toString() == "Ctrl+P"

    # The raw-NN submenu lists all 8 symmetries with their Shift+digit keys.
    raw = w.raw_nn_menu.actions()
    assert [a.text() for a in raw] == [f"Symmetry {s}" for s in
                                       (1, 2, 3, 4, 5, 6, 7, 0)]
    assert [a.shortcut().toString() for a in raw] == [
        f"Shift+{i}" for i in range(1, 9)]
    assert all(a.shortcutContext() == Qt.WidgetShortcut for a in raw)
    _close(w)


def test_hotkey_hints_never_activate_as_shortcuts(qapp):
    """The menu hotkey reminders are decoration only: the keys are dispatched by
    the app-level filter (EditorTab.handle_key), never by Qt's shortcut map.

    Checked where the two paths visibly diverge: with no tab open the filter
    declines every key, so a tool must NOT change tools. A real
    Qt.WindowShortcut on the same action would fire here (the mode actions live
    on a visible toolbar) and switch the tool.
    """
    from PySide6.QtTest import QTest

    from katsura.ui.mainwindow import MainWindow
    from katsura.ui.modes import EditMode
    w = MainWindow()
    w.show()
    qapp.processEvents()

    w.close_tab(0)                        # zero tabs is a supported state
    qapp.processEvents()
    assert w.tabs.count() == 0 and w.current_tab() is None
    for key in (Qt.Key_9, Qt.Key_3, Qt.Key_I, Qt.Key_O, Qt.Key_P):
        QTest.keyClick(w, key)
    qapp.processEvents()
    assert w.mode is EditMode.PLAY        # nothing was activated
    _close(w)


def test_typing_in_the_comment_box_is_not_a_hotkey(qapp):
    """Keys that double as hotkeys (digits, i/p/o, Space, Backspace) still type
    normally while the comment editor has focus."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    from katsura.ui.mainwindow import MainWindow
    from katsura.ui.modes import EditMode
    w = MainWindow()
    w.show()
    tab = w.current_tab()
    click(tab, P(3, 3))
    tab.comment.setFocus()
    qapp.processEvents()
    if not tab.comment.hasFocus():
        pytest.skip("offscreen platform would not focus the comment editor")

    w.set_mode(EditMode.SELECT)
    for text, key in [("i", Qt.Key_I), ("3", Qt.Key_3), ("p", Qt.Key_P),
                      ("o", Qt.Key_O), (" ", Qt.Key_Space), ("9", Qt.Key_9),
                      ("", Qt.Key_Backspace)]:
        qapp.sendEvent(tab.comment,
                       QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, text))
    qapp.processEvents()
    assert tab.comment.toPlainText() == "i3po "     # Backspace ate the '9'
    assert w.mode is EditMode.SELECT                # no tool switched underneath
    assert tab.game.move_number == 1                # and no node was deleted
    _close(w)


def test_pass_button_on_toolbar(qapp):
    from PySide6.QtWidgets import QToolBar

    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()

    # Pass is a command, not a tool: its own toolbar group keeps it out of the
    # tool row, separated the way Navigate and Engine are.
    bars = {tb.windowTitle(): tb for tb in w.findChildren(QToolBar)}
    assert w.act_pass in bars["Commands"].actions()
    assert w.act_pass not in bars["Tools"].actions()
    assert w.act_pass.iconText() == "Pass"          # button text, not "&Pass"

    # The button plays a pass for the side to move.
    assert tab.game.move_number == 0
    w.act_pass.trigger()
    assert tab.game.move_number == 1
    assert tab.game.current.has("B") and tab.game.current.get_one("B") == ""
    _close(w)


def test_engine_menu_labels_and_rules_entry(qapp):
    from katsura.engine.settings import PRESETS, KataRules
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()

    # The two cache-clearing commands are named analogously.
    assert w.act_clear_gui_cache.text().replace("&", "") == \
        "Clear Cached Analysis (GUI)"
    assert w.act_clear_engine_cache.text().replace("&", "") == \
        "Clear Cached Analysis (Engine)"
    assert "halted" in w.act_clear_engine_cache.toolTip()

    # Live Analysis says which way it will go, and is not a checkbox.
    assert not w.act_live_analysis.isCheckable()
    assert w.act_live_analysis.text().replace("&", "") == "Start Live Analysis"

    # The rules dialog is reachable from the Engine menu, not just the toolbar.
    assert w.act_engine_rules.text().replace("&", "") == "Rules…"

    # A recognised ruleset names itself on the toolbar button; anything else is
    # simply "Rules" (it used to read "Custom…").
    w._update_rules_button(PRESETS["japanese"])
    assert w.rules_button.text() == "Japanese"
    w._update_rules_button(KataRules(ko="SIMPLE", scoring="AREA", tax="SEKI"))
    assert w.rules_button.text() == "Rules"
    tab.set_engine_rules(PRESETS["chinese"])
    w._refresh_engine_controls()
    assert w.rules_button.text() == "Chinese"
    _close(w)


def test_analysis_panel_has_explanatory_tooltips(qapp):
    """Each stat is explained in the reader's terms: no engine field names, no
    lectures on sign conventions the display already makes plain."""
    from katsura.ui.analysispanel import AnalysisInfoPanel
    p = AnalysisInfoPanel()
    tips = [p.winrate_label.toolTip(), p.lead_label.toolTip(), p.bar.toolTip()]
    for key, needle in [("visits", "playouts"), ("stdev", "standard deviation"),
                        ("kl", "policy"), ("noresult", "no result")]:
        tip = p._fields[key].toolTip()
        assert needle.lower() in tip.lower(), key
        tips.append(tip)
    assert "winning" in p.winrate_label.toolTip()
    assert "points" in p.lead_label.toolTip()
    for tip in tips:
        assert tip
        for jargon in ("scoreLead", "scoreStdev", "noResult", "KL divergence"):
            assert jargon not in tip, tip


def test_analysis_panel_no_result_always_rounds(qapp):
    """No-result is rounded to the display precision like every other counter,
    rather than vanishing below a hidden threshold."""
    from katsura.engine.analysis import PanelStats
    from katsura.ui.analysispanel import AnalysisInfoPanel
    p = AnalysisInfoPanel()
    p.set_stats(PanelStats(no_result=0.0007))
    assert p._fields["noresult"].text() == "0.07%"
    p.set_stats(PanelStats(no_result=0.0))
    assert p._fields["noresult"].text() == "0.00%"
    p.set_stats(PanelStats())                       # engine reported nothing
    assert p._fields["noresult"].text() == "—"


def test_menu_action_tooltips_are_reachable(qapp):
    """Menus must opt in to showing action tooltips; ours carry real help."""
    from PySide6.QtWidgets import QMenu

    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    menus = w.menuBar().findChildren(QMenu)
    assert menus and all(m.toolTipsVisible() for m in menus)
    _close(w)


def test_tree_paint_survives_a_node_added_since_the_layout(qapp):
    """A repaint between an edit and its relayout must not raise out of
    paintEvent — it just skips the not-yet-laid-out node."""
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    click(tab, P(3, 3))
    tab.game.insert_empty_node()            # deliberately without a relayout
    canvas = tab.tree.canvas
    from PySide6.QtCore import QRectF
    conns, nodes = canvas._collect_visible(QRectF(0, 0, 800, 800))
    assert nodes and all(id(n) in canvas._pos for _, n, _ in nodes)
    _close(w)


def test_node_info_names_points_beyond_gtp_columns(qapp):
    """A board wider than GTP's 25 named columns still gets a node-info line."""
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w._add_tab(Document.new(30))
    click(tab, P(28, 0))
    tab._update_info()
    assert "last: ?30" in tab.info.text()
    _close(w)


def test_min_weight_pref_drops_circle_and_label_together(qapp):
    """Below the threshold a candidate is not drawn at all — the old behaviour
    kept the circle and dropped only the number."""
    from katsura.engine.analysis import parse_analysis_line
    from katsura.model.game import Game
    from katsura.ui.boardview import BoardView
    from katsura.ui.dialogs import PreferencesDialog
    from katsura.ui.settings import Prefs

    prefs = Prefs()
    assert prefs.analysis_min_weight == 0.002        # 0.2% of the top weight
    bv = BoardView(prefs)
    bv.set_game(Game.new(19))
    bv.resize(400, 400)
    # D4 is the top move; C3 carries 0.05% of its weight, Q16 20%.
    a = parse_analysis_line(
        "info move D4 visits 100 winrate 0.5 scoreLead 0 prior 0.5 weight 1000 "
        "order 0 pv D4 "
        "info move Q16 visits 20 winrate 0.5 scoreLead 0 prior 0.2 weight 200 "
        "order 1 pv Q16 "
        "info move C3 visits 1 winrate 0.5 scoreLead 0 prior 0.01 weight 0.5 "
        "order 2 pv C3 rootInfo visits 121", 19, 19)
    bv.set_analysis(a)
    assert {m.move for m, _ in bv.drawable_candidates()} == {"D4", "Q16"}

    prefs.analysis_min_weight = 0.5                  # only >=50% of the top
    assert {m.move for m, _ in bv.drawable_candidates()} == {"D4"}

    # And nothing of the dropped move reaches the pixels: Q16's intersection
    # renders exactly as a bare board intersection does.
    q16, empty = P(15, 3), P(9, 9)
    bv.grab()                                        # lays out the geometry
    assert _intersection_pixels(bv, q16) == _intersection_pixels(bv, empty)
    prefs.analysis_min_weight = 0.002
    assert _intersection_pixels(bv, q16) != _intersection_pixels(bv, empty)

    # Round-trips through the Preferences dialog.
    dlg = PreferencesDialog(prefs)
    assert dlg.min_weight.value() == 0.2
    dlg.min_weight.setValue(50.0)
    dlg.apply_to(prefs)
    assert prefs.analysis_min_weight == 0.5


def test_label_threshold_keeps_the_circle(qapp):
    """Between the two thresholds a move keeps its circle and loses its numbers
    — the board still shows where the search looked."""
    from katsura.engine.analysis import parse_analysis_line
    from katsura.model.game import Game
    from katsura.ui.boardview import BoardView
    from katsura.ui.dialogs import PreferencesDialog
    from katsura.ui.settings import Prefs

    prefs = Prefs()
    assert prefs.analysis_min_weight == 0.002        # drawn at all: 0.2%
    assert prefs.analysis_min_label_weight == 0.01   # numbers too: 1%
    bv = BoardView(prefs)
    bv.set_game(Game.new(19))
    # Q16 carries 0.5% of the top move's weight: above one threshold, below the
    # other.
    a = parse_analysis_line(
        "info move D4 visits 100 winrate 0.5 scoreLead 0 prior 0.5 weight 1000 "
        "order 0 pv D4 "
        "info move Q16 visits 3 winrate 0.5 scoreLead 0 prior 0.02 weight 5 "
        "order 1 pv Q16 rootInfo visits 103", 19, 19)
    bv.set_analysis(a)
    drawn = {m.move: (m, ratio) for m, ratio in bv.drawable_candidates()}
    assert set(drawn) == {"D4", "Q16"}
    assert not bv._labels_candidate(*drawn["Q16"])
    assert bv._labels_candidate(*drawn["D4"])        # order-0 is always labelled

    prefs.analysis_min_label_weight = 0.001
    assert bv._labels_candidate(*drawn["Q16"])

    dlg = PreferencesDialog(prefs)
    assert dlg.min_label_weight.value() == 0.1
    dlg.min_label_weight.setValue(2.0)
    dlg.apply_to(prefs)
    assert prefs.analysis_min_label_weight == 0.02


def _intersection_pixels(bv, pt) -> list:
    """The four diagonal pixels just inside an intersection's stone radius.

    Diagonals only: the axis directions sit on the grid lines, which are drawn
    for empty and occupied points alike.
    """
    img = bv.grab().toImage()
    c = bv._xy(pt.x, pt.y)
    off = bv._cell * 0.32
    return [img.pixelColor(int(c.x() + dx * off), int(c.y() + dy * off)).name()
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1))]


def test_ownership_hover_shows_percent(qapp):
    """With the heatmap on, hovering a point reports Black's share of it."""
    from katsura.engine.analysis import parse_analysis_line
    from katsura.model.game import Game
    from katsura.ui.boardview import BoardView
    from katsura.ui.settings import Prefs

    bv = BoardView(Prefs())
    bv.set_game(Game.new(19))
    bv.resize(420, 420)
    # ownership is in the current player's perspective; Black is to move here,
    # so +1 means Black owns the point outright.
    own = ["0.0"] * 361
    own[0] = "1.0"          # A19 = Point(0, 0): all Black
    own[1] = "-1.0"         # B19 = Point(1, 0): all White
    a = parse_analysis_line(
        "info move D4 visits 100 winrate 0.5 scoreLead 0 prior 0.5 order 0 pv D4 "
        "rootInfo visits 100 ownership " + " ".join(own), 19, 19)
    captured = []
    bv.analysisReadout.connect(captured.append)
    bv.set_analysis(a)
    assert "ownership" not in captured[-1]        # heatmap off -> nothing

    bv.toggle_ownership()
    bv._hover = Point(0, 0)
    bv._emit_readout()
    assert "ownership B 100.0%" in captured[-1]
    bv._hover = Point(1, 0)
    bv._emit_readout()
    assert "ownership B 0.0%" in captured[-1]
    bv._hover = Point(5, 5)
    bv._emit_readout()
    assert "ownership B 50.0%" in captured[-1]

    # It shows even with move-analysis info hidden ('i'), since the heatmap is
    # an independent overlay.
    bv.prefs.show_analysis_overlay = False
    bv._emit_readout()
    assert captured[-1] == "ownership B 50.0%"
    # And goes away with the heatmap.
    bv.toggle_ownership()
    assert captured[-1] == ""


class _FakeEngineProc:
    """A stand-in for GtpEngine where only pause/resume/liveness matter."""

    def __init__(self):
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def is_paused(self):
        return self.paused

    def is_running(self):
        return True


def _fake_attached_controller(w, name):
    """A controller that looks attached (config + fake engine), as the window
    would hold after attach_engine, without launching a process."""
    from katsura.engine.config import EngineConfig
    from katsura.engine.controller import AnalysisController
    ctrl = AnalysisController(w)
    ctrl.config = EngineConfig(name, "false")
    ctrl.display_name = name
    ctrl.engine = _FakeEngineProc()
    ctrl.set_active(False)
    return ctrl


def test_analysis_info_engine_selector(qapp):
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    # No engine attached: the accessory selector is hidden.
    assert not tab.engine_select.isVisibleTo(tab.sec_analysis)

    # One attached engine: reads as a plain (non-interactive) label.
    a = _fake_attached_controller(w, "KataGo-9x9")
    w.engine_controllers.append(a)
    w.select_engine(a)
    assert tab.engine_select.text() == "KataGo-9x9"
    assert tab.engine_select.isVisibleTo(tab.sec_analysis)
    assert not tab.engine_select.isEnabled()        # single engine: no drop-down

    # A tab opened while attached picks the selector up too.
    w.new_game(9)
    tab2 = w.current_tab()
    assert tab2 is not tab
    assert tab2.engine_select.text() == "KataGo-9x9"

    # Two attached engines: it becomes a drop-down showing the shown engine.
    b = _fake_attached_controller(w, "Remote")
    w.engine_controllers.append(b)
    w.select_engine(b)
    assert a.engine.is_paused()                     # deselected engine is halted
    assert b.is_active() and not a.is_active()
    for t in (tab, tab2):
        assert t.engine_select.text() == "Remote ▾"
        assert t.engine_select.isEnabled()
        assert t.engine_select._names == ["KataGo-9x9", "Remote"]
    checks = [act.isChecked() for act in tab.engine_select._menu.actions()]
    assert checks == [False, True]

    # Picking the other engine in the header selector switches window-wide.
    tab.engine_select.engineSelected.emit(0)
    assert w.engine_controller is a
    assert b.engine.is_paused() and not b.is_active()
    assert tab2.engine_select.text() == "KataGo-9x9 ▾"

    # Detaching everything hides the selector on every tab.
    w.engine_controllers.clear()
    w._current_engine = None
    w._update_engine_ui()
    assert not tab.engine_select.isVisibleTo(tab.sec_analysis)
    assert not tab2.engine_select.isVisibleTo(tab2.sec_analysis)
    _close(w)


def test_spinner_and_inert_click_focus(qapp):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSpinBox

    from katsura.ui.mainwindow import _TEXT_WIDGETS, MainWindow
    w = MainWindow(); w.resize(1180, 820); w.show(); qapp.processEvents()
    tab = w.current_tab()

    def sub_pos(sb, which):
        opt = QStyleOptionSpinBox(); sb.initStyleOption(opt)
        return sb.style().subControlRect(QStyle.CC_SpinBox, opt, which, sb).center()

    # Arrow click steps the value but does NOT focus the spinner (hotkeys keep
    # working): focus stays off the text widget.
    tab.board.setFocus(); qapp.processEvents()
    v0 = w.komi_spin.value()
    QTest.mouseClick(w.komi_spin, Qt.LeftButton, Qt.NoModifier,
                     sub_pos(w.komi_spin, QStyle.SC_SpinBoxUp))
    qapp.processEvents(); qapp.processEvents()
    assert w.komi_spin.value() == v0 + 0.5
    assert not w.komi_spin.hasFocus()
    assert not isinstance(QApplication.focusWidget(), _TEXT_WIDGETS)

    # A click in the number field DOES focus it for typing.
    QTest.mouseClick(w.komi_spin, Qt.LeftButton, Qt.NoModifier,
                     sub_pos(w.komi_spin, QStyle.SC_SpinBoxEditField))
    qapp.processEvents()
    assert isinstance(QApplication.focusWidget(), _TEXT_WIDGETS)

    # Clicking inert chrome (the node-info label) releases focus to the board.
    tab.comment.setFocus(); qapp.processEvents()
    assert tab.comment.hasFocus()
    QTest.mouseClick(tab.info, Qt.LeftButton, Qt.NoModifier, tab.info.rect().center())
    qapp.processEvents()
    assert tab.board.hasFocus() and not tab.comment.hasFocus()

    # But clicking into the comment still focuses it (it is not inert).
    QTest.mouseClick(tab.comment.viewport(), Qt.LeftButton, Qt.NoModifier,
                     tab.comment.viewport().rect().center())
    qapp.processEvents()
    assert tab.comment.hasFocus()
    _close(w)


def test_analysis_interval_pref(qapp):
    from katsura.engine.controller import AnalysisController
    from katsura.ui.dialogs import PreferencesDialog
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    # Default reporting period flows into a controller made at attach time
    # (attach_engine passes prefs.analysis_interval_cs).
    assert w.prefs.analysis_interval_cs == 25
    ctrl = AnalysisController(w, interval_cs=w.prefs.analysis_interval_cs)
    assert ctrl.interval_cs == 25
    # The dialog shows it in ms and round-trips through cs.
    dlg = PreferencesDialog(w.prefs)
    assert dlg.analysis_interval.value() == 250
    dlg.analysis_interval.setValue(400)
    dlg.apply_to(w.prefs)
    assert w.prefs.analysis_interval_cs == 40
    ctrl.set_interval_cs(w.prefs.analysis_interval_cs)
    assert ctrl.interval_cs == 40
    _close(w)


def test_game_info_fields_and_undo(qapp):
    from katsura.ui.document import Document
    from katsura.ui.mainwindow import MainWindow
    w = MainWindow()
    tab = w.current_tab()
    tab.on_info_field_edited("PB", "Black Player")
    tab.on_info_field_edited("PW", "White Player")
    tab.on_info_field_edited("RE", "W+R")
    assert tab.game.root.get_one("PB") == "Black Player"
    assert tab.game.get_info("RE") == "W+R"
    tab.undo()                                    # one undo step per field
    assert tab.game.get_info("RE") == ""
    # Round-trips through save/load.
    tab.on_info_field_edited("RU", "Chinese")
    tab.on_info_field_edited("KM", "7.5")
    doc2 = Document(roots=__import__("katsura.sgf.tree", fromlist=["parse_collection"])
                    .parse_collection(tab.document.to_sgf()), game=None)
    from katsura.model.game import Game
    g2 = Game(doc2.roots[0])
    assert g2.get_komi() == 7.5 and g2.get_info("PB") == "Black Player"
    assert g2.get_info("RU") == "Chinese"
    _close(w)


def test_collapsible_section_fixed_vs_growable(qapp):
    from PySide6.QtWidgets import QLabel

    from katsura.ui.collapsible import CollapsibleSection

    # Compact (non-growable) section: a fixed open height, not resizable.
    c = CollapsibleSection("X", QLabel("body"), expanded=True, growable=False)
    open_h = c.height()
    c.set_open_height(500)                  # ignored for non-growable
    assert c.height() == open_h
    c.set_expanded(False)
    assert c.height() < open_h              # collapsed -> header only
    c.set_expanded(True)
    assert c.height() == open_h             # restored

    # Growable section: resizable, with a minimum clamp.
    g = CollapsibleSection("Y", QLabel("b"), expanded=True, growable=True,
                           open_height=200)
    assert g.height() == 200
    g.set_open_height(260)
    assert g.height() == 260
    g.set_open_height(1)                    # clamped up to a sane minimum
    assert g.height() > 1 and g.open_height > 1
    collapsed = g._header_h()
    g.set_expanded(False)
    assert g.height() == collapsed


def test_pane_grip_resizes_section_above(qapp):
    from PySide6.QtWidgets import QLabel

    from katsura.ui.collapsible import CollapsibleSection, PaneGrip

    sec = CollapsibleSection("T", QLabel("b"), expanded=True, growable=True,
                             open_height=200)
    grip = PaneGrip(sec)
    before = sec.open_height
    grip._last_y = 100.0
    # Simulate a downward drag of 40px (build a tiny event stand-in).
    class _E:
        def globalPosition(self):
            class _P:
                def y(self_inner):
                    return 140.0
            return _P()
    grip.mouseMoveEvent(_E())
    assert sec.open_height == before + 40


def test_prefs_ignore_a_stored_value_whose_meaning_changed(qapp):
    """Every save writes every setting, so a default nobody chose gets
    persisted; when such a default's meaning changes, the stale value must not
    silently outlive it."""
    from katsura.ui.settings import Prefs

    class FakeSettings:
        def __init__(self, schema):
            self._schema = schema

        def value(self, key, default=None):
            return self._schema if key == "prefs/_schema" else default

    # Written before the schema bump (or by a build that never wrote one).
    assert "analysis_min_label_weight" in Prefs._outdated(FakeSettings(1))
    assert "analysis_min_label_weight" in Prefs._outdated(FakeSettings(None))
    # Written by this build: honoured.
    assert Prefs._outdated(FakeSettings(Prefs._SCHEMA)) == set()
    # The marker itself is not a setting.
    assert "_schema" not in Prefs.__dataclass_fields__
