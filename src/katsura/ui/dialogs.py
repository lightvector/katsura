"""Small modal dialogs: New Game and Preferences."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QVBoxLayout,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QPushButton,
    QHBoxLayout,
    QLabel,
)
from PySide6.QtGui import QColor

from ..go.board import MAX_BOARD_SIZE
from ..engine.settings import (
    KataRules, KO_RULES, SCORING_RULES, TAX_RULES,
    WHITE_HANDICAP_BONUS_RULES, PRESET_BUTTONS, PRESETS,
)
from .settings import Prefs


class NewGameDialog(QDialog):
    def __init__(self, parent=None, default_size: int = 19):
        super().__init__(parent)
        self.setWindowTitle("New game")
        form = QFormLayout(self)
        # Not named .width/.height: those are QWidget methods, and shadowing
        # them makes any dlg.width() call blow up on a QSpinBox.
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, MAX_BOARD_SIZE)
        self.width_spin.setValue(default_size)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(1, MAX_BOARD_SIZE)
        self.height_spin.setValue(default_size)
        self.link = QCheckBox("Square (link width/height)")
        self.link.setChecked(True)
        self.link.toggled.connect(self._sync)
        self.width_spin.valueChanged.connect(self._on_width)
        form.addRow("Width (columns):", self.width_spin)
        form.addRow("Height (rows):", self.height_spin)
        form.addRow(self.link)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._sync(True)

    def _on_width(self, v):
        if self.link.isChecked():
            self.height_spin.setValue(v)

    def _sync(self, on):
        self.height_spin.setEnabled(not on)
        if on:
            self.height_spin.setValue(self.width_spin.value())

    def result_size(self) -> tuple[int, int]:
        return self.width_spin.value(), self.height_spin.value()


class _ColorButton(QPushButton):
    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self._color = color
        self.setFixedWidth(60)
        self.clicked.connect(self._pick)
        self._refresh()

    def _refresh(self):
        self.setStyleSheet(f"background-color: {self._color};")
        self.setText(self._color)

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self, "Choose colour")
        if c.isValid():
            self._color = c.name()
            self._refresh()

    def color(self) -> str:
        return self._color


def _group_label(text: str, first: bool = False) -> QLabel:
    """A bold heading row inside the preferences form."""
    lab = QLabel(text)
    lab.setStyleSheet(
        "font-weight: bold;" + ("" if first else " margin-top: 8px;"))
    return lab


class PreferencesDialog(QDialog):
    """Display/behaviour preferences, grouped by what they affect."""

    def __init__(self, prefs: Prefs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self._prefs = prefs
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.show_coords = QCheckBox()
        self.show_coords.setChecked(prefs.show_coordinates)
        self.show_numbers = QCheckBox()
        self.show_numbers.setChecked(prefs.show_move_numbers)
        self.show_last = QCheckBox()
        self.show_last.setChecked(prefs.show_last_move_marker)
        self.show_hints = QCheckBox()
        self.show_hints.setChecked(prefs.show_variation_hints)
        self.centered_tree = QCheckBox()
        self.centered_tree.setChecked(prefs.centered_tree)
        self.forbid_multi_suicide = QCheckBox()
        self.forbid_multi_suicide.setChecked(prefs.forbid_multi_suicide)
        self.forbid_simple_ko = QCheckBox()
        self.forbid_simple_ko.setChecked(prefs.forbid_simple_ko)
        self.show_overlay = QCheckBox()
        self.show_overlay.setChecked(prefs.show_analysis_overlay)
        self.show_pv_hover = QCheckBox()
        self.show_pv_hover.setChecked(prefs.show_pv_on_hover)

        self.page_step = QSpinBox()
        self.page_step.setRange(2, 100)
        self.page_step.setValue(prefs.page_step)
        self.default_size = QSpinBox()
        self.default_size.setRange(1, MAX_BOARD_SIZE)
        self.default_size.setValue(prefs.default_board_size)

        # Analysis: drop candidates below this fraction of the top weight.
        self.min_weight = QDoubleSpinBox()
        self.min_weight.setRange(0.0, 100.0)
        self.min_weight.setDecimals(2)
        self.min_weight.setSingleStep(0.1)
        self.min_weight.setSuffix(" %")
        self.min_weight.setValue(prefs.analysis_min_weight * 100.0)
        self.min_weight.setToolTip(
            "Moves the search has spent less than this share of its top move's "
            "weight on are left off the board entirely — circle and numbers "
            "both. The engine's own choice is always shown.")

        self.min_label_weight = QDoubleSpinBox()
        self.min_label_weight.setRange(0.0, 100.0)
        self.min_label_weight.setDecimals(2)
        self.min_label_weight.setSingleStep(0.1)
        self.min_label_weight.setSuffix(" %")
        self.min_label_weight.setValue(prefs.analysis_min_label_weight * 100.0)
        self.min_label_weight.setToolTip(
            "Below this share the move still gets its circle, but no numbers "
            "— so the board shows where the search looked without a wall of "
            "unreadable text")

        # Analysis reporting period (kata-analyze interval), shown in ms.
        self.analysis_interval = QSpinBox()
        self.analysis_interval.setRange(50, 2000)
        self.analysis_interval.setSingleStep(10)
        self.analysis_interval.setSuffix(" ms")
        self.analysis_interval.setValue(prefs.analysis_interval_cs * 10)

        self.board_color = _ColorButton(prefs.board_color)
        self.bg_color = _ColorButton(prefs.background_color)

        form.addRow(_group_label("Board", first=True))
        form.addRow("Show coordinates:", self.show_coords)
        form.addRow("Show move numbers:", self.show_numbers)
        form.addRow("Mark last move:", self.show_last)
        form.addRow("Show variation hints:", self.show_hints)
        form.addRow("Centre current line in tree:", self.centered_tree)
        form.addRow("Board colour:", self.board_color)
        form.addRow("Background colour:", self.bg_color)

        form.addRow(_group_label("Editing"))
        form.addRow("Moves per Page Up/Down:", self.page_step)
        form.addRow("Default board size:", self.default_size)
        form.addRow("Forbid multi-stone self-capture on play:",
                    self.forbid_multi_suicide)
        form.addRow("Forbid simple ko on play:", self.forbid_simple_ko)

        form.addRow(_group_label("Analysis"))
        form.addRow("Show move analysis info:", self.show_overlay)
        form.addRow("Show PV on hover:", self.show_pv_hover)
        form.addRow("Hide analysis moves below (% of top weight):",
                    self.min_weight)
        form.addRow("Hide analysis numbers below (% of top weight):",
                    self.min_label_weight)
        form.addRow("Analysis reporting period:", self.analysis_interval)

        lay.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def apply_to(self, prefs: Prefs) -> None:
        prefs.show_coordinates = self.show_coords.isChecked()
        prefs.show_move_numbers = self.show_numbers.isChecked()
        prefs.show_last_move_marker = self.show_last.isChecked()
        prefs.show_variation_hints = self.show_hints.isChecked()
        prefs.centered_tree = self.centered_tree.isChecked()
        prefs.forbid_multi_suicide = self.forbid_multi_suicide.isChecked()
        prefs.forbid_simple_ko = self.forbid_simple_ko.isChecked()
        prefs.show_analysis_overlay = self.show_overlay.isChecked()
        prefs.show_pv_on_hover = self.show_pv_hover.isChecked()
        prefs.page_step = self.page_step.value()
        prefs.default_board_size = self.default_size.value()
        prefs.analysis_min_weight = self.min_weight.value() / 100.0
        prefs.analysis_min_label_weight = self.min_label_weight.value() / 100.0
        prefs.analysis_interval_cs = max(1, round(self.analysis_interval.value() / 10))
        prefs.board_color = self.board_color.color()
        prefs.background_color = self.bg_color.color()


# Display labels for each rules field (value -> shown text).
_KO_LABELS = {"SIMPLE": "Simple", "POSITIONAL": "Positional (superko)",
              "SITUATIONAL": "Situational (superko)"}
_SCORING_LABELS = {"AREA": "Area", "TERRITORY": "Territory"}
_TAX_LABELS = {"NONE": "None", "SEKI": "Seki", "ALL": "All"}
_WHB_LABELS = {"0": "None", "N-1": "N − 1 points", "N": "N points"}


def _combo(values, labels) -> QComboBox:
    box = QComboBox()
    for v in values:
        box.addItem(labels[v], v)
    return box


class RulesDialog(QDialog):
    """Configure the full KataGo ruleset the engine analyses under."""

    def __init__(self, rules: KataRules, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Engine rules")

        lay = QVBoxLayout(self)

        # Checkable preset buttons: the one whose ruleset exactly matches the
        # current fields is shown selected — on open and as the fields change —
        # exactly as if it had just been clicked.
        presets = QHBoxLayout()
        presets.addWidget(QLabel("Presets:"))
        self._preset_buttons: dict[str, QPushButton] = {}
        for label, key in PRESET_BUTTONS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self._apply_preset(k))
            self._preset_buttons[key] = btn
            presets.addWidget(btn)
        presets.addStretch(1)
        lay.addLayout(presets)

        form = QFormLayout()
        self.ko = _combo(KO_RULES, _KO_LABELS)
        self.scoring = _combo(SCORING_RULES, _SCORING_LABELS)
        self.tax = _combo(TAX_RULES, _TAX_LABELS)
        self.suicide = QCheckBox("Allow multi-stone self-capture")
        self.button = QCheckBox("Button (area scoring only)")
        self.whb = _combo(WHITE_HANDICAP_BONUS_RULES, _WHB_LABELS)
        self.whb.setToolTip(
            "Bonus points White gets in handicap games, where N is the number "
            "of Black handicap stones")
        self.friendly_pass = QCheckBox("Passing before capturing all dead "
                                       "stones is OK")
        self.friendly_pass.setToolTip(
            "When unchecked, dead stones have to be captured on the board "
            "before either side may pass")
        self.scoring.currentIndexChanged.connect(self._sync_button_enabled)
        for w in (self.ko, self.scoring, self.tax, self.whb):
            w.currentIndexChanged.connect(self._sync_preset_buttons)
        for w in (self.suicide, self.button, self.friendly_pass):
            w.toggled.connect(self._sync_preset_buttons)
        form.addRow("Scoring:", self.scoring)
        form.addRow("Ko:", self.ko)
        form.addRow("Tax:", self.tax)
        form.addRow("Self-capture:", self.suicide)
        form.addRow("Button:", self.button)
        form.addRow("White handicap bonus:", self.whb)
        form.addRow("Friendly pass:", self.friendly_pass)
        lay.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._load(rules)

    def _load(self, rules: KataRules) -> None:
        self.ko.setCurrentIndex(self.ko.findData(rules.ko))
        self.scoring.setCurrentIndex(self.scoring.findData(rules.scoring))
        self.tax.setCurrentIndex(self.tax.findData(rules.tax))
        self.suicide.setChecked(rules.suicide)
        self.button.setChecked(rules.has_button)
        self.whb.setCurrentIndex(self.whb.findData(rules.white_handicap_bonus))
        self.friendly_pass.setChecked(rules.friendly_pass_ok)
        self._sync_button_enabled()
        self._sync_preset_buttons()

    def _sync_button_enabled(self) -> None:
        self.button.setEnabled(self.scoring.currentData() == "AREA")

    def _sync_preset_buttons(self) -> None:
        """Check the preset button (if any) whose ruleset exactly equals the
        current dialog state. Field edits that leave a preset uncheck it.

        Matched per button (not via preset_name), since some non-button presets
        (korean, chinese-ogs/-kgs) duplicate a button preset's ruleset.
        """
        current = self.result_rules()
        for key, btn in self._preset_buttons.items():
            btn.setChecked(PRESETS[key].normalized() == current)

    def _apply_preset(self, key: str) -> None:
        self._load(PRESETS[key])

    def result_rules(self) -> KataRules:
        return KataRules(
            ko=self.ko.currentData(),
            scoring=self.scoring.currentData(),
            tax=self.tax.currentData(),
            suicide=self.suicide.isChecked(),
            has_button=self.button.isChecked(),
            white_handicap_bonus=self.whb.currentData(),
            friendly_pass_ok=self.friendly_pass.isChecked(),
        ).normalized()
