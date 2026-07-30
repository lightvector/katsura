"""Collapsible right-hand panes, stacked top-to-bottom.

The behaviour is a plain vertical stack — NOT a splitter — so panes never fight
over space:

* a **collapsed** section is just its header;
* an **expanded compact** section (Analysis Info, SGF Info) is a fixed height =
  header + content's preferred height — it is never resizable;
* an **expanded growable** section (Tree, Comments) is a fixed height that the
  user can change by dragging the thin grip directly below it; dragging resizes
  only that pane, everything below simply shifts.

A trailing stretch in the container soaks up any leftover space at the bottom,
and the whole stack lives in a scroll area, so collapsing panes leaves empty
space below (and an over-tall stack scrolls) instead of squishing anything.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QToolButton, QSizePolicy,
)


class CollapsibleSection(QWidget):
    """Header button + a body that shows/hides when the header is toggled.

    An optional ``accessory`` widget is shown at the right edge of the header
    row (used e.g. for the Tree pane's node count).
    """

    toggled = Signal(bool)

    def __init__(self, title: str, content: QWidget, *, expanded: bool = False,
                 growable: bool = False, open_height: int = 300,
                 accessory: QWidget = None, parent=None):
        super().__init__(parent)
        self.growable = growable
        self.content = content
        self.open_height = open_height
        self.grip: PaneGrip | None = None    # set by the container for growable

        self.header = QToolButton()
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.header.setAutoRaise(True)
        self.header.toggled.connect(self._on_toggled)

        self._header_row = QWidget()
        hrow = QHBoxLayout(self._header_row)
        hrow.setContentsMargins(0, 0, 6, 0)
        hrow.setSpacing(4)
        hrow.addWidget(self.header)
        hrow.addStretch(1)
        if accessory is not None:
            hrow.addWidget(accessory)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._header_row)
        lay.addWidget(self.content)
        self.content.setVisible(expanded)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._apply_height(expanded)

    def _header_h(self) -> int:
        return self._header_row.sizeHint().height()

    def _expanded_height(self) -> int:
        if self.growable:
            return max(self._min_open(), self.open_height)
        return self._header_h() + self.content.sizeHint().height()

    def _min_open(self) -> int:
        return self._header_h() + 48

    def _apply_height(self, expanded: bool) -> None:
        self.setFixedHeight(self._expanded_height() if expanded else self._header_h())

    def _on_toggled(self, on: bool) -> None:
        self.header.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.content.setVisible(on)
        self._apply_height(on)
        if self.grip is not None:
            self.grip.setVisible(on)
        self.toggled.emit(on)

    def set_open_height(self, h: int) -> None:
        """Set the expanded height of a growable section (clamped to a minimum)."""
        if not self.growable:
            return
        self.open_height = max(self._min_open(), h)
        if self.is_expanded():
            self.setFixedHeight(self.open_height)

    def set_expanded(self, on: bool) -> None:
        self.header.setChecked(on)

    def is_expanded(self) -> bool:
        return self.header.isChecked()


class PaneGrip(QWidget):
    """A thin draggable handle that resizes the growable section above it."""

    def __init__(self, section: CollapsibleSection, parent=None):
        super().__init__(parent)
        self.section = section
        self.setFixedHeight(7)
        self.setCursor(Qt.SizeVerCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._last_y: float | None = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._last_y = event.globalPosition().y()

    def mouseMoveEvent(self, event) -> None:
        if self._last_y is not None:
            y = event.globalPosition().y()
            dy = int(y - self._last_y)
            if dy:
                self.section.set_open_height(self.section.open_height + dy)
                self._last_y = y

    def mouseReleaseEvent(self, event) -> None:
        self._last_y = None

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        y = self.height() / 2
        cx = self.width() / 2
        p.setPen(QColor(120, 124, 132))
        for dx in (-9, 0, 9):
            p.drawLine(int(cx + dx - 3), int(y), int(cx + dx + 3), int(y))
