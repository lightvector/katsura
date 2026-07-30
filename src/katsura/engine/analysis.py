"""Parse ``kata-analyze`` / ``lz-analyze`` output lines into structured data.

KataGo emits a *single line* per reporting interval, containing one ``info``
block per analysed move, optionally followed by top-level ``rootInfo`` and
``ownership`` sections. Within an ``info`` block the fields are space-separated
``key value`` pairs, except ``pv`` (and the other list-valued fields) which run
until the next field/section marker.

We deliberately request only ``ownership`` (and rely on the default fields), so
the only variable-length field inside a block is ``pv``; the parser is
nonetheless robust to fields appearing in any order and to unknown future
fields, as the KataGo docs advise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..sgf.coords import Point
from .coords import vertex_to_point

# Section markers that start a new top-level chunk of the line.
_TOP_LEVEL = {"info", "rootInfo", "ownership", "ownershipStdev"}
# Scalar (single-token-value) fields inside an ``info`` block.
_INFO_SCALARS = {
    "move", "visits", "edgeVisits", "utility", "winrate", "scoreMean",
    "scoreStdev", "scoreLead", "scoreSelfplay", "prior", "lcb", "utilityLcb",
    "weight", "order", "noResultValue", "isSymmetryOf",
}
# List-valued (variable-length) fields inside an ``info`` block.
_INFO_LISTS = {
    "pv", "pvVisits", "pvEdgeVisits", "movesOwnership", "movesOwnershipStdev",
}
# Tokens that terminate a list-valued field.
_LIST_STOP = _TOP_LEVEL | _INFO_SCALARS | _INFO_LISTS


@dataclass
class PanelStats:
    """Black-perspective stats for the Analysis Info pane (any field optional)."""

    winrate_black: float | None = None
    lead_black: float | None = None
    visits: int | None = None
    score_stdev: float | None = None
    policy_kl: float | None = None
    no_result: float | None = None


@dataclass
class MoveInfo:
    """Analysis for a single candidate move."""

    move: str                      # GTP vertex, or "pass"
    point: Point | None         # board point (None for pass)
    visits: int
    winrate: float                 # [0, 1], from the side-to-move's perspective
    # How far ahead the side to move is *relative to an even game*, in points —
    # a lead, not a prediction of the final score (the two diverge where the
    # outcome distribution is lopsided). This is what the GUI labels "lead".
    score_lead: float
    prior: float                   # policy prior, [0, 1]
    weight: float                  # total visit weight (falls back to visits)
    order: int                     # 0 = best
    edge_visits: int = 0           # root's "wanted" visits for the move
    score_stdev: float = 0.0       # stdev of the predicted final score (biased high)
    # The predicted *final score* (side-to-move's perspective, in points), which
    # score_stdev is the standard deviation of. Defaults to score_lead when an
    # engine omits it, so the readout always has something to show.
    score_selfplay: float = 0.0
    no_result_value: float | None = None  # P(no-result), if requested
    pv: list[str] = field(default_factory=list)        # GTP vertices
    pv_points: list[Point | None] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def edge_weight(self) -> float:
        """Weight scaled to edge visits: ``weight * edgeVisits / max(visits, 1)``.

        This is the weight KataGo "wants" to give the move at the root, which is
        the right quantity for the colour scale and the policy-KL distribution.
        Falls back to ``weight`` when edgeVisits == visits.
        """
        return self.weight * self.edge_visits / max(self.visits, 1)


@dataclass
class Analysis:
    """A full analysis snapshot parsed from one reporting line."""

    moves: list[MoveInfo] = field(default_factory=list)
    root: dict = field(default_factory=dict)           # parsed rootInfo (floats)
    ownership: list[float] | None = None            # row-major from top-left
    width: int = 19
    height: int = 19

    @property
    def total_visits(self) -> int:
        if "visits" in self.root:
            return int(self.root["visits"])
        return sum(m.visits for m in self.moves)

    @property
    def best(self) -> MoveInfo | None:
        return self.moves[0] if self.moves else None

    @property
    def root_winrate(self) -> float | None:
        """Root winrate (side-to-move perspective) from ``rootInfo``.

        Falls back to the best move's winrate when an engine omits rootInfo.
        """
        if "winrate" in self.root:
            return self.root["winrate"]
        return self.best.winrate if self.best else None

    @property
    def root_score_lead(self) -> float | None:
        """Root lead in points (side-to-move perspective) from ``rootInfo``.

        A lead relative to an even game, not a predicted final score — see
        :class:`MoveInfo`.
        """
        for k in ("scoreLead", "scoreMean"):
            if k in self.root:
                return self.root[k]
        return self.best.score_lead if self.best else None

    @property
    def root_score_stdev(self) -> float | None:
        """Root score stdev from ``rootInfo`` (falls back to the best move)."""
        if "scoreStdev" in self.root:
            return self.root["scoreStdev"]
        return self.best.score_stdev if self.best else None

    @property
    def root_no_result(self) -> float | None:
        """Best move's no-result probability (rootInfo has none), if available."""
        return self.best.no_result_value if self.best else None

    def policy_kl(self) -> float | None:
        """KL divergence of the search's edge-weight distribution from the policy.

        ``KL(W || P) = sum_m W(m) * (log W(m) - log P(m))`` over searched moves,
        where ``W(m)`` is the move's edge-weight normalised to a distribution and
        ``P(m)`` is its policy prior. This is exactly the user's
        ``sum_m -W(m) * (log P(m) - log W(m))`` (non-negative). Returns ``None``
        when there is nothing to measure.
        """
        ws = [(m.edge_weight, m.prior) for m in self.moves if m.edge_weight > 0]
        total = sum(w for w, _ in ws)
        if total <= 0:
            return None
        kl = 0.0
        for w, prior in ws:
            wd = w / total
            p = max(prior, 1e-12)       # guard log(0) for zero-prior moves
            kl += wd * (math.log(wd) - math.log(p))
        return kl


