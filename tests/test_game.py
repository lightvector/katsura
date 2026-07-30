"""Tests for the Game model: navigation, move/setup editing, markup."""

import pytest

from katsura.go.board import BLACK, WHITE, EMPTY
from katsura.model.game import Game, GameError, parse_board_size
from katsura.model import markup as M
from katsura.sgf.coords import Point
from katsura.sgf.tree import parse_collection, serialize_collection


def P(x, y):
    return Point(x, y)


def test_new_game_defaults():
    g = Game.new(19)
    assert g.width == 19 and g.height == 19
    assert g.to_move == BLACK
    assert g.move_number == 0


def test_parse_board_size_variants():
    assert parse_board_size(parse_collection("(;SZ[19])")[0]) == (19, 19)
    assert parse_board_size(parse_collection("(;SZ[9])")[0]) == (9, 9)
    assert parse_board_size(parse_collection("(;SZ[2:9])")[0]) == (2, 9)
    assert parse_board_size(parse_collection("(;GM[1])")[0]) == (19, 19)
    # Garbage sizes degrade instead of crashing the load.
    assert parse_board_size(parse_collection("(;SZ[100])")[0]) == (52, 52)
    assert parse_board_size(parse_collection("(;SZ[9:361])")[0]) == (9, 52)
    assert parse_board_size(parse_collection("(;SZ[0])")[0]) == (1, 1)
    Game(parse_collection("(;GM[1]SZ[100]AB[ZZ])")[0])   # constructs, no raise


def test_out_of_board_setup_points_ignored():
    # jj = (9,9): off a 9x9 entirely (raw index would raise). ja = (9,0): off
    # board but its flat index (9) would silently WRAP to point (0,1).
    g = Game(parse_collection("(;GM[1]SZ[9]AB[jj][ja][cc])")[0])
    assert g.board.get(2, 2) == BLACK          # the in-bounds stone landed
    assert g.board.get(0, 1) == EMPTY          # no wrap-around stone
    assert sum(1 for y in range(9) for x in range(9)
               if g.board.get(x, y) != EMPTY) == 1
    # The raw property is preserved verbatim on save.
    assert "[jj]" in serialize_collection([g.root])


def test_malformed_coordinates_tolerated():
    # A malformed move value replays as a pass; malformed/garbage point-list
    # values are skipped. Navigation and save must not crash or alter them.
    src = "(;GM[1]SZ[9]AB[!!][cc];B[x9];W[ee])"
    g = Game(parse_collection(src)[0])
    g.go_to_end()
    assert g.board.get(2, 2) == BLACK
    assert g.board.get(4, 4) == WHITE
    assert g.move_number == 2                  # the malformed B counted as a move
    out = serialize_collection([g.root])
    assert "B[x9]" in out
    assert "[!!]" in out


def test_play_creates_nodes_and_alternates():
    g = Game.new(19)
    g.play(P(3, 3))
    assert g.board.get(3, 3) == BLACK
    assert g.to_move == WHITE
    assert g.move_number == 1
    g.play(P(15, 15))
    assert g.board.get(15, 15) == WHITE
    assert g.to_move == BLACK
    assert g.move_number == 2


def test_play_reuses_existing_child():
    g = Game.new(19)
    n1 = g.play(P(3, 3))
    g.back()
    n2 = g.play(P(3, 3))
    assert n1 is n2  # did not create a duplicate
    assert len(g.root.children) == 1


def test_play_branches():
    g = Game.new(19)
    g.play(P(3, 3))
    g.back()
    g.play(P(15, 15))  # different move -> new branch
    assert len(g.root.children) == 2


def test_illegal_move_rejected():
    g = Game.new(19)
    g.play(P(3, 3))
    with pytest.raises(GameError):
        g.play(P(3, 3))  # occupied


def test_navigation():
    g = Game.new(19)
    for x in range(5):
        g.play(P(x, 0))
    assert g.move_number == 5
    g.go_to_start()
    assert g.move_number == 0
    g.forward(3)
    assert g.move_number == 3
    g.back(2)
    assert g.move_number == 1
    g.go_to_end()
    assert g.move_number == 5


def test_forward_back_clamped():
    g = Game.new(19)
    g.play(P(0, 0))
    assert g.back(99) is True
    assert g.move_number == 0
    assert g.back(1) is False
    assert g.forward(99) is True
    assert g.forward(1) is False


