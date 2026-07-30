"""Tests for the engine subsystem: analysis parsing, position building, and the
GtpEngine sync/streaming loop driven against a mock GTP engine."""

import os
import sys
import time

import pytest

from katsura.engine.analysis import parse_analysis_line
from katsura.engine.coords import point_to_vertex, vertex_to_point
from katsura.engine.position import AnalysisRequest, build_request
from katsura.model.game import Game
from katsura.sgf.coords import Point

MOCK = os.path.join(os.path.dirname(__file__), "_mockgtp.py")

# A real captured KataGo kata-analyze line (truncated pv) for the parser test.
REAL_LINE = (
    "info move D17 visits 1422 edgeVisits 1423 utility -0.0472713 winrate 0.474997 "
    "scoreMean -0.374367 scoreStdev 23.79 scoreLead -0.374367 scoreSelfplay -0.728877 "
    "prior 0.299843 lcb 0.473165 utilityLcb -0.0524001 weight 1421.24 order 0 "
    "pv D17 C15 F16 C3 C4 "
    "info move D16 visits 1301 winrate 0.471202 scoreLead -0.451653 prior 0.394853 "
    "order 1 pv D16 C3 D3 "
    "rootInfo visits 2800 winrate 0.474 scoreLead -0.40 "
    "ownership " + " ".join(["0.1"] * 361)
)


# -- coords ---------------------------------------------------------------

def test_vertex_roundtrip():
    # Top-left on a 19x19 board is A19; bottom-right is T1.
    assert point_to_vertex(Point(0, 0), 19) == "A19"
    assert point_to_vertex(Point(18, 18), 19) == "T1"
    # 'I' is skipped, so column index 8 is 'J'.
    assert point_to_vertex(Point(8, 3), 19) == "J16"
    assert vertex_to_point("A19", 19) == Point(0, 0)
    assert vertex_to_point("T1", 19) == Point(18, 18)
    assert vertex_to_point("J16", 19) == Point(8, 3)
    assert point_to_vertex(None, 19) == "pass"
    assert vertex_to_point("pass", 19) is None
    # A row off the board is rejected, not folded into a negative y.
    for bad in ("A99", "A0", "T-1", "ZZ"):
        with pytest.raises(ValueError):
            vertex_to_point(bad, 19)


# -- analysis parsing ------------------------------------------------------

def test_parse_analysis_line():
    a = parse_analysis_line(REAL_LINE, 19, 19)
    assert len(a.moves) == 2
    best = a.moves[0]
    assert best.move == "D17"
    assert best.order == 0
    assert best.visits == 1422
    assert abs(best.winrate - 0.474997) < 1e-6
    assert abs(best.score_lead - (-0.374367)) < 1e-6
    assert best.point == vertex_to_point("D17", 19)
    assert best.pv[:3] == ["D17", "C15", "F16"]
    assert a.moves[1].move == "D16"
    assert a.root["visits"] == 2800
    assert a.ownership is not None and len(a.ownership) == 361
    assert a.total_visits == 2800


def test_move_info_edge_weight_and_extra_fields():
    line = ("info move D4 visits 100 edgeVisits 120 winrate 0.55 scoreLead 1.5 "
            "scoreStdev 20.0 prior 0.3 weight 90 noResultValue 0.002 order 0 pv D4 "
            "rootInfo visits 100 winrate 0.55 scoreLead 1.5 scoreStdev 22.0")
    a = parse_analysis_line(line, 19, 19)
    m = a.moves[0]
    assert m.edge_visits == 120
    assert abs(m.edge_weight - 90 * 120 / 100) < 1e-9      # weight*edgeVisits/visits
    assert m.score_stdev == 20.0
    assert m.no_result_value == 0.002
    assert a.root_score_stdev == 22.0
    assert a.root_no_result == 0.002


def test_policy_kl_matches_definition():
    import math

    from katsura.engine.analysis import Analysis, MoveInfo

    def mv(weight, prior):
        # edge_visits == visits so edge_weight == weight
        return MoveInfo(move="x", point=None, visits=100, winrate=0.5,
                        score_lead=0.0, prior=prior, weight=weight, order=0,
                        edge_visits=100)

    # Equal search distribution vs policy -> KL 0.
    a = Analysis(moves=[mv(50, 0.5), mv(50, 0.5)])
    assert abs(a.policy_kl()) < 1e-12
    # Divergent: W=[0.9,0.1], P=[0.5,0.5].
    a = Analysis(moves=[mv(90, 0.5), mv(10, 0.5)])
    expect = 0.9 * math.log(0.9 / 0.5) + 0.1 * math.log(0.1 / 0.5)
    assert abs(a.policy_kl() - expect) < 1e-9
    assert a.policy_kl() > 0


def test_parse_raw_nn():
    from katsura.engine.analysis import parse_raw_nn
    text = ("symmetry 3 whiteWin 0.4 whiteLoss 0.55 noResult 0.05 whiteLead -2.0 "
            "whiteScoreSelfplay -1.0 whiteScoreSelfplaySq 50.0 varTimeLeft 30.0 "
            "shorttermWinlossError 0.1 shorttermScoreError 2.0 policyPass 0.01 "
            "policy " + " ".join(["0.001"] * 361) + " "
            "whiteOwnership " + " ".join(["0.2"] * 361))
    r = parse_raw_nn(text, 19, 19)
    assert r.symmetry == 3
    assert abs(r.black_winrate - (0.55 + 0.5 * 0.05)) < 1e-9
    assert abs(r.black_lead - 2.0) < 1e-9
    assert abs(r.score_stdev - 7.0) < 1e-9            # sqrt(50 - (-1)^2)
    assert len(r.policy) == 361 and r.policy_pass == 0.01
    assert r.ownership is not None and len(r.ownership) == 361


# -- position building -----------------------------------------------------

def _play(game, color, pt):
    game.play(pt, color)


def test_build_request_simple_moves():
    from katsura.go.board import BLACK, WHITE
    g = Game.new(19)
    g.play(Point(3, 15), BLACK)     # D4 (x=3, y=15 -> row 4)
    g.play(Point(15, 3), WHITE)     # Q16
    req = build_request(g, 1)
    assert req.anchor_stones == ()                 # empty-board root anchor
    assert req.moves == (("B", "D4"), ("W", "Q16"))
    assert req.color == "B"                         # black to move after two moves
    assert req.width == 19 and req.height == 19


def test_build_request_setup_is_boundary():
    from katsura.go.board import BLACK, WHITE
    g = Game.new(19)
    g.set_setup_point(Point(3, 3), BLACK)           # a setup edit -> boundary node
    g.play(Point(15, 15), WHITE)
    req = build_request(g, 1)
    # The anchor is the setup node, so its stone is set wholesale, not played.
    assert ("B", point_to_vertex(Point(3, 3), 19)) in req.anchor_stones
    assert req.moves == (("W", point_to_vertex(Point(15, 15), 19)),)


