# The position model: how SGF nodes are interpreted and played forward

This is the **authoritative semantic model** for how a pile of SGF moves and
setup edits is interpreted into board positions. The GUI, the loader, and any
future engine integration must all agree with this. If you change behaviour,
update this file and the tests in `tests/` that encode these invariants.

## Vocabulary

- **Position at a node** = the board obtained by replaying root → node.
- **Parent position** = the position at a node's parent (empty board for the root).
- A group is **dead** when it has zero liberties. A valid board never contains a
  dead group.

## Node application (the core algorithm)

Implemented once, in `model/replay.py` (`apply_node` → `NodeApplication`), and
used by **both** the editor's replay (`Game._recompute`) and the engine's
request builder (`engine/position.build_request`) — they have to agree exactly,
or the engine analyses a position the board is not showing.

To apply a single node to a board that already holds the parent position, in
order:

1. **Setup edits (AB / AW / AE), applied atomically.** All of the node's
   `AB` (→ black), `AW` (→ white), `AE` (→ empty) points are written to the
   board *simultaneously* (a point must appear in at most one of the three).
   No captures happen between individual stones.
2. **Setup legality enforcement.** *After* all setup edits are written, every
   dead group is removed in one pass, judged against the fully-edited position
   (the "simultaneous" rule: a stone is captured iff its group has no liberty in
   the final edited position). This guarantees the position stays valid even if
   an intermediate single-stone application would have been illegal. (Answers
   "are edits atomic?": **yes**.) The *nominal* position (after step 1, before
   this sweep) is the **setup layer**; the stones this sweep removes — and the
   move in step 3 does not re-occupy — are the node's **ghost stones**
   (`Game.setup_ghosts`, mapped to their nominal colour). They are still present
   in the SGF setup layer but absent from the legal board, so the GUI draws them
   at ~50% opacity and the setup tool treats them as present (see below).
3. **The move (B or W), if present**, played under the single legality predicate
   below. A node has at most one of `B`/`W`.
4. **`PL` (player-to-move), if present**, sets whose turn it is *next*. It is
   applied **after** the move, so `PL` overrides the normal alternation for the
   following move. (A node with both a move and `PL` is unusual but well-defined:
   `PL` wins.)

`to_move` after a node:
- starts as Black at the root, except a **handicap** root (`AB` present, no `PL`,
  no move) implies White to move;
- a move flips it to the opponent of the *attempted* move's colour (even if the
  move was skipped as illegal — see below);
- `PL` overrides it.

## The single legality predicate (clicking AND replay use the same one)

Given a move of `color` at point `p` on a board:

| Case | Verdict | Effect |
|------|---------|--------|
| `p` is a pass (empty value) | legal | nothing; turn flips |
| `p` already occupied | **illegal** | move skipped |
| place `color`, capture dead opponents, own group then has 0 liberties, **own group size == 1** | **illegal** (single-stone suicide) | move skipped |
| same but **own group size > 1** | legal (multi-stone suicide) | own group removed |
| otherwise | legal | opponent dead groups removed |

- **No ko or superko enforcement** anywhere. Simple-ko and superko repetitions
  are allowed.
- **"Skipped" means**: the board is left exactly as it was before the move (no
  stone placed, nothing captured). `to_move` still flips per the attempted
  colour. The SGF property (`B[..]`/`W[..]`) is preserved verbatim.

### Why skip illegal moves instead of forcing them

When an edit higher in a branch makes a later move illegal (e.g. a stone of the
opposing colour is placed under where the move was later played), we must **not**
force the stone down with the played colour. Forcing it makes the effect on
liberties/ko undefined, and — critically — a future analysis engine cannot be
fed a move history that contains an illegal move played on top of another stone.
It *can* be fed a history that simply **omits** that move. So: the move is
skipped, the position proceeds as if it were never played, and the GUI shows a
red circle at the attempted point for visibility. When we add engine support,
the move list sent to the engine must omit skipped moves while preserving the
order of the legal ones.

## Interactive rules (clicking on the board)

Clicking uses the same base predicate, but illegal results are simply **not
recorded** (no node is created; a brief status message explains why), and two
*optional* restrictions can be layered on **interactive clicks only** (SGF replay
/ loading is never affected by them — it stays fully tolerant). Both are
preferences passed to `Game.play(..., forbid_multi_suicide=, forbid_ko=)`:

- Playing on an occupied point: always rejected.
- Single-stone suicide: always rejected.
- Multi-stone self-capture: allowed by default; rejected when **Forbid
  multi-stone self-capture on play** is on (pref `forbid_multi_suicide`, default
  **off**).
- Simple ko: allowed by default; rejected when **Forbid simple ko on play** is on
  (pref `forbid_simple_ko`, default **on**). "Simple ko" = a move that captures
  exactly one stone and recreates the whole-board position that existed
  immediately before the opponent's last move (i.e. `board_at(current.parent)`).
