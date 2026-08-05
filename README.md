# ONI Hub

A modern, portable Mod manager for **Oxygen Not Included**.

> Alpha software. Back up your `mods.json` and Mod configuration files before testing new builds.

## Features

- Read and manage ONI Mod enable states and load order
- Drag-and-drop ordering
- Steam Workshop subscription synchronization through an isolated worker process
- Automatic installation of `*_legacy.bin` ZIP packages into the ONI user Mod directory
- Notes, tags, favorites, preview images, and JSON configuration editing
- Portable SQLite database and settings beside the executable
- Snapshot presets with export and import support

## Repository layout

```text
src/onihub/          Application package
  app.py             PySide6 desktop UI
  worker.py          Isolated Steam worker mode
  steam_bridge.py    Steamworks bridge
  legacy_installer.py Workshop package installer
  storage.py         mods.json and Mod file persistence
  database.py        Portable SQLite storage
  paths.py           Path discovery and validation
  models.py          Shared data models

tests/               Automated tests
scripts/             Local build scripts
.github/workflows/   CI and Windows release builds
```

## Run from source

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m onihub
```

## Build the Windows executable

Double-click `BUILD_EXE.bat`, or run:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m PyInstaller --noconfirm --clean ONIHub.spec
```

The result is written to `dist\ONIHub.exe`.

## Important Steam behavior

ONI Hub launches the same executable with `--steam-worker` for Steam operations. Only that child process initializes Steam with App ID `457140`; the GUI process does not. Steam may temporarily show Oxygen Not Included as running while synchronization is active.

## Data and privacy

ONI Hub is portable. Its database, settings, cache, backups, logs, and presets are stored beside the executable. These files are excluded from Git by default.

## License

MIT License.
