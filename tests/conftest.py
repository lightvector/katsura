"""Shared pytest configuration.

Force Qt to use the offscreen platform so the GUI tests run headless, in CI or
on a developer machine, without requiring a display.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="session", autouse=True)
def settings_dir(tmp_path_factory):
    """Send every ``QSettings`` in the test run to a throwaway directory.

    Tests build ``MainWindow`` and save ``Prefs``, which otherwise write to the
    very store the installed app reads, clobbering the developer's own board
    colours, engine list and window state.
    Redirecting the scope's path sends every ``QSettings(ORG, APP)`` there,
    without a single module having to know about it. Both formats need setting:
    the two-argument constructor resolves its path through ``NativeFormat`` even
    when the default format says otherwise. On Windows ``NativeFormat`` is the
    registry, which ``setPath`` cannot redirect, so ask for INI files there —
    and if some platform still slips past all that,
    ``test_settings_never_touch_the_real_store`` fails rather than letting the
    run quietly scribble on the developer's settings.
    """
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings

    path = tmp_path_factory.mktemp("settings")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    for fmt in (QSettings.IniFormat, QSettings.NativeFormat):
        for scope in (QSettings.UserScope, QSettings.SystemScope):
            QSettings.setPath(fmt, scope, str(path))
    return path


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["pytest"])
    yield app
