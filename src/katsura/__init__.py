"""Katsura - a high-quality graphical SGF editor for the game of Go."""

__version__ = "0.1.0"

# The QSettings identity (organisation, application) every persisted setting
# lives under. It sits at the package root, not under ui/, so that any
# subpackage — the engine included — can reach it without depending on the GUI.
ORG = "katsura"
APP = "katsura"