def test_forward_remembers_last_variation():
    g = Game.new(19)
    g.play(P(3, 3))     # child 0
    g.back()
    g.play(P(15, 15))   # child 1, now current; root's preferred child = 1
    g.back()
    # Forward should return to the *last explored* variation (child 1), not child 0.
    g.forward()
    assert g.board.get(15, 15) == BLACK
    # Visit child 0; now forward should remember that one.
    g.back()
    g.goto(g.root.children[0])
    g.back()
    g.forward()
    assert g.board.get(3, 3) == BLACK


def test_setup_editing_diff():
    g = Game.new(19)
    g.set_setup_point(P(3, 3), BLACK)
    g.set_setup_point(P(4, 4), WHITE)
    assert g.board.get(3, 3) == BLACK
    assert g.board.get(4, 4) == WHITE
    assert g.root.get("AB") == ["dd"]
    assert g.root.get("AW") == ["ee"]


def test_setup_enforces_validity():
    g = Game.new(19)
    # Surround a point with black, then try to place a white stone with no libs.
    g.set_setup_point(P(1, 0), BLACK)
    g.set_setup_point(P(0, 1), BLACK)
    g.set_setup_point(P(0, 0), WHITE)
    # White at (0,0) has no liberties -> removed; should not be on board.
    assert g.board.get(0, 0) == EMPTY


# -- setup capture resolution (normal mode bakes captures into the SGF) -------

def _state(g):
    """A complete snapshot of the interpreted GUI state at the current node."""
    return (list(g.board._grid), dict(g.setup_ghosts), g.to_move)


def test_setup_capture_baked_restores_non_edit():
    # The motivating bug: a black corner stone surrounded by white is captured,
    # and the capture is reflected *into the SGF* (the AB specifier is dropped),
    # restoring the point to a pure non-edit rather than a nominally illegal one.
    g = Game.new(19)
    g.set_setup_point(P(0, 0), BLACK)
    g.set_setup_point(P(1, 0), WHITE)
    g.set_setup_point(P(0, 1), WHITE)        # captures the black corner
    assert g.board.get(0, 0) == EMPTY
    assert not g.root.has("AB")              # the AB specifier is gone
    assert g.root.get("AW") == ["ab", "ba"]
    assert P(0, 0) not in g.setup_ghosts     # baked, so no leftover ghost


def test_setup_capture_of_inherited_stone_records_ae():
    # When the captured stone was inherited from the parent, removing it needs an
    # explicit AE on the editing node (minimal-diff vs the parent).
    g = Game.new(19)
    g.set_setup_point(P(0, 0), BLACK)        # AB on the root
    g.play(P(5, 5))                          # a move -> next setup is a child
    g.set_setup_point(P(1, 0), WHITE)
    g.set_setup_point(P(0, 1), WHITE)        # captures the inherited corner
    child = g.current
    assert g.board.get(0, 0) == EMPTY
    assert child.get("AE") == ["aa"]         # inherited stone removed via AE
    assert P(0, 0) not in g.setup_ghosts


def test_setup_capture_only_adjacent_groups():
    # Illegality elsewhere is not touched by a local capture: a pre-existing dead
    # group (placed via force edits) stays a ghost while a separate corner is
    # captured cleanly.
    g = Game.new(19)
    g.set_setup_point(P(10, 10), BLACK)                     # "kk"
    for p in [P(11, 10), P(9, 10), P(10, 11), P(10, 9)]:
        g.set_setup_point(p, WHITE, force_redundant=True)   # force: leave it dead
    assert g.setup_ghosts == {P(10, 10): BLACK}
    # A separate normal capture in the corner removes only the corner's AB.
    g.set_setup_point(P(0, 0), BLACK)                       # "aa"
    g.set_setup_point(P(1, 0), WHITE)
    g.set_setup_point(P(0, 1), WHITE)
    assert g.board.get(0, 0) == EMPTY
    assert g.root.get("AB") == ["kk"]              # "aa" baked out; "kk" retained
    assert g.setup_ghosts == {P(10, 10): BLACK}    # distant illegality untouched


def test_normal_setup_self_capture_records_nothing():
    g = Game.new(19)
    g.set_setup_point(P(1, 0), BLACK)
    g.set_setup_point(P(0, 1), BLACK)
    g.set_setup_point(P(0, 0), WHITE)        # self-captures (no opponent dies)
    assert g.board.get(0, 0) == EMPTY
    assert not g.root.has("AW")              # the dead white leaves no specifier
    assert P(0, 0) not in g.setup_ghosts     # resolved, not a ghost


