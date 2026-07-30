# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec: a self-contained one-folder app (no Python needed).

Build (from the matching platform — PyInstaller cannot cross-compile):

    python -m PyInstaller katsura.spec

Output lands in ``dist/katsura/`` — the launcher exe next to an
``_internal/`` folder holding the bundled Python + Qt. Zip that folder to
release. See docs/PACKAGING.md.
"""

import os
import sys

a = Analysis(
    [os.path.join(SPECPATH, "packaging", "launch.py")],
    pathex=[os.path.join(SPECPATH, "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # Big libraries PyInstaller sometimes drags in via optional imports; none
    # are used by the app.
    excludes=["tkinter", "numpy", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="katsura",
    debug=False,
    strip=False,
    upx=False,
    console=False,           # windowed app: no console box on Windows
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="katsura",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Katsura.app",
        icon=None,
        bundle_identifier="org.katsura.katsura",
        info_plist={"NSHighResolutionCapable": True},
    )
