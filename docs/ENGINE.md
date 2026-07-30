# Engine attachment (live analysis)

This document describes how the editor talks to a Go engine for **live analysis**
(there is no intent to *play* against the engine). The design goals were:

* An "engine" is just an **arbitrary shell command** that speaks GTP on
  stdin/stdout — a local `katago gtp ...` or a remote
  `ssh host 'cd ...; ./katago gtp ...'`. Any number of named commands are
  saved, and **several can be attached at once** (one shown at a time — see
  "Multiple attached engines").
* **Preserve move history.** KataGo biases its suggestions on the last several
  moves and enforces superko, so we drive the engine with GTP `play` commands
  wherever possible, falling back to `set_position` only when crossing an SGF
  "edit" node (setup stones, etc.).
* **Never block the GUI**, and behave well under latency: if the user scrubs
  through many positions while an older response is still in flight, the engine
  skips straight to the latest and never analyses the ones in between.
* **No races**, regardless of how slow the engine/link is.
* A **GTP console** to type arbitrary commands while analysis is paused and the
  engine holds its current position.

## Modules (`src/katsura/engine/`)

| File | Role |
|------|------|
| `coords.py` | GTP vertex ↔ `Point` (matches the board's on-screen coordinates). |
| `analysis.py` | Parse a `kata-analyze` line into `Analysis` (moves, rootInfo, ownership). |
| `position.py` | `build_request()` — turn the current node into an `AnalysisRequest` (runs on the GUI thread; reads the SGF tree). |
| `gtp.py` | `GtpEngine` — the subprocess + the single worker thread. |
| `config.py` | `EngineConfig` (name + command), persisted via QSettings. |
| `controller.py` | `AnalysisController` — glues the active tab's position to *one* engine and routes analysis to the board overlay. The window holds one controller per attached engine. |

The UI side: `ui/enginedialog.py` (manage saved engines), `ui/console.py`
(per-engine GTP console), board overlay in `ui/boardview.py`, the shown-engine
selector in `ui/enginecontrols.py` (`EngineSelectorButton`), and the Engine
menu + read-out + wiring in `ui/mainwindow.py`.

## Multiple attached engines

Any number of saved engines can be attached at once — the window keeps **one
`AnalysisController` per attached engine** (`MainWindow.engine_controllers`),
each owning its own `GtpEngine` process, per-position analysis cache, console
transcript, raw-NN state, and Live-Analysis enabled flag. Exactly one of them
is the **shown** (current) engine (`MainWindow._current_engine`, exposed as the
`engine_controller` property, `None` when nothing is attached).

**Invariant:** an engine whose analysis is *not* being shown has its process
held open and running, but is **never analysing live** and never touches the
board overlay or the Analysis Info panel. `AnalysisController.set_active(False)`
enforces it: it auto-pauses the engine (interrupting any running search), exits
a raw-NN view, resets the console-pause flag, and stops tracking positions
(`refresh_position` / `show_raw_nn` / `_on_analysis` are gated on `_active`).
`set_active(True)` re-applies that engine's cached view of the current position
and resumes — the fresh request is placed *before* the resume so the worker can
never pick up a stale pending position from before the deactivation pause.

Selection is **window-wide** and surfaced in two places, both tied to the same
state:

* the **Analysis Info header accessory** (`EngineSelectorButton`, per tab, fed
  by `MainWindow._update_engine_selectors`): with one engine attached it reads
  as the old plain grey name label; with more it becomes a small drop-down
  (`name ▾`) listing the attached engines, the shown one checked;
* the **GTP console's Engine drop-down** (see below).

Picking an engine anywhere calls `MainWindow.select_engine`: the old current is
deactivated (search halted), the new one activated (its per-engine cache
repaints the position instantly if it was analysed before), and the
Live-Analysis menu item / status bar / selectors / console all re-sync. Engine
controls — Space, Live Analysis, raw-NN (`Shift+1`–`Shift+8`), the console — always apply
to the shown engine. The per-tab analysis *settings* (komi/rules/WRN/PDA) are
unchanged and feed whichever engine is shown; caches never mix engines because
each controller has its own.

Menu behaviour (`ui/mainwindow.py`):

* **Attach Engine ▸** — one action per saved engine; an engine that is already
  attached is greyed out and labelled `name (attached)`. Attaching a new engine
  makes it the shown one by default; "attaching" an already-attached engine
  just re-selects it.
* **Detach Engine ▸** — a submenu with one action per *attached* engine,
  labelled with the name of the engine it would detach. Detaching the shown
  engine falls back to the most recently attached remaining engine (or fully
  clears the analysis UI when it was the last).
* **Start / Stop Live Analysis** — one action whose *label* says which way it
  will go (`MainWindow._update_live_analysis_action`, resynced from
  `_update_engine_status`). It is deliberately **not** checkable: a checkmark on
  the noun "Live Analysis" left it ambiguous whether the tick meant "running" or
  "will run". The state of record is the controller's `is_enabled()`; the action
  only reads and sets it (`MainWindow.set_live_analysis`).
* **Raw NN View ▸** — a submenu with one entry per symmetry, in `Shift+1`…
  `Shift+8` order (so the shortcut column reads 1…8), each with a tooltip. Same
  code path as the hotkeys; enabled only while an engine is attached.
* **Rules…** — opens the same `RulesDialog` as the Engine-toolbar button, so the
  ruleset is reachable from the menus too.
* **Clear Cached Analysis (GUI)** — forgets every per-position analysis the
  GUI has cached for the shown engine (`AnalysisController.clear_cache()`);
  the current position re-evaluates from scratch (overlay/panel drop their
  cached values, fresh live results repopulate them).
* **Clear Cached Analysis (Engine)** — halts any ongoing analysis of the
  shown engine (stops Live Analysis, exactly like pressing Space — press
  Space to resume) and sends KataGo's `clear_cache` (clears its search tree
  and NN cache) through the console command path, so the command and its
  response are recorded in that engine's console transcript. Named to match
  the GUI-side command; the "halts analysis" caveat lives in its tooltip.

