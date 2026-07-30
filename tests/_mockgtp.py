"""A minimal mock GTP engine for tests.

Speaks just enough GTP for :class:`GtpEngine`: responds to commands, logs every
received command line to the file named by ``argv[1]`` (for assertions), and
streams a fixed ``kata-analyze`` line until interrupted by a newline.

Special hooks (so tests can exercise the fallback paths):
* ``MOCKGTP_FAIL_PLAY`` env var: a vertex for which ``play`` returns a GTP error.
* ``MOCKGTP_OLD_RAW_NN`` env var: if set, ``kata-raw-nn`` rejects the v1.17+
  ``COLOR SYMMETRY`` argument form (mimicking an older KataGo).
"""

import os
import select
import sys

W = [19]
H = [19]
_logf = open(sys.argv[1], "a") if len(sys.argv) > 1 else None
_fail_play = os.environ.get("MOCKGTP_FAIL_PLAY", "")
_old_raw_nn = bool(os.environ.get("MOCKGTP_OLD_RAW_NN", ""))


def _w(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def _ok(body=""):
    _w(f"= {body}\n\n" if body else "=\n\n")


def _err(body="error"):
    _w(f"? {body}\n\n")


def _log(cmd):
    if _logf:
        _logf.write(cmd + "\n")
        _logf.flush()


def _analyze(parts):
    ownership = "ownership" in parts
    n = W[0] * H[0]
    blocks = (
        "info move D4 visits 100 winrate 0.55 scoreLead 1.5 prior 0.3 lcb 0.54 "
        "order 0 pv D4 Q16"
        " info move Q16 visits 50 winrate 0.52 scoreLead 0.5 prior 0.2 lcb 0.5 "
        "order 1 pv Q16 D4"
        " rootInfo visits 150 winrate 0.55 scoreLead 1.5"
    )
    if ownership:
        blocks += " ownership " + " ".join(["0.0"] * n)
    _w("=\n")                          # opening status line of the streaming response
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            sys.stdin.readline()       # consume the interrupting newline/command
            _w("\n")                   # terminating blank line
            return
        _w(blocks + "\n")


def main():
    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        cmd = line.strip()
        if cmd == "":
            continue
        _log(cmd)
        parts = cmd.split()
        name = parts[0]
        if name == "quit":
            _ok()
            break
        elif name == "name":
            _ok("MockGTP")
        elif name == "protocol_version":
            _ok("2")
        elif name == "rectangular_boardsize":
            W[0], H[0] = int(parts[1]), int(parts[2])
            _ok()
        elif name == "boardsize":
            if len(parts) >= 3:
                W[0], H[0] = int(parts[1]), int(parts[2])
            else:
                W[0] = H[0] = int(parts[1])
            _ok()
        elif name == "play":
            if len(parts) >= 3 and parts[2].upper() == _fail_play.upper():
                _err("illegal move")
            else:
                _ok()
        elif name in ("kata-analyze", "lz-analyze"):
            _analyze(parts)
        elif name == "kata-raw-nn":
            # v1.17+ syntax: kata-raw-nn [COLOR] SYMMETRY (colour comes first).
            args = parts[1:]
            if args and args[0].lower() in ("b", "w", "black", "white"):
                if _old_raw_nn:
                    _err("could not parse symmetry")
                    continue
                args = args[1:]
            sym = args[0] if args else "0"
            n = W[0] * H[0]
            _ok(
                f"symmetry {sym} whiteWin 0.45 whiteLoss 0.5 noResult 0.05 "
                "whiteLead -1.5 whiteScoreSelfplay -1.0 whiteScoreSelfplaySq 50.0 "
                "varTimeLeft 25.0 shorttermWinlossError 0.1 shorttermScoreError 1.5 "
                "policyPass 0.01 policy " + " ".join(["0.001"] * n) + " "
                "whiteOwnership " + " ".join(["0.1"] * n))
        else:
            _ok()


if __name__ == "__main__":
    main()
