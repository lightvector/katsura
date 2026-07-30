"""Application entry point."""

from __future__ import annotations

import ctypes.util
import os
import sys


def _select_platform() -> None:
    """Pick a Qt platform plugin on Linux/WSL.

    Under WSLg on a HiDPI display the two Qt backends each trade off:

    * **xcb** (XWayland): menus, mouse cursor and window decorations all behave,
      but WSLg runs XWayland at a 1x framebuffer that the compositor upscales, so
      rendering is a little soft. The framebuffer size is fixed by WSLg, so this
      can't be sharpened from inside the app.
    * **wayland**: renders at the display's true scale (crisp), but on WSLg Qt's
      Wayland popups desync (menu-bar items can mis-click), the mouse cursor is
      oversized, and disabling client-side decorations removes the title bar.

    The menu mis-click makes Wayland unsafe for normal use, so we default to xcb
    (functional). To trade crispness for those quirks, run with
    ``QT_QPA_PLATFORM=wayland`` explicitly. See the README.
    """
    if sys.platform != "linux":
        return
    if os.environ.get("QT_QPA_PLATFORM"):
        return                      # respect an explicit user override
    if os.environ.get("DISPLAY"):
        needed = ["xkbcommon-x11", "xcb-cursor"]
        if all(ctypes.util.find_library(name) for name in needed):
            os.environ["QT_QPA_PLATFORM"] = "xcb"
            return
    if os.environ.get("WAYLAND_DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "wayland"  # no xcb available; better than nothing


_select_platform()

from PySide6.QtCore import Qt  # noqa: E402  (after platform setup)
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from .mainwindow import MainWindow  # noqa: E402
from .. import APP, ORG  # noqa: E402
from .settings import Prefs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # Crisper rendering on fractional-DPI displays: don't round the scale factor.
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    app = QApplication(argv)
    app.setApplicationName(APP)
    app.setOrganizationName(ORG)

    prefs = Prefs().load()
    # With file arguments, start with no Untitled tab — just the opened files.
    paths = [p for p in argv[1:] if p.lower().endswith((".sgf", ".sgfs"))]
    window = MainWindow(prefs, initial_tab=not paths)
    window.show()

    for path in paths:
        window.open_path(path)
    # Nothing opened (bad file, or the user declined a multi-game .sgfs):
    # fall back to the usual fresh Untitled tab.
    if paths and window.tabs.count() == 0:
        window.new_game(prefs.default_board_size)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