## Position model: anchors, boundaries, and history

For a target node `T`, `build_request()` walks the path from the root and finds
the deepest **boundary** ancestor — the node from which the engine board is set
wholesale with `set_position`:

* the **root** is always a boundary (its board, possibly with handicap/setup
  stones, is the base);
* any node with **setup edits** (`AB`/`AW`/`AE`) is a boundary;
* a **multi-stone-suicide** move is a boundary (engines differ on suicide, so we
  reproduce that board directly rather than trust a `play`).

Everything strictly between the anchor and `T` is a **pure-move** suffix, emitted
as explicit `play COLOR VERTEX` commands. Two special cases:

* a **skipped illegal move** (occupied / single-stone suicide, per `docs/MODEL.md`)
  contributes **no** `play` — so the engine's history simply omits it, which is
  exactly what we want;
* **`PL`** (player-to-move overrides) never force a boundary: every `play`
  carries an explicit colour, and the side to analyse for is taken from the
  tab's **effective** side-to-move at `T` — the game's side-to-move, or the
  transient Ctrl+Click player flip when one is active
  (`EditorTab.effective_to_move()`). The flip changes no SGF, but because
  `color` is part of `position_key` the flipped side is analysed and cached as
  a distinct position — exactly as if a `PL` property had set the other side.
  All overlay/panel perspective conversions honour the same effective side
  (`BoardView._to_move()`), since the analysis is *for* that side.

The request therefore carries: board size, rules (`kata-set-rules`), komi, the
side-to-move colour, the anchor's stones, the move list, the search parameters
(`analysisWideRootNoise`, `playoutDoublingAdvantage`), and — only as a last
resort — the full target board.

