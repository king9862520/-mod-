# ONI Hub V4 Preview Preview

## Single EXE, multiple processes
Only one file is distributed: `ONIHub.exe`.

Normal launch opens the GUI. Steam synchronization launches the same EXE again with:

```text
ONIHub.exe --steam-worker --job ... --result ...
```

The GUI process never initializes SteamAPI. Only the short-lived child process uses App ID 457140.

## Settings
Use the **设置** button to configure:

- Oxygen Not Included game directory
- Steam Workshop directory (`steamapps\workshop\content\457140`)
- ONI user directory (`Documents\Klei\OxygenNotIncluded`)

Settings are stored beside the EXE in `settings.json`.

## Preset packages
V4 Preview presets are `.onihub` packages. A preset contains:

- Enabled and disabled Mod lists
- Mod load order
- Workshop IDs and static IDs
- Valid JSON configuration files discovered inside each Mod directory

Loading a preset restores the Mod state, order, and included JSON files. Existing JSON files and `mods.json` are backed up before replacement.

Use **导出** to share a preset as one `.onihub` file. Use **导入** to add and optionally load a shared preset. Missing subscribed Steam Mods can be downloaded through **刷新 / 同步订阅**.

Local presets are stored beside the EXE in:

```text
presets\*.onihub
```

## Build
Double-click `BUILD_EXE.bat`. Output:

```text
dist\ONIHub.exe
```

## V4 Preview preset behavior fix

- Saving a preset creates a snapshot only. It does not activate or apply it.
- A preset is applied only when the user explicitly clicks **Load**.
- After applying, ONI Hub immediately returns to normal unmanaged mode.
- Mods installed after a preset was created keep their current enabled state and can be moved normally.
- Importing a preset stores it first; applying it remains a separate explicit action.
