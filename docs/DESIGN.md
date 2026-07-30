# Architecture

The codebase is layered so the non-GUI parts are independently testable.

```
src/katsura/
  __init__.py     Version + the QSettings identity (ORG/APP) every layer persists
                  under, so no subpackage has to import another's modules for it
  sgf/            Pure SGF: parsing, serialization, coordinates (no GUI, no Go rules)
    coords.py     Point, SGF<->point, point lists (compression), tt-pass compat
    tree.py       SgfNode, tolerant parser, faithful serializer, escaping
  go/             Go rules, board-only (no SGF, no GUI)
    board.py      Board (non-square), captures, liberties, suicide tolerance
  model/          Bridges sgf + go for editing
    replay.py     apply_node(): the one implementation of MODEL.md's
                  setup -> sweep -> move -> PL, shared by the editor's replay
                  and the engine's request builder
    game.py       Game: current node, replayed board, navigation, edits
    markup.py     Property-name constants (marks, labels, moves, setup)
  engine/         GTP engine integration for live analysis (see docs/ENGINE.md)
    coords.py     GTP vertex <-> Point + display labels (match the board's coords)
    settings.py   Per-tab analysis settings: komi, KataRules, search params,
                  SGF RU/KM interpretation
    analysis.py   Parse kata-analyze / kata-raw-nn output -> Analysis / RawNN
    position.py   build_request(): current node -> AnalysisRequest (anchors/moves)
    gtp.py        GtpEngine: subprocess + single worker thread, serial GTP +
                  streaming analysis + console; race-free, coalescing
    config.py     EngineConfig (name + shell command), persisted via QSettings
    controller.py AnalysisController: active position <-> engine <-> overlay
  ui/             PySide6 front end (depends on everything above)
    app.py        Entry point (main()); selects the Qt platform (xcb default on
                  WSL — see the README), sets HiDPI render hints
    mainwindow.py QMainWindow: menus, tabs, toolbars (tools/navigation/engine
                  settings), attached-engine set, app-level key filter, cross-tab
                  clipboards (selection buffer + subtree mark)
    editortab.py  Per-document controller: board+tree+comment+gameinfo wiring,
                  tools, navigation, undo/redo, selection/paint state machines
    boardview.py  Custom-painted board: stones, markup, halos, hover previews,
                  paint-drag (setup/marks), selection drag, paste ghost,
                  engine analysis overlay (candidate moves + winrate/score, PV)
    treeview.py   Graphical variation tree: unified compact/centered layout
                  (_layout), stable nav layout (_nav_pos), vertical centering
    gameinfo.py   Root-metadata grid (SGF komi, PB/PW/RE/RU/DT/...)
    analysispanel.py Analysis Info pane: winrate bar + root stats, Black-relative
    collapsible.py Stacked collapsible right-hand panes + drag grips
    enginecontrols.py Engine-toolbar spin boxes (komi/WRN/PDA) + engine selector
    enginedialog.py Manage saved engines (name + GTP command)
    console.py    GTP console window (raw commands while analysis is paused)
    document.py   Open file: collection of game trees + path + dirty + undo stack
    modes.py      EditMode (tools) + per-modifier help text
    selection.py  Pure rotate/flip transforms for the selection paste buffer
    dialogs.py    New Game + Preferences dialogs
    settings.py   QSettings-backed preferences (Prefs dataclass)
```

See `docs/MODEL.md` for the authoritative editing/position semantics (legality,
atomic setup, PL-after-move, redundancy, selection/subtree clipboards).

## Key design decisions

### Round-trip fidelity (sgf/tree.py)
- A node stores properties as an ordered `dict[str, list[str]]` of *decoded*
  values. Order of properties and of variations is preserved.