def test_build_request_tolerates_garbage_coordinates():
    from katsura.sgf.tree import parse_collection
    # Off-board setup (jj on 9x9), malformed setup value, malformed move value:
    # build_request must produce the same position the GUI model shows.
    g = Game(parse_collection("(;GM[1]SZ[9]AB[jj][!!][cc];B[x9];W[ee])")[0])
    g.go_to_end()
    req = build_request(g, 1)
    assert req.anchor_stones == (("B", point_to_vertex(Point(2, 2), 9)),)
    # The malformed B replays as a pass; the W move is a real play.
    assert req.moves == (("B", "pass"), ("W", point_to_vertex(Point(4, 4), 9)))


def test_build_request_incremental_keys_differ_by_position():
    from katsura.go.board import BLACK
    g = Game.new(19)
    n1 = g.play(Point(3, 15), BLACK)
    k_after_one = build_request(g, 0).position_key
    g.back()
    k_root = build_request(g, 0).position_key
    assert k_after_one != k_root
    g.goto(n1)
    assert build_request(g, 0).position_key == k_after_one


def test_settings_step_helpers():
    from katsura.engine.settings import clamp_komi, step_pda, step_wide_root_noise
    # Wide-root-noise steps through the discrete list, clamped at the ends.
    assert step_wide_root_noise(0.04, up=True) == 0.10
    assert step_wide_root_noise(0.04, up=False) == 0.01
    assert step_wide_root_noise(1.0, up=True) == 1.0
    assert step_wide_root_noise(0.0, up=False) == 0.0
    # PDA snaps to the adjacent clean 0.5 multiple (not merely +/-0.5).
    assert step_pda(0.0, up=True) == 0.5
    assert step_pda(0.3, up=True) == 0.5
    assert step_pda(0.3, up=False) == 0.0
    assert step_pda(0.5, up=False) == 0.0
    assert step_pda(3.0, up=True) == 3.0
    # Komi clamps to KataGo's v1.17+ range, snaps to a half-integer, and
    # degrades non-numbers to 0 rather than passing them through.
    assert clamp_komi(999) == 400.0 and clamp_komi(-999) == -400.0
    assert clamp_komi(6.5) == 6.5 and clamp_komi(7) == 7.0
    assert clamp_komi(6.3) == 6.5 and clamp_komi(6.1) == 6.0
    assert clamp_komi(float("nan")) == 0.0
    assert clamp_komi(float("inf")) == 0.0
    assert clamp_komi("junk") == 0.0
    assert str(clamp_komi(-0.2)) == "0.0"        # never a signed zero


def test_sgf_rules_recognition():
    from katsura.engine.settings import PRESETS, preset_name, sgf_rules, sgf_rules_name

    cases = {
        # Plain names, any case.
        "Japanese": "japanese", "japanese": "japanese", "JAPANESE": "japanese",
        "Chinese": "chinese", "Korean": "korean", "AGA": "aga", "aga": "aga",
        # Abbreviations.
        "JP": "japanese", "CN": "chinese", "KR": "korean", "NZ": "new-zealand",
        "TT": "tromp-taylor", "BGA": "bga",
        # Punctuation / spacing variants of the same ruleset.
        "New Zealand": "new-zealand", "new-zealand": "new-zealand",
        "NewZealand": "new-zealand", "new_zealand": "new-zealand",
        "Tromp-Taylor": "tromp-taylor", "Tromp Taylor": "tromp-taylor",
        "TrompTaylor": "tromp-taylor",
        "chinese-ogs": "chinese-ogs", "Chinese (OGS)": "chinese-ogs",
        "Stone Scoring": "stone-scoring", "AGA-button": "aga-button",
        # Ing / Goe: spelled out in PRESETS, KataGo having no shorthand for it.
        "Ing": "ing", "Ing SST": "ing", "Goe": "ing",
        # Native names and trailing junk after the ruleset name.
        "Nihon Kiin": "japanese", "Japanese;Komi:6.5": "japanese",
        "chinese, area scoring": "chinese",
        # Last-resort: the scoring style alone still pins down the important bit.
        "Area": "chinese", "territory": "japanese",
    }
    for ru, expected in cases.items():
        assert sgf_rules_name(ru) == expected, ru
        rules = sgf_rules(ru)
        assert rules == PRESETS[expected], ru

    # "scoring" must not be mistaken for the "ing" ruleset, nor "SST" for "TT".
    assert sgf_rules_name("Stone Scoring") == "stone-scoring"
    assert sgf_rules_name("SST") is None

    # Unrecognised / empty values leave the caller on its default.
    for ru in ("", "   ", None, "Klingon", "12345", "!!!"):
        assert sgf_rules_name(ru) is None
        assert sgf_rules(ru) is None

    # A ruleset KataGo has no shorthand for still names itself on the toolbar.
    assert preset_name(PRESETS["ing"]) == "ing"


def test_initial_settings_from_sgf_rules_and_komi():
    from katsura.engine.position import initial_settings
    from katsura.engine.settings import DEFAULT_RULES, PRESETS
    from katsura.sgf.tree import parse_collection

    def settings(props):
        return initial_settings(Game(parse_collection(f"(;GM[1]SZ[19]{props})")[0]))

    # Rules and komi are both seeded from the SGF.
    s = settings("RU[Chinese]KM[7.5]")
    assert s.rules == PRESETS["chinese"] and s.komi == 7.5
    s = settings("RU[JP]KM[6]")
    assert s.rules == PRESETS["japanese"] and s.komi == 6.0
    # No RU / an unrecognised RU -> the default ruleset; no KM -> 7.0.
    assert settings("").rules == DEFAULT_RULES
    assert settings("RU[Klingon]").rules == DEFAULT_RULES
    assert settings("RU[Chinese]").komi == 7.0
    # Out-of-range, off-grid and outright invalid komi all stay usable.
    assert settings("KM[99999]").komi == 400.0
    assert settings("KM[-99999]").komi == -400.0
    assert settings("KM[6.34]").komi == 6.5          # snapped to a half-integer
    assert settings("KM[abc]").komi == 7.0           # unparseable -> default
    assert settings("KM[nan]").komi == 7.0           # parses, but not a number
    assert settings("KM[inf]").komi == 7.0
    assert settings("KM[]").komi == 7.0
    assert settings("KM[7.5子]").komi == 7.5         # unit suffix tolerated