# -- force (ctrl) edits keep illegal setup; interpretation shows ghosts --------

def test_force_setup_keeps_illegal_as_ghost():
    g = Game.new(19)
    g.set_setup_point(P(0, 0), BLACK)
    g.set_setup_point(P(1, 0), WHITE, force_redundant=True)
    g.set_setup_point(P(0, 1), WHITE, force_redundant=True)
    assert g.board.get(0, 0) == EMPTY                 # interpretation removes it
    assert g.setup_ghosts == {P(0, 0): BLACK}         # ...but it is a ghost
    assert g.root.get("AB") == ["aa"]                 # AB retained verbatim


def test_loaded_illegal_setup_becomes_ghost():
    roots = parse_collection("(;GM[1]SZ[19]AW[aa]AB[ab][ba])")
    g = Game(roots[0])
    assert g.board.get(0, 0) == EMPTY                 # dead white removed
    assert g.setup_ghosts == {P(0, 0): WHITE}         # shown as a ghost


def test_ghost_not_shown_when_move_reoccupies_point():
    # A setup stone removed by the legality sweep, then re-occupied by the node's
    # own move, is a real stone (not a ghost).
    roots = parse_collection("(;GM[1]SZ[19]AB[ba][ab]AW[aa]B[aa])")
    g = Game(roots[0])
    # AW[aa] is dead (surrounded by AB) -> swept; then B[aa] plays there legally.
    assert g.board.get(0, 0) == BLACK
    assert P(0, 0) not in g.setup_ghosts


# -- the setup *layer* the tool edits (pre-interpretation) --------------------

def test_setup_layer_treats_ghost_as_present():
    g = Game.new(19)
    g.set_setup_point(P(0, 0), BLACK)
    g.set_setup_point(P(1, 0), WHITE, force_redundant=True)
    g.set_setup_point(P(0, 1), WHITE, force_redundant=True)
    # The board shows empty, but the setup layer still sees the black ghost.
    assert g.board.get(0, 0) == EMPTY
    assert g.setup_layer_color(P(0, 0)) == BLACK


def test_setup_click_target_cycles_stone_empty_tool():
    g = Game.new(19)
    assert g.setup_click_target(P(3, 3), BLACK) == BLACK     # empty -> tool colour
    g.set_setup_point(P(3, 3), WHITE)
    assert g.setup_click_target(P(3, 3), BLACK) == EMPTY     # opposite colour erases
    assert g.setup_click_target(P(3, 3), WHITE) == EMPTY     # same colour erases too


def test_setup_click_target_treats_ghost_as_a_stone():
    g = Game.new(19)
    g.set_setup_point(P(0, 0), WHITE)
    g.set_setup_point(P(1, 0), BLACK, force_redundant=True)
    g.set_setup_point(P(0, 1), BLACK, force_redundant=True)  # white ghost at (0,0)
    assert g.board.get(0, 0) == EMPTY
    assert g.setup_click_target(P(0, 0), BLACK) == EMPTY     # ghost -> erase first


def test_clicking_a_ghost_can_erase_it():
    g = Game.new(19)
    g.set_setup_point(P(0, 0), BLACK)
    g.set_setup_point(P(1, 0), WHITE, force_redundant=True)
    g.set_setup_point(P(0, 1), WHITE, force_redundant=True)
    # Emulate the tool's toggle decision off the setup layer.
    target = EMPTY if g.setup_layer_color(P(0, 0)) == BLACK else BLACK
    g.set_setup_point(P(0, 0), target)
    assert not g.root.has("AB")              # the ghost's AB is removed
    assert P(0, 0) not in g.setup_ghosts


# -- the core invariant: interpreted state is a pure function of root->node ----

def test_state_invariant_across_navigation():
    g = Game.new(19)
    g.set_setup_point(P(0, 0), BLACK)
    g.set_setup_point(P(1, 0), WHITE, force_redundant=True)
    g.set_setup_point(P(0, 1), WHITE, force_redundant=True)
    root = g.current
    g.play(P(10, 10))                        # a move to navigate over
    g.goto(root)
    before = _state(g)
    g.go_to_end(); g.go_to_start(); g.goto(root)     # wander away and back
    assert _state(g) == before
    assert g.setup_ghosts == {P(0, 0): BLACK}


