"""PyInstaller entry point.

Frozen builds need a plain script (``python -m katsura`` has no file for
PyInstaller to analyse). Keep this to a bare import-and-run so all real logic
stays in the package.
"""

from katsura.ui.app import main

raise SystemExit(main())
