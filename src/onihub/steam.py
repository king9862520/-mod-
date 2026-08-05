from __future__ import annotations
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

API = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"


def fetch_details(ids: Iterable[str], timeout: int = 20) -> dict[str, dict]:
    numeric = [x for x in dict.fromkeys(ids) if x.isdigit()]
    result: dict[str, dict] = {}
    for start in range(0, len(numeric), 100):
        chunk = numeric[start:start + 100]
        payload: dict[str, str] = {"itemcount": str(len(chunk))}
        for i, mod_id in enumerate(chunk):
            payload[f"publishedfileids[{i}]"] = mod_id
        request = urllib.request.Request(
            API,
            data=urllib.parse.urlencode(payload).encode("ascii"),
            headers={"User-Agent": "ONI Hub/1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        for item in data.get("response", {}).get("publishedfiledetails", []):
            pid = str(item.get("publishedfileid", ""))
            if pid:
                result[pid] = item
    return result


def download(url: str, target: Path, timeout: int = 20) -> Path | None:
    if not url:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ONI Hub/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            target.write_bytes(response.read())
        return target
    except Exception:
        return None