def test_state_invariant_across_save_reload():
    g = Game.new(19)
    g.set_setup_point(P(0, 0), BLACK)
    g.set_setup_point(P(1, 0), WHITE, force_redundant=True)
    g.set_setup_point(P(0, 1), WHITE, force_redundant=True)
    before = _state(g)
    out = serialize_collection([g.root])
    g2 = Game(parse_collection(out)[0])
    assert _state(g2) == before
    assert g2.setup_ghosts == {P(0, 0): BLACK}


def test_resolved_capture_board_equals_interpretation():
    # After a normal capture the recorded SGF, re-interpreted from scratch, gives
    # exactly the same board with no ghosts (the resolution matches the model).
    g = Game.new(19)
    g.set_setup_point(P(0, 0), BLACK)
    g.set_setup_point(P(1, 0), WHITE)
    g.set_setup_point(P(0, 1), WHITE)
    out = serialize_collection([g.root])
    g2 = Game(parse_collection(out)[0])
    assert list(g2.board._grid) == list(g.board._grid)
    assert g2.setup_ghosts == {} == g.setup_ghosts


def test_setup_on_move_node_creates_child():
    g = Game.new(19)
    g.play(P(3, 3))
    move_node = g.current
    g.set_setup_point(P(10, 10), BLACK)
    assert g.current is not move_node
    assert g.current.parent is move_node
    assert g.board.get(10, 10) == BLACK


def test_handicap_white_to_move():
    roots = parse_collection("(;GM[1]SZ[19]AB[dd][pp][dp])")
    g = Game(roots[0])
    assert g.to_move == WHITE


def test_pl_overrides_to_move():
    roots = parse_collection("(;GM[1]SZ[19]AB[dd]PL[B])")
    g = Game(roots[0])
    assert g.to_move == BLACK


def test_marks_toggle():
    g = Game.new(19)
    g.toggle_mark(P(3, 3), M.MarkType.TRIANGLE)
    assert g.get_marks()[P(3, 3)] == "TR"
    g.toggle_mark(P(3, 3), M.MarkType.TRIANGLE)
    assert P(3, 3) not in g.get_marks()


def test_marks_mutually_exclusive_per_point():
    g = Game.new(19)
    g.toggle_mark(P(3, 3), M.MarkType.TRIANGLE)
    g.toggle_mark(P(3, 3), M.MarkType.SQUARE)
    marks = g.get_marks()
    assert marks[P(3, 3)] == "SQ"  # square replaced triangle


def test_labels():
    g = Game.new(19)
    g.set_label(P(3, 3), "A")
    g.set_label(P(4, 4), "B")
    assert g.get_labels() == {P(3, 3): "A", P(4, 4): "B"}
    g.set_label(P(3, 3), "")
    assert P(3, 3) not in g.get_labels()


def test_comment_roundtrip():
    g = Game.new(19)
    g.play(P(3, 3))
    g.set_comment("A nice move]\\with escapes")
    out = serialize_collection([g.root])
    reparsed = parse_collection(out)
    g2 = Game(reparsed[0])
    g2.go_to_end()
    assert g2.get_comment() == "A nice move]\\with escapes"


def test_delete_node():
    g = Game.new(19)
    g.play(P(3, 3))
    g.play(P(4, 4))
    last = g.current
    g.delete_node(last)
    assert g.move_number == 1
    assert not g.current.children


def test_tolerant_load_invalid_position():
    # An SGF whose setup leaves a stone without liberties should still load to
    # a valid board.
    roots = parse_collection("(;GM[1]SZ[19]AW[aa]AB[ab][ba])")
    g = Game(roots[0], tolerant=True)
    assert g.board.get(0, 0) == EMPTY  # the dead white stone is gone


def test_pass_move():
    g = Game.new(19)
    g.pass_move()
    assert g.to_move == WHITE
    assert g.current.get_one("B") == ""


def test_pl_after_move_controls_next_player():
    # Black plays (so White would normally be next); setting PL[B] on that same
    # node must make Black to move next (PL applies after the move).
    g = Game.new(19)
    g.play(P(3, 3))
    assert g.to_move == WHITE
    g.set_player_to_move(BLACK)
    assert g.to_move == BLACK