## Engine analysis settings (per tab)

The komi/rules/search-params used for analysis are **engine settings**, distinct
from the SGF's own `KM`/`RU`. They live on each `EditorTab` as an
`AnalysisSettings` (`engine/settings.py`) and are edited from the **Engine
toolbar**, not the SGF:

* **Komi** — a spin box (`KomiSpinBox`, bumps by 0.5). Every path into the
  setting goes through `clamp_komi`, which **snaps to the nearest half-integer
  and clamps to `±400`** — KataGo requires an integer-or-half-integer komi, and
  accepts `±400` from **v1.17.0** (`±150` before that; sending a komi an older
  engine rejects costs only a failed `komi` command, which the worker logs).
  Non-finite/garbage input degrades to 0 rather than poisoning the request and
  the cache key. The toolbar box is *written back* when a typed value is
  snapped, so it never displays something other than what the engine gets. The
  spin box's width is derived from `KOMI_LIMIT` (its widest possible text), so
  widening the range widens the box automatically. The setting is *initialised*
  from the SGF komi on load (or 7.0 if unspecified or not a real number) and
  then edited independently; it is never unspecified. The SGF's own komi lives
  in the **SGF Info** pane (`KM`, may be blank) and does not drive analysis.
* **Rules** — a button opening `RulesDialog`, which configures the full KataGo
  ruleset (`KataRules`: scoring area/territory, ko simple/positional/situational,
  tax none/seki/all, multi-stone self-capture, button, white handicap bonus
  0/N−1/N, friendly-pass-ok) with one-click presets (Japanese, Chinese, Stone
  scoring, Tromp-Taylor). The preset buttons are checkable and live-synced: the
  one whose ruleset exactly equals the current fields is shown selected — on
  open and as fields change — matched per button (some non-button presets like
  korean / chinese-ogs duplicate a button preset). Serialised to a single-token
  JSON for `kata-set-rules`. The button shows the matching ruleset's name, or
  just **"Rules"** when the fields match none (it used to read "Custom…", which
  looked like an option you hadn't chosen rather than a way in).

  **Seeded from the SGF `RU`** by `settings.sgf_rules`, which is deliberately
  heuristic — `RU` is free text. The value is normalised (lower-cased, every
  non-alphanumeric run collapsed to one space) and matched in three passes,
  most specific first: the whole phrase against `_RU_ALIASES`; the same with
  spaces squeezed out (so `NewZealand` / `new_zealand` / `chinese-ogs` all land
  together); then an ordered probe list where multi-word needles match as
  substrings and single words only as whole *tokens* — which is what keeps the
  "ing" in "stone scoring" and the "tt" in "Ing SST" from being false hits.
  Handles case, abbreviations (`JP`, `CN`, `KR`, `NZ`, `TT`), native names
  (`Nihon Kiin`), and trailing junk (`Japanese;Komi:6.5`); bare `Area` /
  `Territory` are the last-resort probes, since the scoring style alone still
  pins down the most important bit. Anything unrecognised leaves the tab on
  `DEFAULT_RULES`. `PRESETS` also holds rulesets KataGo has no shorthand for,
  spelled out field by field — currently **Ing/Goe** (situational superko +
  area + no self-capture + no handicap bonus; Ing's disturbing-ko provisions
  have no exact KataGo equivalent). They need no separate table because nothing
  ever sends a *name* to the engine: rules go over the wire as the full JSON of
  `KataRules.to_gtp()`.
* **analysisWideRootNoise** — a spin box stepping through the discrete set
  `0, 0.01, 0.04, 0.10, 0.25, 0.50, 1.0` (default 0.04).
* **playoutDoublingAdvantage** — a spin box from **White's** perspective
  (default 0.0, range `±3`, steps snap to the next clean 0.5). The engine pins
  `playoutDoublingAdvantagePla WHITE` whenever it sets the value.

All four feed `AnalysisRequest.position_key`, so the per-position cache and the
"same position?" check never mix searches run under different settings — change
any of them and the position re-analyses. The engine applies rules/params via
value-tracked `kata-set-rules` / `kata-set-param` (only re-sent when changed).

**Focus discipline.** Clicking a spin box's up/down arrow steps the value but
hands keyboard focus straight back (the box never grabs it for typing), so
board-navigation hotkeys keep working; a click *in the number field* still
focuses it to type. And clicking inert chrome (toolbar gaps, the node-info
label, pane backgrounds) drops focus from any text field back to the board —
see `MainWindow.eventFilter` (`_release_focus_on_inert_click` /
`_is_inert_target`, keyed off the deepest widget under the cursor) and
`_FitSpinBox` (defers a focus release after arrow clicks).

## Sync algorithm (value-based, history-preserving)

The worker tracks what it has actually sent **by value**: `(board size, komi,
rules, search params, anchor stones, played moves)`. To drive the engine to a
new request:

1. If the **board size** differs, send `rectangular_boardsize` (this clears the
   board, so the anchor/moves are invalidated). Send `kata-set-rules` / `komi`
   if they changed.
2. If the request's **anchor stones equal** the engine's current anchor, sync
   **incrementally**: compute the common move prefix, `undo` the extra moves,
   then `play` the new ones. This is the common "scrub forward/back inside a
   line" case — cheap and history-preserving.
3. Otherwise **reset**: `set_position <anchor stones>` then `play` the moves.
4. If the engine **rejects a `play`** (e.g. a ko/suicide illegal under *its*
   rules), fall back to `set_position <full target board>` (no history) so the
   board still matches exactly.

Because the diff is purely by value (not node identity), it is robust across
tree edits and undo/redo, and never desyncs.

## Concurrency & latency

* **One worker thread** is the sole reader and writer of the subprocess, so GTP
  stays strictly serial; a second thread only drains stderr for logging.
* The GUI thread only ever: overwrites a single `_pending` request (**newest
  wins**), toggles `_paused`, or enqueues a console command — all under one lock,
  none blocking on engine I/O.
* A `kata-analyze` opens a streaming GTP response (`=` status line, then one
  `info ...` line per interval, terminated by a blank line). To move to a new
  position the worker **interrupts** the search by writing a single newline.
* **Coalescing:** while a search runs, new positions just overwrite `_pending`.
  When the search is interrupted the worker picks up only the latest, so rapid
  navigation analyses just the final position.
* **Race-free interruption:** the engine is marked "analyzing" only *after* the
  `kata-analyze` command is on the wire, and the worker re-checks for a newer
  request in that window and self-interrupts if needed. So an interrupt newline
  can never arrive ahead of the analyze command (which would otherwise leave a
  search running forever on a stale position).
* Each analysis is tagged with the request's `seq`; the controller drops results
  whose `seq` is not the current one.

## Analysis overlay & controls

- **Spacebar** starts / pauses / resumes live analysis (when an engine is
  attached). The running/paused state lives on the **status bar** (right side,
  a permanent widget so message flashes never clobber it): `● Analyzing — press
  Space to pause` (green) / `❚❚ Analysis paused — press Space to resume` /
  console / starting / error / off.
- **Per-position cache + no flicker:** `AnalysisController` caches each
  position's latest `Analysis` keyed by `AnalysisRequest.position_key` (bounded
  LRU). Navigating to a *visited* position restores its overlay instantly;
  navigating to an *un-analysed* position clears the overlay (never shows a
  wrong-position one). Resuming after a pause shows the cached overlay for the
  held position immediately, so there is no blank frame while the engine
  recomputes. Detaching clears the cache. (Visits can legitimately step backward
  when an older cached position is re-analysed from scratch — expected.)