- Superko (longer-cycle repetition): never enforced.

## Setup-edit recording (AB / AW / AE)

A setup edit sets a point to a target colour (black / white / empty). The tool
toggles a point between *holding a stone* and *empty* (`Game.setup_click_target`):
a click on a point that already holds a stone of **either** colour erases it (sets
empty); only a click on an **empty** point lays the tool colour. So successive
clicks cycle `stone → empty → tool colour` — a black click on a white stone first
*clears* it, and the next click places black (never white → black in one click).

For a **paint stroke**, only the *first* cell decides the action: if it holds a
stone the whole drag erases; if it is empty the whole drag lays the tool colour,
**overwriting** any stones it crosses (a placing black stroke turns crossed white
stones black directly, and leaves crossed black stones black). The action is fixed
for the stroke — later cells never re-trigger the erase-first rule.

**The tool acts on the setup layer, not the legal board.** Whether a point "holds
a stone" is judged against the *nominal* layer (`Game.setup_layer_color` =
interpreted parent + this node's own AB/AW/AE), so a zero-liberty **ghost** stone
counts as present and can be toggled off — there is always a direct way to edit a
setup point to empty. (On a node that holds a move, setup spawns a fresh child,
whose nominal layer is just the interpreted current position.)

The target colour is then recorded:

- **Normal edit**: record the minimal diff vs the *parent* position. The point is
  added to `AB`/`AW`/`AE` **only if** the target differs from the parent's colour
  there; if it matches the parent it is recorded nowhere (no redundant specifier).
  Consequence: add-then-erase of a stone absent in the parent leaves nothing;
  ending on the parent's colour leaves nothing; ending on a different colour
  leaves exactly one specifier.
- **Forced edit (ctrl / shift+ctrl)**: the same target logic, but the specifier is
  **always** recorded even when redundant (target == parent colour). This matters
  for future copy/paste of nodes between branches, where a specifier that is
  redundant here may not be redundant on the destination node.

Setup edits live on a node without a move; if the current node has a move, a new
child node is created to hold them.

### Capture resolution (normal edits bake captures into the SGF)

A **normal** setup edit that places a stone of `color` at `p` then resolves the
captures that placement implies and **records them as further edits**, so the SGF
ends up holding a position that is legal *around `p`* rather than a nominally
illegal one the interpreter would have to fix every time. The rule mirrors a real
move — **setup stone wins**:

1. remove every **opponent** group orthogonally adjacent to `p` that has no
   liberty in the nominal position, then
2. if the placed stone's own group then still has no liberty, remove it too
   (single- and multi-stone self-capture alike — a setup stone that cannot live
   is simply removed, there is no "illegal" verdict here).

Only groups **adjacent to (or including) `p`** are considered; a dead group
*elsewhere* on the board is left untouched (it stays a ghost, resolved only by
interpretation). Each removed point is written back with the normal minimal-diff
rule, so a captured stone becomes `AE` if it was inherited from the parent, or
simply loses its `AB`/`AW` (restoring a pure non-edit) if this node had added it.
So: place black at 1-1, surround it with white, and the black stone's `AB` is
*dropped* — 1-1 becomes empty with no specifier — rather than lingering as an
illegal setup stone.

A **forced (ctrl)** edit does **none** of this: it records the target verbatim and
resolves nothing, deliberately accepting an invalid setup state. The illegality is
then resolved only when the SGF is *interpreted* (step 2 above), and shows as ghost
stones. This is also what happens for any externally-authored SGF whose setup is
illegal — the model never assumes the tool produced the position.

(Erasing — `color == EMPTY` — never captures anything, so it skips resolution.
The selection paste/erase compound edits also do not resolve captures; they record
stones with the plain minimal-diff rule and rely on interpretation, as before.)

**Edit halo**: the board shades every point that carries an `AB`/`AW`/`AE`
specifier on the current node — including redundant (forced) ones — so it is
clear what the node explicitly sets, independent of what is visually on the board.

## Invariants (must always hold)

1. Every position the GUI shows is **valid** (no dead groups).
2. Replaying an SGF and re-serializing changes only what the user edited
   (round-trip fidelity; unknown properties preserved).
3. The board shown at a node depends only on root → node, and is identical
   whether reached by clicking, by navigation, or by loading from disk.
4. A move's contribution to the board is all-or-nothing: it is either played
   legally (possibly capturing) or skipped entirely. It is never half-applied.
5. `to_move` is a pure function of root → node (moves flip it; `PL` overrides;
   handicap root implies White).