def test_illegal_move_in_sgf_is_skipped_not_forced():
    # A move replayed onto an occupied point must be skipped (board unchanged),
    # flagged illegal, with to_move still flipping. See docs/MODEL.md.
    src = "(;GM[1]SZ[19];B[dd];W[dd])"  # white plays on black's stone
    roots = parse_collection(src)
    g = Game(roots[0])
    g.go_to_end()  # at the W[dd] node
    from katsura.go.board import BLACK as B
    assert g.board.get(3, 3) == B          # still the black stone, not white
    assert g.last_move_illegal is True
    assert g.last_move_point == P(3, 3)
    assert g.to_move == BLACK               # white's (skipped) move still flips turn


def test_setup_redundancy_trimmed_normally():
    # Parent has a black stone; an edit that ends on black there records nothing.
    g = Game.new(19)
    g.set_setup_point(P(3, 3), BLACK)      # AB on root
    g.play(P(15, 15))                      # a move so the next setup is a child
    g.set_setup_point(P(3, 3), BLACK)      # redundant (already black from parent)
    assert not g.current.has("AB")         # nothing recorded
    assert not g.current.has("AE")


def test_setup_force_keeps_redundant():
    g = Game.new(19)
    g.set_setup_point(P(3, 3), BLACK)
    g.play(P(15, 15))
    g.set_setup_point(P(3, 3), BLACK, force_redundant=True)
    assert g.current.get("AB") == ["dd"]   # recorded even though redundant


def test_setup_toggle_and_minimal_diff():
    # Add then erase a stone absent in the parent leaves no specifier.
    g = Game.new(19)
    g.set_setup_point(P(3, 3), BLACK)
    g.set_setup_point(P(3, 3), EMPTY)
    assert not g.root.has("AB")
    assert not g.root.has("AE")


def test_insert_empty_node():
    g = Game.new(19)
    g.play(P(3, 3))
    n = g.insert_empty_node()
    assert n.parent is g.root.children[0]
    assert not n.props and not n.children
    assert g.current is n


def test_shift_variation_reorders():
    g = Game.new(19)
    g.play(P(3, 3)); g.back()
    g.play(P(15, 15)); g.back()    # root has two children, current path was child1
    g.goto(g.root.children[1])     # the second variation
    assert g.shift_variation(-1) is True
    assert g.root.children[0] is g.current   # moved up to first
    assert g.shift_variation(-1) is False    # already first


def test_promote_closest_rotates_to_front():
    g = Game.new(19)
    for _ in range(3):
        g.play(P(3, 3)); g.back()      # three children at root (dup-reuse -> actually 1)
    # Build genuine siblings.
    g = Game.new(19)
    g.play(P(0, 0)); g.back()
    g.play(P(1, 1)); g.back()
    g.play(P(2, 2)); g.back()
    g.goto(g.root.children[2])
    assert g.promote_closest() is True
    assert g.root.children[0] is g.current


def test_clone_is_deep():
    g = Game.new(19)
    g.play(P(3, 3)); g.play(P(4, 4))
    clone = g.root.children[0].clone()
    assert clone is not g.root.children[0]
    assert clone.get_one("B") == g.root.children[0].get_one("B")
    # Mutating the clone doesn't touch the original.
    clone.children[0].set_one("C", "x")
    assert not g.root.children[0].children[0].has("C")


def test_subtree_cut_and_copy():
    g = Game.new(19)
    g.play(P(3, 3))            # node A (move1)
    g.play(P(4, 4))            # node B under A
    g.back()                   # at A
    g.play(P(5, 5)); g.back()  # node C under A (sibling of B)
    a = g.root.children[0]
    b = a.children[0]
    c = a.children[1]
    # Copy subtree b under c.
    g.goto(c)
    clone = g.copy_subtree(b, c)
    assert clone in c.children and clone is not b
    assert b in a.children          # original still present (copy)
    # Cut subtree c under root.
    g.copy = None
    g.cut_subtree(c, g.root)
    assert c in g.root.children and c not in a.children


def test_cannot_paste_into_self():
    g = Game.new(19)
    g.play(P(3, 3)); g.play(P(4, 4))
    a = g.root.children[0]
    descendant = a.children[0]
    assert g.can_transplant(a, descendant) is False
    with pytest.raises(GameError):
        g.cut_subtree(a, descendant)


