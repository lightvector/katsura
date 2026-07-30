"""A GTP engine subprocess with a single worker thread.

:class:`GtpEngine` runs an arbitrary shell command (anything that speaks GTP on
stdin/stdout — a local ``katago gtp ...`` or an ``ssh host '...'`` command) and
drives live analysis of a position.

Concurrency model (see ``docs/ENGINE.md``):

* **One** worker thread is the sole reader and writer of the subprocess, so GTP
  stays strictly serial and there are no I/O races. A second small thread only
  drains stderr for logging.
* The GUI thread interacts through three tiny, lock-guarded operations:
  :meth:`request` (overwrite the single "desired position", newest wins),
  :meth:`pause`/:meth:`resume`, and :meth:`send_console`. None of them block on
  engine I/O.
* When a new :meth:`request` arrives mid-search, the GUI writes a single newline
  to interrupt the running ``kata-analyze``; the worker then picks up only the
  *latest* desired position, so scrubbing through many nodes analyses just the
  final one. Intermediate positions are never sent.

The engine's known board state is tracked **by value** (board size, komi, rules,
anchor stones, played moves), so syncing to a new target is incremental
``undo``/``play`` when possible and a ``set_position`` reset only when crossing a
boundary — never depending on node identity.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
from collections import deque
from typing import Optional

from PySide6.QtCore import QObject, Signal

from .analysis import parse_analysis_line
from .position import AnalysisRequest
from .settings import DEFAULT_INTERVAL_CS


def _fmt_param(value: float) -> str:
    """Format a float search-param value compactly (no trailing zeros)."""
    s = f"{value:.4f}".rstrip("0").rstrip(".")
    return s or "0"


class _EngineDead(Exception):
    """Internal: the subprocess closed its output (EOF)."""


class GtpEngine(QObject):
    """Manages one GTP engine subprocess and its live-analysis loop."""

    # state is one of: "starting", "ready", "stopped", "error: <msg>"
    stateChanged = Signal(str)
    analysis = Signal(int, object)          # (request seq, Analysis)
    logLine = Signal(str)                   # a line of engine stderr
    consoleResponse = Signal(str, bool, str)  # (command, ok, response-text)
    # Emitted (with a human-readable reason incl. exit code) when the process
    # terminates *unexpectedly* — never as a result of an explicit stop().
    died = Signal(str)
    rawNn = Signal(int, bool, str)          # (symmetry, ok, raw kata-raw-nn text)

    def __init__(self, interval_cs: int = DEFAULT_INTERVAL_CS,
                 ownership: bool = True, parent=None):
        super().__init__(parent)
        self._interval_cs = interval_cs
        self._ownership = ownership

        self._proc: Optional[subprocess.Popen] = None
        self._worker: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()       # guards the fields below + engine state
        self._write_lock = threading.Lock()  # guards writes to the subprocess stdin
        self._event = threading.Event()     # wakes the worker when work arrives
        self._stop_flag = False

        self._pending: Optional[AnalysisRequest] = None
        self._analyzing = False
        self._paused = False
        self._commands: deque[str] = deque()
        # Pending kata-raw-nn: (symmetry, position request to sync to first).
        self._raw_request: Optional[tuple] = None
        self._raw_nn_color_ok = True        # engine accepts kata-raw-nn COLOR SYM
        self._warned: set[str] = set()      # one-shot warnings (worker thread)

        # Engine board state (what we have actually sent), tracked by value.
        self._es_size: Optional[tuple] = None
        self._es_komi: Optional[str] = None
        self._es_rules: Optional[str] = None
        self._es_anchor: Optional[tuple] = None
        self._es_moves: list = []
        # Engine-global search params (persist across positions; tracked by value).
        self._es_wide_root_noise: Optional[float] = None
        self._es_pda: Optional[float] = None

    # -- lifecycle ---------------------------------------------------------

    @staticmethod
    def _shell_argv(command: str) -> list[str]:
        """Build the argv that runs ``command`` in a POSIX shell.

        Engine commands are written for a POSIX shell, and usually depend on the
        Linux side of the machine for ssh keys, environment and paths. A Windows
        build has no ``bash`` on PATH, so route the command through ``wsl.exe``:
        it runs inside WSL exactly as it would natively, with stdio piped across.
        Set ``KATSURA_ENGINE_SHELL`` to override the wrapper — a specific WSL
        distro, or ``cmd /c`` for a genuinely native Windows engine command.
        """
        override = os.environ.get("KATSURA_ENGINE_SHELL")
        if override:
            # shlex, not split(): the wrapper may contain a quoted path.
            return shlex.split(override) + [command]
        if sys.platform == "win32":
            return ["wsl.exe", "bash", "-lc", command]
        return ["bash", "-lc", command]

    def start(self, command: str) -> None:
        """Launch ``command`` in a POSIX shell and begin the worker."""
        if self._proc is not None:
            raise RuntimeError("engine already started")
        self._set_state("starting")
        self._proc = subprocess.Popen(
            self._shell_argv(command),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Explicit UTF-8: with text=True alone, a native-Windows GUI would
            # use the locale codec (cp1252) and a UTF-8 byte in an ssh banner
            # would raise UnicodeDecodeError, misreported as engine death.
            encoding="utf-8", errors="replace", bufsize=1,
            # Own process group (POSIX), so stopping can signal the whole tree:
            # the command runs under bash -lc, making the engine a *grandchild*
            # that terminate() on bash alone would miss.
            start_new_session=(sys.platform != "win32"),
            # No console window for the wsl.exe/ssh.exe child: when the GUI is
            # a windowed exe (no console of its own), Windows would otherwise
            # pop a blank console for the console-subsystem child — and closing
            # it kills the engine connection. Stdio is fully piped, and Windows
            # shutdown uses terminate()/kill() (not console Ctrl events), so a
            # hidden console changes nothing else.
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        """Stop analysis and terminate the subprocess."""
        self._stop_flag = True
        with self._lock:
            interrupt = self._analyzing
            self._analyzing = False
        if interrupt:
            self._interrupt()
        self._event.set()
        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:
                pass
            if self._worker is not None:
                self._worker.join(timeout=2.0)
            try:
                self._signal_tree(proc, hard=False)
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._signal_tree(proc, hard=True)
                except Exception:
                    pass
                # Always reap the killed child; without this final wait it
                # would linger as a zombie until interpreter exit.
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
        self._proc = None

    @staticmethod
    def _signal_tree(proc: subprocess.Popen, hard: bool) -> None:
        """Terminate (or kill) the engine and its whole process tree.

        On POSIX the child leads its own session (start_new_session), so
        signalling the process group reaches the actual engine, not just the
        ``bash -lc`` wrapper it is a grandchild of.
        """
        if sys.platform != "win32":
            try:
                os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
                return
            except Exception:
                pass                      # group gone or not a leader: fall back
        if hard:
            proc.kill()
        else:
            proc.terminate()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- public API (GUI thread) ------------------------------------------

    def request(self, req: AnalysisRequest) -> None:
        """Set the desired position to analyse (newest wins; interrupts search)."""
        with self._lock:
            self._pending = req
            interrupt = self._analyzing and not self._paused
            if interrupt:
                self._analyzing = False
        if interrupt:
            self._interrupt()
        self._event.set()

    def pause(self) -> None:
        """Pause live analysis, holding the engine at its current board state."""
        with self._lock:
            self._paused = True
            interrupt = self._analyzing
            self._analyzing = False
        if interrupt:
            self._interrupt()
        self._event.set()

    def resume(self) -> None:
        """Resume live analysis (the controller re-requests the current position)."""
        with self._lock:
            self._paused = False
        self._event.set()

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_interval(self, interval_cs: int) -> None:
        """Set the kata-analyze reporting interval (centiseconds).

        A plain int assignment, read by the worker when it next builds an
        analyze command; the controller re-requests so it takes effect promptly.
        """
        self._interval_cs = interval_cs

    def send_console(self, command: str) -> None:
        """Queue a raw GTP command (used by the console; analysis should be paused)."""
        with self._lock:
            self._commands.append(command)
        self._event.set()

    def request_raw_nn(self, symmetry: int,
                       req: Optional[AnalysisRequest] = None) -> None:
        """Queue a one-shot ``kata-raw-nn`` (newest wins; interrupts any search).

        ``req`` is the position the evaluation is *for*: the worker syncs to it
        first. Without it, a raw-nn issued while a position request was still
        pending (e.g. over a slow ssh link) would evaluate the engine's stale
        board — the pending request is discarded when the search is paused.
        """
        with self._lock:
            self._raw_request = (symmetry, req)
            interrupt = self._analyzing and not self._paused
            if interrupt:
                self._analyzing = False
        if interrupt:
            self._interrupt()
        self._event.set()

    # -- worker thread -----------------------------------------------------

    def _run(self) -> None:
        try:
            self._send("name")            # handshake: confirms the engine speaks GTP
            self._set_state("ready")
            while not self._stop_flag:
                cmd = self._pop_command()
                if cmd is not None:
                    ok, resp = self._send(cmd)
                    # An arbitrary console command may have changed anything —
                    # board (clear_board/play/undo/boardsize/loadsgf), komi,
                    # rules, search params. Diffing the next sync against the
                    # old values would silently skip re-sending state the
                    # engine no longer has (e.g. analyzing an empty board
                    # after a console clear_board while the GUI shows the
                    # game), so forget everything and re-send from scratch.
                    self._invalidate_engine_state()
                    self.consoleResponse.emit(cmd, ok, resp)
                    continue
                raw = self._pop_raw_request()
                if raw is not None:
                    raw_sym, raw_req = raw
                    if raw_req is not None:
                        self._sync_to(raw_req)     # evaluate the *right* board
                    ok, text = self._raw_nn_eval(raw_sym, raw_req)
                    self.rawNn.emit(raw_sym, ok, text)
                    continue
                with self._lock:
                    req = self._pending
                    self._pending = None
                    paused = self._paused
                    if req is None or paused:
                        self._analyzing = False
                if req is None or paused:
                    self._event.wait(0.5)
                    self._event.clear()
                    continue
                self._sync_to(req)
                with self._lock:
                    if self._pending is not None or self._paused or self._commands:
                        continue
                # Write the analyze command BEFORE marking _analyzing, so that a
                # concurrent interrupt newline can never reach the engine ahead of
                # the analyze command (which would leave the search un-interruptible).
                self._write(self._analyze_cmd(req))
                with self._lock:
                    self._analyzing = True
                    raced = (self._pending is not None or self._paused
                             or bool(self._commands))
                if raced:
                    # A request/pause/command arrived during the write window; the
                    # controller may not have interrupted, so do it ourselves now
                    # (guaranteed to land after the analyze command on the wire).
                    self._write_raw("\n")
                try:
                    self._stream(req)
                finally:
                    with self._lock:
                        self._analyzing = False
        except _EngineDead:
            if not self._stop_flag:
                self._emit_death("the engine process exited")
        except Exception as e:  # noqa: BLE001 - surface any worker failure
            if not self._stop_flag:
                self._emit_death(f"engine error: {e}")

    def _emit_death(self, reason: str) -> None:
        """Report an unexpected termination, enriched with the exit code."""
        proc = self._proc
        code = None
        if proc is not None:
            try:
                code = proc.wait(timeout=1.0)
            except Exception:
                try:
                    code = proc.poll()
                except Exception:
                    code = None
        if code is not None:
            reason = f"{reason} (exit code {code})"
        self._set_state("stopped")
        self.died.emit(reason)

    def _pop_command(self) -> Optional[str]:
        with self._lock:
            if self._commands:
                return self._commands.popleft()
            return None

    def _pop_raw_request(self) -> Optional[tuple]:
        with self._lock:
            raw = self._raw_request
            self._raw_request = None
            return raw

    def _warn_once(self, msg: str) -> None:
        """Surface a one-shot warning on the log (worker thread only)."""
        if msg not in self._warned:
            self._warned.add(msg)
            self.logLine.emit(f"[Katsura] {msg}")

    def _raw_nn_eval(self, symmetry: int,
                     req: Optional[AnalysisRequest]) -> tuple[bool, str]:
        """Run ``kata-raw-nn``, naming the side to evaluate for when known.

        KataGo v1.17+ accepts ``kata-raw-nn COLOR SYMMETRY``; the explicit
        colour makes the evaluation follow the request's side to move (a PL
        property, or the GUI's transient Ctrl+Click flip) rather than whatever
        the engine infers from its own move history. An older engine rejects
        the colour form, so on failure fall back to the bare command — for that
        engine's lifetime — and warn once.
        """
        if req is not None and self._raw_nn_color_ok:
            ok, text = self._send(f"kata-raw-nn {req.color.lower()} {symmetry}")
            if ok:
                return ok, text
            self._raw_nn_color_ok = False
            self._warn_once(
                "engine rejected 'kata-raw-nn COLOR SYMMETRY' (KataGo v1.17+); "
                "falling back to plain kata-raw-nn, which evaluates for the "
                "engine's own idea of the side to move")
        return self._send(f"kata-raw-nn {symmetry}")

    def _analyze_cmd(self, req: AnalysisRequest) -> str:
        parts = ["kata-analyze", req.color, str(self._interval_cs),
                 "rootInfo", "true", "noResultValue", "true"]
        if self._ownership:
            parts += ["ownership", "true"]
        return " ".join(parts)

    # -- position sync (worker thread) ------------------------------------

    def _invalidate_engine_state(self) -> None:
        """Forget every value-tracked belief about the engine's state."""
        self._es_size = None
        self._es_komi = None
        self._es_rules = None
        self._es_anchor = None
        self._es_moves = []
        self._es_wide_root_noise = None
        self._es_pda = None

    def _sync_to(self, req: AnalysisRequest) -> None:
        size = (req.width, req.height)
        if self._es_size != size:
            ok, _ = self._send(f"rectangular_boardsize {req.width} {req.height}")
            if not ok and req.width == req.height:
                # Plain GTP engines lack the KataGo extension; square boards
                # can still use the standard command.
                ok, _ = self._send(f"boardsize {req.width}")
            if ok:
                self._es_size = size
            else:
                self._warn_once(
                    f"engine rejected board size {req.width}x{req.height}; "
                    "analysis may be for the wrong board")
            self._es_anchor = None
            self._es_moves = []
        if req.rules and self._es_rules != req.rules:
            ok, _ = self._send(f"kata-set-rules {req.rules}")
            if ok:
                self._es_rules = req.rules
        if req.komi is not None and self._es_komi != req.komi:
            ok, _ = self._send(f"komi {req.komi}")
            if ok:
                self._es_komi = req.komi
        # Search params are board-independent, so set them here only when they
        # change. playoutDoublingAdvantage is specified from White's perspective,
        # so pin its reference player to White whenever we (re)set it.
        if self._es_pda != req.playout_doubling_advantage:
            self._send("kata-set-param playoutDoublingAdvantagePla WHITE")
            ok, _ = self._send(
                f"kata-set-param playoutDoublingAdvantage "
                f"{_fmt_param(req.playout_doubling_advantage)}")
            if ok:
                self._es_pda = req.playout_doubling_advantage
        if self._es_wide_root_noise != req.wide_root_noise:
            ok, _ = self._send(
                f"kata-set-param analysisWideRootNoise "
                f"{_fmt_param(req.wide_root_noise)}")
            if ok:
                self._es_wide_root_noise = req.wide_root_noise

        if self._es_anchor == req.anchor_stones:
            self._sync_incremental(req)
        else:
            self._sync_reset(req)

    def _sync_incremental(self, req: AnalysisRequest) -> None:
        em, rm = self._es_moves, req.moves
        common = 0
        while common < len(em) and common < len(rm) and em[common] == rm[common]:
            common += 1
        for _ in range(len(em) - common):
            ok, _ = self._send("undo")
            if not ok:
                # The engine's history no longer matches our belief; anything
                # built on it would be silently wrong forever.
                self._full_set(req)
                return
        moves = list(em[:common])
        for k in range(common, len(rm)):
            color, vertex = rm[k]
            ok, _ = self._send(f"play {color} {vertex}")
            if not ok:
                self._full_set(req)
                return
            moves.append((color, vertex))
        self._es_moves = moves

    def _sync_reset(self, req: AnalysisRequest) -> None:
        if not self._set_position(req.anchor_stones):
            self._es_anchor = None
            self._es_moves = []
            return
        self._es_anchor = req.anchor_stones
        self._es_moves = []
        moves = []
        for color, vertex in req.moves:
            ok, _ = self._send(f"play {color} {vertex}")
            if not ok:
                self._full_set(req)
                return
            moves.append((color, vertex))
        self._es_moves = moves

    def _full_set(self, req: AnalysisRequest) -> None:
        """Last resort: set the entire target board (no history) when a play is rejected."""
        self._set_position(req.target_stones)
        self._es_anchor = None        # history is now unknown; force a reset next time
        self._es_moves = []

    def _set_position(self, stones: tuple) -> bool:
        cmd = "set_position"
        for color, vertex in stones:
            cmd += f" {color} {vertex}"
        ok, _ = self._send(cmd)
        if not ok:
            # Leave the state trackers unset (callers do) so we keep retrying;
            # surface the problem once instead of analysing a wrong board
            # silently. (set_position is a KataGo extension.)
            self._warn_once("engine rejected set_position; it may not support "
                            "KataGo's GTP extensions — analysis may be for the "
                            "wrong position")
        return ok

    # -- low-level GTP I/O (worker thread) --------------------------------

    def _send(self, line: str) -> tuple[bool, str]:
        self._write(line)
        return self._read_response()

    def _write(self, line: str) -> None:
        self._write_raw(line + "\n")

    def _write_raw(self, data: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                raise _EngineDead()

    def _interrupt(self) -> None:
        """Write the search-interrupting newline; safe on the GUI thread.

        A dead pipe is not an error *here*: request/pause/stop run on the GUI
        thread, where _EngineDead must never propagate out of a Qt slot — the
        worker's reader hits EOF on its own and emits ``died``.
        """
        try:
            self._write_raw("\n")
        except _EngineDead:
            pass

    def _read_response(self) -> tuple[bool, str]:
        status: Optional[str] = None
        body: list[str] = []
        while True:
            raw = self._proc.stdout.readline()
            if raw == "":
                raise _EngineDead()
            line = raw.rstrip("\n")
            if status is None:
                s = line.strip()
                if s == "":
                    continue
                if s[0] in "=?":
                    status = s
                else:
                    self.logLine.emit(line)   # stray stdout chatter -> log
                continue
            if line.strip() == "":
                break
            body.append(line)
        ok = status[0] == "="
        text = status[1:].strip()
        if body:
            text = "\n".join(([text] if text else []) + body)
        return ok, text

    def _stream(self, req: AnalysisRequest) -> None:
        """Read live ``kata-analyze`` output until it is interrupted.

        An analyze command opens a *streaming* GTP response: a ``=`` (or ``?``
        on error) status line, then one ``info ...`` line per reporting interval
        as the response body, terminated by a blank line when we interrupt it
        with a newline. So the opening status line is skipped (not treated as the
        end) and the blank line is the real terminator.
        """
        started = False
        while True:
            raw = self._proc.stdout.readline()
            if raw == "":
                raise _EngineDead()
            line = raw.strip()
            if line == "":
                if started:
                    return                     # terminating blank line
                continue
            started = True
            if line[0] in "=?":
                continue                       # opening status line of the response
            if line.startswith("info") or line.startswith("rootInfo") \
                    or line.startswith("ownership"):
                try:
                    result = parse_analysis_line(line, req.width, req.height)
                except Exception:
                    continue
                self.analysis.emit(req.seq, result)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.rstrip("\n")
                if line:
                    self.logLine.emit(line)
        except Exception:
            pass

    def _set_state(self, state: str) -> None:
        self.stateChanged.emit(state)
