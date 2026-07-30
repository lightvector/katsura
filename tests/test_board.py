"""Tests for Go board capture logic and rules tolerance."""

import pytest

from katsura.go.board import BLACK, EMPTY, WHITE, Board, IllegalMove
from katsura.sgf.coords import Point


def P(x, y):
    return Point(x, y)


def test_simple_capture():
    b = Board(19)
    # White stone at (1,0) surrounded by black on a corner-ish layout.
    b.play(WHITE, P(0, 0))
    b.play(BLACK, P(1, 0))
    b.play(BLACK, P(0, 1))
    # White at (0,0) now has liberties? neighbors: (1,0)=B, (0,1)=B -> 0 libs.
    # Black's last move captured it.
    assert b.get(0, 0) == EMPTY


def test_capture_returns_points():
    b = Board(19)
    b.play(WHITE, P(0, 0))
    b.play(BLACK, P(1, 0))
    captured = b.play(BLACK, P(0, 1))
    assert P(0, 0) in captured
    assert b.get(0, 0) == EMPTY


def test_suicide_rejected_by_default():
    b = Board(19)
    b.play(BLACK, P(1, 0))
    b.play(BLACK, P(0, 1))
    with pytest.raises(IllegalMove):
        b.play(WHITE, P(0, 0))
    # Board unchanged at that point.
    assert b.get(0, 0) == EMPTY


def test_single_stone_suicide_rejected():
    from katsura.go.board import ILLEGAL_SUICIDE_SINGLE
    b = Board(19)
    b.play(BLACK, P(1, 0))
    b.play(BLACK, P(0, 1))
    status, captured = b.play_classified(WHITE, P(0, 0))
    assert status == ILLEGAL_SUICIDE_SINGLE
    assert b.get(0, 0) == EMPTY  # not applied


def test_multi_stone_suicide_allowed():
    from katsura.go.board import MOVE_SUICIDE_MULTI
    b = Board(19)
    # Black surrounds a 2-space white region fully; white filling the last point
    # is a multi-stone suicide -> allowed, the white group is removed.
    # Build: white at (0,0),(0,1); black wall around except (0,1) being filled.
    b.play(WHITE, P(0, 0))
    b.play(BLACK, P(1, 0))
    b.play(BLACK, P(1, 1))
    b.play(BLACK, P(0, 2))
    # Now white plays (0,1): white group (0,0),(0,1) has no liberties -> multi suicide.
    status, captured = b.play_classified(WHITE, P(0, 1))
    assert status == MOVE_SUICIDE_MULTI
    assert b.get(0, 0) == EMPTY and b.get(0, 1) == EMPTY
    assert P(0, 0) in captured and P(0, 1) in captured


def test_occupied_rejected():
    b = Board(19)
    b.play(BLACK, P(3, 3))
    with pytest.raises(IllegalMove):
        b.play(WHITE, P(3, 3))


def test_pass_is_legal_and_noop():
    b = Board(19)
    assert b.play(BLACK, None) == []


def test_non_square_board():
    b = Board(width=2, height=9)
    assert b.width == 2 and b.height == 9
    b.play(BLACK, P(0, 0))
    b.play(WHITE, P(1, 0))
    b.play(WHITE, P(0, 1))
    # Black at (0,0): neighbors (1,0)=W, (0,1)=W -> captured.
    assert b.get(0, 0) == EMPTY


def test_capturing_gives_liberty_not_suicide():
    # Classic: a move that looks suicidal but captures, giving a liberty.
    b = Board(19)
    # Build a white stone at (0,0) in atari, black surrounding except (0,1).
    b.play(WHITE, P(0, 0))
    b.play(BLACK, P(1, 0))
    # Now black plays (0,1): captures white at (0,0); not suicide.
    captured = b.play(BLACK, P(0, 1))
    assert P(0, 0) in captured
    assert b.get(0, 1) == BLACK


def test_remove_dead_groups():
    b = Board(19)
    # Manually set up a stone with no liberties via setup (invalid position).
    b.apply_setup(add_white=[P(0, 0)], add_black=[P(1, 0), P(0, 1)])
    removed = b.remove_dead_groups()
    assert P(0, 0) in removed
    assert b.get(0, 0) == EMPTY


def test_setup_no_capture_semantics():
    b = Board(19)
    # Setup does not auto-capture; both stones remain even if adjacent.
    b.apply_setup(add_black=[P(3, 3)], add_white=[P(3, 4)])
    assert b.get(3, 3) == BLACK
    assert b.get(3, 4) == WHITE


def test_resolve_setup_capture_removes_adjacent_opponent():
    b = Board(19)
    b.apply_setup(add_white=[P(0, 0)], add_black=[P(0, 1)])
    b.apply_setup(add_black=[P(1, 0)])               # the just-placed stone
    removed = b.resolve_setup_capture(P(1, 0), BLACK)
    assert P(0, 0) in removed                         # the surrounded white dies
    assert b.get(0, 0) == EMPTY
    assert b.get(1, 0) == BLACK and b.get(0, 1) == BLACK


def test_resolve_setup_capture_self_capture_removes_own_group():
    b = Board(19)
    b.apply_setup(add_black=[P(1, 0), P(0, 1)])       # surrounders
    b.apply_setup(add_white=[P(0, 0)])               # placed white: no liberties
    removed = b.resolve_setup_capture(P(0, 0), WHITE)
    assert P(0, 0) in removed                         # own stone removed
    assert b.get(0, 0) == EMPTY
    assert b.get(1, 0) == BLACK and b.get(0, 1) == BLACK   # opponents untouched


def test_resolve_setup_capture_opponents_before_self():
    # The placed black stone fills its own last liberty but FIRST captures a
    # white stone, which gives the black group a liberty -> black survives.
    b = Board(19)
    b.apply_setup(add_white=[P(0, 0)], add_black=[P(1, 0), P(1, 1), P(0, 2)])
    b.apply_setup(add_black=[P(0, 1)])               # placed: captures W(0,0)
    removed = b.resolve_setup_capture(P(0, 1), BLACK)
    assert P(0, 0) in removed                         # white captured first
    assert b.get(0, 1) == BLACK                       # black NOT self-captured


def test_resolve_setup_capture_leaves_distant_illegality():
    b = Board(19)
    # A dead white stone far away (an illegal position) must be left alone.
    b.apply_setup(add_white=[P(10, 10)],
                  add_black=[P(11, 10), P(9, 10), P(10, 11), P(10, 9)])
    # A separate, local capture near the corner.
    b.apply_setup(add_white=[P(0, 0)], add_black=[P(0, 1)])
    b.apply_setup(add_black=[P(1, 0)])
    removed = b.resolve_setup_capture(P(1, 0), BLACK)
    assert P(0, 0) in removed and P(10, 10) not in removed
    assert b.get(10, 10) == WHITE                     # distant dead stone stays


def test_resolve_setup_capture_noop_when_nothing_dies():
    b = Board(19)
    b.apply_setup(add_black=[P(3, 3)])
    removed = b.resolve_setup_capture(P(3, 3), BLACK)
    assert removed == []
    assert b.get(3, 3) == BLACK


def test_board_copy_independent():
    b = Board(9)
    b.play(BLACK, P(4, 4))
    c = b.copy()
    c.play(WHITE, P(0, 0))
    assert b.get(0, 0) == EMPTY
    assert c.get(4, 4) == BLACK