def _to_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _to_int(s: str, default: int = 0) -> int:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _make_move_info(d: dict, height: int) -> MoveInfo:
    move = d.get("move", "")
    try:
        point = vertex_to_point(move, height) if move else None
    except ValueError:
        point = None
    pv = list(d.get("pv", []))
    pv_points = []
    for v in pv:
        try:
            pv_points.append(vertex_to_point(v, height))
        except ValueError:
            pv_points.append(None)
    visits = _to_int(d.get("visits"))
    nr = d.get("noResultValue")
    lead = _to_float(d.get("scoreLead", d.get("scoreMean")))
    return MoveInfo(
        move=move,
        point=point,
        visits=visits,
        winrate=_to_float(d.get("winrate")),
        score_lead=lead,
        prior=_to_float(d.get("prior")),
        weight=_to_float(d.get("weight", visits)),
        order=_to_int(d.get("order")),
        edge_visits=_to_int(d.get("edgeVisits", visits)),
        score_stdev=_to_float(d.get("scoreStdev")),
        score_selfplay=_to_float(d.get("scoreSelfplay", lead), lead),
        no_result_value=None if nr is None else _to_float(nr),
        pv=pv,
        pv_points=pv_points,
        raw=d,
    )


def parse_analysis_line(line: str, width: int, height: int) -> Analysis:
    """Parse one ``kata-analyze`` output line into an :class:`Analysis`."""
    toks = line.split()
    n = len(toks)
    i = 0
    moves: list[MoveInfo] = []
    root: dict = {}
    ownership: list[float] | None = None

    while i < n:
        tok = toks[i]
        if tok == "info":
            i += 1
            d: dict = {}
            while i < n and toks[i] not in _TOP_LEVEL:
                key = toks[i]
                i += 1
                if key in _INFO_LISTS:
                    vals = []
                    while i < n and toks[i] not in _LIST_STOP:
                        vals.append(toks[i])
                        i += 1
                    d[key] = vals
                elif i < n:
                    d[key] = toks[i]
                    i += 1
            moves.append(_make_move_info(d, height))
        elif tok == "rootInfo":
            i += 1
            while i < n and toks[i] not in _TOP_LEVEL:
                key = toks[i]
                i += 1
                if i < n:
                    root[key] = _to_float(toks[i])
                    i += 1
        elif tok in ("ownership", "ownershipStdev"):
            is_own = tok == "ownership"
            i += 1
            count = width * height
            vals = []
            while i < n and len(vals) < count and toks[i] not in _TOP_LEVEL:
                vals.append(_to_float(toks[i]))
                i += 1
            if is_own:
                ownership = vals
        else:
            i += 1  # unknown stray token; skip defensively

    moves.sort(key=lambda m: m.order)
    return Analysis(moves=moves, root=root, ownership=ownership,
                    width=width, height=height)


