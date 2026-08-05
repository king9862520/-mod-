from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .models import ModEntry
from .paths import ONIPaths, application_root

IMAGE_NAMES = (
    "preview.png", "Preview.png", "preview.jpg", "preview.jpeg", "preview.webp",
    "icon.png", "Icon.png", "thumb.png", "thumbnail.png",
)

# The user's game is running Spaced Out. ONI itself writes enabled mods as:
#   "enabled": false,
#   "enabledForDlc": ["EXPANSION1_ID"]
# Do not invent DLC IDs and do not set the legacy `enabled` flag to true.
ACTIVE_DLC_ID = "EXPANSION1_ID"


def _yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _metadata(folder: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for filename in ("mod.yaml", "mod_info.yaml", "modinfo.yaml"):
        path = folder / filename
        if path.exists():
            merged.update(_yaml(path))
    return merged


def _preview(folder: Path) -> Path | None:
    for name in IMAGE_NAMES:
        p = folder / name
        if p.is_file():
            return p
    for p in folder.glob("*.png"):
        if "preview" in p.name.lower() or "icon" in p.name.lower():
            return p
    return None


def load_config(path: Path) -> tuple[Any, list[dict[str, Any]]]:
    if not path.exists():
        return {"version": 1, "mods": [], "mod_load_in_progress": False}, []
    try:
        root = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"version": 1, "mods": [], "mod_load_in_progress": False}, []
    if isinstance(root, list):
        return root, [x for x in root if isinstance(x, dict)]
    if isinstance(root, dict):
        mods = root.get("mods", [])
        return root, [x for x in mods if isinstance(x, dict)] if isinstance(mods, list) else []
    return {"version": 1, "mods": [], "mod_load_in_progress": False}, []


def _item_keys(item: dict[str, Any]) -> list[str]:
    """Return every identifier ONI may use, with staticID first."""
    keys: list[str] = []
    label = item.get("label") if isinstance(item.get("label"), dict) else {}
    for value in (
        item.get("staticID"),
        item.get("staticId"),
        label.get("id"),
        item.get("id"),
        item.get("mod_id"),
    ):
        if value is None:
            continue
        text = str(value)
        if text and text not in keys:
            keys.append(text)
    return keys


def _is_enabled(item: dict[str, Any]) -> bool:
    """Read enablement exactly as ONI stores it for Spaced Out."""
    enabled_for_dlc = item.get("enabledForDlc", [])
    return (
        isinstance(enabled_for_dlc, list)
        and ACTIVE_DLC_ID in enabled_for_dlc
    )


def scan_mods(paths: ONIPaths) -> tuple[list[ModEntry], Any]:
    root, items = load_config(paths.mods_json)

    config_map: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    for index, item in enumerate(items):
        for key in _item_keys(item):
            config_map.setdefault(key, item)
            order.setdefault(key, index)

    found: dict[str, ModEntry] = {}

    # ONI loads these real user directories, not Steam's workshop cache.
    for source, base in (("Local", paths.local_mods), ("Steam", paths.workshop_mods)):
        if not base or not base.exists():
            continue
        for folder in base.iterdir():
            if not folder.is_dir() or folder.name.startswith("."):
                continue

            meta = _metadata(folder)
            folder_id = folder.name
            static_id = str(meta.get("staticID") or meta.get("staticId") or meta.get("id") or folder_id)
            workshop_id = folder_id if source == "Steam" and folder_id.isdigit() else ""

            cfg: dict[str, Any] = {}
            # staticID is the authoritative identity. Workshop/folder ID is a fallback.
            for candidate in (static_id, workshop_id, folder_id):
                if candidate and candidate in config_map:
                    cfg = config_map[candidate]
                    break

            label = cfg.get("label") if isinstance(cfg.get("label"), dict) else {}
            title = str(label.get("title") or meta.get("title") or meta.get("name") or folder.name)
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [x.strip() for x in tags.split(",") if x.strip()]

            public_id = workshop_id or folder_id
            found[f"{source}:{folder_id}"] = ModEntry(
                mod_id=public_id,
                title=title,
                source=source,
                path=folder,
                # config_id now always means ONI staticID, never Workshop ID.
                config_id=static_id,
                enabled=_is_enabled(cfg),
                description=str(label.get("description") or meta.get("description") or ""),
                version=str(meta.get("version") or meta.get("minimumSupportedBuild") or ""),
                author=str(meta.get("author") or meta.get("creator") or ""),
                preview=_preview(folder),
                workshop_url=(
                    f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
                    if workshop_id else ""
                ),
                tags=list(tags) if isinstance(tags, list) else [],
                raw_config=cfg,
            )

    def sort_key(mod: ModEntry):
        keys = [mod.config_id, mod.mod_id, mod.path.name]
        index = min((order[k] for k in keys if k in order), default=999999)
        return index, mod.title.casefold()

    return sorted(found.values(), key=sort_key), root