- **Display toggles** (View menu + Preferences): `show_analysis_overlay` hides
  the board overlay entirely (discs + hover-PV) while analysis keeps running and
  the Analysis Info pane keeps updating; `show_pv_on_hover` keeps the hovered
  stone+stats but drops the PV continuation.
- The **Analysis Info pane** (per tab, a collapsible right-hand section) shows
  the **shown engine's name** as an accessory to the right of its header — the
  `EngineSelectorButton` described under "Multiple attached engines" (hidden
  when no engine is attached; engines are window-level/shared across tabs, so
  `MainWindow._update_engine_selectors` — called from `_update_engine_status`
  and `_add_tab` — fans the list + selection out to every tab's
  `EditorTab.set_engine_choices`). It also shows
  root stats from `rootInfo` (`Analysis.root_winrate` / `root_score_lead`, with a
  best-move fallback) from **Black's perspective**: a winrate label, a winrate
  bar that fills black from the left with Black's win%, a lead label, and a small
  grid of visits (`Analysis.total_visits`), score stdev, policy KL and
  no-result. The stats are NOT the top move's — they come from rootInfo. Each
  row's caption and value carry a tooltip explaining the stat *for a Go player*:
  no engine field names, and no explanation of sign conventions the `B+`/`W+`
  display already makes plain.
