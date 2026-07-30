"""A simple GTP console for typing raw commands at the shown engine.

The console is **per-engine**: it displays the transcript (commands, responses,
stderr) of the currently *shown* engine, and its Engine drop-down is tied to the
same window-wide selection as the Analysis Info header selector — switching it
switches the panel and board overlays too. While the console is open, live
analysis of the shown engine is paused (that engine holds its current board
state), so the user can adjust parameters or run debug commands and inspect the
results, then close the console to resume analysis.

The window owns the per-engine transcripts (on each controller); this widget is
display-only: the window echoes commands, appends responses/log lines for the
shown engine, and repopulates the output wholesale when the selection changes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class GtpConsole(QWidget):
    """A non-modal console window: engine selector + transcript + input."""

    commandEntered = Signal(str)
    engineSelected = Signal(int)     # user picked an engine in the drop-down
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("GTP Console")
        self.resize(620, 460)
        self._names: list[str] = []
        self._current = -1

        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Engine:"))
        self.engine_combo = QComboBox()
        self.engine_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        # `activated` fires only on user interaction, so programmatic
        # set_engines() updates never loop back into the window.
        self.engine_combo.activated.connect(self.engineSelected.emit)
        top.addWidget(self.engine_combo)
        top.addStretch(1)
        lay.addLayout(top)

        lay.addWidget(QLabel(
            "Live analysis of the shown engine is paused while this console "
            "is open."))

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Monospace", 9))
        lay.addWidget(self.output, 1)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a GTP command and press Enter…")
        self.input.returnPressed.connect(self._submit)
        send = QPushButton("Send")
        send.clicked.connect(self._submit)
        row.addWidget(self.input, 1)
        row.addWidget(send)
        lay.addLayout(row)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        # No local echo: the window records "> cmd" in the engine's transcript
        # and appends it here, so the display always matches the transcript.
        self.commandEntered.emit(text)

    def set_engines(self, names: list[str], current: int) -> None:
        """Repopulate the selector (no signals; mirrors the app-wide selection)."""
        names = list(names)
        if names == self._names and current == self._current:
            return
        self._names = names
        self._current = current
        self.engine_combo.blockSignals(True)
        self.engine_combo.clear()
        for name in names:
            self.engine_combo.addItem(name)
        if 0 <= current < len(names):
            self.engine_combo.setCurrentIndex(current)
        self.engine_combo.blockSignals(False)
        self.engine_combo.setEnabled(len(names) > 1)

    def set_transcript(self, lines: list[str]) -> None:
        """Replace the output with an engine's full transcript."""
        self.output.setPlainText("\n".join(lines))
        self.output.moveCursor(QTextCursor.End)

    def append_line(self, text: str) -> None:
        self.output.appendPlainText(text)
        self.output.moveCursor(QTextCursor.End)

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)