def test_request_key_includes_engine_settings():
    from dataclasses import replace

    from katsura.engine.settings import PRESETS, AnalysisSettings
    g = Game.new(19)
    base = AnalysisSettings()
    k0 = build_request(g, 0, base).position_key
    # Each engine setting that differs must produce a distinct cache key.
    assert build_request(g, 0, replace(base, komi=6.5)).position_key != k0
    assert build_request(g, 0, replace(base, wide_root_noise=0.25)).position_key != k0
    assert build_request(
        g, 0, replace(base, playout_doubling_advantage=1.0)).position_key != k0
    assert build_request(
        g, 0, replace(base, rules=PRESETS["chinese"])).position_key != k0


def test_build_request_to_move_override():
    from katsura.go.board import WHITE
    g = Game.new(19)
    base = build_request(g, 0)
    assert base.color == "B"
    # The GUI-level (Ctrl+Click) player flip: same board, other side to move.
    flipped = build_request(g, 0, to_move=WHITE)
    assert flipped.color == "W"
    # Distinct cache keys — analysed as if a PL property set the other side.
    assert flipped.position_key != base.position_key


def test_rules_to_gtp_is_single_token():
    from katsura.engine.settings import PRESETS, preset_name
    js = PRESETS["japanese"].to_gtp()
    assert " " not in js and js.startswith("{") and '"scoring":"TERRITORY"' in js
    assert preset_name(PRESETS["chinese"]) == "chinese"


# -- GtpEngine against the mock -------------------------------------------