- Each candidate is a disc filled by `_scale_color(1-(1-sqrt(weight/top-weight))^2)`
  (a 7-stop red→green ramp in `boardview.py`; the 1-(1-sqrt(x))^2 mapping softens the ramp);
  the **order-0** move always gets the fixed cyan `_ORDER0_FILL`. Every disc has
  a thin border that is a slightly darkened version of its own fill.
- **Borders flag disagreement:** if the order-0 move is beaten on *either* axis
  — by ≥0.1% winrate (`_BETTER_WINRATE_MARGIN`) or ≥0.1 points
  (`_BETTER_SCORE_MARGIN`) — it gets a thin **red** border, and every move that
  beats it by that margin gets a thin **blue** border (`_BEATEN_BORDER`,
  `_BETTER_BORDER`). One margin per axis, each in its own units, and both
  deliberately sensitive: the flag is meant to catch *any* real disagreement
  between the search's choice and its own numbers. (A single shared `0.1` used
  to serve both, which meant 10% on the winrate axis.)
- **Which candidates are drawn** is decided by two weight thresholds, both a
  fraction of the top move's weight and both configurable in Preferences, and
  both bypassed by the order-0 move (always drawn in full):
  - below `Prefs.analysis_min_weight` (default **0.2%**) the move is left off
    the board **entirely** — `BoardView.drawable_candidates()`, the single seam
    the overlay and its tests share;
  - below `Prefs.analysis_min_label_weight` (default **1%**) it keeps its circle
    but loses its numbers (`BoardView._labels_candidate`), so the board still
    shows *where* the search looked without a wall of unreadable text.

  Clutter comes in two grades, which is why there are two: a move too weak to be
  worth reading is still worth seeing.
- Searched moves are labelled with winrate and score lead (1 dp) + visit count,
  in **black at the disc's own alpha** (so faint discs get faint labels).
  Winrate/lead are shown **from Black's perspective** regardless of side to move
  (flipped when White is to move); the red/blue border comparisons stay in the
  side-to-move's own terms.
- **Hovering** a candidate draws it as a full-opacity stone with its winrate/
  lead/visits (white text on a black stone, dark on white) instead of a "1", and
  its PV as the move-numbered continuation (2, 3, …) — shown even for a move
  below the weight threshold, which is not otherwise drawn.