def test_snapshot_and_paste_compound():
    g = Game.new(19)
    g.set_setup_point(P(3, 3), BLACK)
    g.set_setup_point(P(4, 3), WHITE)
    g.toggle_mark(P(3, 4), M.MarkType.TRIANGLE)
    g.set_label(P(4, 4), "A")
    items, dims = g.snapshot_points({P(3, 3), P(4, 3), P(3, 4), P(4, 4)})
    assert dims == (2, 2)
    assert len(items) == 4
    # Paste onto a fresh game at an offset.
    g2 = Game.new(19)
    g2.apply_paste(items, P(10, 10))
    assert g2.board.get(10, 10) == BLACK
    assert g2.board.get(11, 10) == WHITE
    assert g2.get_marks()[P(10, 11)] == "TR"
    assert g2.get_labels()[P(11, 11)] == "A"


def test_apply_erase_clears_contents():
    g = Game.new(19)
    g.set_setup_point(P(3, 3), BLACK)
    g.toggle_mark(P(3, 3), M.MarkType.SQUARE)
    g.apply_erase({P(3, 3)})
    assert g.board.get(3, 3) == EMPTY
    assert P(3, 3) not in g.get_marks()


def test_apply_move_shifts_and_drops_offboard():
    g = Game.new(19)
    g.set_setup_point(P(1, 1), BLACK)
    g.apply_move({P(1, 1)}, 2, 3)
    assert g.board.get(1, 1) == EMPTY
    assert g.board.get(3, 4) == BLACK


def _ko_position():
    """A textbook simple-ko shape: white at (1,1), capturable by black at (2,1)."""
    g = Game.new(19)
    for p in [P(0, 1), P(1, 0), P(1, 2)]:
        g.set_setup_point(p, BLACK)
    for p in [P(1, 1), P(2, 0), P(2, 2), P(3, 1)]:
        g.set_setup_point(p, WHITE)
    g.set_player_to_move(BLACK)
    return g


def test_simple_ko_recapture_forbidden_only_when_enabled():
    g = _ko_position()
    g.play(P(2, 1), BLACK)                 # captures the white stone at (1,1)
    assert g.board.get(1, 1) == EMPTY and g.board.get(2, 1) == BLACK
    # The immediate recapture at (1,1) restores the prior position -> simple ko.
    with pytest.raises(GameError):
        g.play(P(1, 1), WHITE, forbid_ko=True)
    # The rejected attempt must leave the displayed state untouched: the ko
    # check replays the parent position via board_at, which used to clobber
    # the last-move marker as a side effect.
    assert g.last_move_point == P(2, 1)
    assert g.last_move_illegal is False
    assert g.to_move == WHITE and g.move_number == 1
    # Default (tolerant) behaviour allows it.
    g.play(P(1, 1), WHITE, forbid_ko=False)
    assert g.board.get(1, 1) == WHITE and g.board.get(2, 1) == EMPTY


def test_multi_stone_self_capture_forbidden_only_when_enabled():
    g = Game.new(19)
    for p in [P(0, 1), P(1, 1), P(2, 0)]:
        g.set_setup_point(p, WHITE)
    g.set_setup_point(P(0, 0), BLACK)
    g.set_player_to_move(BLACK)
    # Playing (1,0) makes a 2-stone black group with no liberties (no capture).
    with pytest.raises(GameError):
        g.play(P(1, 0), BLACK, forbid_multi_suicide=True)
    g.play(P(1, 0), BLACK, forbid_multi_suicide=False)   # tolerated by default
    assert g.board.get(0, 0) == EMPTY and g.board.get(1, 0) == EMPTY


def test_non_ko_single_capture_is_allowed_under_ko_rule():
    """A single capture that does NOT recreate the prior position is fine."""
    g = Game.new(19)
    g.play(P(0, 1), BLACK)
    g.play(P(0, 0), WHITE)
    g.play(P(1, 0), BLACK)                 # captures W at (0,0); not a ko shape
    assert g.board.get(0, 0) == EMPTY
    # White elsewhere, then black retakes (0,0): legal, not a simple-ko recapture.
    g.play(P(5, 5), WHITE)
    g.play(P(0, 0), BLACK, forbid_ko=True)
    assert g.board.get(0, 0) == BLACK


