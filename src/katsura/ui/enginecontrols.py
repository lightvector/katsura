"""Toolbar spin-box widgets for the per-tab engine analysis settings.

Each is a thin :class:`QDoubleSpinBox` subclass whose stepping matches what the
corresponding KataGo parameter wants:

* :class:`KomiSpinBox` — bumps by 0.5, displays without trailing zeros.
* :class:`WideRootNoiseSpinBox` — steps through the discrete
  ``analysisWideRootNoise`` values (0, 0.01, 0.04, 0.10, 0.25, 0.50, 1.0).
* :class:`PdaSpinBox` — ``playoutDoublingAdvantage`` from White's perspective;
  steps snap to the adjacent clean multiple of 0.5.

Width is derived from each box's *own* font metrics against its widest possible
text (recomputed on show, when the real font/style is in effect), so values
never clip — a flat pixel width fails under the native-Windows HiDPI font.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QMenu,
    QStyle,
    QStyleOptionSpinBox,
    QToolButton,
)

from ..engine.settings import (
    KOMI_LIMIT,
    PDA_LIMIT,
    step_pda,
    step_wide_root_noise,
)


class EngineSelectorButton(QToolButton):
    """Accessory for the Analysis Info header: which engine's analysis is shown.

    With a single attached engine it reads as a plain grey label (the engine's
    name, as before). With more than one it becomes a small drop-down: clicking
    it lists the attached engines and picking one switches the **window-wide**
    shown engine (panel, board overlays, console). Hidden when nothing is
    attached.
    """

    engineSelected = Signal(int)     # index into the window's attached list

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoRaise(True)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setFocusPolicy(Qt.NoFocus)
        # Keep the single-engine (disabled) look identical to the old label.
        self.setStyleSheet(
            "QToolButton { color: #999; border: none; padding: 0 2px; }"
            "QToolButton:disabled { color: #999; }"
            "QToolButton::menu-indicator { image: none; }")
        self._names: list[str] = []
        self._current = -1
        self._menu = QMenu(self)
        self.setVisible(False)

    def set_engines(self, names: list[str], current: int) -> None:
        """Show ``names[current]``; offer the rest via the drop-down (if >1)."""
        names = list(names)
        if names == self._names and current == self._current:
            return
        self._names = names
        self._current = current
        multi = len(names) > 1
        shown = names[current] if 0 <= current < len(names) else ""
        self.setText(f"{shown} ▾" if multi else shown)
        self._menu.clear()
        for i, name in enumerate(names):
            act = self._menu.addAction(
                name, lambda _=False, i=i: self.engineSelected.emit(i))
            act.setCheckable(True)
            act.setChecked(i == current)
        self.setMenu(self._menu if multi else None)
        self.setEnabled(multi)
        self.setVisible(bool(names))


class _FitSpinBox(QDoubleSpinBox):
    """A spin box that sizes itself to fit ``_SAMPLE`` (its widest text).

    Clicking the up/down arrows steps the value but does **not** leave the box
    focused for typing, so board-navigation hotkeys keep working. (Qt's
    windowing layer focuses the box on any click regardless of the event
    handler, so we let that happen and then release the focus on the next event
    loop turn — only for arrow clicks; a click in the number field still focuses
    it for typing.)
    """

    _SAMPLE = "0.00"
    TOOLTIP = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setKeyboardTracking(False)
        if self.TOOLTIP:
            self.setToolTip(self.TOOLTIP)

    def mousePressEvent(self, event) -> None:
        opt = QStyleOptionSpinBox()
        self.initStyleOption(opt)
        sub = self.style().hitTestComplexControl(
            QStyle.CC_SpinBox, opt, event.position().toPoint(), self)
        super().mousePressEvent(event)
        if sub in (QStyle.SC_SpinBoxUp, QStyle.SC_SpinBoxDown):
            QTimer.singleShot(0, self._release_focus)

    def _release_focus(self) -> None:
        if self.hasFocus() or self.lineEdit().hasFocus():
            self.clearFocus()

    def _refit(self) -> None:
        """Fix the width to exactly fit ``_SAMPLE`` in the current font: the
        string's advance plus Qt's 2px cursor allowance (what QAbstractSpinBox's
        own sizeHint uses), wrapped in the style's spin-box chrome via
        sizeFromContents — no flat slack, so the box is never wider than its
        most-character-containing possible value needs."""
        fm = self.fontMetrics()
        content_w = fm.horizontalAdvance(self._SAMPLE) + 2
        try:
            opt = QStyleOptionSpinBox()
            self.initStyleOption(opt)
            full = self.style().sizeFromContents(
                QStyle.CT_SpinBox, opt, QSize(content_w, fm.height()), self)
            self.setFixedWidth(full.width())
        except Exception:                       # very defensive style fallback
            self.setFixedWidth(content_w + 54)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refit()


class KomiSpinBox(_FitSpinBox):
    # Widest typable value: a sign, as many digits as the limit needs, and two
    # decimals — all '8', the widest digit in most proportional fonts. Derived
    # from KOMI_LIMIT so widening the range widens the box automatically.
    _SAMPLE = "-" + "8" * len(str(int(KOMI_LIMIT))) + ".88"
    TOOLTIP = (f"Komi the engine analyses with, in steps of 0.5 "
               f"(−{KOMI_LIMIT:.0f}…{KOMI_LIMIT:.0f}). Separate from the SGF's "
               f"own komi, which lives in the SGF Info pane")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(-KOMI_LIMIT, KOMI_LIMIT)
        self.setDecimals(2)
        self.setSingleStep(0.5)

    def textFromValue(self, v: float) -> str:
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return "0" if s in ("", "-0") else s


class WideRootNoiseSpinBox(_FitSpinBox):
    _SAMPLE = "0.00"
    TOOLTIP = ("Wide root noise, 0…1: larger values explore more moves at the "
               "position being analysed (KataGo's analysisWideRootNoise)")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0.0, 1.0)
        self.setDecimals(2)

    def stepBy(self, steps: int) -> None:
        v = self.value()
        for _ in range(abs(steps)):
            v = step_wide_root_noise(v, up=steps > 0)
        self.setValue(v)

    def textFromValue(self, v: float) -> str:
        return f"{v:.2f}"


class PdaSpinBox(_FitSpinBox):
    _SAMPLE = "-3.0"
    TOOLTIP = ("Playout doubling advantage for White, −3…3: positive analyses "
               "as if White were a stronger KataGo than Black, negative the "
               "reverse (KataGo's playoutDoublingAdvantage)")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(-PDA_LIMIT, PDA_LIMIT)
        self.setDecimals(1)
        self.setSingleStep(0.5)

    def stepBy(self, steps: int) -> None:
        v = self.value()
        for _ in range(abs(steps)):
            v = step_pda(v, up=steps > 0)
        self.setValue(v)

    def textFromValue(self, v: float) -> str:
        return f"{v:.1f}"