- The status bar shows the hovered candidate's full stats, prefixed
  **`Stats for <move>`** so it is obvious what the numbers belong to: win, lead,
  **score** (`scoreSelfplay`: the predicted *final score*, formatted like a Go
  result — `B+3.0` / `W+1.5` — with its standard deviation appended as
  `(std 14.2)`; distinct from **lead**, which is how far ahead the side to move
  is relative to an even game, and diverges from the final score where the
  outcome distribution is lopsided), visits, weight,
  **edgeW** = `weight*edgeVisits/max(visits,1)`, **policy** (KataGo's
  `prior`, named after the thing users look for), and **no-result** whenever the
  engine reported one at all (rounded like every other field — there is no
  threshold below which it silently disappears). With no hover it shows the
  **pass** eval if reported, through the same `_move_stat_parts` formatter, so
  the two lines carry identical fields. `MoveInfo.score_selfplay` falls back to
  `score_lead` when an engine omits `scoreSelfplay`. The candidate colour scale
  keys off `edge_weight`, not plain weight.
- Live updates run at the **analysis reporting period** (the `kata-analyze`
  interval), default 25 cs and configurable in Preferences (shown there in ms);
  `AnalysisController.set_interval_cs` / `GtpEngine.set_interval` apply a change
  to the live engine on its next search. Paint uses
  antialiasing + text antialiasing + smooth-pixmap hints, and the app requests
  pass-through HiDPI scaling for crisper rendering.

## Board display modes (keyboard, when a non-text widget has focus)

Driven from `EditorTab.handle_key` → `MainWindow` → `BoardView` flags. The
overlay state lives on the board: `_analysis`, `_raw_nn`, `_policy_mode`,
`_show_ownership`.

- **`i`** — toggle *Show Move Analysis Info* (pref `show_analysis_overlay`):
  gates the per-move discs/stats/readout ONLY. Ownership is independent, so you
  can hide the moves to read the ownership map.
- **`p`** — policy-prior view: each searched move shows only its prior (2 dp
  under 1%), coloured by `prior / max-prior`.
- Hovering with the heatmap on appends **`ownership B <pct>%`** to the status
  bar: Black's predicted share of the hovered point, `(1 - white)/2`, from 0%
  (all White) to 100% (all Black) — Black-perspective like every other number in
  the readout. It is composed independently of the move stats, so it shows even
  with move-analysis info hidden ('i'), the heatmap being an independent overlay.
- **`o`** — ownership heatmap: fills each board cell under `_OWNERSHIP_SCALE`
  (`t = (white_ownership+1)/2`, a hue ramp red = Black → blue = White, chosen so
  the two sides differ in hue rather than brightness). Stones are drawn
  normally over it (an earlier version faded likely-dead stones; that was
  dropped as too noisy). Uses the search's `ownership` (perspective-converted to
  White) or, in raw mode, raw `whiteOwnership`. Works in every mode.

  **Anti-flicker (stale ownership).** Repainting ownership off-and-on for every
  one-stone move is visually loud, so during *live* analysis, when the position
  changes and ownership is shown, the controller records the ownership currently
  drawn as a transient **stale ownership** (white perspective) and the board
  keeps drawing it on the new position until that position's own result arrives.
  It is a pure display value — `BoardView._stale_ownership`, set/cleared only via
  `AnalysisController` — with no tie to the cache or the SGF. The White-perspective
  value is recorded **when the result is drawn** (`_apply_overlay` →
  `_last_ownership_white`), while the board's side to move still matches the
  analysis; `_capture_stale_ownership` then promotes that recorded value on the
  next live position change. (Re-deriving it from the live board *after* a move
  has flipped the side to move would invert the colours — that was a bug.) It is
  cleared the moment a real result is drawn, and whenever live analysis halts
  (pause / console / raw-NN / detach), so it never lingers stale.
