from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Callable, Iterable

from .paths import APP_ID, _steam_roots


def _tokens(text: str) -> list[str]:
    return re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', text)


def _parse_vdf(text: str) -> dict:
    flat: list[str] = []
    for quoted, brace in _tokens(text):
        flat.append(bytes(quoted, "utf-8").decode("unicode_escape") if quoted else brace)
    pos = 0
    def parse_obj() -> dict:
        nonlocal pos
        out: dict = {}
        while pos < len(flat):
            token = flat[pos]
            if token == "}":
                pos += 1
                break
            key = token; pos += 1
            if pos >= len(flat): break
            value = flat[pos]; pos += 1
            out[key] = parse_obj() if value == "{" else value
        return out
    return parse_obj()


def installed_workshop_times(workshop_content: Path | None) -> dict[str, int]:
    if not workshop_content:
        return {}
    acf = workshop_content.parent.parent / f"appworkshop_{APP_ID}.acf"
    if not acf.exists():
        return {}
    try:
        root = _parse_vdf(acf.read_text(encoding="utf-8", errors="ignore"))
        app = root.get("AppWorkshop", root)
        installed = app.get("WorkshopItemsInstalled", {})
        result: dict[str, int] = {}
        if isinstance(installed, dict):
            for item_id, info in installed.items():
                if isinstance(info, dict):
                    value = info.get("timeupdated") or info.get("TimeUpdated") or 0
                    try: result[str(item_id)] = int(value)
                    except (TypeError, ValueError): pass
        return result
    except Exception:
        return {}


def folder_latest_mtime(folder: Path) -> int:
    try:
        latest = int(folder.stat().st_mtime)
        for root, _, files in os.walk(folder):
            for name in files:
                try: latest = max(latest, int((Path(root) / name).stat().st_mtime))
                except OSError: pass
        return latest
    except OSError:
        return 0


def find_steam_exe() -> Path | None:
    if os.name == "nt":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as h:
                        value, _ = winreg.QueryValueEx(h, "SteamExe" if hive == winreg.HKEY_CURRENT_USER else "InstallPath")
                        p = Path(value)
                        if p.is_dir(): p = p / "steam.exe"
                        if p.exists(): return p
                except OSError:
                    pass
        except Exception:
            pass
    for root in _steam_roots():
        p = root / "steam.exe"
        if p.exists(): return p
    return None


def ensure_steam_running(progress: Callable[[str], None] | None = None) -> Path:
    exe = find_steam_exe()
    if not exe:
        raise RuntimeError("没有找到 Steam 客户端。请先安装并登录 Steam。")
    if progress: progress("正在连接已登录的 Steam 客户端…")
    try:
        subprocess.Popen([str(exe), "-silent"], cwd=exe.parent,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        raise RuntimeError(f"无法启动 Steam 客户端：{exc}") from exc
    time.sleep(2)
    return exe


def copy_item_atomic(src: Path, dst: Path, item_id: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.parent / f".{item_id}.onihub-new"
    backup = dst.parent / f".{item_id}.onihub-backup"
    shutil.rmtree(temp, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(src, temp)
    if dst.exists(): dst.replace(backup)
    temp.replace(dst)
    shutil.rmtree(backup, ignore_errors=True)


def request_steam_client_updates(
    ids: Iterable[str],
    workshop_cache: Path,
    target_workshop: Path,
    progress: Callable[[str], None] | None = None,
    timeout_seconds: int = 600,
) -> tuple[list[str], list[str], str]:
    """Ask the logged-in Steam client to process subscribed Workshop updates.

    Steam exposes no supported per-item download API to ordinary external apps.
    ONI Hub therefore opens Steam's Downloads page, waits for subscribed content
    to change in Steam's Workshop cache, then mirrors completed items into ONI's
    Documents/Klei/.../mods/Steam directory.
    """
    numeric = [str(x) for x in dict.fromkeys(ids) if str(x).isdigit()]
    if not numeric:
        return [], [], "没有有效的 Workshop ID。"
    ensure_steam_running(progress)
    baseline_times = installed_workshop_times(workshop_cache)
    baseline_mtimes = {mid: folder_latest_mtime(workshop_cache / mid) for mid in numeric}
    target_workshop.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("已请求 Steam 检查订阅内容。请保持 Steam 在线；必要时在 Steam 下载页面点继续。")
    try:
        webbrowser.open("steam://open/downloads")
    except Exception:
        pass

    success: list[str] = []
    pending = set(numeric)
    logs: list[str] = []
    start = time.monotonic()
    while pending and time.monotonic() - start < timeout_seconds:
        current_times = installed_workshop_times(workshop_cache)
        for mid in list(pending):
            src = workshop_cache / mid
            if not src.exists() or not any(src.iterdir()):
                continue
            current_mtime = folder_latest_mtime(src)
            changed = (current_times.get(mid, 0) > baseline_times.get(mid, 0)
                       or current_mtime > baseline_mtimes.get(mid, 0))
            # If Steam already had the remote revision before this request, allow
            # syncing when the cache is newer than ONI's live copy.
            dst = target_workshop / mid
            if not changed and current_mtime <= folder_latest_mtime(dst):
                continue
            try:
                from .legacy_installer import install_workshop_item
                install_workshop_item(src, dst, progress)
                success.append(mid); pending.remove(mid)
                if progress: progress(f"已同步 {len(success)}/{len(numeric)}：Mod {mid}")
            except Exception as exc:
                logs.append(f"COPY ERROR {mid}: {exc}")
        if pending:
            if progress:
                elapsed = int(time.monotonic() - start)
                progress(f"等待 Steam 下载：{len(pending)} 个未完成（{elapsed}s / {timeout_seconds}s）")
            time.sleep(2)
    failed = sorted(pending)
    if failed:
        logs.append("Steam 客户端未在等待时间内更新这些项目：" + ", ".join(failed))
    return success, failed, "\n".join(logs)[-30000:]

# Compatibility name used by the current UI worker.
def download_workshop_items(ids, cache_dir, target_workshop, progress=None, workshop_cache=None):
    cache = workshop_cache or cache_dir
    return request_steam_client_updates(ids, Path(cache), Path(target_workshop), progress)
