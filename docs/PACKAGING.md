# Packaging & release builds

How Katsura becomes a standalone download-unzip-run app. Users need **no
Python, no venv, nothing pre-installed**: PyInstaller bundles a private Python
runtime + PySide6 + the app into one folder with a launcher executable.

## Artifacts

One-folder builds (launcher exe + an `_internal/` directory), zipped:

| platform | zip contents | run |
|----------|--------------|-----|
| Windows | `katsura/katsura.exe` + `_internal/` | double-click `katsura.exe` |
| Linux | `katsura/katsura` + `_internal/` | `./katsura` |
| macOS | `Katsura.app` | double-click the app |

One-folder (not one-file) is deliberate: instant startup and far fewer
antivirus false positives; "unzip and run" is the distribution story.

## Local builds

The spec is `katsura.spec` at the repo root; output goes to `dist/`.
PyInstaller cannot cross-compile — each platform builds on itself:

```bash
pip install pyinstaller
python -m PyInstaller katsura.spec --noconfirm
```

Use `--distpath dist/windows --workpath build/windows` (etc.) to keep
platforms' outputs apart when building both from the same tree. Zip the
resulting `dist/katsura` folder. Expect a bundle on the order of 100–200 MB
unzipped and roughly half that zipped — nearly all of it the bundled Python
runtime and Qt, so the exact figure tracks the PySide6 and PyInstaller versions
rather than anything about this app.

Smoke test: launch the built exe (`QT_QPA_PLATFORM=offscreen
dist/.../katsura` works headless on Linux) and check it stays up.

## Engines

Nothing engine-related is bundled. An engine is any saved shell command
speaking GTP; on Windows the frozen app routes commands through
`wsl.exe bash -lc` (`GtpEngine._shell_argv`), so ssh/bash engine commands work
unchanged.

## CI releases (GitHub Actions)

`.github/workflows/release.yml`: pushing a tag `v*` builds all three platforms — the only way to get macOS
builds without a Mac — runs the test suite on the Linux leg as a release gate,
and attaches the zips to a **draft** GitHub Release for review.
`workflow_dispatch` builds artifacts without releasing. Notes:

* Linux builds on `ubuntu-24.04` → needs glibc ≥ 2.39 (2024+ distros) at
  runtime. Supporting older distros means building in an older container.
* macOS builds on `macos-latest` → Apple Silicon only. Intel needs a second
  matrix entry on an Intel runner.
* Keep the `version` in `pyproject.toml` in sync with the tag by hand.

## Known friction (applies to any unsigned indie app)

* **Windows SmartScreen**: first run of a downloaded exe shows "Windows
  protected your PC" → More info → Run anyway. Fix = paid code-signing cert.
* **macOS Gatekeeper**: downloaded unsigned apps need right-click → Open the
  first time (or `xattr -d com.apple.quarantine`). Fix = Apple Developer ID
  signing + notarization ($99/yr).

## Future polish (not done)

* An app icon (`icon=` in the spec + window icon in `ui/app.py`).
* Real installers wrapping the same folders (Inno Setup MSI, DMG, AppImage)
  → Start-menu entries and `.sgf` file association.
* Windows version-info resource on the exe.
