from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .paths import ONIPaths
from .steam_bridge import SteamClientBridge


def _path(value):
    return Path(value) if value else None


def load_paths(data: dict) -> ONIPaths:
    return ONIPaths(
        user_root=Path(data["user_root"]),
        mods_json=Path(data["mods_json"]),
        local_mods=Path(data["local_mods"]),
        workshop_mods=Path(data["workshop_mods"]),
        workshop_cache=_path(data.get("workshop_cache")),
        game_root=_path(data.get("game_root")),
    )


def emit(message: str) -> None:
    print(json.dumps({"type": "progress", "message": message}, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steam-worker", action="store_true")
    parser.add_argument("--job", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    job_path = Path(args.job)
    result_path = Path(args.result)
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
        paths = load_paths(job["paths"])
        operation = job.get("operation")
        with SteamClientBridge(paths) as steam:
            if operation == "sync":
                result = steam.sync_subscriptions(paths.workshop_mods, emit)
            elif operation == "subscribe":
                item_id = str(job["item_id"])
                emit(f"正在订阅 Mod {item_id}…")
                steam.subscribe(item_id)
                steam.request_download(item_id, True)
                result = {"operation": operation, "item_id": item_id}
            elif operation == "unsubscribe":
                item_id = str(job["item_id"])
                emit(f"正在取消订阅 Mod {item_id}…")
                steam.unsubscribe(item_id)
                result = {"operation": operation, "item_id": item_id}
            else:
                raise ValueError(f"Unsupported worker operation: {operation}")
        payload = {"ok": True, "result": result}
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception as exc:
        payload = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
        try:
            result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        print(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
