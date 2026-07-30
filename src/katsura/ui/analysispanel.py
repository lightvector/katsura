"""The Analysis Info pane: root-level engine stats for the current position.

Everything here is shown **from Black's perspective** (consistent with the board
overlay): the winrate bar fills with black from the left with Black's win
probability. The numbers come from KataGo's ``rootInfo`` (falling back to the
best move when an older engine omits it), not the top move — except in the
continuity case where the *parent* node's analysis of the move leading here is
shown until this position has its own analysis.
"""

from __future__ import annotations


from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel

from ..engine.analysis import PanelStats


class WinrateBar(QWidget):
    """A horizontal bar that fills black from the left with Black's winrate.

    With no winrate (``None``) it draws a neutral grey, so it never resembles
    one side or the other having a filled bar.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wr: float | None = None     # Black's winrate in [0, 1]
        self.setMinimumHeight(22)
        self.setMaximumHeight(22)

    def set_winrate(self, wr: float | None) -> None:
        self._wr = None if wr is None else max(0.0, min(1.0, wr))
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if self._wr is None:
            p.fillRect(r, QColor(110, 110, 110))      # neutral: no information
        else:
            p.fillRect(r, QColor(232, 232, 232))       # White-ahead ground
            fill = QRectF(r.left(), r.top(), r.width() * self._wr, r.height())
            p.fillRect(fill, QColor(20, 20, 20))       # Black fill from the left
            midx = r.left() + r.width() / 2
            p.setPen(QPen(QColor(150, 60, 60), 1.0))
            p.drawLine(QPointF(midx, r.top()), QPointF(midx, r.bottom()))
        p.setPen(QPen(QColor(110, 110, 110), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r)


_WINRATE_TIP = ("Chance of winning, averaged over the whole search rather than "
                "taken from the best move alone.")
_LEAD_TIP = ("How far ahead the leading side is, in points, averaged over the "
             "whole search. Not the same as the predicted final score, which "
             "the hover readout shows as 'score'.")
_BAR_TIP = "The winrate, filled from the left; the centre tick is an even game."

# Explanations for the secondary stat rows (shown on both caption and value).
_FIELD_TIPS = {
    "visits": "Playouts the search has spent on this position.",
    "stdev": (
        "Predicted standard deviation of the final score, in points. MCTS "
        "biases it upwards, so compare positions rather than reading it as an "
        "absolute."),
    "kl": (
        "How far the search's distribution over moves has moved away from the "
        "raw policy priors. Near zero means the search still agrees with the "
        "net's first instinct."),
    "noresult": (
        "Chance the game ends with no result — a triple ko or another cycle — "
        "instead of a win for either side."),
}


class AnalysisInfoPanel(QWidget):
    """Body widget for the 'Analysis Info' collapsible section."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 8)
        lay.setSpacing(4)

        labels = QHBoxLayout()
        self.winrate_label = QLabel("Win —")
        self.lead_label = QLabel("Lead —")
        self.lead_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bold = QFont()
        bold.setBold(True)
        self.winrate_label.setFont(bold)
        self.lead_label.setFont(bold)
        self.winrate_label.setToolTip(_WINRATE_TIP)
        self.lead_label.setToolTip(_LEAD_TIP)
        labels.addWidget(self.winrate_label)
        labels.addStretch(1)
        labels.addWidget(self.lead_label)
        lay.addLayout(labels)

        self.bar = WinrateBar()
        self.bar.setToolTip(_BAR_TIP)
        lay.addWidget(self.bar)

        # A small grid of secondary stats.
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(1)
        self._fields: dict[str, QLabel] = {}
        rows = [("visits", "Visits"), ("stdev", "Score stdev"),
                ("kl", "Policy KL"), ("noresult", "No-result")]
        for i, (key, label) in enumerate(rows):
            cap = QLabel(label)
            cap.setStyleSheet("color: #999;")
            val = QLabel("—")
            val.setStyleSheet("color: #ccc;")
            # Both halves of the row explain the stat, so hovering anywhere on it
            # works (these are not self-explanatory names).
            tip = _FIELD_TIPS.get(key, "")
            cap.setToolTip(tip)
            val.setToolTip(tip)
            grid.addWidget(cap, i, 0)
            grid.addWidget(val, i, 1)
            self._fields[key] = val
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        self.clear()

    def set_stats(self, stats: PanelStats | None) -> None:
        s = stats or PanelStats()
        if s.winrate_black is None:
            self.winrate_label.setText("Win —")
            self.bar.set_winrate(None)
        else:
            self.winrate_label.setText(f"Win  B {s.winrate_black * 100:.1f}%")
            self.bar.set_winrate(s.winrate_black)
        if s.lead_black is None:
            self.lead_label.setText("Lead —")
        else:
            side = "B" if s.lead_black >= 0 else "W"
            self.lead_label.setText(f"{side}+{abs(s.lead_black):.1f}  Lead")
        self._fields["visits"].setText("—" if s.visits is None else f"{s.visits:,}")
        self._fields["stdev"].setText(
            "—" if s.score_stdev is None else f"{s.score_stdev:.1f}")
        self._fields["kl"].setText(
            "—" if s.policy_kl is None else f"{s.policy_kl:.3f}")
        self._fields["noresult"].setText(
            "—" if s.no_result is None else f"{s.no_result * 100:.2f}%")

    def clear(self) -> None:
        self.set_stats(None)