- Unknown properties are kept verbatim and re-emitted on save.
- Escaping is uniform on write (`\` and `]` only), which is always safe because
  point/number values never contain those characters. On read, backslash
  escapes are undone and an escaped newline is a soft (removed) line break.
- Serialization turns the parent/children tree back into `(...)`/tail form:
  single-child chains stay inline; a branch emits each child as its own `(...)`.

### Board and rules (go/board.py) — see docs/MODEL.md for the full spec
- `Board(width, height)` supports 1..52 in each dimension (SGF's a..Z range).
- One legality predicate, `play_classified()`, used identically for clicking and
  replay: occupied / single-stone suicide are illegal (board untouched);
  multi-stone suicide is legal (own group removed); **no ko/superko enforcement**.
  An illegal move is never half-applied.
- `remove_dead_groups()` coerces a setup position into a valid one.

### Model (model/game.py)
- The board at a node is recomputed by replaying from the root (cheap; avoids
  incremental-undo bookkeeping).
- Illegal moves in an SGF are **skipped** (not forced), flagged via
  `last_move_illegal`; turn still flips. (Engine-friendly — see MODEL.md.)
- Setup is atomic; setup edits record the **minimal unambiguous AB/AW/AE diff**
  (or forced/redundant under ctrl, for copy-paste fidelity).
- `PL` applies after the node's move; handicap (`AB`, no `PL`/move) ⇒ White.

### Variation tree at scale (ui/treeview.py)

A large tree — a joseki dictionary runs to tens of thousands of nodes — made the
panel sluggish, because `paintEvent` redrew every node and connector regardless
of what was on screen. Four properties keep it fast, and the first is the one to
preserve when touching the layout:

- **Culling is exact, not heuristic.** `_compute_subtree_bounds()` records each
  subtree's display extent once per relayout (iterative post-order, so no
  recursion limit). Every connector joins a node to its own child, and both lie
  inside that node's subtree, so a subtree's box bounds its connectors as well as
  its nodes — a box disjoint from the viewport can be pruned whole, with no
  safety margin. `_collect_visible` does that, taking paint from O(all nodes) to
  O(visible). Connectors are tested by their own two-cell box independently of
  child recursion, since one can straddle the viewport with both endpoints off
  screen.
- The **golden-line set** is cached and rebuilt only when the current node
  changes, so scroll repaints reuse it.
- **Relayout only when the golden line changes.** The centered layout is a pure
  function of that line, so moving along it is a scroll and repaint rather than
  an O(n) relayout (`golden_layout_stale()`).
- **Hit-testing is O(1)** via a `(col, row) -> node` index built during relayout;
  the hit radius fits inside one cell, so a click rounds to a cell and checks one
  node.

The first and last are guarded by tests that compare against brute-force scans
over every viewport position (`test_tree_culling_matches_bruteforce`,
`test_tree_node_hit_test_matches_bruteforce`) — pruning must never drop or add a
node, and the fast hit-test must agree everywhere.

### Engine integration (engine/) — see docs/ENGINE.md
- Live analysis only (no play). An engine is any shell command speaking GTP;
  several are saved (`engine/config.py`) and launched on demand.
- History-preserving sync: drive the engine with `play` commands, resetting with
  `set_position` only at SGF "edit" boundaries; skipped-illegal moves are omitted
  from history (matches `docs/MODEL.md`). One worker thread, value-based engine
  state, coalescing + race-free interruption for smooth scrubbing under latency.

## Open work

### Features
- PV mini-board; configurable visit/thread params per engine; analysis of *all*
  children at a node.
- Multiple game trees per file (collection) surfaced in the UI — `Document`
  already keeps them; only the first is shown and editable today.
- Persist window geometry, splitter/pane sizes and collapsible state; a Recent
  Files menu.
- Cut/Copy/Paste in the Edit menu — currently key-only and undiscoverable. While
  there, reconsider `Ctrl+P` for Pass (Print mnemonic, and confusable with `p`
  for the policy view).
- Engine capability detection: `list_commands` at handshake, so a non-KataGo GTP
  engine reports "doesn't support kata-analyze" instead of idling forever.
- Engine auto-restart after an unexpected death.
- GTP console: cap the scrollback (`setMaximumBlockCount`), and close the log gap
  where stderr is only appended while the console is visible.
- Custom board themes.

### Refactors
- **`Game` is a god object** (~1000 lines). The natural seams are a pure
  interpreter, pure `SgfNode` surgery helpers, and the editing/recording layer.
  Pays off when collections get surfaced.
- **`EditorTab` extractions**: a SelectionController, a SubtreeClipboard, and
  per-`EditMode` tool dispatch.
- **`AnalysisController` ↔ window coupling**: the controller reaches into
  `window.current_tab()` in several places. A narrow "position source + overlay
  sink" interface is what background or multi-engine analysis needs.
- **Engine parameter plumbing** costs four edits per parameter (request field,
  cache-key entry, tracked value, sync block). Collapse to a generic params tuple
  plus one "send what changed" loop before adding maxVisits/threads.
- **The single `_pending` slot** is right for live scrubbing (newest wins), but
  analysing all children needs a small priority queue with per-purpose coalescing.
- **`set_game(same_object)` as a refresh idiom** does two full relayouts per edit
  and resets hover state; split the "load a game" and "refresh" entry points.

### Performance (measured; none urgent)
Measured on a 300-move game, a setup paint stroke costs a few milliseconds per
cell and an undo snapshot well under one, so all of this is latent rather than
felt at ordinary sizes:
`BoardView.paintEvent` rebuilds every frame (a static board pixmap, per-radius
stone pixmaps and size-keyed fonts would cache well); a K-cell setup stroke does
~3·K·N full replays; `build_request` copies the board at every path node but
reads two; undo keeps 200 uncompressed serialize+reparse snapshots, so a 5 MB SGF
can pin ~1 GB; and `forbid_ko` replays the whole game per click when navigation
already computed the parent board.

### Known gaps
- **Coverage holes**: `ui/enginedialog.py` and `ui/console.py` are thin, and the
  board tests bypass the real mouse→point mapping, so a click-translation
  regression would pass everything. File open/save error paths are untested.
- The mock-engine tests are POSIX-only with nothing enforcing it — they want a
  `skipif`. CI sidesteps this by running Linux only, so a Windows contributor is
  the one who would hit it. Test windows are closed by hand-rolled calls that
  leak on assertion failure; a yield fixture would fix both.
- `pyproject.toml` duplicates the version already in `__init__.py` (use
  `dynamic`).
- SGF nits: `:` is not escaped in composed values (`LB` label text), and a stray
  `(` after a game tree fabricates a phantom empty tree, visible as an extra
  `(;)` on save.
- Modernization: `Optional[X]` → `X | None`, `typing.Iterable` →
  `collections.abc`, `MarkType(str, Enum)` → `StrEnum`, and the stringly-typed
  `transform_geometry(kind)` → `Literal`.
