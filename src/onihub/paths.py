from __future__ import annotations
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

APP_ID = "457140"


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def settings_path() -> Path:
    return application_root() / "settings.json"


def load_settings() -> dict:
    path = settings_path()
    defaults = {
        "portable_mode": True,
        "game_root": "",
        "workshop_cache": "",
        "user_root": "",
    }
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults.update({k: data.get(k, defaults[k]) for k in defaults})
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


def save_settings(data: dict) -> None:
    current = load_settings()
    current.update(data)
    settings_path().write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_portable_layout() -> dict[str, Path]:
    root = application_root()
    paths = {
        "root": root,
        "cache": root / "cache",
        "preview": root / "cache" / "preview",
        "workshop": root / "cache" / "workshop",
        "temp": root / "cache" / "temp",
        "backups": root / "backups",
        "config_backups": root / "backups" / "config",
        "mods_backups": root / "backups" / "mods",
        "logs": root / "logs",
        "plugins": root / "plugins",
    }
    for key, value in paths.items():
        if key != "root":
            value.mkdir(parents=True, exist_ok=True)
    if not settings_path().exists():
        save_settings({})
    return paths


@dataclass(slots=True)
class ONIPaths:
    user_root: Path
    mods_json: Path
    local_mods: Path
    workshop_mods: Path
    workshop_cache: Path | None
    game_root: Path | None


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value) / "Steam")
    roots += [Path("C:/Steam"), Path("D:/Steam"), Path("D:/SteamLibrary"), Path("E:/SteamLibrary"), Path("F:/SteamLibrary")]
    found: list[Path] = []
    for root in roots:
        if root.exists() and root not in found:
            found.append(root)
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            for raw in re.findall(r'"path"\s+"([^"]+)"', text):
                p = Path(raw.replace("\\\\", "\\"))
                if p.exists() and p not in found:
                    found.append(p)
    return found


def auto_detect_paths() -> tuple[Path | None, Path | None, Path]:
    docs = Path.home() / "Documents"
    user_root = docs / "Klei" / "OxygenNotIncluded"
    game_root = None
    workshop_cache = None
    for root in _steam_roots():
        candidate = root / "steamapps/common/OxygenNotIncluded"
        if candidate.exists() and game_root is None:
            game_root = candidate
        candidate_cache = root / f"steamapps/workshop/content/{APP_ID}"
        if candidate_cache.exists() and workshop_cache is None:
            workshop_cache = candidate_cache
    return game_root, workshop_cache, user_root


def normalize_game_root(path: Path) -> Path:
    if path.is_file():
        return path.parent
    return path


def normalize_workshop_root(path: Path) -> Path:
    path = path.resolve()
    if path.name == APP_ID and path.parent.name.lower() == "content":
        return path
    candidates = [
        path / f"steamapps/workshop/content/{APP_ID}",
        path / f"workshop/content/{APP_ID}",
        path / f"content/{APP_ID}",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return path


def validate_paths(game_root: Path | None, workshop_cache: Path | None, user_root: Path) -> list[str]:
    errors: list[str] = []
    if not game_root or not game_root.exists():
        errors.append("缺氧游戏目录不存在")
    elif not ((game_root / "OxygenNotIncluded.exe").exists() or (game_root / "OxygenNotIncluded_Data").exists()):
        errors.append("游戏目录中未找到 OxygenNotIncluded.exe 或 OxygenNotIncluded_Data")
    if not workshop_cache or not workshop_cache.exists():
        errors.append(f"Workshop 目录不存在（应为 content\\{APP_ID}）")
    elif workshop_cache.name != APP_ID:
        errors.append(f"Workshop 目录应指向 ...\\steamapps\\workshop\\content\\{APP_ID}")
    if not user_root.exists():
        errors.append("缺氧用户目录不存在")
    return errors


def discover_paths() -> ONIPaths:
    detected_game, detected_workshop, detected_user = auto_detect_paths()
    settings = load_settings()

    game_root = Path(settings["game_root"]).expanduser() if str(settings.get("game_root", "")).strip() else detected_game
    workshop_cache = Path(settings["workshop_cache"]).expanduser() if str(settings.get("workshop_cache", "")).strip() else detected_workshop
    user_root = Path(settings["user_root"]).expanduser() if str(settings.get("user_root", "")).strip() else detected_user

    if game_root:
        game_root = normalize_game_root(game_root)
    if workshop_cache:
        workshop_cache = normalize_workshop_root(workshop_cache)

    mods_root = user_root / "mods"
    return ONIPaths(
        user_root=user_root,
        mods_json=mods_root / "mods.json",
        local_mods=mods_root / "Local",
        workshop_mods=mods_root / "Steam",
        workshop_cache=workshop_cache,
        game_root=game_root,
    )
