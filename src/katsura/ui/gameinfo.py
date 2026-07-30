"""The body of the 'SGF Info' pane: a compact grid of root-node metadata edits.

This holds the SGF's own root properties, including its **SGF komi** (``KM``),
which may be unspecified. The SGF komi is distinct from the *engine* komi on the
toolbar — the engine komi is merely initialised from it on load and then edited
independently. The collapsible header/toggle is supplied by
:class:`CollapsibleSection`.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QGridLayout, QLineEdit, QLabel

from ..model.game import format_komi_value

# (SGF property, display label). Paired two-per-row in the grid below.
FIELDS = [
    ("PB", "Black"), ("BR", "Rank"),
    ("PW", "White"), ("WR", "Rank"),
    ("RE", "Result"), ("RU", "Rules"),
    ("DT", "Date"), ("EV", "Event"),
    ("RO", "Round"), ("PC", "Place"),
    ("HA", "Handicap"), ("GN", "Game"),
]


def _fmt_komi(value) -> str:
    """The SGF komi as text (blank when unspecified), formatted as it is stored."""
    return "" if value is None else format_komi_value(value)


class SgfInfoWidget(QWidget):
    """A grid of root-property line edits (no header — wrap in a section)."""

    fieldEdited = Signal(str, str)   # (prop, value) on commit

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._edits: dict[str, QLineEdit] = {}

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 2, 6, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)

        # SGF komi (KM) gets its own first row; it may be left blank (unspecified).
        self.komi_edit = QLineEdit()
        self.komi_edit.setClearButtonEnabled(True)
        self.komi_edit.setPlaceholderText("unspecified")
        self.komi_edit.editingFinished.connect(lambda: self._on_commit("KM"))
        grid.addWidget(QLabel("Komi:"), 0, 0)
        grid.addWidget(self.komi_edit, 0, 1)

        for i, (prop, label) in enumerate(FIELDS):
            row, col = divmod(i, 2)
            row += 1                       # row 0 is the komi field
            lab = QLabel(label + ":")
            edit = QLineEdit()
            edit.setClearButtonEnabled(True)
            edit.editingFinished.connect(lambda p=prop: self._on_commit(p))
            self._edits[prop] = edit
            grid.addWidget(lab, row, col * 2)
            grid.addWidget(edit, row, col * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

    def load(self, game) -> None:
        self._loading = True
        self.komi_edit.setText(_fmt_komi(game.get_komi()))
        for prop, edit in self._edits.items():
            edit.setText(game.get_info(prop))
        self._loading = False

    def _on_commit(self, prop: str) -> None:
        if self._loading:
            return
        edit = self.komi_edit if prop == "KM" else self._edits[prop]
        self.fieldEdited.emit(prop, edit.text())
