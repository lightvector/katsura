"""Glue between the active editor position and the GTP engine.

The controller owns at most one :class:`GtpEngine`. Whenever the current tab's
position changes it builds a fresh :class:`AnalysisRequest` and hands it to the
engine (which coalesces to the newest). Analyses that come back are matched
against the latest request's ``seq`` and, if current, pushed to the board
overlay and emitted for the win-rate read-out.

The window may hold **several** controllers at once — one per attached engine —
but only one of them is *active* (its analysis is shown). An inactive
controller's engine process is kept up but paused: it is never analysing live
and never touches the board overlay or the panel (see :meth:`set_active`).

All engine I/O is asynchronous; this object only ever does cheap, non-blocking
work on the GUI thread.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..go.board import BLACK
from .analysis import PanelStats, parse_raw_nn
from .config import EngineConfig
from .gtp import GtpEngine
from .position import build_request
from .settings import DEFAULT_INTERVAL_CS


class AnalysisController(QObject):
    """Drives live analysis of the window's current position."""

    analysisUpdated = Signal(object)    # PanelStats for the current position, or None
    stateChanged = Signal(str)          # engine state string
    engineDied = Signal(str)            # unexpected termination (reason w/ exit code)
    consoleResponse = Signal(str, bool, str)  # (command, ok, response text)

    def __init__(self, window, interval_cs: int = DEFAULT_INTERVAL_CS):
        super().__init__(window)
        self.window = window
        self.interval_cs = interval_cs
        self.engine: Optional[GtpEngine] = None
        self.config: Optional[EngineConfig] = None
        # The engine's name, kept across detach so a death can still be
        # reported with the name of the engine that died.
        self.display_name = ""
        # Per-engine GTP console history (formatted lines; the window appends).
        self.console_transcript: list[str] = []
        self.state = "stopped"
        self._active = True             # is this the engine whose analysis is shown?
        self._enabled = True
        self._console_paused = False
        self._seq = 0
        self._current_seq = -1
        self._current_key = None
        self._current_candidate = None
        self._last_key = None
        self._shown_board = None
        self._idle_paused = False       # we paused because no tab was open
        # Per-position analysis cache (keyed by AnalysisRequest.position_key), so
        # scrubbing back to a visited position shows its last analysis instantly
        # instead of a blank board while the engine recomputes. Bounded LRU-ish.
        self._cache: dict = {}
        self._cache_cap = 512
        # Raw-NN view (Shift+1…Shift+8): a transient, paused display of one
        # kata-raw-nn evaluation, exited by navigation / edits / esc / starting
        # analysis.
        self._raw_mode = False
        self._raw_symmetry = None
        # Which board currently carries the raw-NN overlay. Tracked like
        # _shown_board because the raw view can be torn down *after* the current
        # tab changed (switching tabs exits raw mode), and the overlay has to be
        # cleared off the board that actually has it — not off the new tab.
        self._raw_board = None
        # Transient "stale ownership" for flicker-free ownership display: the
        # ownership shown for the previous live position, drawn on the next one
        # until its own result arrives. Purely a display value — independent of
        # the cache and the SGF model. Cleared when a result is drawn or analysis
        # halts (pause / console / raw-NN / detach).
        self._stale_ownership = None
        # The last drawn result's ownership in White's perspective, recorded at
        # draw time (when the board's side to move still matches it) and promoted
        # to stale on the next position change — see _apply_overlay / _capture.
        self._last_ownership_white = None

    # -- attach / detach ---------------------------------------------------

    def attach(self, config: EngineConfig) -> None:
        """Start (or restart) the engine from a saved command."""
        self.detach()
        self.config = config
        self.display_name = config.name
        self.engine = GtpEngine(interval_cs=self.interval_cs)
        for sig, slot in self._engine_connections(self.engine):
            sig.connect(slot)
        try:
            self.engine.start(config.command)
        except Exception as e:  # noqa: BLE001
            self._on_died(f"could not start engine: {e}")

    def _engine_connections(self, eng: GtpEngine):
        return ((eng.stateChanged, self._on_state),
                (eng.analysis, self._on_analysis),
                (eng.logLine, self._on_log),
                (eng.died, self._on_died),
                (eng.rawNn, self._on_raw_nn),
                (eng.consoleResponse, self._on_console_response))

    def detach(self) -> None:
        """Stop the engine and clear the overlay."""
        eng = self.engine
        self.engine = None
        # Disconnect the old engine's signals so late (queued, cross-thread)
        # emissions can't reach us once a NEW engine is attached — e.g. an old
        # `died` arriving after attach() used to tear down the new engine. The
        # sender guards in the slots cover events already in flight.
        if eng is not None:
            for sig, slot in self._engine_connections(eng):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.config = None
        self._last_key = None
        self._current_key = None
        self._current_candidate = None
        self._current_seq = -1
        self._idle_paused = False
        if self._raw_mode:
            self._raw_mode = False
            self._raw_symmetry = None
            self._set_board_raw(None)
        self._cache.clear()             # detaching forgets all cached analyses
        if eng is not None:
            try:
                eng.stop()
            except Exception:
                pass
        self._hard_clear()
        self._set_state("stopped")

    def is_attached(self) -> bool:
        return self.engine is not None

    # -- selection (several engines may be attached; one is shown) ---------

    def set_active(self, on: bool) -> None:
        """Select/deselect this controller as the one whose analysis is shown.

        Invariant: an inactive controller's engine process is held open but is
        **never analysing live** — deactivating auto-halts any running search —
        and it never touches the board overlay or the Analysis Info panel.
        """
        if on == self._active:
            return
        self._active = on
        if not on:
            self._console_paused = False    # console pause follows the shown engine
            if self._raw_mode:
                self._raw_mode = False
                self._raw_symmetry = None
                self._set_board_raw(None)
            self._clear_stale_ownership()
            self._shown_board = None        # the successor repaints the overlay
            if self.engine is not None:
                self.engine.pause()
            return
        # Activated: re-apply this engine's view of the current position, then
        # let it analyse again. The request is placed *before* resuming so the
        # worker can never pick up a stale pending position from before the
        # deactivation pause.
        self._last_key = None
        self.refresh_position()
        if (self.engine is not None and self._enabled
                and not self._console_paused and not self._idle_paused):
            self.engine.resume()

    def is_active(self) -> bool:
        return self._active

    def set_interval_cs(self, interval_cs: int) -> None:
        """Change the analysis reporting period (kata-analyze interval).

        Applies to a live engine on its next search; we force a re-request so
        the change takes effect promptly rather than after the next navigation.
        """
        self.interval_cs = interval_cs
        if self.engine is not None:
            self.engine.set_interval(interval_cs)
            self._last_key = None
            self.refresh_position()

    # -- enable / console pause -------------------------------------------

    def set_enabled(self, on: bool) -> None:
        """Toggle live analysis without detaching the engine."""
        self._enabled = on
        if self.engine is None:
            return
        if on:
            self.engine.resume()
            self._last_key = None
            self.refresh_position()
        else:
            # Pause the search but leave the last analysis on the board.
            self._clear_stale_ownership()
            self.engine.pause()

    def is_enabled(self) -> bool:
        return self._enabled

    def is_console_paused(self) -> bool:
        return self._console_paused

    def is_running(self) -> bool:
        """True when the engine is actively analysing the current position."""
        return (self.engine is not None and self.state == "ready"
                and self._enabled and not self._console_paused)

    def begin_console(self) -> None:
        """Pause analysis so the console can issue commands at the held position.

        The last analysis is left on the board (the engine holds its position).
        """
        self._console_paused = True
        self._clear_stale_ownership()
        if self.engine is not None:
            self.engine.pause()

    def end_console(self) -> None:
        self._console_paused = False
        if self.engine is not None and self._enabled:
            self.engine.resume()
            self._last_key = None
            self.refresh_position()

    # -- position tracking -------------------------------------------------

    def refresh_position(self) -> None:
        """Re-evaluate the current position and (re)request analysis if it changed.

        On a position change the overlay switches to *this* position's cached
        analysis if we have one (so navigating back and forth is flicker-free),
        otherwise it clears — a stale overlay from a different position never
        lingers. Only the *new request* is gated on being enabled / not
        console-paused.
        """
        if not self._active:
            return                      # only the shown engine tracks positions
        if self.engine is None or self.state != "ready":
            return
        if self._raw_mode:
            # Any position/state change leaves the transient raw-NN view.
            self._raw_mode = False
            self._raw_symmetry = None
            self._set_board_raw(None)
            self._last_key = None
            if self._enabled and not self._console_paused:
                self.engine.resume()
        tab = self.window.current_tab()
        if tab is None:
            # No position to analyse: also stop the running search (it would
            # otherwise keep burning compute on the abandoned position) and
            # invalidate the seq so its late results are ignored.
            self._last_key = None
            self._current_key = None
            self._current_candidate = None
            self._current_seq = -1
            self._hard_clear()
            self.engine.pause()
            self._idle_paused = True
            return
        try:
            candidate = build_request(tab.game, 0, tab.analysis_settings,
                                      to_move=tab.effective_to_move())
        except Exception:
            # An unbuildable position must not leave the previous position's
            # overlay showing over it.
            self._last_key = None
            self._current_key = None
            self._current_seq = -1
            self._hard_clear()
            return
        key = candidate.position_key
        if key == self._last_key:
            return                      # same position; keep the overlay / search
        self._last_key = key
        self._current_key = key
        self._current_candidate = candidate
        # Before the overlay changes, remember the ownership currently shown so it
        # can bridge the gap on the new position until its own result arrives.
        self._capture_stale_ownership()
        # Show this position's cached analysis if any; else clear the board overlay
        # but let the panel fall back to the parent's eval of the move leading here.
        self._apply_overlay(self._cache.get(key), candidate)
        if not self._enabled or self._console_paused:
            return                      # paused: don't request a new analysis
        if self._idle_paused:
            self.engine.resume()        # undo the no-tab pause above
            self._idle_paused = False
        self._seq += 1
        self._current_seq = self._seq
        req = dataclasses.replace(candidate, seq=self._seq)
        self.engine.request(req)

    def clear_cache(self) -> None:
        """Forget every cached analysis for this engine.

        If this engine is shown, the current position re-evaluates from scratch:
        the overlay/panel drop their cached values (going blank while paused)
        and live analysis, if running, repopulates them with fresh results.
        """
        self._cache.clear()
        self._last_key = None
        if self._active:
            self.refresh_position()

    def _cache_put(self, key, analysis) -> None:
        if key is None:
            return
        self._cache.pop(key, None)          # refresh LRU order
        self._cache[key] = analysis
        while len(self._cache) > self._cache_cap:
            self._cache.pop(next(iter(self._cache)))

    # -- raw-NN view -------------------------------------------------------

    def show_raw_nn(self, symmetry: int) -> None:
        """Pause the search and request one ``kata-raw-nn`` evaluation to display."""
        if not self._active or self.engine is None or self.state != "ready":
            return
        self._raw_mode = True
        self._raw_symmetry = symmetry
        self._clear_stale_ownership()       # halting live analysis clears stale
        self.engine.pause()                 # hold position; suppress live overlay
        # Hand the worker the position this evaluation is FOR: pausing discards
        # any pending sync, so without it a raw-nn issued while a request was
        # still in flight would evaluate the engine's previous board.
        tab = self.window.current_tab()
        req = None
        if tab is not None:
            try:
                req = build_request(tab.game, 0, tab.analysis_settings,
                                    to_move=tab.effective_to_move())
            except Exception:
                req = None
        self.engine.request_raw_nn(symmetry, req)

    def exit_raw_nn(self) -> bool:
        """Leave the raw-NN view (returns whether it was active)."""
        if not self._raw_mode:
            return False
        self.refresh_position()             # tears down + resumes/re-requests
        return True

    def is_raw_mode(self) -> bool:
        return self._raw_mode

    def _on_raw_nn(self, symmetry: int, ok: bool, text: str) -> None:
        if self._from_stale_engine():
            return
        if not self._raw_mode or symmetry != self._raw_symmetry or not ok:
            return
        tab = self.window.current_tab()
        if tab is None:
            return
        try:
            raw = parse_raw_nn(text, tab.game.width, tab.game.height)
        except Exception:
            return
        self._set_board_raw(raw)

    def _set_board_raw(self, raw) -> None:
        """Show ``raw`` on the current tab's board, or clear (``None``) whichever
        board is holding the raw overlay."""
        board = self._board()
        prev = self._raw_board
        if prev is not None and prev is not board:
            prev.set_raw_nn(None)       # a tab we left keeps no stale raw overlay
            self._raw_board = None
        if raw is None:
            if self._raw_board is not None:
                self._raw_board.set_raw_nn(None)
                self._raw_board = None
            elif board is not None:
                board.set_raw_nn(None)
            return
        if board is not None:
            board.set_raw_nn(raw)
            self._raw_board = board

    # -- engine callbacks --------------------------------------------------

    def _from_stale_engine(self) -> bool:
        """True when the delivering signal came from a detached engine.

        Cross-thread emissions are queued; one can arrive after detach() (or
        after a new attach()), and must not affect the current engine's state.
        Direct calls (sender() is None) are never stale.
        """
        snd = self.sender()
        return snd is not None and snd is not self.engine

    def _on_state(self, state: str) -> None:
        if self._from_stale_engine():
            return
        self._set_state(state)
        if state == "ready":
            self._last_key = None
            self.refresh_position()
        elif state == "stopped" or state.startswith("error"):
            self._hard_clear()

    def _on_analysis(self, seq: int, analysis) -> None:
        if self._from_stale_engine():
            return
        if not self._active:
            return                      # queued result arriving after deselection
        if seq != self._current_seq:
            return                      # stale: belongs to a position we left
        self._cache_put(self._current_key, analysis)
        self._apply_overlay(analysis, self._current_candidate)

    def _on_log(self, line: str) -> None:
        # Deliberately unguarded: a dying engine's last stderr lines are
        # exactly what the user needs to see in the console.
        self.window.on_engine_log(self, line)

    def _on_console_response(self, command: str, ok: bool, text: str) -> None:
        if self._from_stale_engine():
            return
        self.consoleResponse.emit(command, ok, text)

    def _on_died(self, reason: str) -> None:
        """The engine terminated unexpectedly: tear down to a clean detached
        state and let the window surface the reason (and the captured log)."""
        if self._from_stale_engine():
            return                          # an OLD engine's death, not ours
        if self.engine is None:
            return                          # already torn down
        self.detach()
        self.engineDied.emit(reason)

    # -- overlay plumbing --------------------------------------------------

    def _apply_overlay(self, analysis, candidate) -> None:
        """Set the board overlay to ``analysis`` (may be None) and update the
        panel with this position's stats — or, when there is no analysis yet,
        the parent node's evaluation of the move leading here (continuity)."""
        self._set_board_overlay(analysis)
        board = self._board()
        if analysis is not None:
            # Remember this result's ownership in White's perspective NOW, while
            # the board's side-to-move still matches the analysis. (Capturing it
            # later, after a move has flipped the side to move, would invert the
            # colours.) A real result is drawn, so stale ownership isn't needed.
            if board is not None:
                own = board.current_ownership_white()
                self._last_ownership_white = list(own) if own is not None else None
            self._stale_ownership = None
            if board is not None:
                board.set_stale_ownership(None)
        elif board is not None:
            board.set_stale_ownership(self._stale_ownership)
        self.analysisUpdated.emit(self._panel_stats(analysis, candidate))

    def _board(self):
        tab = self.window.current_tab()
        return tab.board if tab is not None else None

    def _capture_stale_ownership(self) -> None:
        """Promote the last drawn result's ownership to 'stale', if live analysis
        is running and ownership is being shown. Uses the White-perspective value
        recorded when that result was drawn (NOT the live board, whose side to
        move may already have flipped), so the colours don't invert. No-op
        otherwise."""
        if not self._enabled or self._console_paused:
            return
        board = self._board()
        if board is None or not board.ownership_shown():
            return
        if self._last_ownership_white is not None:
            self._stale_ownership = list(self._last_ownership_white)

    def _clear_stale_ownership(self) -> None:
        self._stale_ownership = None
        self._last_ownership_white = None
        if not self._active:
            return          # an inactive controller must not touch the board
        board = self._board()
        if board is not None:
            board.set_stale_ownership(None)

    def _set_board_overlay(self, analysis) -> None:
        tab = self.window.current_tab()
        board = tab.board if tab is not None else None
        if self._shown_board is not None and self._shown_board is not board:
            self._shown_board.set_analysis(None)
        if board is not None:
            board.set_analysis(analysis)
        self._shown_board = board

    def _hard_clear(self) -> None:
        """Clear board overlay AND panel outright (detach / stop — no fallback)."""
        self._clear_stale_ownership()
        if self._shown_board is not None:
            self._shown_board.set_analysis(None)
            self._shown_board = None
        self.analysisUpdated.emit(None)

    def _panel_stats(self, analysis, candidate) -> Optional[PanelStats]:
        tab = self.window.current_tab()
        if tab is None:
            return None
        # The transient Ctrl+Click flip changes which side the analysis is FOR,
        # so perspective conversion must use the effective side, not the SGF's.
        black_to_move = tab.effective_to_move() == BLACK
        if analysis is not None and analysis.moves:
            wr = analysis.root_winrate
            lead = analysis.root_score_lead
            if wr is not None and not black_to_move:
                wr = 1.0 - wr
            if lead is not None and not black_to_move:
                lead = -lead
            return PanelStats(wr, lead, analysis.total_visits,
                              analysis.root_score_stdev, analysis.policy_kl(),
                              analysis.root_no_result)
        mi, col = self._parent_move_eval(candidate)
        if mi is not None:
            bwr = mi.winrate if col == "B" else 1.0 - mi.winrate
            blead = mi.score_lead if col == "B" else -mi.score_lead
            return PanelStats(bwr, blead, mi.visits, mi.score_stdev, None,
                              mi.no_result_value)
        return None

    def _parent_move_eval(self, candidate):
        """The parent position's MoveInfo for the move that leads to the current
        node, from the cache (or (None, None)). Reconstructs the parent's
        position_key by dropping the last play; the move colour is the parent's
        side to move."""
        if candidate is None or not candidate.moves:
            return None, None
        last_col, last_vtx = candidate.moves[-1]
        # Reconstruct the parent request (drop the last play; the move colour is
        # the parent's side to move) and reuse its position_key so this stays in
        # sync with whatever fields position_key includes.
        parent = dataclasses.replace(
            candidate, color=last_col, moves=candidate.moves[:-1])
        pa = self._cache.get(parent.position_key)
        if pa is None:
            return None, None
        for m in pa.moves:
            if m.move == last_vtx:
                return m, last_col
        return None, None

    def _set_state(self, state: str) -> None:
        self.state = state
        self.stateChanged.emit(state)