# -- raw neural-net evaluation (kata-raw-nn) -------------------------------

# Single-float scalar keys in a kata-raw-nn block.
_RAW_SCALARS = {
    "whiteWin", "whiteLoss", "noResult", "whiteLead", "whiteScore",
    "whiteScoreSelfplay", "whiteScoreSelfplaySq", "whiteScoreSq", "varTimeLeft",
    "shorttermWinlossError", "shorttermScoreError", "policyPass",
}
# Board-sized (W*H floats) keys.
_RAW_ARRAYS = {"policy", "whiteOwnership"}


@dataclass
class RawNN:
    """One ``kata-raw-nn`` evaluation (a single symmetry).

    Win/loss/lead/ownership are reported by KataGo from **White's** perspective;
    the helper properties convert to Black's perspective for display.
    """

    symmetry: int = 0
    width: int = 19
    height: int = 19
    scalars: dict = field(default_factory=dict)        # raw key -> float
    policy: list[float] = field(default_factory=list)  # W*H, NaN for illegal
    policy_pass: float = 0.0
    ownership: list[float] | None = None             # W*H, white's perspective

    def _s(self, key: str, default: float = 0.0) -> float:
        v = self.scalars.get(key)
        return default if v is None else v

    @property
    def black_winrate(self) -> float:
        """Black win prob incl. half the no-result mass: blackWin + 0.5*noResult."""
        return self._s("whiteLoss") + 0.5 * self._s("noResult")

    @property
    def no_result(self) -> float:
        return self._s("noResult")

    @property
    def black_lead(self) -> float:
        """Points Black is ahead by (= -whiteLead)."""
        return -self._s("whiteLead")

    @property
    def black_score_selfplay(self) -> float:
        return -self._s("whiteScoreSelfplay")

    @property
    def score_stdev(self) -> float:
        """sqrt(Var) from E[score^2] - E[score]^2 over selfplay (perspective-free)."""
        mean = self._s("whiteScoreSelfplay")
        sq = self._s("whiteScoreSelfplaySq")
        return math.sqrt(max(0.0, sq - mean * mean))

    @property
    def var_time_left(self) -> float:
        return self._s("varTimeLeft")

    @property
    def shortterm_winloss_error(self) -> float:
        return self._s("shorttermWinlossError")

    @property
    def shortterm_score_error(self) -> float:
        return self._s("shorttermScoreError")


def parse_raw_nn(text: str, width: int, height: int) -> RawNN:
    """Parse a single ``kata-raw-nn`` response block into a :class:`RawNN`."""
    toks = text.split()
    n = len(toks)
    count = width * height
    raw = RawNN(width=width, height=height)
    i = 0
    while i < n:
        key = toks[i]
        i += 1
        if key == "symmetry":
            if i < n:
                raw.symmetry = _to_int(toks[i])
                i += 1
        elif key == "policyPass":
            if i < n:
                raw.policy_pass = _to_float(toks[i])
                i += 1
        elif key in _RAW_SCALARS:
            if i < n:
                raw.scalars[key] = _to_float(toks[i])
                i += 1
        elif key in _RAW_ARRAYS:
            vals = []
            while i < n and len(vals) < count and toks[i] not in _RAW_SCALARS \
                    and toks[i] not in _RAW_ARRAYS and toks[i] != "symmetry":
                vals.append(_to_float(toks[i]))
                i += 1
            if key == "policy":
                raw.policy = vals
            else:
                raw.ownership = vals
        # Unknown key: skip its name token; best-effort robustness.
    return raw