6. The **entire** interpreted GUI state at a node — board, `to_move`,
   `move_number`, last-move flag, **and the ghost set** — is a pure function of
   root → node, recomputed (`Game._recompute`) on every navigation. Editing tools
   only ever mutate the SGF tree and then recompute; nothing caches interpreted
   state that a tool writes directly. Therefore navigating away and back, or
   saving and reloading, can never change what a node interprets to. The setup
   tool's capture resolution is *only* a convenience that bakes local legality
   into the recorded edits — it can change which specifiers the SGF holds, but the
   interpreter remains the sole authority on what those specifiers mean, so this
   invariant holds regardless of how the specifiers were produced.

## Undo / redo cursor placement

An undo history entry is a serialized-SGF snapshot plus **two** cursor positions
(`ui/document.py`): `path`, restored with that state, and `counter_path`, the
position for the *other* end of the same edit. `commit_edit` runs *after* the
edit has been applied, so it records the live cursor as `counter_path` — "where
the edit itself left you", inclusive of any movement the edit caused: the node a
played move created, the parent a Backspace deletion fell back to, the node whose
comment you typed, the target a subtree was pasted onto.

`undo`/`redo` then swap the pair (`Document._counterpart`), so:

* **undo** puts you just *before* the edit it reverted (unchanged behaviour);
* **redo** puts you where that edit *ended* — not wherever you happened to have
  navigated to before pressing undo, which may be somewhere entirely unrelated
  and made it impossible to see what had just been re-applied.

Because the pair swaps on each operation, this holds however deep the stacks get.
`push_undo()` (record-then-edit) cannot know the post-edit cursor, so it leaves
`counter_path` unset and undo falls back to the live cursor; prefer
`begin_edit`/`commit_edit` for any edit that moves the cursor.

## Selection edits (the Select tool) and subtree cut/copy

Two independent clipboard-like mechanisms, both fully undoable:

**Subtree cut/copy/paste** operate on the *game tree*. Ctrl+X/Ctrl+C mark the
current node's subtree (it cannot be the root); the active node bumps to the
parent and the subtree is outlined (red=cut, green=copy) in the variation tree.
Ctrl+V transplants it under the current node (must be outside the marked
subtree). A cut is consumed on paste; a copy persists for repeated pastes. The
mark is cleared by Esc, by editing any node inside it, or by starting a board
selection. (`Game.cut_subtree`/`copy_subtree`/`can_transplant`, `SgfNode.clone`.)

**Board-content selection** operates on *board contents at the current node*: a
selected point's "contents" = the stone there (as a setup edit) plus any
mark/label on the current node. Cut/copy snapshot those into a buffer
(`Game.snapshot_points`); paste/erase/move are single compound edits
(`Game.apply_paste`/`apply_erase`/`apply_move`) that:
- apply all changes to one node (a child setup node is created if the current
  node already holds a move; marks/labels otherwise stay on the current node);
- drop any content that lands off-board;
- record stones with the normal minimal-diff setup logic.

The two mechanisms are mutually exclusive (starting one clears the other). See
`ui/editortab.py` for the state machine and `ui/selection.py` for the
rotate/flip buffer transforms.

**Cross-SGF (cross-tab).** Both clipboards live on the `MainWindow`, shared by
all tabs: `window.selection_buffer` and a single `window.subtree_mark`
(`(tab, node, mode)`; only one tab can have a subtree marked at a time). Pasting
applies in the *current* tab as its own undo event. A cross-tab subtree cut
grafts a clone into the target tab (target's undo) and then deletes the source
in the origin tab as a *separate, independent* undo event there. Undo/redo in a
tab drops a subtree mark *it* owns (its nodes are re-parsed and thus stale) but
leaves a mark owned by another tab intact.

**Cross-board-size subtree paste.** When source and target board sizes differ,
the clone is refit before grafting (`fit_subtree_to_board` in `model/game.py`):
the bounding box of every coordinate-bearing operation in the subtree (moves,
AB/AW/AE, marks, labels, TB/TW/DD/VW, AR/LN endpoints) is anchored, per axis,
to whichever source-board edge it lies nearer, and all coordinates are shifted
so the box keeps the same offset from the matching corner of the target board.
Operations still off-board after the shift are pruned: a move loses its B/W
property (the node survives), individual list points/labels vanish, and an
AR/LN drops whole if either endpoint is off. A legacy `tt` pass is normalised
to `[]` so a paste onto a >19 board cannot reinterpret it as a move. The status
bar reports the pruned-operation count. Points off the *source* board (garbage
SGF) don't influence the anchor. The same `_map_node_points` helper also
implements `Game.transform_geometry`'s whole-tree rotate/flip.

## Open follow-ups tied to this model

- **Engine integration**: feed move histories that omit skipped (illegal) moves
  while preserving legal-move order; setup nodes become engine board-setups.
- **Copy/paste of branches**: forced (redundant) specifiers are retained so a
  pasted node reproduces its intended edits on a different parent.
