"""Saved engine commands, persisted via QSettings.

An "engine" here is just a human-readable name plus an arbitrary shell command
that speaks GTP on stdin/stdout. The command can be anything — a local
``katago gtp -config ... -model ...`` or a remote ``ssh host 'cd ...; ./katago
gtp ...'``. The user can save any number of these and launch any one of them.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings

from .. import APP, ORG


@dataclass
class EngineConfig:
    """A named, launchable GTP engine command."""

    name: str
    command: str

    def to_dict(self) -> dict:
        return {"name": self.name, "command": self.command}

    @classmethod
    def from_dict(cls, d: dict) -> "EngineConfig":
        return cls(name=str(d.get("name", "")), command=str(d.get("command", "")))


def load_engines() -> list[EngineConfig]:
    """Load the saved engine list (empty if none configured yet)."""
    s = QSettings(ORG, APP)
    raw = s.value("engines/list")
    if not raw:
        return []
    out: list[EngineConfig] = []
    for item in raw:
        if isinstance(item, dict) and item.get("command"):
            out.append(EngineConfig.from_dict(item))
    return out


def save_engines(engines: list[EngineConfig]) -> None:
    """Persist the engine list."""
    s = QSettings(ORG, APP)
    s.setValue("engines/list", [e.to_dict() for e in engines])
    s.sync()
