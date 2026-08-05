from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paths import APP_ID, ONIPaths

# Steamworks EItemState flags
SUBSCRIBED = 1
LEGACY_ITEM = 2
INSTALLED = 4
NEEDS_UPDATE = 8
DOWNLOADING = 16
DOWNLOAD_PENDING = 32


@dataclass(slots=True)
class SteamItem:
    item_id: str
    state: int
    install_path: Path | None = None
    size_on_disk: int = 0
    downloaded: int = 0
    total: int = 0

    @property
    def subscribed(self) -> bool:
        return bool(self.state & SUBSCRIBED)

    @property
    def installed(self) -> bool:
        return bool(self.state & INSTALLED)

    @property
    def needs_update(self) -> bool:
        return bool(self.state & NEEDS_UPDATE)


class SteamBridgeError(RuntimeError):
    pass


class SteamClientBridge:
    """Minimal ctypes wrapper over Steamworks' exported flat C API.

    It loads the steam_api64.dll shipped with Oxygen Not Included and asks the
    already-running, already-logged-in Steam client for the current account's
    subscribed Workshop items. No password or SteamCMD is used.
    """

    def __init__(self, paths: ONIPaths):
        self.paths = paths
        self.dll: ctypes.WinDLL | None = None
        self.ugc = None
        self._old_cwd: str | None = None
        self._old_appid: str | None = None
        self._old_gameid: str | None = None

    def _dll_candidates(self) -> list[Path]:
        if not self.paths.game_root:
            return []
        root = self.paths.game_root
        return [
            root / "OxygenNotIncluded_Data" / "Plugins" / "x86_64" / "steam_api64.dll",
            root / "OxygenNotIncluded_Data" / "Plugins" / "steam_api64.dll",
            root / "steam_api64.dll",
        ]

    def __enter__(self) -> "SteamClientBridge":
        if os.name != "nt":
            raise SteamBridgeError("Steam 订阅同步目前只支持 Windows。")
        dll_path = next((p for p in self._dll_candidates() if p.exists()), None)
        if not dll_path:
            raise SteamBridgeError("没有在《缺氧》目录找到 steam_api64.dll。")

        self._old_cwd = os.getcwd()
        self._old_appid = os.environ.get("SteamAppId")
        self._old_gameid = os.environ.get("SteamGameId")
        os.environ["SteamAppId"] = APP_ID
        os.environ["SteamGameId"] = APP_ID
        os.chdir(str(dll_path.parent))

        try:
            self.dll = ctypes.WinDLL(str(dll_path))
            self.dll.SteamAPI_Init.restype = ctypes.c_bool
            self.dll.SteamAPI_Init.argtypes = []
            if not self.dll.SteamAPI_Init():
                raise SteamBridgeError(
                    "Steam API 初始化失败。请确认 Steam 客户端已运行、已登录，且账号拥有《缺氧》。"
                )
            self.dll.SteamAPI_RunCallbacks.restype = None
            self.dll.SteamAPI_Shutdown.restype = None
            self.ugc = self._get_ugc_interface()
            self._bind_functions()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.dll is not None:
            try:
                self.dll.SteamAPI_Shutdown()
            except Exception:
                pass
        self.dll = None
        self.ugc = None
        if self._old_cwd:
            try:
                os.chdir(self._old_cwd)
            except OSError:
                pass
        if self._old_appid is None:
            os.environ.pop("SteamAppId", None)
        else:
            os.environ["SteamAppId"] = self._old_appid
        if self._old_gameid is None:
            os.environ.pop("SteamGameId", None)
        else:
            os.environ["SteamGameId"] = self._old_gameid

    def _get_ugc_interface(self):
        assert self.dll is not None
        for version in range(22, 13, -1):
            name = f"SteamAPI_SteamUGC_v{version:03d}"
            try:
                fn = getattr(self.dll, name)
            except AttributeError:
                continue
            fn.restype = ctypes.c_void_p
            fn.argtypes = []
            value = fn()
            if value:
                return ctypes.c_void_p(value)
        raise SteamBridgeError("steam_api64.dll 中没有找到可用的 ISteamUGC 接口。")

    def _bind_functions(self) -> None:
        assert self.dll is not None
        bindings = {
            "SteamAPI_ISteamUGC_GetNumSubscribedItems": (ctypes.c_uint32, [ctypes.c_void_p]),
            "SteamAPI_ISteamUGC_GetSubscribedItems": (
                ctypes.c_uint32,
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint32],
            ),
            "SteamAPI_ISteamUGC_GetItemState": (
                ctypes.c_uint32,
                [ctypes.c_void_p, ctypes.c_uint64],
            ),
            "SteamAPI_ISteamUGC_DownloadItem": (
                ctypes.c_bool,
                [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_bool],
            ),
            "SteamAPI_ISteamUGC_GetItemDownloadInfo": (
                ctypes.c_bool,
                [ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)],
            ),
            "SteamAPI_ISteamUGC_GetItemInstallInfo": (
                ctypes.c_bool,
                [ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)],
            ),
            "SteamAPI_ISteamUGC_SubscribeItem": (
                ctypes.c_uint64,
                [ctypes.c_void_p, ctypes.c_uint64],
            ),
            "SteamAPI_ISteamUGC_UnsubscribeItem": (
                ctypes.c_uint64,
                [ctypes.c_void_p, ctypes.c_uint64],
            ),
        }
        for name, (restype, argtypes) in bindings.items():
            try:
                fn = getattr(self.dll, name)
            except AttributeError as exc:
                raise SteamBridgeError(f"Steam API 缺少函数：{name}") from exc
            fn.restype = restype
            fn.argtypes = argtypes

    def run_callbacks(self) -> None:
        assert self.dll is not None
        self.dll.SteamAPI_RunCallbacks()

    def subscribed_ids(self) -> list[str]:
        assert self.dll is not None and self.ugc is not None
        count = int(self.dll.SteamAPI_ISteamUGC_GetNumSubscribedItems(self.ugc))
        if count <= 0:
            return []
        array = (ctypes.c_uint64 * count)()
        written = int(self.dll.SteamAPI_ISteamUGC_GetSubscribedItems(self.ugc, array, count))
        return [str(int(array[i])) for i in range(min(count, written))]

    def item_state(self, item_id: str) -> int:
        assert self.dll is not None and self.ugc is not None
        return int(self.dll.SteamAPI_ISteamUGC_GetItemState(self.ugc, ctypes.c_uint64(int(item_id))))

    def item_info(self, item_id: str) -> SteamItem:
        assert self.dll is not None and self.ugc is not None
        state = self.item_state(item_id)
        size = ctypes.c_uint64(0)
        folder = ctypes.create_string_buffer(32768)
        timestamp = ctypes.c_uint32(0)
        path: Path | None = None
        if self.dll.SteamAPI_ISteamUGC_GetItemInstallInfo(
            self.ugc, ctypes.c_uint64(int(item_id)), ctypes.byref(size), folder, len(folder), ctypes.byref(timestamp)
        ):
            raw = folder.value.decode("utf-8", errors="replace")
            if raw:
                path = Path(raw)
        downloaded = ctypes.c_uint64(0)
        total = ctypes.c_uint64(0)
        self.dll.SteamAPI_ISteamUGC_GetItemDownloadInfo(
            self.ugc, ctypes.c_uint64(int(item_id)), ctypes.byref(downloaded), ctypes.byref(total)
        )
        return SteamItem(item_id, state, path, int(size.value), int(downloaded.value), int(total.value))

    def request_download(self, item_id: str, high_priority: bool = True) -> bool:
        assert self.dll is not None and self.ugc is not None
        return bool(self.dll.SteamAPI_ISteamUGC_DownloadItem(
            self.ugc, ctypes.c_uint64(int(item_id)), ctypes.c_bool(high_priority)
        ))

    def subscribe(self, item_id: str) -> None:
        assert self.dll is not None and self.ugc is not None
        self.dll.SteamAPI_ISteamUGC_SubscribeItem(self.ugc, ctypes.c_uint64(int(item_id)))
        self._wait_subscription(item_id, True)

    def unsubscribe(self, item_id: str) -> None:
        assert self.dll is not None and self.ugc is not None
        self.dll.SteamAPI_ISteamUGC_UnsubscribeItem(self.ugc, ctypes.c_uint64(int(item_id)))
        self._wait_subscription(item_id, False)

    def _wait_subscription(self, item_id: str, expected: bool, timeout: int = 20) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.run_callbacks()
            current = bool(self.item_state(item_id) & SUBSCRIBED)
            if current == expected:
                return
            time.sleep(0.1)
        action = "订阅" if expected else "取消订阅"
        raise SteamBridgeError(f"Steam 没有在 {timeout} 秒内确认{action} Mod {item_id}。")

    def sync_subscriptions(
        self,
        target_dir: Path,
        progress: Callable[[str], None] | None = None,
        timeout_per_item: int = 300,
    ) -> dict:
        """Download missing/outdated subscriptions and mirror them to ONI's live directory."""
        from .legacy_installer import install_workshop_item

        subscribed = self.subscribed_ids()
        target_dir.mkdir(parents=True, exist_ok=True)
        local_ids = {p.name for p in target_dir.iterdir() if p.is_dir() and p.name.isdigit()}
        orphan_ids = sorted(local_ids - set(subscribed))
        missing: list[str] = []
        updates: list[str] = []
        synced: list[str] = []
        failed: dict[str, str] = {}

        for index, item_id in enumerate(subscribed, 1):
            info = self.item_info(item_id)
            target = target_dir / item_id
            if not info.installed or info.install_path is None or not info.install_path.exists():
                missing.append(item_id)
            elif info.needs_update:
                updates.append(item_id)
            elif not target.exists():
                # Steam already has it, but ONI has not mirrored it yet.
                try:
                    install_workshop_item(info.install_path, target, progress)
                    synced.append(item_id)
                except Exception as exc:
                    failed[item_id] = f"同步失败：{exc}"

            if item_id not in missing and item_id not in updates:
                continue
            if progress:
                progress(f"请求 Steam 下载 {index}/{len(subscribed)}：{item_id}")
            if not self.request_download(item_id, True):
                failed[item_id] = "Steam 拒绝了 DownloadItem 请求"
                continue
            end = time.monotonic() + timeout_per_item
            while time.monotonic() < end:
                self.run_callbacks()
                current = self.item_info(item_id)
                if current.total and progress:
                    percent = min(100, int(current.downloaded * 100 / current.total))
                    progress(f"Steam 下载 Mod {item_id}：{percent}%")
                if current.installed and not current.needs_update and current.install_path and current.install_path.exists():
                    try:
                        install_workshop_item(current.install_path, target, progress)
                        synced.append(item_id)
                    except Exception as exc:
                        failed[item_id] = f"下载完成但同步失败：{exc}"
                    break
                time.sleep(0.2)
            else:
                failed[item_id] = f"等待 Steam 下载超时（{timeout_per_item}s）"

        return {
            "subscribed": subscribed,
            "missing": missing,
            "updates": updates,
            "synced": sorted(set(synced)),
            "orphans": orphan_ids,
            "failed": failed,
        }