- **`Shift+1`–`Shift+8`** — raw-NN view (the bare digits now select tools;
  `EditorTab._digit_of` accepts both the digit keycode and the shifted character
  platforms may report instead, e.g. `!` for Shift+1):
  `MainWindow.show_raw_nn_view` → `show_raw_nn` pauses
  the search and runs `kata-raw-nn COLOR SYMMETRY` (`1..7`→`1..7`, `8`→`0`, from
  `RAW_NN_KEY_ORDER` in `ui/modes.py` — one table shared by the key dispatch and
  the Engine ▸ Raw NN View menu) once via
  `GtpEngine.request_raw_nn` / `rawNn`. The board shows the raw policy on **every**
  legal point; the status bar shows the raw stats (Black-perspective winrate =
  `whiteLoss + 0.5*noResult`, no-result, lead = `-whiteLead`, **score** =
  selfplay score with `(std …)` appended, the stdev being
  `sqrt(E[s²]-E[s]²)` from `whiteScoreSelfplaySq`, varTimeLeft,
  shortterm win/loss + score errors, policyPass). It is **transient**: navigation,
  edits, `Esc`, **switching tabs**, or starting analysis exit it (`exit_raw_nn` /
  auto-exit at the top of `refresh_position`). Because the teardown can run
  *after* the current tab changed, the controller tracks which board holds the
  overlay (`_raw_board`, like `_shown_board`) and clears it off *that* board —
  otherwise the tab you left kept showing raw policy that `Esc` could no longer
  dismiss, since the mode itself had already exited. `COLOR` is the request's side to move (so a `PL` or the
  transient Ctrl+Click flip applies to raw evaluations too) — a KataGo v1.17+
  extension (`kata-raw-nn [b|w] SYMMETRY`); if the engine rejects it (pre-1.17),
  `GtpEngine._raw_nn_eval` falls back to the bare `kata-raw-nn SYMMETRY` for that
  engine's lifetime and warns once on its log.

Policy KL (`Analysis.policy_kl`) in the Analysis Info pane is
`KL(edge-weight distribution ‖ policy prior)` over searched moves.

## Failure handling (start error / crash / dropped ssh)

`GtpEngine` emits a `died(reason)` signal — with the process exit code — on any
*unexpected* termination (EOF/broken pipe/worker exception), distinct from an
explicit `stop()`. `AnalysisController` turns that into a full `detach()` (so the
engine is genuinely detached, not left half-attached) and re-emits `engineDied`.
The window drops that controller from the attached set — if it was the shown
engine, it falls back to another attached engine (or, when it was the last,
disables the engine actions and clears the analysis UI) — and **auto-pops the
GTP console** showing the dead engine's captured transcript plus the exit
reason, under a `name (stopped)` pseudo-entry in the console's engine selector
(it disappears once a live engine is picked). A synchronous start failure
(`Popen` raises) routes through the same path. The controller's `display_name`
survives the detach so the death is reported with the engine's name.

## GTP console (per engine)

The console displays the **shown** engine and has an **Engine drop-down** tied
to the same window-wide selection as the Analysis Info header selector —
switching it also switches the panel and board overlays. Each controller keeps
its own `console_transcript` (commands `> cmd`, responses `=`/`?`, stderr
`[engine]` lines, appended by the window via `_console_line`); switching the
selection repopulates the console output wholesale from the newly shown
engine's transcript, so nothing is lost across switches.

Opening the console **pauses** the shown engine's analysis
(`AnalysisController.begin_console()` → `GtpEngine.pause()`), which interrupts
the current search and leaves that engine holding its current board — and the
console pause *follows the selection*: switching engines while the console is
open console-pauses the newly shown engine and (like any deselection) halts the
old one. Typed commands go to the shown engine through its serialized worker
(`send_console`), and responses/stderr stream back into the console. Closing
the console resumes the shown engine's analysis and re-requests the current
position (deselected engines simply stay paused).

## Testing

`tests/test_engine.py` drives a mock GTP engine (`tests/_mockgtp.py`) that logs
every received command and streams a fixed analyze line shaped like the real
engine (opening `=`, body lines, blank terminator). It covers parsing,
`build_request` boundaries, full/incremental sync, the play-failure fallback,
coalescing, the console pause, and a window-level overlay integration. The mock
makes the subsystem fully testable headlessly with no KataGo binary.