def backup_config(path: Path) -> Path | None:
    if not path.exists():
        return None
    folder = application_root() / "backups" / "mods"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"mods-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    shutil.copy2(path, target)
    return target


def _new_record(mod: ModEntry) -> dict[str, Any]:
    label_id = mod.mod_id or mod.path.name or mod.config_id
    return {
        "label": {
            "distribution_platform": 1 if mod.source == "Steam" else 0,
            "id": label_id,
            "title": mod.title,
            "version": 0,
        },
        "status": 1,
        "enabled": False,
        "enabledForDlc": [],
        "crash_count": 0,
        "reinstall_path": None,
        "staticID": mod.config_id or label_id,
    }


def _set_enabled_exact(item: dict[str, Any], enabled: bool) -> None:
    """Mutate only the Spaced Out DLC membership in ONI's existing record."""
    # ONI keeps this false for the user's current DLC configuration.
    item["enabled"] = False

    current = item.get("enabledForDlc", [])
    dlcs = [str(x) for x in current] if isinstance(current, list) else []

    if enabled:
        if ACTIVE_DLC_ID not in dlcs:
            dlcs.append(ACTIVE_DLC_ID)
    else:
        dlcs = [dlc for dlc in dlcs if dlc != ACTIVE_DLC_ID]

    item["enabledForDlc"] = dlcs


def save_config(path: Path, original_root: Any, mods: list[ModEntry]) -> Path | None:
    """Update existing ONI records and persist the Hub load order.

    Existing record dictionaries are preserved byte-for-field through normal JSON
    serialization; only enabledForDlc/enabled are mutated. Records unknown to the
    current scan are retained after the ordered known records.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_config(path)

    # Always reload the latest on-disk file immediately before saving. This
    # prevents an older UI snapshot from overwriting changes made by ONI/Steam.
    output, output_items = load_config(path)
    output = copy.deepcopy(output)
    if isinstance(output, dict):
        items = output.get("mods", [])
        if not isinstance(items, list):
            raise ValueError("mods.json has no valid mods array")
    elif isinstance(output, list):
        items = output
    else:
        raise ValueError("Unsupported mods.json root format")

    # Match only existing records. Never create or delete records.
    index_by_key: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        for key in _item_keys(item):
            index_by_key.setdefault(key, index)

    ordered_records: list[Any] = []
    used_indices: set[int] = set()
    for mod in mods:
        match_index: int | None = None
        for key in (mod.config_id, mod.mod_id, mod.path.name):
            if key and key in index_by_key:
                match_index = index_by_key[key]
                break
        if match_index is None or match_index in used_indices:
            # ONI owns record creation. Skipping is safer than producing a
            # record the game may reject and then rebuilding the whole file.
            continue

        item = items[match_index]
        if isinstance(item, dict):
            _set_enabled_exact(item, mod.enabled)
        ordered_records.append(item)
        used_indices.add(match_index)

    # Keep records that are not represented by a currently installed Mod.
    ordered_records.extend(item for index, item in enumerate(items) if index not in used_indices)
    if isinstance(output, dict):
        output["mods"] = ordered_records
    else:
        output = ordered_records

    # Preserve all top-level values while writing the reordered mods array.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    return backup


def restore_latest(path: Path) -> Path | None:
    folder = path.parent / "onihub_backups"
    backups = sorted(folder.glob("mods-*.json"), reverse=True) if folder.exists() else []
    if not backups:
        return None
    shutil.copy2(backups[0], path)
    return backups[0]