def test_transform_rotate_cw_moves_setup_marks_labels():
    """Rotating 90° CW remaps every coordinate-bearing property on every node."""
    roots = parse_collection(
        "(;GM[1]SZ[19]AB[aa]AW[sa];B[ab]CR[ba]LB[bb:X]TR[sb])")
    g = Game(roots[0])
    g.transform_geometry("rot_cw")
    root, nd = g.root, g.root.children[0]
    # (x,y) -> (h-1-y, x) on a 19x19 board, so h-1 = 18.
    assert sorted(root.get(M.ADD_BLACK)) == [point_sgf(P(18, 0))]   # aa -> (18,0)
    assert sorted(root.get(M.ADD_WHITE)) == [point_sgf(P(18, 18))]  # sa(18,0)->(18,18)
    assert nd.get_one(M.BLACK_MOVE) == point_sgf(P(17, 0))          # ab(0,1)->(17,0)
    assert nd.get("CR") == [point_sgf(P(18, 1))]                   # ba(1,0)->(18,1)
    assert nd.get("TR") == [point_sgf(P(17, 18))]                 # sb(18,1)->(17,18)
    assert nd.get(M.LABEL_PROP) == [f"{point_sgf(P(17, 1))}:X"]    # bb(1,1)->(17,1)


def test_transform_rotate_swaps_nonsquare_size():
    g = Game(parse_collection("(;GM[1]SZ[5:9]AB[ab])")[0])
    g.transform_geometry("rot_cw")
    assert (g.width, g.height) == (9, 5)
    assert g.root.get_one("SZ") == "9:5"
    # ab is (0,1) on a 5x9 board; CW -> (9-1-1, 0) = (7, 0).
    assert g.root.get(M.ADD_BLACK) == [point_sgf(P(7, 0))]


def test_transform_flip_h_is_self_inverse():
    sgf = "(;GM[1]SZ[19]AB[ab][cd]AW[ef];B[gh]LB[ij:7])"
    g = Game(parse_collection(sgf)[0])
    g.transform_geometry("flip_h")
    g.transform_geometry("flip_h")
    assert serialize_collection([g.root]).strip() == sgf


def test_transform_preserves_passes():
    g = Game(parse_collection("(;GM[1]SZ[19];B[])")[0])
    g.transform_geometry("rot_cw")
    assert g.root.children[0].get_one(M.BLACK_MOVE) == ""


def point_sgf(p):
    from katsura.sgf.coords import point_to_sgf
    return point_to_sgf(p)


# -- fit_subtree_to_board (cross-board-size subtree paste) -------------------

def _subtree_of(sgf):
    """Parse a one-line SGF and return the root's first child, detached —
    the shape a cross-tab paste hands to fit_subtree_to_board."""
    node = parse_collection(sgf)[0].children[0]
    node.detach()
    return node


def test_fit_subtree_top_left_anchor_no_shift():
    from katsura.model.game import fit_subtree_to_board
    # Moves near the top-left of a 19x19: pasting onto 9x9 keeps coordinates.
    sub = _subtree_of("(;GM[1]SZ[19];B[cc];W[dd])")
    pruned = fit_subtree_to_board(sub, 19, 19, 9, 9)
    assert pruned == 0
    assert sub.get_one(M.BLACK_MOVE) == point_sgf(P(2, 2))
    assert sub.children[0].get_one(M.WHITE_MOVE) == point_sgf(P(3, 3))


def test_fit_subtree_bottom_right_anchor_shifts():
    from katsura.model.game import fit_subtree_to_board
    # qq = (16,16), 2 off the bottom-right of 19x19. On 13x13 the same offset
    # from that corner is (10,10); pp(15,15) follows along to (9,9).
    sub = _subtree_of("(;GM[1]SZ[19];B[qq];W[pp])")
    pruned = fit_subtree_to_board(sub, 19, 19, 13, 13)
    assert pruned == 0
    assert sub.get_one(M.BLACK_MOVE) == point_sgf(P(10, 10))
    assert sub.children[0].get_one(M.WHITE_MOVE) == point_sgf(P(9, 9))


def test_fit_subtree_prunes_offboard_ops():
    from katsura.model.game import fit_subtree_to_board
    # Box spans (2,2)..(16,16) on 19x19, anchored top-left (tie). On 9x9 the
    # far stones/marks fall off and are pruned; near ones survive.
    sub = _subtree_of("(;GM[1]SZ[19];B[cc]AB[qq]CR[cc][qq]LB[cc:A][qq:B];W[qq])")
    pruned = fit_subtree_to_board(sub, 19, 19, 9, 9)
    # Pruned: AB[qq], CR[qq], LB[qq:B], and the W[qq] move itself.
    assert pruned == 4
    assert sub.get_one(M.BLACK_MOVE) == point_sgf(P(2, 2))
    assert sub.get(M.ADD_BLACK) == []            # emptied list drops the property
    assert sub.get("CR") == [point_sgf(P(2, 2))]
    assert sub.get(M.LABEL_PROP) == [f"{point_sgf(P(2, 2))}:A"]
    assert not sub.children[0].has(M.WHITE_MOVE)  # pruned move loses its property