def _spin(qapp, pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return pred()


@pytest.fixture
def engine_and_log(qapp, tmp_path):
    from katsura.engine.gtp import GtpEngine
    log = tmp_path / "gtp.log"
    cmd = f"{sys.executable} {MOCK} {log}"
    eng = GtpEngine(interval_cs=5)
    states = []
    analyses = []
    eng.stateChanged.connect(states.append)
    eng.analysis.connect(lambda seq, a: analyses.append((seq, a)))
    eng.start(cmd)
    try:
        assert _spin(qapp, lambda: "ready" in states), f"never ready: {states}"
        yield eng, log, states, analyses
    finally:
        eng.stop()
        qapp.processEvents()


def test_engine_death_is_reported_with_exit_code(qapp):
    from katsura.engine.gtp import GtpEngine
    cmd = f'{sys.executable} -c "import sys; sys.exit(3)"'
    eng = GtpEngine(interval_cs=5)
    deaths, states = [], []
    eng.died.connect(deaths.append)
    eng.stateChanged.connect(states.append)
    eng.start(cmd)
    try:
        assert _spin(qapp, lambda: bool(deaths)), f"no death: states={states}"
        assert "exit code 3" in deaths[0]
        assert "ready" not in states          # never handshook
    finally:
        eng.stop()
        qapp.processEvents()


def test_gui_thread_calls_survive_engine_death(engine_and_log, qapp):
    """request/pause/raw-NN/stop after the process dies must not raise.

    They run on the GUI thread; a dead stdin pipe there must be swallowed
    (the worker's reader hits EOF and reports the death via `died`).
    """
    eng, log, states, analyses = engine_and_log
    deaths = []
    eng.died.connect(deaths.append)
    eng.request(_req(1, moves=[("B", "D4")]))
    assert _spin(qapp, lambda: any(s == 1 for s, _ in analyses)), "no analysis"
    eng._proc.kill()
    eng._proc.wait(timeout=5)
    # All of these may hit a broken pipe mid-call; none may raise.
    eng.request(_req(2, moves=[("B", "Q16")]))
    eng.pause()
    eng.request_raw_nn(3)
    eng.stop()
    assert _spin(qapp, lambda: bool(deaths) or "stopped" in states)


def test_engine_stop_does_not_report_death(engine_and_log, qapp):
    eng, log, states, analyses = engine_and_log
    deaths = []
    eng.died.connect(deaths.append)
    eng.stop()
    qapp.processEvents()
    time.sleep(0.05)
    qapp.processEvents()
    assert deaths == []                        # a clean stop is not a death


def _log_lines(log):
    if not log.exists():
        return []
    return [ln.strip() for ln in log.read_text().splitlines() if ln.strip()]


def _req(seq, anchor=(), moves=(), color="B", komi="6.5"):
    return AnalysisRequest(
        seq=seq, width=19, height=19, rules=None, komi=komi, color=color,
        anchor_stones=tuple(anchor), moves=tuple(moves),
        target_stones=tuple(anchor) + tuple(("B", v) for _, v in moves),
    )


def test_engine_full_sync_and_analyze(engine_and_log, qapp):
    eng, log, states, analyses = engine_and_log
    eng.request(_req(1, moves=[("B", "D4"), ("W", "Q16")]))
    assert _spin(qapp, lambda: any(s == 1 for s, _ in analyses)), "no analysis"
    lines = _log_lines(log)
    assert "rectangular_boardsize 19 19" in lines
    assert any(ln.startswith("set_position") for ln in lines)
    assert "play B D4" in lines
    assert "play W Q16" in lines
    assert any(ln.startswith("kata-analyze") for ln in lines)
    seq, a = next((s, a) for s, a in analyses if s == 1)
    assert a.best.move == "D4"


def test_console_command_invalidates_engine_state(engine_and_log, qapp):
    """After a console GTP command, the next sync must re-send from scratch.

    A console command can change anything (clear_board here); diffing against
    the pre-console value-tracked state would send *nothing* and analyse the
    wrong board.
    """
    eng, log, states, analyses = engine_and_log
    eng.request(_req(1, moves=[("B", "D4")]))
    assert _spin(qapp, lambda: any(s == 1 for s, _ in analyses)), "no analysis"
    eng.pause()
    responses = []
    eng.consoleResponse.connect(lambda c, ok, r: responses.append(c))
    eng.send_console("clear_board")
    assert _spin(qapp, lambda: bool(responses)), "console command never ran"
    n_before = len(_log_lines(log))
    eng.resume()
    eng.request(_req(2, moves=[("B", "D4")]))     # same position, new seq
    assert _spin(qapp, lambda: any(s == 2 for s, _ in analyses)), "no re-analysis"
    tail = _log_lines(log)[n_before:]
    assert "rectangular_boardsize 19 19" in tail
    assert "komi 6.5" in tail
    assert any(ln.startswith("set_position") for ln in tail)
    assert "play B D4" in tail


def test_engine_sends_rules_and_params(engine_and_log, qapp):
    eng, log, states, analyses = engine_and_log
    rules = (
        '{"friendlyPassOk":true,"hasButton":false,"ko":"SIMPLE",'
        '"scoring":"AREA","suicide":false,"tax":"NONE",'
        '"whiteHandicapBonus":"N"}'
    )
    req = AnalysisRequest(
        seq=1, width=19, height=19, rules=rules, komi="7.5", color="B",
        anchor_stones=(), moves=(("B", "D4"),), target_stones=(("B", "D4"),),
        wide_root_noise=0.25, playout_doubling_advantage=1.5,
    )
    eng.request(req)
    assert _spin(qapp, lambda: any(s == 1 for s, _ in analyses)), "no analysis"
    lines = _log_lines(log)
    assert f"kata-set-rules {rules}" in lines
    assert "komi 7.5" in lines
    assert "kata-set-param playoutDoublingAdvantagePla WHITE" in lines
    assert "kata-set-param playoutDoublingAdvantage 1.5" in lines
    assert "kata-set-param analysisWideRootNoise 0.25" in lines


def test_engine_incremental_undo_and_play(engine_and_log, qapp):
    eng, log, states, analyses = engine_and_log
    # Go to a 2-move position, wait for analysis.
    eng.request(_req(1, moves=[("B", "D4"), ("W", "Q16")]))
    assert _spin(qapp, lambda: any(s == 1 for s, _ in analyses))
    # Step back one move: same anchor -> a single 'undo', no new set_position.
    base = len(_log_lines(log))
    eng.request(_req(2, moves=[("B", "D4")]))
    assert _spin(qapp, lambda: any(s == 2 for s, _ in analyses))
    new_lines = _log_lines(log)[base:]
    assert "undo" in new_lines
    assert not any(ln.startswith("set_position") for ln in new_lines)
    # Step forward again: a single 'play', still no set_position.
    base2 = len(_log_lines(log))
    eng.request(_req(3, moves=[("B", "D4"), ("W", "Q16")]))
    assert _spin(qapp, lambda: any(s == 3 for s, _ in analyses))
    fwd_lines = _log_lines(log)[base2:]
    assert "play W Q16" in fwd_lines
    assert not any(ln.startswith("set_position") for ln in fwd_lines)


def test_engine_coalesces_to_latest(engine_and_log, qapp):
    eng, log, states, analyses = engine_and_log
    moves = []
    for i, v in enumerate(["D4", "Q16", "D16", "Q4", "C3"], start=1):
        moves.append(("B" if i % 2 else "W", v))
        eng.request(_req(i, moves=list(moves)))   # rapid-fire, no waiting
    # The newest position (seq 5) must eventually be analysed...
    assert _spin(qapp, lambda: any(s == 5 for s, _ in analyses))
    # ...and the engine board ends with the last move played.
    assert "play B C3" in _log_lines(log)


def test_engine_play_failure_falls_back_to_set_position(qapp, tmp_path):
    from katsura.engine.gtp import GtpEngine
    log = tmp_path / "gtp.log"
    env_cmd = f"MOCKGTP_FAIL_PLAY=Q16 {sys.executable} {MOCK} {log}"
    eng = GtpEngine(interval_cs=5)
    analyses = []
    states = []
    eng.stateChanged.connect(states.append)
    eng.analysis.connect(lambda seq, a: analyses.append((seq, a)))
    eng.start(env_cmd)
    try:
        assert _spin(qapp, lambda: "ready" in states)
        eng.request(_req(1, moves=[("B", "D4"), ("W", "Q16")]))
        assert _spin(qapp, lambda: any(s == 1 for s, _ in analyses))
        lines = _log_lines(log)
        # Q16 play was rejected -> a second set_position (the full target board).
        assert sum(1 for ln in lines if ln.startswith("set_position")) >= 2
    finally:
        eng.stop()
        qapp.processEvents()


def test_board_analysis_overlay_paints(qapp):
    from PySide6.QtGui import QImage

    from katsura.model.game import Game
    from katsura.ui.boardview import BoardView, _scale_color
    from katsura.ui.settings import Prefs

    # Colour scale endpoints and an interior point.
    assert _scale_color(0.0).alpha() == 15
    assert (_scale_color(1.0).red(), _scale_color(1.0).green(),
            _scale_color(1.0).blue()) == (100, 250, 140)

    bv = BoardView(Prefs())
    bv.set_game(Game.new(19))
    bv.resize(420, 420)
    # Q16 beats the order-0 D4 on both winrate (+0.1) and score (+1.0): D4 should
    # get a red border, Q16 a blue one. Just assert painting doesn't raise.
    line = ("info move D4 visits 100 winrate 0.6 scoreLead 2.0 weight 100 order 0 "
            "pv D4 info move Q16 visits 80 winrate 0.7 scoreLead 3.0 weight 80 "
            "order 1 pv Q16 D4")
    bv.set_analysis(parse_analysis_line(line, 19, 19))
    img = QImage(420, 420, QImage.Format_ARGB32)
    img.fill(0)
    bv.render(img)
    bv.set_analysis(None)


def test_window_live_analysis_overlay(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.go.board import BLACK
    from katsura.ui.mainwindow import MainWindow

    log = tmp_path / "gtp.log"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        assert _spin(qapp, lambda: win.engine_controller.state == "ready")
        # Becoming ready analyses the initial (empty-board) position.
        assert _spin(qapp, lambda: win.current_tab().board._analysis is not None)
        # Make a move; the Analysis Info pane should populate for the new position.
        tab = win.current_tab()
        tab.game.play(Point(3, 15), BLACK)
        tab.refresh()
        panel = tab.analysis_panel
        assert _spin(qapp, lambda: "%" in panel.winrate_label.text())
        assert panel.bar._wr is not None
    finally:
        win.engine_controller.detach()
        for i in range(win.tabs.count()):
            w = win.tabs.widget(i)
            w.document.dirty = False
        win.close()
        qapp.processEvents()


def test_transient_player_flip_reanalyzes_for_other_side(qapp, tmp_path):
    """Ctrl+Click's transient player flip (no SGF change) must re-drive the
    engine to analyse the same board for the other side, and flipping back must
    restore the original side's (cached) analysis."""
    from katsura.engine.config import EngineConfig
    from katsura.go.board import BLACK, WHITE
    from katsura.ui.mainwindow import MainWindow

    log = tmp_path / "gtp.log"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        assert _spin(qapp, lambda: win.engine_controller.state == "ready")
        assert _spin(qapp, lambda: win.current_tab().board._analysis is not None)
        tab = win.current_tab()
        assert any(ln.startswith("kata-analyze B") for ln in _log_lines(log))
        assert not any(ln.startswith("kata-analyze W") for ln in _log_lines(log))

        # Ctrl+Click in Play mode: flip who plays next, GUI-only.
        tab._click_play(Point(3, 15), shift=False, ctrl=True)
        assert tab.transient_color == WHITE
        assert not tab.game.current.has("PL")           # SGF untouched
        assert _spin(qapp, lambda: any(
            ln.startswith("kata-analyze W") for ln in _log_lines(log)))
        assert _spin(qapp, lambda: tab.board._analysis is not None)

        # Flip back: Black's analysis of this position is served from the cache
        # immediately (same position_key as before the flip).
        tab._click_play(Point(3, 15), shift=False, ctrl=True)
        assert tab.effective_to_move() == BLACK
        qapp.processEvents()
        assert tab.board._analysis is not None
    finally:
        win.engine_controller.detach()
        for i in range(win.tabs.count()):
            win.tabs.widget(i).document.dirty = False
        win.close()
        qapp.processEvents()


def test_paused_navigation_uses_per_position_cache(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.go.board import BLACK, WHITE
    from katsura.ui.mainwindow import MainWindow

    log = tmp_path / "gtp.log"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        assert _spin(qapp, lambda: win.engine_controller.state == "ready")
        tab = win.current_tab()
        tab.game.play(Point(3, 15), BLACK)
        tab.refresh()
        tab.game.play(Point(15, 3), WHITE)
        tab.refresh()
        board = tab.board
        assert _spin(qapp, lambda: board._analysis is not None)

        # Pause in place: the overlay stays (same position).
        win.set_live_analysis(False)
        qapp.processEvents()
        assert board._analysis is not None

        # Navigate to the (never-analysed) 1-move position: nothing cached -> clear.
        tab.on_navigate("back")
        qapp.processEvents()
        assert board._analysis is None

        # Navigate forward to the analysed 2-move position: its cached analysis
        # comes back instantly (no engine round-trip, even while paused).
        tab.on_navigate("forward")
        qapp.processEvents()
        assert board._analysis is not None
    finally:
        win.engine_controller.detach()
        for i in range(win.tabs.count()):
            win.tabs.widget(i).document.dirty = False
        win.close()
        qapp.processEvents()


def test_resume_keeps_overlay_from_cache(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.go.board import BLACK
    from katsura.ui.mainwindow import MainWindow

    log = tmp_path / "gtp.log"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        assert _spin(qapp, lambda: win.engine_controller.state == "ready")
        tab = win.current_tab()
        tab.game.play(Point(3, 15), BLACK)
        tab.refresh()
        board = tab.board
        assert _spin(qapp, lambda: board._analysis is not None)

        win.set_live_analysis(False)                # pause
        qapp.processEvents()
        assert board._analysis is not None
        win.set_live_analysis(True)                 # resume: cache shown at once
        qapp.processEvents()
        assert board._analysis is not None          # never blanked
    finally:
        win.engine_controller.detach()
        # Detaching clears the per-tab Analysis Info pane and the cache.
        qapp.processEvents()
        assert win.current_tab().analysis_panel.bar._wr is None
        assert win.engine_controller._cache == {}
        for i in range(win.tabs.count()):
            win.tabs.widget(i).document.dirty = False
        win.close()
        qapp.processEvents()


def test_raw_nn_syncs_to_its_position_first(engine_and_log, qapp):
    """kata-raw-nn must evaluate the position it was requested FOR.

    Pausing discards a pending position request, so the raw request carries
    the position itself and the worker syncs before sending kata-raw-nn.
    """
    eng, log, states, analyses = engine_and_log
    raws = []
    eng.rawNn.connect(lambda sym, ok, text: raws.append(sym))
    eng.pause()
    eng.request_raw_nn(2, _req(1, moves=[("B", "D4")]))
    assert _spin(qapp, lambda: 2 in raws), "no raw-nn response"
    lines = _log_lines(log)
    assert "play B D4" in lines
    # v1.17+ form: the request's side to move is named explicitly.
    assert lines.index("play B D4") < lines.index("kata-raw-nn b 2")


def test_raw_nn_color_falls_back_for_old_engines(qapp, tmp_path):
    """Pre-1.17 KataGo rejects `kata-raw-nn COLOR SYMMETRY`: the worker retries
    the bare form, warns once, and skips the colour form from then on."""
    from katsura.engine.gtp import GtpEngine
    log = tmp_path / "gtp.log"
    env_cmd = f"MOCKGTP_OLD_RAW_NN=1 {sys.executable} {MOCK} {log}"
    eng = GtpEngine(interval_cs=5)
    states, raws, warnings = [], [], []
    eng.stateChanged.connect(states.append)
    eng.rawNn.connect(lambda sym, ok, text: raws.append((sym, ok)))
    eng.logLine.connect(warnings.append)
    eng.start(env_cmd)
    try:
        assert _spin(qapp, lambda: "ready" in states)
        eng.pause()
        eng.request_raw_nn(2, _req(1, moves=[("B", "D4")], color="W"))
        assert _spin(qapp, lambda: (2, True) in raws), "no raw-nn response"
        lines = _log_lines(log)
        assert "kata-raw-nn w 2" in lines           # colour form tried first
        assert "kata-raw-nn 2" in lines             # then the bare fallback
        assert any("kata-raw-nn" in w for w in warnings)
        # The fallback sticks: the next raw-nn skips the colour form outright.
        eng.request_raw_nn(3, _req(2, moves=[("B", "D4")], color="W"))
        assert _spin(qapp, lambda: (3, True) in raws)
        lines = _log_lines(log)
        assert "kata-raw-nn w 3" not in lines
        assert "kata-raw-nn 3" in lines
    finally:
        eng.stop()
        qapp.processEvents()


def test_stale_died_signal_cannot_kill_new_engine(qapp, tmp_path):
    """A queued `died` from a detached engine must not tear down its successor."""
    from PySide6.QtCore import QObject

    from katsura.engine.config import EngineConfig
    from katsura.engine.controller import AnalysisController

    class _Win(QObject):
        def current_tab(self):
            return None
        def on_engine_log(self, ctrl, line):
            pass

    log = tmp_path / "gtp.log"
    cmd = f"{sys.executable} {MOCK} {log}"
    ctrl = AnalysisController(_Win())
    deaths = []
    ctrl.engineDied.connect(deaths.append)
    try:
        ctrl.attach(EngineConfig("A", cmd))
        assert _spin(qapp, lambda: ctrl.state == "ready")
        eng_a = ctrl.engine
        ctrl.attach(EngineConfig("B", cmd))      # detaches + disconnects A
        assert _spin(qapp, lambda: ctrl.state == "ready")
        eng_b = ctrl.engine
        assert eng_b is not eng_a
        eng_a.died.emit("late death of the old engine")
        qapp.processEvents()
        assert ctrl.engine is eng_b              # B survived
        assert deaths == []
        assert ctrl.state == "ready"
    finally:
        ctrl.detach()
        qapp.processEvents()


def test_no_tab_pauses_search_until_a_tab_returns(qapp, tmp_path):
    """With no tab, refresh_position halts the search; a tab resumes it."""
    from PySide6.QtCore import QObject

    from katsura.engine.config import EngineConfig
    from katsura.engine.controller import AnalysisController
    from katsura.engine.settings import AnalysisSettings
    from katsura.model.game import Game
    from katsura.ui.boardview import BoardView
    from katsura.ui.settings import Prefs

    class _Tab:
        def __init__(self):
            self.game = Game.new(19)
            self.board = BoardView(Prefs())
            self.board.set_game(self.game)
            self.analysis_settings = AnalysisSettings()
        def effective_to_move(self):
            return self.game.to_move

    class _Win(QObject):
        def __init__(self):
            super().__init__()
            self.tab = None
        def current_tab(self):
            return self.tab
        def on_engine_log(self, ctrl, line):
            pass

    win = _Win()
    ctrl = AnalysisController(win)
    log = tmp_path / "gtp.log"
    try:
        ctrl.attach(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        assert _spin(qapp, lambda: ctrl.state == "ready")
        # Became ready with no tab: the (idle) engine is held paused.
        assert ctrl.engine.is_paused()
        assert ctrl._current_seq == -1
        win.tab = _Tab()
        ctrl.refresh_position()                  # a tab appeared: resume + request
        assert not ctrl.engine.is_paused()
        assert ctrl._current_seq >= 0
        win.tab = None
        ctrl.refresh_position()                  # tab gone again: back to paused
        assert ctrl.engine.is_paused()
        assert ctrl._current_seq == -1
    finally:
        ctrl.detach()
        qapp.processEvents()


def test_stale_ownership_bridges_position_change(qapp):
    from PySide6.QtCore import QObject

    from katsura.engine.controller import AnalysisController
    from katsura.engine.settings import AnalysisSettings
    from katsura.go.board import BLACK, WHITE
    from katsura.model.game import Game
    from katsura.ui.boardview import BoardView
    from katsura.ui.settings import Prefs

    line = ("info move D4 visits 100 winrate 0.55 scoreLead 1.5 prior 0.4 "
            "weight 90 edgeVisits 100 order 0 pv D4 "
            "rootInfo visits 150 ownership " + " ".join(["0.3"] * 361))
    a = parse_analysis_line(line, 19, 19)

    game = Game.new(19)
    board = BoardView(Prefs())
    board.set_game(game)

    class _Tab:
        def __init__(self):
            self.game = game
            self.board = board
            self.analysis_settings = AnalysisSettings()
        def effective_to_move(self):
            return self.game.to_move

    class _Win(QObject):
        def __init__(self):
            super().__init__()
            self._tab = _Tab()
        def current_tab(self):
            return self._tab

    ctrl = AnalysisController(_Win())
    board.toggle_ownership()                      # ownership shown
    ctrl._apply_overlay(a, None)                  # draw the "prior" position result
    white_own = board.current_ownership_white()
    assert white_own is not None and white_own[0] == -0.3   # Black to move -> negated

    # Position changes with no cached result yet: capture stale, then clear overlay.
    ctrl._capture_stale_ownership()
    assert ctrl._stale_ownership == white_own
    ctrl._apply_overlay(None, None)
    assert board._analysis is None                # no live result on the board
    assert board.current_ownership_white() == white_own   # ...but stale still drawn
    assert board._ownership_active()

    # A real result arriving clears the stale ownership.
    ctrl._apply_overlay(a, None)
    assert ctrl._stale_ownership is None and board._stale_ownership is None

    # Perspective regression: after a move, the board's side to move flips. The
    # captured stale must stay in White's perspective (recorded at draw time),
    # NOT be re-derived from the flipped board (which would invert the colours).
    game.play(Point(3, 3), BLACK)                 # now White to move
    assert game.to_move == WHITE
    ctrl._capture_stale_ownership()
    assert ctrl._stale_ownership[0] == -0.3       # unchanged (not +0.3)

    # Halting live analysis clears stale too.
    ctrl._clear_stale_ownership()
    assert ctrl._stale_ownership is None and board._stale_ownership is None
    assert ctrl._last_ownership_white is None

    # Not live (paused): a position change does NOT record stale.
    ctrl._apply_overlay(a, None)                  # repopulate last-drawn ownership
    ctrl._console_paused = True
    ctrl._capture_stale_ownership()
    assert ctrl._stale_ownership is None


def test_policy_and_ownership_paint(qapp):
    from PySide6.QtGui import QImage

    from katsura.engine.analysis import parse_raw_nn
    from katsura.model.game import Game
    from katsura.ui.boardview import BoardView
    from katsura.ui.settings import Prefs

    line = ("info move D4 visits 100 winrate 0.55 scoreLead 1.5 prior 0.4 "
            "weight 90 edgeVisits 100 order 0 pv D4 "
            "info move Q16 visits 50 winrate 0.52 scoreLead 0.5 prior 0.05 "
            "weight 45 edgeVisits 50 order 1 pv Q16 "
            "rootInfo visits 150 ownership " + " ".join(["0.3"] * 361))
    a = parse_analysis_line(line, 19, 19)
    bv = BoardView(Prefs())
    bv.set_game(Game.new(19))
    bv.resize(420, 420)
    bv.set_analysis(a)
    img = QImage(420, 420, QImage.Format_ARGB32)
    img.fill(0)
    bv.toggle_policy_mode()                              # policy-prior view
    bv.render(img)
    bv.toggle_ownership()                                # + ownership heatmap
    bv.render(img)
    raw = parse_raw_nn(
        "symmetry 0 whiteWin 0.5 whiteLoss 0.45 noResult 0.05 whiteLead 1.0 "
        "whiteScoreSelfplay 1.0 whiteScoreSelfplaySq 50 varTimeLeft 20 "
        "shorttermWinlossError 0.1 shorttermScoreError 1.0 policyPass 0.01 "
        "policy " + " ".join(["0.002"] * 361) + " "
        "whiteOwnership " + " ".join(["0.2"] * 361), 19, 19)
    bv.set_raw_nn(raw)                                   # raw-NN policy view
    bv.render(img)
    assert bv.has_raw_nn()


def test_raw_nn_view_flow(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.ui.mainwindow import MainWindow

    log = tmp_path / "gtp.log"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        assert _spin(qapp, lambda: win.engine_controller.state == "ready")
        tab = win.current_tab()
        assert _spin(qapp, lambda: tab.board._analysis is not None)
        win.show_raw_nn_view(3)
        assert _spin(qapp, lambda: tab.board.has_raw_nn())
        assert win.engine_controller.is_raw_mode()
        assert tab.board._raw_nn.symmetry == 3
        assert len(tab.board._raw_nn.policy) == 361
        assert "raw-nn" in win.statusBar().currentMessage()
        # The window path names the side to move (v1.17+ colour argument).
        assert "kata-raw-nn b 3" in _log_lines(log)
        # Esc leaves the raw view and the board returns to normal analysis.
        win.exit_raw_nn_view()
        qapp.processEvents()
        assert not tab.board.has_raw_nn()
        assert not win.engine_controller.is_raw_mode()
    finally:
        win.engine_controller.detach()
        for i in range(win.tabs.count()):
            win.tabs.widget(i).document.dirty = False
        win.close()
        qapp.processEvents()


def test_switching_tabs_exits_raw_nn_view(qapp, tmp_path):
    """Switching tabs leaves the raw-NN view, clearing the overlay off the tab
    that had it — not off the tab just switched to (which would strand the old
    board showing raw policy that Esc could no longer dismiss)."""
    from katsura.engine.config import EngineConfig
    from katsura.ui.mainwindow import MainWindow

    log = tmp_path / "gtp.log"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        assert _spin(qapp, lambda: win.engine_controller.state == "ready")
        first = win.current_tab()
        assert _spin(qapp, lambda: first.board._analysis is not None)
        win.show_raw_nn_view(3)
        assert _spin(qapp, lambda: first.board.has_raw_nn())

        second = win.new_game(19)                 # opening it selects it
        qapp.processEvents()
        assert win.current_tab() is second
        assert not win.engine_controller.is_raw_mode()
        assert not first.board.has_raw_nn()       # the overlay left with the mode
        assert not second.board.has_raw_nn()

        # Back on the first tab, Esc has nothing stale to fight over and the
        # raw view can be re-entered normally.
        win.tabs.setCurrentWidget(first)
        qapp.processEvents()
        assert not first.board.has_raw_nn()
        win.show_raw_nn_view(2)
        assert _spin(qapp, lambda: first.board.has_raw_nn())
        assert win.exit_raw_nn_view()
        qapp.processEvents()
        assert not first.board.has_raw_nn()
    finally:
        win.engine_controller.detach()
        for i in range(win.tabs.count()):
            win.tabs.widget(i).document.dirty = False
        win.close()
        qapp.processEvents()


def test_board_readout_hover_and_pass(qapp):
    from katsura.model.game import Game
    from katsura.ui.boardview import BoardView
    from katsura.ui.settings import Prefs

    bv = BoardView(Prefs())
    bv.set_game(Game.new(19))
    bv.resize(420, 420)
    line = ("info move D4 visits 100 edgeVisits 110 winrate 0.6 scoreLead 2.0 "
            "scoreSelfplay 3.0 scoreStdev 21.0 prior 0.3 weight 90 "
            "noResultValue 0.02 order 0 pv D4 "
            "info move pass visits 5 winrate 0.4 scoreLead -1.0 prior 0.01 weight 5 "
            "order 1 pv pass rootInfo visits 105")
    a = parse_analysis_line(line, 19, 19)
    captured = []
    bv.analysisReadout.connect(captured.append)
    bv.set_analysis(a)
    assert captured and captured[-1].startswith("pass")     # no hover -> pass eval
    assert "win B 40.0%" in captured[-1]

    bv._hover = Point(3, 15)                                  # hover D4
    bv._emit_readout()
    txt = captured[-1]
    # Hovering names what the numbers are about, and the selfplay score reads
    # like a Go result with its stdev attached.
    assert txt.startswith("Stats for D4")
    assert "win B 60.0%" in txt and "lead B+2.0" in txt
    assert "score B+3.0 (std 21.0)" in txt
    assert "edgeW 99" in txt                                  # 90*110/100
    assert "policy 30.00%" in txt and "prior" not in txt
    assert "no-result 2.00%" in txt
    # The pass line has no scoreSelfplay of its own: it falls back to scoreLead.
    # It reads with the same fields as a hovered move (one shared formatter).
    bv._hover = None
    bv._emit_readout()
    assert "score W+1.0" in captured[-1]
    assert "visits 5" in captured[-1] and "policy 1.00%" in captured[-1]


def test_panel_continuity_from_parent_move(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.go.board import BLACK
    from katsura.ui.mainwindow import MainWindow

    log = tmp_path / "gtp.log"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        assert _spin(qapp, lambda: win.engine_controller.state == "ready")
        tab = win.current_tab()
        # Let the root position be analysed and cached (mock reports D4 @ 0.55).
        assert _spin(qapp, lambda: tab.board._analysis is not None)
        win.set_live_analysis(False)                    # pause: no new requests
        qapp.processEvents()
        # Navigate into D4 — a candidate the parent (root) analysis evaluated.
        tab.game.play(Point(3, 15), BLACK)               # D4
        tab.refresh()
        qapp.processEvents()
        # No analysis for D4's position yet, but the panel shows the parent's
        # eval of D4 (Black 55.0%) for continuity, and the board overlay is clear.
        assert tab.board._analysis is None
        assert tab.analysis_panel.bar._wr is not None
        assert abs(tab.analysis_panel.bar._wr - 0.55) < 1e-6
        assert "55.0" in tab.analysis_panel.winrate_label.text()
    finally:
        win.engine_controller.detach()
        for i in range(win.tabs.count()):
            win.tabs.widget(i).document.dirty = False
        win.close()
        qapp.processEvents()


def test_engine_console_pause(engine_and_log, qapp):
    eng, log, states, analyses = engine_and_log
    responses = []
    eng.consoleResponse.connect(lambda c, ok, t: responses.append((c, ok, t)))
    eng.pause()
    eng.send_console("showboard")
    assert _spin(qapp, lambda: any(c == "showboard" for c, _, _ in responses))
    cmd, ok, _ = next(r for r in responses if r[0] == "showboard")
    assert ok


# -- multiple attached engines ---------------------------------------------

def _close_win(win, qapp):
    for c in list(win.engine_controllers):
        win.detach_engine(c)
    for i in range(win.tabs.count()):
        win.tabs.widget(i).document.dirty = False
    win.close()
    qapp.processEvents()


def test_multi_engine_attach_select_detach(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.ui.mainwindow import MainWindow

    cmd_a = f"{sys.executable} {MOCK} {tmp_path / 'a.log'}"
    cmd_b = f"{sys.executable} {MOCK} {tmp_path / 'b.log'}"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("A", cmd_a))
        ctrl_a = win.engine_controller
        assert _spin(qapp, lambda: ctrl_a.state == "ready")
        assert _spin(qapp, lambda: win.current_tab().board._analysis is not None)

        # Attaching a second engine switches the shown engine to the new one...
        win.attach_engine(EngineConfig("B", cmd_b))
        ctrl_b = win.engine_controller
        assert ctrl_b is not ctrl_a
        assert win.engine_controllers == [ctrl_a, ctrl_b]
        # ...and the INVARIANT holds for the deselected engine: its process is
        # held open and running, but it is never analysing live.
        assert ctrl_a.engine.is_running()
        assert ctrl_a.engine.is_paused()
        assert not ctrl_a.is_active() and ctrl_b.is_active()
        assert _spin(qapp, lambda: ctrl_b.state == "ready")
        assert _spin(qapp, lambda: win.current_tab().board._analysis is not None)

        # The Analysis Info header selector lists both, shows B, and is a
        # drop-down now that more than one engine is attached.
        tab = win.current_tab()
        assert tab.engine_select._names == ["A", "B"]
        assert tab.engine_select._current == 1
        assert tab.engine_select.isEnabled()

        # The Attach menu greys out engines that are already attached.
        win.engines = [EngineConfig("A", cmd_a), EngineConfig("C", "true")]
        win._rebuild_attach_menu()
        texts = [(a.text(), a.isEnabled()) for a in win.attach_menu.actions()]
        assert texts == [("A (attached)", False), ("C", True)]

        # The Detach menu holds one entry per attached engine.
        win._rebuild_detach_menu()
        assert [a.text() for a in win.detach_menu.actions()] == ["A", "B"]

        # Attaching an already-attached engine just re-selects it: B halts
        # (auto-pausing its running search), A resumes, no third process.
        win.attach_engine(EngineConfig("A", cmd_a))
        assert win.engine_controller is ctrl_a
        assert len(win.engine_controllers) == 2
        assert ctrl_b.engine.is_paused()
        assert not ctrl_a.engine.is_paused()
        # A's per-engine cache repaints its analysis for this position at once.
        assert win.current_tab().board._analysis is not None

        # Detaching the shown engine falls back to the remaining one.
        win.detach_engine(ctrl_a)
        assert win.engine_controllers == [ctrl_b]
        assert win.engine_controller is ctrl_b
        assert ctrl_b.is_active()
        assert not ctrl_b.engine.is_paused()
    finally:
        _close_win(win, qapp)


def test_console_is_per_engine_and_follows_selection(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.ui.mainwindow import MainWindow

    win = MainWindow()
    try:
        win.attach_engine(
            EngineConfig("A", f"{sys.executable} {MOCK} {tmp_path / 'a.log'}"))
        ctrl_a = win.engine_controller
        assert _spin(qapp, lambda: ctrl_a.state == "ready")
        win.attach_engine(
            EngineConfig("B", f"{sys.executable} {MOCK} {tmp_path / 'b.log'}"))
        ctrl_b = win.engine_controller
        assert _spin(qapp, lambda: ctrl_b.state == "ready")

        # Opening the console targets the shown engine (B) and pauses it.
        win.on_gtp_console()
        console = win.gtp_console
        assert console.isVisible()
        assert ctrl_b.is_console_paused()
        assert console.engine_combo.currentText() == "B"

        # A typed command lands in B's transcript (and display) only.
        console.commandEntered.emit("showboard")
        assert _spin(qapp, lambda: any(
            ln.startswith(("=", "?")) for ln in ctrl_b.console_transcript))
        assert "> showboard" in ctrl_b.console_transcript
        assert all("showboard" not in ln for ln in ctrl_a.console_transcript)
        assert "> showboard" in console.output.toPlainText()

        # Switching the console's selector switches the app-wide shown engine:
        # A becomes shown (console-paused), B is deselected and held paused.
        console.engineSelected.emit(0)
        qapp.processEvents()
        assert win.engine_controller is ctrl_a
        assert ctrl_a.is_console_paused()
        assert not ctrl_b.is_console_paused()
        assert ctrl_b.engine.is_paused()
        assert "showboard" not in console.output.toPlainText()  # A's transcript

        # Closing the console resumes only the shown engine.
        console.close()
        qapp.processEvents()
        assert not ctrl_a.is_console_paused()
        assert not ctrl_a.engine.is_paused()
        assert ctrl_b.engine.is_paused()
    finally:
        _close_win(win, qapp)


def test_clear_cache_actions(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.ui.mainwindow import MainWindow

    log = tmp_path / "gtp.log"
    win = MainWindow()
    try:
        win.attach_engine(EngineConfig("mock", f"{sys.executable} {MOCK} {log}"))
        ctrl = win.engine_controller
        assert _spin(qapp, lambda: ctrl.state == "ready")
        board = win.current_tab().board
        assert _spin(qapp, lambda: board._analysis is not None)
        assert ctrl._cache

        # Clear the GUI cache: everything cached is forgotten, the overlay
        # drops instantly, then live analysis repopulates it freshly.
        win.on_clear_gui_cache()
        assert ctrl._cache == {}
        assert board._analysis is None
        assert _spin(qapp, lambda: board._analysis is not None)

        # Clear the engine's own cache: halts analysis (stays halted, like
        # Space) and sends clear_cache through the console path, so the
        # command + response land in this engine's console transcript.
        win.on_clear_engine_cache()
        assert not ctrl.is_enabled()
        assert not ctrl.is_enabled()
        assert ctrl.engine.is_paused()
        assert _spin(qapp, lambda: "clear_cache" in _log_lines(log))
        assert "> clear_cache" in ctrl.console_transcript
        assert _spin(qapp, lambda: any(
            ln.startswith("=") for ln in ctrl.console_transcript))
    finally:
        _close_win(win, qapp)


def test_current_engine_death_falls_back_to_other(qapp, tmp_path):
    from katsura.engine.config import EngineConfig
    from katsura.ui.mainwindow import MainWindow

    win = MainWindow()
    try:
        win.attach_engine(
            EngineConfig("A", f"{sys.executable} {MOCK} {tmp_path / 'a.log'}"))
        ctrl_a = win.engine_controller
        assert _spin(qapp, lambda: ctrl_a.state == "ready")
        win.attach_engine(
            EngineConfig("B", f"{sys.executable} {MOCK} {tmp_path / 'b.log'}"))
        ctrl_b = win.engine_controller
        assert _spin(qapp, lambda: ctrl_b.state == "ready")

        # Kill the shown engine's process: the window falls back to A.
        ctrl_b.engine._proc.kill()
        assert _spin(qapp, lambda: win.engine_controller is ctrl_a)
        assert win.engine_controllers == [ctrl_a]
        assert ctrl_a.is_active()

        # The console popped, showing the dead engine's transcript under a
        # "(stopped)" pseudo-entry; the live fallback obeys the console pause.
        console = win.gtp_console
        assert console is not None and console.isVisible()
        assert console.engine_combo.currentText() == "B (stopped)"
        assert "Engine stopped" in console.output.toPlainText()
        assert ctrl_a.is_console_paused()

        # Picking the live engine in the selector returns to its transcript.
        console.engineSelected.emit(0)
        qapp.processEvents()
        assert console.engine_combo.currentText() == "A"
        assert "Engine stopped" not in console.output.toPlainText()
        console.close()
        qapp.processEvents()
        assert not ctrl_a.is_console_paused()
    finally:
        _close_win(win, qapp)
