"""Allow ``python -m katsura`` to launch the GUI."""

from .ui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