def test_fit_subtree_axes_anchor_independently():
    from katsura.model.game import fit_subtree_to_board
    # A stone near the bottom-left of 19x19 (x anchors left, y anchors bottom):
    # ar = (0,17), 1 off the bottom. On 9x9 it lands at (0,7).
    sub = _subtree_of("(;GM[1]SZ[19];B[ar])")
    assert fit_subtree_to_board(sub, 19, 19, 9, 9) == 0
    assert sub.get_one(M.BLACK_MOVE) == point_sgf(P(0, 7))


def test_fit_subtree_small_to_big_keeps_corner_offset():
    from katsura.model.game import fit_subtree_to_board
    # hh = (7,7) is the bottom-right-most point of a 9x9 box (1 off each edge);
    # on 19x19 it becomes (17,17).
    sub = _subtree_of("(;GM[1]SZ[9];B[hh])")
    assert fit_subtree_to_board(sub, 9, 9, 19, 19) == 0
    assert sub.get_one(M.BLACK_MOVE) == point_sgf(P(17, 17))


def test_fit_subtree_line_arrow_pruned_whole():
    from katsura.model.game import fit_subtree_to_board
    # One endpoint off the destination board drops the whole arrow.
    sub = _subtree_of("(;GM[1]SZ[19];B[cc]AR[cc:qq]LN[cc:dd])")
    pruned = fit_subtree_to_board(sub, 19, 19, 9, 9)
    assert pruned == 1
    assert sub.get("AR") == []
    assert sub.get("LN") == [f"{point_sgf(P(2, 2))}:{point_sgf(P(3, 3))}"]


def test_fit_subtree_legacy_tt_pass_stays_a_pass():
    from katsura.model.game import fit_subtree_to_board
    # tt is a pass on <=19 boards but a real point on bigger ones; the refit
    # must normalise it so the paste doesn't invent a move at (19,19).
    sub = _subtree_of("(;GM[1]SZ[19];B[cc];W[tt])")
    assert fit_subtree_to_board(sub, 19, 19, 21, 21) == 0
    assert sub.children[0].get_one(M.WHITE_MOVE) == ""


def test_fit_subtree_pointless_subtree_is_noop():
    from katsura.model.game import fit_subtree_to_board
    sub = _subtree_of("(;GM[1]SZ[19];B[]C[just a pass])")
    assert fit_subtree_to_board(sub, 19, 19, 9, 9) == 0
    assert sub.get_one(M.BLACK_MOVE) == ""
    assert sub.get_one(M.COMMENT) == "just a pass"


def test_set_komi_keeps_fractional_precision():
    """Komi is stored as typed: rounding to 2 dp used to rewrite 6.125 as 6.12."""
    g = Game.new(19)
    for value, text in [(6.5, "6.5"), (7.0, "7"), (0.0, "0"), (-0.0, "0"),
                        (6.125, "6.125"), (5.25, "5.25"), (-3.5, "-3.5")]:
        g.set_komi(value)
        assert g.root.get_one("KM") == text
        assert g.get_komi() == value
    g.set_komi(None)
    assert not g.root.has("KM") and g.get_komi() is None


def test_apply_move_with_empty_selection_is_a_noop():
    g = Game.new(9)
    g.set_setup_point(P(2, 2), BLACK)
    before = g.board.get(2, 2)
    g.apply_move(set(), 1, 1)             # used to raise from min() on empty
    assert g.board.get(2, 2) == before


def test_remembered_variation_is_the_node_not_an_index():
    """Forward returns to the variation you explored, even if the variations
    have since been reordered — an index-keyed memory would follow the slot."""
    g = Game.new(9)
    g.play(P(0, 0))
    branch = g.current
    g.play(P(1, 1))                       # variation A
    a = g.current
    g.back(1)
    g.play(P(2, 2))                       # variation B, explored last
    b = g.current
    g.back(1)
    assert g.preferred_child(branch) is b

    branch.children.reverse()             # B is now the *first* child
    assert g.preferred_child(branch) is b

    g.delete_node(b)                      # gone: fall back to the first child
    assert g.preferred_child(branch) is a
    assert branch.remembered_child is b   # stale pointer, simply ignored
