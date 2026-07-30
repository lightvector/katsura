"""Dialogs for managing saved engine commands."""

from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QVBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QMessageBox,
    QFileDialog,
)

from ..engine.config import EngineConfig
from .settings import ORG, APP


class EngineEditDialog(QDialog):
    """Edit a single engine's name and shell command."""

    def __init__(self, parent=None, config: Optional[EngineConfig] = None):
        super().__init__(parent)
        self.setWindowTitle("Engine")
        self.resize(560, 260)
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit(config.name if config else "")
        self.name_edit.setPlaceholderText("e.g. KataGo (remote)")
        lay.addWidget(self.name_edit)

        lay.addWidget(QLabel(
            "Command (a shell command that speaks GTP on stdin/stdout):"))
        self.cmd_edit = QPlainTextEdit(config.command if config else "")
        self.cmd_edit.setPlaceholderText(
            "ssh host 'cd ~/katago; ./katago gtp -config gtp.cfg -model model.bin.gz'")
        lay.addWidget(self.cmd_edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _accept(self) -> None:
        if not self.cmd_edit.toPlainText().strip():
            QMessageBox.warning(self, "Engine", "The command cannot be empty.")
            return
        self.accept()

    def result_config(self) -> EngineConfig:
        name = self.name_edit.text().strip() or "Engine"
        return EngineConfig(name=name, command=self.cmd_edit.toPlainText().strip())


class EngineManagerDialog(QDialog):
    """List, add, edit and remove saved engine commands."""

    def __init__(self, parent, engines: list[EngineConfig]):
        super().__init__(parent)
        self.setWindowTitle("Manage Engines")
        self.resize(520, 360)
        self.engines = [EngineConfig(e.name, e.command) for e in engines]

        lay = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._edit())
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        for label, slot in [("Add…", self._add), ("Edit…", self._edit),
                            ("Remove", self._remove)]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        for label, slot in [("Move Up", lambda: self._move(-1)),
                            ("Move Down", lambda: self._move(1))]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        for label, slot in [("Export…", self._export), ("Import…", self._import)]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            row2.addWidget(b)
        row2.addStretch(1)
        lay.addLayout(row2)

        # Tell the user exactly where the list is persisted (QSettings — the
        # registry on Windows, an INI file under ~/.config on Linux). Export/
        # Import move it to/from a portable JSON file.
        loc = QSettings(ORG, APP).fileName()
        where = QLabel(f"Saved in: {loc}")
        where.setWordWrap(True)
        where.setStyleSheet("color: #888; font-size: 11px;")
        where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(where)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._reload()

    def _reload(self) -> None:
        self.list.clear()
        for e in self.engines:
            item = QListWidgetItem(e.name)
            item.setToolTip(e.command)
            self.list.addItem(item)

    def _add(self) -> None:
        dlg = EngineEditDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self.engines.append(dlg.result_config())
            self._reload()
            self.list.setCurrentRow(len(self.engines) - 1)

    def _edit(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self.engines)):
            return
        dlg = EngineEditDialog(self, self.engines[row])
        if dlg.exec() == QDialog.Accepted:
            self.engines[row] = dlg.result_config()
            self._reload()
            self.list.setCurrentRow(row)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.engines):
            del self.engines[row]
            self._reload()

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        new = row + delta
        if 0 <= row < len(self.engines) and 0 <= new < len(self.engines):
            self.engines[row], self.engines[new] = self.engines[new], self.engines[row]
            self._reload()
            self.list.setCurrentRow(new)

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Engines", "engines.json", "JSON files (*.json);;All files (*)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in self.engines], f, indent=2)
        except OSError as e:
            QMessageBox.critical(self, "Export failed", f"Could not write file:\n{e}")
            return
        QMessageBox.information(
            self, "Export", f"Exported {len(self.engines)} engine(s) to:\n{path}")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Engines", "", "JSON files (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Import failed", f"Could not read file:\n{e}")
            return
        if not isinstance(data, list):
            QMessageBox.critical(self, "Import failed",
                                 "Expected a JSON list of engines.")
            return
        added = 0
        for item in data:
            if isinstance(item, dict) and item.get("command"):
                self.engines.append(EngineConfig.from_dict(item))
                added += 1
        self._reload()
        if added:
            self.list.setCurrentRow(len(self.engines) - 1)
        QMessageBox.information(self, "Import",
                                f"Imported {added} engine(s) from:\n{path}")
