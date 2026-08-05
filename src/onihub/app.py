from __future__ import annotations
import html
import json
import os
import subprocess
import sys
import time
import webbrowser
import configparser
import shutil
import zipfile
import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QThread, Signal, QTimer, QMimeData
from PySide6.QtGui import QIcon, QPixmap, QDrag, QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSplitter,
    QStatusBar, QVBoxLayout, QWidget, QInputDialog, QDialog, QDialogButtonBox,
    QCheckBox, QProgressDialog, QPlainTextEdit, QFileDialog, QFormLayout, QListView, QMenu,
    QGroupBox, QTabWidget
)

from .models import ModEntry
from .paths import (APP_ID, discover_paths, ensure_portable_layout, application_root,
                    load_settings, save_settings, auto_detect_paths, normalize_game_root,
                    normalize_workshop_root, validate_paths)
from .steam import fetch_details, download
from .storage import restore_latest, save_config, scan_mods
from .workshop import installed_workshop_times, folder_latest_mtime, request_steam_client_updates
from .steam_bridge import SteamClientBridge, SteamBridgeError
from .database import Database
from .indexer import discover_json_configs, score_config

ROLE_MOD = Qt.ItemDataRole.UserRole

class SteamWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str)
    def __init__(self, ids: list[str]):
        super().__init__(); self.ids = ids
    def run(self):
        try: self.completed.emit(fetch_details(self.ids))
        except Exception as exc: self.failed.emit(str(exc))


class DownloadWorker(QThread):
    completed = Signal(list, list, str)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, ids: list[str], workshop_cache: Path, target: Path):
        super().__init__()
        self.ids = ids
        self.workshop_cache = workshop_cache
        self.target = target

    def run(self):
        try:
            ok, failed, log = request_steam_client_updates(
                self.ids, self.workshop_cache, self.target, self.progress.emit
            )
            self.completed.emit(ok, failed, log)
        except Exception as exc:
            self.failed.emit(str(exc))


class SteamProcessWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, paths, operation: str, item_id: str | None = None):
        super().__init__()
        self.paths = paths
        self.operation = operation
        self.item_id = item_id

    @staticmethod
    def _worker_command(job_file: Path, result_file: Path) -> list[str]:
        root = application_root()
        if getattr(sys, "frozen", False):
            # Single EXE, two processes. The child enters worker mode through argv.
            return [sys.executable, "--steam-worker", "--job", str(job_file), "--result", str(result_file)]
        return [sys.executable, "-m", "onihub", "--steam-worker", "--job", str(job_file), "--result", str(result_file)]

    def run(self):
        layout = ensure_portable_layout()
        temp_dir = layout["temp"]
        token = f"steam-{os.getpid()}-{int(time.time() * 1000)}-{id(self)}"
        job_file = temp_dir / f"{token}.job.json"
        result_file = temp_dir / f"{token}.result.json"
        data = {
            "operation": self.operation,
            "item_id": self.item_id,
            "paths": {
                "user_root": str(self.paths.user_root),
                "mods_json": str(self.paths.mods_json),
                "local_mods": str(self.paths.local_mods),
                "workshop_mods": str(self.paths.workshop_mods),
                "workshop_cache": str(self.paths.workshop_cache) if self.paths.workshop_cache else None,
                "game_root": str(self.paths.game_root) if self.paths.game_root else None,
            },
        }
        try:
            job_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            command = self._worker_command(job_file, result_file)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
            )
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("type") == "progress":
                        self.progress.emit(str(event.get("message", "Steam 同步中…")))
                    elif event.get("type") == "error":
                        self.progress.emit(str(event.get("message", "Steam Worker 发生错误")))
                except json.JSONDecodeError:
                    self.progress.emit(line)
            code = process.wait()
            if not result_file.exists():
                raise RuntimeError(f"Steam Worker 未返回结果（退出代码 {code}）。")
            payload = json.loads(result_file.read_text(encoding="utf-8"))
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error") or "Steam Worker 操作失败。")
            self.completed.emit(payload.get("result") or {})
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            for path in (job_file, result_file):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


class SubscriptionSyncWorker(SteamProcessWorker):
    def __init__(self, paths):
        super().__init__(paths, "sync")


class SteamActionWorker(SteamProcessWorker):
    completed_action = Signal(str, str)

    def __init__(self, paths, action: str, item_id: str):
        super().__init__(paths, action, item_id)
        self.action = action
        self.item_id = item_id
        self.completed.connect(self._forward)

    def _forward(self, result: dict):
        self.completed_action.emit(str(result.get("operation", self.action)), str(result.get("item_id", self.item_id)))


class UpdatesDialog(QDialog):
    download_requested = Signal(list)

    def __init__(self, mods: list[ModEntry], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Steam Mod 更新（{len(mods)}）")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        title = QLabel(f"检测到 {len(mods)} 个 Steam Workshop Mod 有更新")
        title.setObjectName("detailTitle")
        layout.addWidget(title)
        note = QLabel("勾选需要更新的 Mod，然后请求已登录的 Steam 客户端下载。ONI Hub 会等待并同步完成的项目。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for mod in mods:
            item = QListWidgetItem()
            item.setData(ROLE_MOD, mod)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            remote = datetime.fromtimestamp(mod.time_updated).strftime("%Y-%m-%d %H:%M") if mod.time_updated else "未知"
            local = datetime.fromtimestamp(mod.local_time_updated).strftime("%Y-%m-%d %H:%M") if mod.local_time_updated else "未知"
            item.setText(f"{mod.title}   [ID {mod.mod_id}]\n本地：{local}    Steam：{remote}")
            item.setSizeHint(QSize(650, 58))
            self.list.addItem(item)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        all_btn = QPushButton("全选")
        none_btn = QPushButton("全不选")
        download_btn = QPushButton("用 Steam 更新所选")
        download_btn.setObjectName("primary")
        close_btn = QPushButton("稍后")
        all_btn.clicked.connect(lambda: self._check_all(True))
        none_btn.clicked.connect(lambda: self._check_all(False))
        download_btn.clicked.connect(self._download)
        close_btn.clicked.connect(self.reject)
        row.addWidget(all_btn); row.addWidget(none_btn); row.addStretch(); row.addWidget(download_btn); row.addWidget(close_btn)
        layout.addLayout(row)

    def _check_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def _download(self):
        ids = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(item.data(ROLE_MOD).mod_id)
        if not ids:
            QMessageBox.information(self, "未选择", "请至少选择一个 Mod。")
            return
        self.download_requested.emit(ids)
        self.accept()

class ModCard(QWidget):
    def __init__(self, mod: ModEntry):
        super().__init__()
        self.setObjectName("modCard")
        row = QHBoxLayout(self); row.setContentsMargins(10, 8, 10, 8); row.setSpacing(10)
        icon = QLabel(); icon.setFixedSize(52, 52); icon.setObjectName("thumb")
        self._set_image(icon, mod.preview)
        text = QVBoxLayout(); text.setSpacing(2)
        title = QLabel(mod.display_title); title.setObjectName("cardTitle"); title.setWordWrap(False)
        if mod.alias.strip():
            meta_text = f"原名：{mod.title}  ·  {mod.subtitle}"
        else:
            meta_text = mod.subtitle
        meta = QLabel(meta_text); meta.setObjectName("cardMeta")
        ident = QLabel(f"ID: {mod.mod_id}"); ident.setObjectName("cardId")
        text.addWidget(title); text.addWidget(meta); text.addWidget(ident)
        row.addWidget(icon); row.addLayout(text, 1)
        if mod.orphaned:
            badge = QLabel("已退订残留"); badge.setObjectName("updateBadge"); row.addWidget(badge)
        elif mod.update_available:
            badge = QLabel("有更新"); badge.setObjectName("updateBadge"); row.addWidget(badge)
        elif mod.favorite:
            badge = QLabel("★ 收藏"); badge.setObjectName("updateBadge"); row.addWidget(badge)
        elif mod.all_tags:
            badge = QLabel(mod.all_tags[0][:12]); badge.setObjectName("badge"); row.addWidget(badge)
    @staticmethod
    def _set_image(label: QLabel, path: Path | None):
        if path and path.exists():
            pix = QPixmap(str(path))
            if not pix.isNull():
                label.setPixmap(pix.scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)); return
        label.setText("MOD"); label.setAlignment(Qt.AlignmentFlag.AlignCenter)

class ModList(QListWidget):
    changed = Signal()
    mods_dropped = Signal(object, int, bool)
    MIME_TYPE = "application/x-onihub-mods"

    def __init__(self, enabled: bool):
        super().__init__()
        self.enabled_target = enabled
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSpacing(4)
        self.setUniformItemSizes(True)

    @staticmethod
    def _key(mod: ModEntry) -> str:
        return str(mod.config_id or mod.mod_id)

    def startDrag(self, supported_actions):
        selected = sorted(self.selectedItems(), key=self.row)
        if not selected and self.currentItem() is not None:
            selected = [self.currentItem()]
        keys = [self._key(item.data(ROLE_MOD)) for item in selected if item.data(ROLE_MOD)]
        if not keys:
            return
        mime = QMimeData()
        mime.setData(self.MIME_TYPE, json.dumps(keys, ensure_ascii=False).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(self.MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(self.MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(self.MIME_TYPE):
            event.ignore()
            return
        try:
            keys = json.loads(bytes(event.mimeData().data(self.MIME_TYPE)).decode("utf-8"))
        except Exception:
            event.ignore()
            return
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item is None:
            insert_row = self.count()
        else:
            insert_row = self.row(item)
            rect = self.visualItemRect(item)
            if pos.y() > rect.center().y():
                insert_row += 1
        self.mods_dropped.emit(keys, insert_row, self.enabled_target)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()


def discover_config_files(mod: ModEntry, mods_root: Path | None = None) -> list[Path]:
    """Recursively find valid JSON files at any depth inside the Mod folder."""
    return discover_json_configs(mod)


class ConfigEditorDialog(QDialog):
    def __init__(self, mod: ModEntry, parent=None):
        super().__init__(parent)
        self.mod = mod
        self.mods_root = mod.path
        self.files = discover_config_files(mod)
        self.current_path: Path | None = None
        self.original_text = ""
        self.setWindowTitle(f"编辑 Mod 配置 - {mod.display_title}")
        self.resize(980, 680)
        outer=QVBoxLayout(self)
        tip=QLabel("递归扫描当前 Mod 文件夹所有层级的有效 JSON；疑似配置排在前面。保存前自动备份并校验格式。")
        tip.setWordWrap(True); outer.addWidget(tip)
        split=QSplitter(Qt.Orientation.Horizontal)
        left=QFrame(); ll=QVBoxLayout(left); ll.addWidget(QLabel("检测到的配置文件"))
        self.file_list=QListWidget(); ll.addWidget(self.file_list,1)
        browse=QPushButton("手动选择配置文件…"); browse.clicked.connect(self._browse); ll.addWidget(browse)
        open_folder=QPushButton("打开配置目录"); open_folder.clicked.connect(self._open_folder); ll.addWidget(open_folder)
        right=QFrame(); rl=QVBoxLayout(right)
        self.path_label=QLabel("未选择文件"); self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); self.path_label.setWordWrap(True); rl.addWidget(self.path_label)
        self.editor=QPlainTextEdit(); self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap); rl.addWidget(self.editor,1)
        actions=QHBoxLayout()
        reload_btn=QPushButton("重新载入"); reload_btn.clicked.connect(self._reload)
        validate_btn=QPushButton("检查格式"); validate_btn.clicked.connect(self._validate_message)
        restore_btn=QPushButton("恢复最近备份"); restore_btn.clicked.connect(self._restore_backup)
        save_btn=QPushButton("保存配置"); save_btn.setObjectName("primary"); save_btn.clicked.connect(self._save)
        close_btn=QPushButton("关闭"); close_btn.clicked.connect(self.reject)
        for b in (reload_btn,validate_btn,restore_btn): actions.addWidget(b)
        actions.addStretch(); actions.addWidget(save_btn); actions.addWidget(close_btn); rl.addLayout(actions)
        split.addWidget(left); split.addWidget(right); split.setSizes([300,680]); outer.addWidget(split,1)
        for path in self.files:
            item=QListWidgetItem(str(path.relative_to(mod.path) if path.is_relative_to(mod.path) else path)); item.setData(Qt.ItemDataRole.UserRole,str(path)); self.file_list.addItem(item)
        self.file_list.currentItemChanged.connect(self._select_file)
        if self.file_list.count(): self.file_list.setCurrentRow(0)
        else:
            self.editor.setPlaceholderText("此 Mod 没有检测到可编辑的 JSON 配置文件。")

    def _select_file(self,item,*_):
        if item: self._load(Path(item.data(Qt.ItemDataRole.UserRole)))
    def _load(self,path:Path):
        try:
            text=path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text=path.read_text(encoding="gb18030",errors="replace")
        except Exception as exc:
            QMessageBox.critical(self,"读取失败",str(exc)); return
        self.current_path=path; self.original_text=text; self.path_label.setText(str(path)); self.editor.setPlainText(text)
    def _reload(self):
        if self.current_path: self._load(self.current_path)
    def _browse(self):
        start=str(self.mod.path)
        name,_=QFileDialog.getOpenFileName(self,"选择当前 Mod 的 JSON 配置",start,"JSON 配置 (*.json)")
        if name: self._load(Path(name))
    def _open_folder(self):
        target=self.current_path.parent if self.current_path else self.mod.path
        if target.exists():
            os.startfile(str(target)) if sys.platform.startswith("win") else subprocess.Popen(["xdg-open",str(target)])
    def _validate(self,text:str,path:Path):
        if path.suffix.lower() != ".json":
            raise ValueError("只支持 JSON 配置文件")
        json.loads(text)
    def _validate_message(self):
        if not self.current_path: return
        try: self._validate(self.editor.toPlainText(),self.current_path)
        except Exception as exc: QMessageBox.critical(self,"格式错误",str(exc))
        else: QMessageBox.information(self,"格式正确","当前配置文件格式检查通过。")
    def _backup_dir(self,path:Path)->Path:
        # Keep backups inside the portable ONI Hub folder, grouped by Mod path.
        try:
            mod_key = self.mod.config_id or self.mod.mod_id or self.mod.path.name
        except Exception:
            mod_key = "unknown"
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(mod_key))
        d = application_root() / "backups" / "config" / safe
        d.mkdir(parents=True, exist_ok=True)
        return d
    def _save(self):
        if not self.current_path: QMessageBox.warning(self,"未选择文件","请先选择配置文件。"); return
        text=self.editor.toPlainText()
        try: self._validate(text,self.current_path)
        except Exception as exc:
            QMessageBox.critical(self,"无法保存",f"配置格式检查失败：\n{exc}"); return
        try:
            stamp=datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup=self._backup_dir(self.current_path)/(self.current_path.name+f".{stamp}.bak")
            if self.current_path.exists(): shutil.copy2(self.current_path,backup)
            tmp=self.current_path.with_name(self.current_path.name+".onihub.tmp")
            tmp.write_text(text,encoding="utf-8"); tmp.replace(self.current_path)
            self.original_text=text
            QMessageBox.information(self,"保存成功",f"已保存：\n{self.current_path}\n\n备份：\n{backup}")
        except Exception as exc: QMessageBox.critical(self,"保存失败",str(exc))
    def _restore_backup(self):
        if not self.current_path: return
        backups=sorted(self._backup_dir(self.current_path).glob(self.current_path.name+".*.bak"),key=lambda p:p.stat().st_mtime,reverse=True)
        if not backups: QMessageBox.information(self,"没有备份","没有找到该配置文件的 ONI Hub 备份。"); return
        try: shutil.copy2(backups[0],self.current_path); self._load(self.current_path); QMessageBox.information(self,"恢复完成",f"已恢复：{backups[0].name}")
        except Exception as exc: QMessageBox.critical(self,"恢复失败",str(exc))




class SettingsDialog(QDialog):
    paths_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ONI Hub 设置")
        self.resize(780, 360)
        layout = QVBoxLayout(self)
        intro = QLabel("路径会保存到 ONIHub.exe 同目录的 settings.json。可手动设置，也可以重新自动检测。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        group = QGroupBox("本地路径")
        form = QFormLayout(group)
        self.game_edit = QLineEdit()
        self.workshop_edit = QLineEdit()
        self.user_edit = QLineEdit()
        form.addRow("缺氧游戏目录", self._path_row(self.game_edit, self._browse_game))
        form.addRow("Steam 工坊目录", self._path_row(self.workshop_edit, self._browse_workshop))
        form.addRow("缺氧用户目录", self._path_row(self.user_edit, self._browse_user))
        layout.addWidget(group)

        hint = QLabel(
            f"工坊目录应选择：...\\steamapps\\workshop\\content\\{APP_ID}\n"
            "用户目录通常是：Documents\\Klei\\OxygenNotIncluded"
        )
        hint.setObjectName("pathline")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        detect = QPushButton("自动检测")
        detect.clicked.connect(self._auto_detect)
        check = QPushButton("检查路径")
        check.clicked.connect(self._validate_message)
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(detect); row.addWidget(check); row.addStretch(); row.addWidget(save); row.addWidget(cancel)
        layout.addLayout(row)
        self._load()

    def _path_row(self, edit: QLineEdit, callback):
        box = QWidget(); row = QHBoxLayout(box); row.setContentsMargins(0,0,0,0)
        row.addWidget(edit, 1)
        button = QPushButton("浏览…"); button.clicked.connect(callback); row.addWidget(button)
        return box

    def _load(self):
        paths = discover_paths()
        self.game_edit.setText(str(paths.game_root or ""))
        self.workshop_edit.setText(str(paths.workshop_cache or ""))
        self.user_edit.setText(str(paths.user_root))

    def _auto_detect(self):
        game, workshop, user = auto_detect_paths()
        self.game_edit.setText(str(game or ""))
        self.workshop_edit.setText(str(workshop or ""))
        self.user_edit.setText(str(user))
        self._validate_message()

    def _browse_game(self):
        path = QFileDialog.getExistingDirectory(self, "选择缺氧游戏目录", self.game_edit.text())
        if path: self.game_edit.setText(path)

    def _browse_workshop(self):
        path = QFileDialog.getExistingDirectory(self, "选择 Steam Workshop content\\457140 目录", self.workshop_edit.text())
        if path: self.workshop_edit.setText(path)

    def _browse_user(self):
        path = QFileDialog.getExistingDirectory(self, "选择缺氧用户目录", self.user_edit.text())
        if path: self.user_edit.setText(path)

    def _values(self):
        game = normalize_game_root(Path(self.game_edit.text().strip())) if self.game_edit.text().strip() else None
        workshop = normalize_workshop_root(Path(self.workshop_edit.text().strip())) if self.workshop_edit.text().strip() else None
        user = Path(self.user_edit.text().strip()) if self.user_edit.text().strip() else Path.home() / "Documents/Klei/OxygenNotIncluded"
        return game, workshop, user

    def _validate_message(self):
        errors = validate_paths(*self._values())
        if errors:
            QMessageBox.warning(self, "路径检查", "发现问题：\n\n" + "\n".join(f"• {x}" for x in errors))
        else:
            QMessageBox.information(self, "路径检查", "三个目录均有效。")

    def _save(self):
        game, workshop, user = self._values()
        errors = validate_paths(game, workshop, user)
        if errors:
            answer = QMessageBox.question(
                self, "路径可能无效",
                "以下路径未通过检查：\n\n" + "\n".join(f"• {x}" for x in errors) + "\n\n仍然保存吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        save_settings({
            "game_root": str(game or ""),
            "workshop_cache": str(workshop or ""),
            "user_root": str(user),
        })
        self.paths_changed.emit()
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ONI Hub V4 Preview - Oxygen Not Included Mod Center")
        self.resize(1450, 880)
        self.paths = discover_paths(); self.mods: list[ModEntry] = []; self.root_config = {"version": 1, "mods": []}
        self.portable = ensure_portable_layout()
        self.app_dir = self.portable["root"]
        self.cache_dir = self.portable["workshop"]
        self.db = Database(self.app_dir / "onihub.db")
        self.meta_cache_file = self.cache_dir / "workshop_cache.json"
        self.alias_file = self.app_dir / "mod_aliases.json"
        self.install_state_file = self.cache_dir / "installed_workshop.json"
        self.preview_cache = self.portable["preview"]
        self.worker: SteamWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self.update_dialog: UpdatesDialog | None = None
        self.subscription_worker: SubscriptionSyncWorker | None = None
        self.steam_action_worker: SteamActionWorker | None = None
        self.dirty = False
        self.subscribed_ids: set[str] = set()
        self.orphan_ids: set[str] = set()
        self._ui(); self.refresh()
        QTimer.singleShot(900, self.check_updates)

    def _ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 10, 12, 8)
        outer.setSpacing(10)

        # Compact command bar: frequent actions first, secondary tools grouped right.
        toolbar = QHBoxLayout()
        self.refresh_btn = QPushButton("⟳  刷新 / 同步订阅")
        self.refresh_btn.setObjectName("toolbarPrimary")
        self.refresh_btn.clicked.connect(self.refresh_subscriptions)
        toolbar.addWidget(self.refresh_btn)
        rescan = QPushButton("本地重扫")
        rescan.clicked.connect(self.refresh)
        toolbar.addWidget(rescan)
        update_all = QPushButton("↓  更新全部")
        update_all.clicked.connect(self.check_updates)
        toolbar.addWidget(update_all)
        preset_btn = QPushButton("预设管理…")
        preset_btn.clicked.connect(self.open_preset_manager)
        toolbar.addWidget(preset_btn)
        restore = QPushButton("恢复备份")
        restore.clicked.connect(self.restore)
        toolbar.addWidget(restore)
        toolbar.addStretch()
        openmods = QPushButton("打开 Mods")
        openmods.clicked.connect(self.open_mods)
        toolbar.addWidget(openmods)
        settings = QPushButton("⚙ 设置")
        settings.clicked.connect(self.open_settings)
        toolbar.addWidget(settings)
        launch = QPushButton("▶ 启动游戏")
        launch.setObjectName("primary")
        launch.clicked.connect(self.launch)
        toolbar.addWidget(launch)
        outer.addLayout(toolbar)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称、ID、作者、备注、标签或 StaticID…")
        self.search.textChanged.connect(self.filter)
        self.source = QComboBox()
        self.source.addItems(["全部来源", "Steam", "Local", "Missing"])
        self.source.currentTextChanged.connect(self.filter)
        self.view_filter = QComboBox()
        self.view_filter.addItems(["全部 Mod", "收藏", "有配置", "有更新", "已退订残留"])
        self.view_filter.currentTextChanged.connect(self.filter)
        # Preset combo remains as the model for the dedicated manager dialog.
        self.preset = QComboBox()
        self._load_preset_names()
        filters.addWidget(self.search, 1)
        filters.addWidget(self.source)
        filters.addWidget(self.view_filter)
        outer.addLayout(filters)

        self.summary_line = QLabel("正在扫描 Mod…")
        self.summary_line.setObjectName("summaryLine")
        outer.addWidget(self.summary_line)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.disabled = ModList(False)
        self.enabled = ModList(True)
        for lst in (self.disabled, self.enabled):
            lst.changed.connect(self.changed)
            lst.mods_dropped.connect(self.handle_mod_drop)
            lst.itemDoubleClicked.connect(self.toggle_item)
            lst.currentItemChanged.connect(self._selection_changed)
            lst.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            lst.customContextMenuRequested.connect(lambda pos, w=lst: self.show_mod_context_menu(w, pos))
        split.addWidget(self._list_panel("未启用 Mods", self.disabled))
        split.addWidget(self._switch_panel())
        split.addWidget(self._list_panel("已启用 Mods（拖动调整顺序）", self.enabled))
        split.addWidget(self._details())
        split.setSizes([350, 105, 440, 530])
        outer.addWidget(split, 1)

        # Persistent save bar: impossible to miss after changing enable state/order.
        save_bar = QFrame()
        save_bar.setObjectName("saveBar")
        save_layout = QHBoxLayout(save_bar)
        save_layout.setContentsMargins(12, 7, 12, 7)
        self.save_state = QLabel("✓ 当前列表已保存")
        self.save_state.setObjectName("savedState")
        self.save_btn = QPushButton("✓ 已保存")
        self.save_btn.setObjectName("saveClean")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save)
        save_layout.addWidget(self.save_state)
        save_layout.addStretch()
        save_layout.addWidget(QLabel("Ctrl+S"))
        save_layout.addWidget(self.save_btn)
        outer.addWidget(save_bar)

        self.pathline = QLabel()
        self.pathline.setObjectName("pathline")
        self.pathline.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.pathline)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.setStyleSheet(STYLE)

        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.search.setFocus)

    def _list_panel(self,title,lst):
        frame=QFrame(); frame.setObjectName("panel"); lay=QVBoxLayout(frame); h=QLabel(title); h.setObjectName("panelTitle"); lay.addWidget(h); lay.addWidget(lst); return frame

    def _switch_panel(self):
        frame=QFrame(); frame.setObjectName("switchPanel")
        lay=QVBoxLayout(frame); lay.setContentsMargins(8, 8, 8, 8); lay.addStretch()
        enable=QPushButton("启用  →"); enable.setObjectName("primary"); enable.clicked.connect(self.enable_selected); lay.addWidget(enable)
        disable=QPushButton("←  禁用"); disable.clicked.connect(self.disable_selected); lay.addWidget(disable)
        lay.addSpacing(16)
        enable_all=QPushButton("全部启用"); enable_all.clicked.connect(self.enable_all); lay.addWidget(enable_all)
        disable_all=QPushButton("全部禁用"); disable_all.clicked.connect(self.disable_all); lay.addWidget(disable_all)
        lay.addStretch()
        return frame

    def _selection_changed(self, current, previous):
        sender=self.sender()
        other=self.enabled if sender is self.disabled else self.disabled
        if current is not None:
            other.clearSelection(); other.setCurrentItem(None)
            self.show_details(current)

    def _move_selected(self, source, target, enabled):
        rows=sorted({source.row(i) for i in source.selectedItems()}, reverse=True)
        if not rows and source.currentItem() is not None:
            rows=[source.row(source.currentItem())]
        if not rows:
            QMessageBox.information(self, "未选择 Mod", "请先选择一个或多个 Mod。")
            return
        moved=[]
        for row in rows:
            item=source.item(row)
            if item is not None:
                moved.append(item.data(ROLE_MOD))
                source.takeItem(row)
        for mod in reversed(moved):
            mod.enabled=enabled; self._add(target,mod)
        self.changed()

    def enable_selected(self): self._move_selected(self.disabled,self.enabled,True)
    def disable_selected(self): self._move_selected(self.enabled,self.disabled,False)
    def enable_all(self):
        self.disabled.selectAll(); self.enable_selected()
    def disable_all(self):
        self.enabled.selectAll(); self.disable_selected()

    @staticmethod
    def _mod_key(mod: ModEntry) -> str:
        return str(mod.config_id or mod.mod_id)

    def _list_mods(self, widget: QListWidget) -> list[ModEntry]:
        return [widget.item(i).data(ROLE_MOD) for i in range(widget.count())]

    def handle_mod_drop(self, keys: list[str], target_index: int, target_enabled: bool):
        """Move one or many Mods inside either list or across both lists."""
        key_set = {str(k) for k in keys}
        enabled_before = self._list_mods(self.enabled)
        disabled_before = self._list_mods(self.disabled)
        source_all = enabled_before + disabled_before
        moved = [m for m in source_all if self._mod_key(m) in key_set]
        if not moved:
            return

        target_before = enabled_before if target_enabled else disabled_before
        # The drop index is measured before removing dragged rows.  Adjust it so
        # internal multi-row moves land exactly where the indicator was shown.
        removed_before = sum(
            1 for i, m in enumerate(target_before)
            if i < target_index and self._mod_key(m) in key_set
        )
        target_index = max(0, target_index - removed_before)

        enabled_after = [m for m in enabled_before if self._mod_key(m) not in key_set]
        disabled_after = [m for m in disabled_before if self._mod_key(m) not in key_set]
        target = enabled_after if target_enabled else disabled_after
        target_index = min(target_index, len(target))
        for m in moved:
            m.enabled = target_enabled
        target[target_index:target_index] = moved

        # self.mods stores enabled order first, then disabled order.  populate()
        # preserves the independent order of both panes.
        self.mods = enabled_after + disabled_after
        self.populate()

        destination = self.enabled if target_enabled else self.disabled
        for i in range(destination.count()):
            item = destination.item(i)
            if self._mod_key(item.data(ROLE_MOD)) in key_set:
                item.setSelected(True)
        if destination.selectedItems():
            destination.setCurrentItem(destination.selectedItems()[0])
        self.changed()

    def _details(self):
        frame=QFrame(); frame.setObjectName("panel"); lay=QVBoxLayout(frame); h=QLabel("Mod 详情"); h.setObjectName("panelTitle"); lay.addWidget(h)
        self.big_preview=QLabel("无预览图"); self.big_preview.setObjectName("bigPreview"); self.big_preview.setMinimumHeight(270); self.big_preview.setAlignment(Qt.AlignmentFlag.AlignCenter); lay.addWidget(self.big_preview)
        self.detail_title=QLabel("请选择一个 Mod"); self.detail_title.setObjectName("detailTitle"); self.detail_title.setWordWrap(True); lay.addWidget(self.detail_title)
        self.detail_meta=QLabel(); self.detail_meta.setWordWrap(True); self.detail_meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); lay.addWidget(self.detail_meta)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame); self.description=QLabel("双击 Mod 可以在启用与未启用之间切换。"); self.description.setWordWrap(True); self.description.setAlignment(Qt.AlignmentFlag.AlignTop); self.description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); scroll.setWidget(self.description); lay.addWidget(scroll,1)
        row=QHBoxLayout(); aliasb=QPushButton("备注名"); aliasb.clicked.connect(self.set_alias); tagsb=QPushButton("标签"); tagsb.clicked.connect(self.set_tags); self.favorite_btn=QPushButton("☆ 收藏"); self.favorite_btn.clicked.connect(self.toggle_favorite); self.config_btn=QPushButton("无配置"); self.config_btn.setObjectName("primary"); self.config_btn.setEnabled(False); self.config_btn.clicked.connect(self.edit_config); openf=QPushButton("打开文件夹"); openf.clicked.connect(self.open_selected); web=QPushButton("Workshop"); web.clicked.connect(self.open_workshop); row.addWidget(aliasb); row.addWidget(tagsb); row.addWidget(self.favorite_btn); row.addWidget(self.config_btn); row.addWidget(openf); row.addWidget(web); lay.addLayout(row); return frame

    def open_preset_manager(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("预设管理（快照，仅点击载入时应用）")
        dialog.resize(620, 260)
        layout = QVBoxLayout(dialog)
        note = QLabel("预设是一次性快照。保存或选择预设不会锁定当前 Mod；只有点击“载入”才会应用列表、顺序和配置。")
        note.setWordWrap(True)
        layout.addWidget(note)
        combo = QComboBox()
        combo.setMinimumWidth(360)
        for i in range(self.preset.count()):
            combo.addItem(self.preset.itemText(i))
        combo.setCurrentText(self.preset.currentText())
        layout.addWidget(combo)
        row = QHBoxLayout()
        actions = [
            ("载入一次", self.load_preset), ("保存当前快照", self.save_preset),
            ("导出分享", self.export_preset), ("导入", self.import_preset),
            ("删除", self.delete_preset),
        ]
        def call(slot):
            self.preset.setCurrentText(combo.currentText())
            slot()
            combo.clear()
            for i in range(self.preset.count()): combo.addItem(self.preset.itemText(i))
            combo.setCurrentText(self.preset.currentText())
        for text, slot in actions:
            b = QPushButton(text)
            if text == "载入一次": b.setObjectName("primary")
            b.clicked.connect(lambda _=False, fn=slot: call(fn))
            row.addWidget(b)
        layout.addLayout(row)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.paths_changed.connect(self._reload_paths)
        dialog.exec()

    def _reload_paths(self):
        self.paths = discover_paths()
        self.refresh()
        self.statusBar().showMessage("路径设置已重新载入。", 6000)

    def refresh_subscriptions(self):
        if self.subscription_worker and self.subscription_worker.isRunning():
            return
        if not self.paths.game_root:
            QMessageBox.warning(self, "未找到游戏", "没有检测到《缺氧》Steam 安装目录。")
            return
        self.progress = QProgressDialog("正在读取 Steam 订阅…", "隐藏", 0, 0, self)
        self.progress.setWindowTitle("ONI Hub - Steam 订阅同步")
        self.progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.progress.setMinimumDuration(0)
        self.progress.show()
        self.subscription_worker = SubscriptionSyncWorker(self.paths)
        self.subscription_worker.progress.connect(self.progress.setLabelText)
        self.subscription_worker.completed.connect(self._subscriptions_done)
        self.subscription_worker.failed.connect(self._subscriptions_failed)
        self.subscription_worker.start()

    def _subscriptions_done(self, result: dict):
        self.progress.close()
        self.subscribed_ids = set(str(x) for x in result.get("subscribed", []))
        self.orphan_ids = set(str(x) for x in result.get("orphans", []))
        self.refresh()
        failed = result.get("failed", {}) or {}
        summary = (
            f"Steam 订阅：{len(self.subscribed_ids)}\n"
            f"缺失并请求下载：{len(result.get('missing', []))}\n"
            f"需要更新：{len(result.get('updates', []))}\n"
            f"已同步到缺氧目录：{len(result.get('synced', []))}\n"
            f"已退订残留：{len(self.orphan_ids)}\n"
            f"失败：{len(failed)}"
        )
        if failed:
            detail = "\n".join(f"{mid}: {reason}" for mid, reason in failed.items())
            QMessageBox.warning(self, "刷新完成（有失败）", summary + "\n\n" + detail[:10000])
        else:
            QMessageBox.information(self, "刷新完成", summary)

    def _subscriptions_failed(self, message: str):
        self.progress.close()
        QMessageBox.critical(
            self, "Steam 订阅同步失败",
            message + "\n\n请确认 Steam 已运行并登录。ONI Hub 使用《缺氧》自带的 steam_api64.dll，不使用 SteamCMD。"
        )

    def show_mod_context_menu(self, widget: QListWidget, pos):
        item = widget.itemAt(pos)
        if not item:
            return
        widget.setCurrentItem(item)
        mod = item.data(ROLE_MOD)
        if not mod or mod.source != "Steam" or not str(mod.mod_id).isdigit():
            return
        menu = QMenu(self)
        if mod.orphaned:
            resub = QAction("重新订阅并下载", self)
            resub.triggered.connect(lambda: self._steam_action("subscribe", mod.mod_id))
            menu.addAction(resub)
        else:
            unsub = QAction("取消订阅", self)
            unsub.triggered.connect(lambda: self._steam_action("unsubscribe", mod.mod_id))
            menu.addAction(unsub)
        delete = QAction("删除本地文件", self)
        delete.triggered.connect(lambda: self.delete_local_mod(mod))
        menu.addAction(delete)
        menu.addSeparator()
        open_folder = QAction("打开文件夹", self)
        open_folder.triggered.connect(lambda: self._open(mod.path))
        menu.addAction(open_folder)
        open_page = QAction("打开 Workshop 页面", self)
        open_page.triggered.connect(lambda: webbrowser.open(mod.workshop_url or f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod.mod_id}"))
        menu.addAction(open_page)
        menu.exec(widget.mapToGlobal(pos))

    def _steam_action(self, action: str, item_id: str):
        if self.steam_action_worker and self.steam_action_worker.isRunning():
            QMessageBox.information(self, "Steam 正忙", "请等待当前 Steam 操作完成。")
            return
        self.steam_action_worker = SteamActionWorker(self.paths, action, item_id)
        self.steam_action_worker.completed_action.connect(self._steam_action_done)
        self.steam_action_worker.failed.connect(lambda msg: QMessageBox.critical(self, "Steam 操作失败", msg))
        self.steam_action_worker.start()
        self.statusBar().showMessage("正在请求 Steam…")

    def _steam_action_done(self, action: str, item_id: str):
        text = "已重新订阅并请求下载" if action == "subscribe" else "已取消订阅"
        self.statusBar().showMessage(f"{text}：{item_id}", 6000)
        QTimer.singleShot(500, self.refresh_subscriptions)

    def delete_local_mod(self, mod: ModEntry):
        if not mod.path or not mod.path.exists():
            self.refresh(); return
        answer = QMessageBox.question(
            self, "确认删除",
            f"确定删除本地 Mod 文件夹？\n\n{mod.path}\n\n此操作不会自动取消订阅。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.rmtree(mod.path)
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))

    def refresh(self):
        try: self.mods,self.root_config=scan_mods(self.paths)
        except Exception as exc: QMessageBox.critical(self,"扫描失败",str(exc)); return
        for m in self.mods:
            if m.source == "Steam" and m.mod_id.isdigit():
                m.steam_subscribed = m.mod_id in self.subscribed_ids if self.subscribed_ids else True
                m.orphaned = m.mod_id in self.orphan_ids
        self._apply_cache(); self._apply_user_data(); self._index_configs(); self._mark_updates(); self.populate(); self.pathline.setText(f"用户目录: {self.paths.user_root}    |    mods.json: {self.paths.mods_json}")
        updates = sum(1 for m in self.mods if m.update_available)
        enabled_count = sum(1 for m in self.mods if m.enabled)
        disabled_count = len(self.mods) - enabled_count
        configured = sum(1 for m in self.mods if m.config_files)
        self.summary_line.setText(f"全部 {len(self.mods)}   |   已启用 {enabled_count}   |   未启用 {disabled_count}   |   可更新 {updates}   |   有配置 {configured}")
        suffix = f"；检测到 {updates} 个更新" if updates else ""
        self.statusBar().showMessage(f"已扫描 {len(self.mods)} 个 Mod{suffix}。",7000)

    def _apply_user_data(self):
        for m in self.mods:
            data=self.db.get_user_data(self._mod_key(m))
            m.alias=str(data.get("alias","") or "")
            m.user_tags=[str(x) for x in data.get("tags",[]) if str(x).strip()]
            m.favorite=bool(data.get("favorite",False))

    def _save_user_data(self, m: ModEntry):
        self.db.set_user_data(self._mod_key(m), alias=m.alias, tags=m.user_tags, favorite=m.favorite)

    def set_alias(self):
        m=self.selected()
        if not m: QMessageBox.information(self,"未选择 Mod","请先选择一个 Mod。"); return
        text,ok=QInputDialog.getText(self,"设置备注名称",f"原名称：{m.title}\n\n备注名称（留空恢复原名）：",text=m.alias)
        if not ok: return
        m.alias=text.strip(); self._save_user_data(m); self.populate(); self.statusBar().showMessage("备注名称已保存",5000)

    def set_tags(self):
        m=self.selected()
        if not m: QMessageBox.information(self,"未选择 Mod","请先选择一个 Mod。"); return
        text,ok=QInputDialog.getText(self,"设置标签","用逗号分隔，例如：必装, UI, 性能",text=", ".join(m.user_tags))
        if not ok: return
        m.user_tags=[]
        for raw in text.replace("，",",").split(","):
            tag=raw.strip()
            if tag and tag not in m.user_tags: m.user_tags.append(tag)
        self._save_user_data(m); self.populate(); self.statusBar().showMessage("标签已保存",5000)

    def toggle_favorite(self):
        m=self.selected()
        if not m: QMessageBox.information(self,"未选择 Mod","请先选择一个 Mod。"); return
        m.favorite=not m.favorite; self._save_user_data(m); self.populate(); self.statusBar().showMessage("已收藏" if m.favorite else "已取消收藏",5000)

    def edit_config(self):
        m=self.selected()
        if not m: QMessageBox.information(self,"未选择 Mod","请先选择一个 Mod。"); return
        files=discover_config_files(m)
        if not files:
            QMessageBox.information(self,"无配置", "这个 Mod 文件夹内没有检测到可编辑的 JSON 配置。")
            return
        dlg=ConfigEditorDialog(m,self); dlg.exec()

    def _index_configs(self):
        for m in self.mods:
            files=discover_config_files(m)
            m.config_files=files
            rows=[]
            for path in files:
                try:
                    rows.append((str(path),str(path.relative_to(m.path)),score_config(path,m.path),path.stat().st_mtime))
                except Exception:
                    continue
            self.db.replace_config_index(self._mod_key(m),rows)

    def _cache(self):
        try: return json.loads(self.meta_cache_file.read_text(encoding="utf-8"))
        except Exception: return {}
    def _apply_cache(self):
        cache=self._cache()
        for m in self.mods:
            d=cache.get(m.mod_id,{})
            if not d: continue
            m.title=d.get("title") or m.title; m.author=d.get("author") or m.author; m.description=d.get("description") or m.description
            m.preview_url=d.get("preview_url",""); m.workshop_url=d.get("workshop_url") or m.workshop_url; m.time_updated=int(d.get("time_updated",0) or 0); m.file_size=int(d.get("file_size",0) or 0); m.tags=d.get("tags") or m.tags
            cached=self.preview_cache/f"{m.mod_id}.jpg"
            if cached.exists(): m.preview=cached

    def _installed_state(self):
        try:
            data = json.loads(self.install_state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_installed_state(self, data: dict):
        self.install_state_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _mark_updates(self):
        # Use the files ONI really loads as the primary local version source.
        # Steam client's ACF is only a fallback because it can remain stale after
        # Steam client cache and ONI live-copy timestamps are compared.
        steam_client_times = installed_workshop_times(self.paths.workshop_cache)
        installed_state = self._installed_state()
        for m in self.mods:
            m.update_available = False
            m.local_time_updated = 0
            if m.source != "Steam" or not m.mod_id.isdigit():
                continue
            folder_time = folder_latest_mtime(m.path) if m.path and m.path.exists() else 0
            state_time = int(installed_state.get(m.mod_id, 0) or 0)
            client_time = int(steam_client_times.get(m.mod_id, 0) or 0)
            m.local_time_updated = max(folder_time, state_time, client_time)
            m.update_available = bool(
                m.time_updated and m.local_time_updated and
                m.time_updated > m.local_time_updated + 300
            )

    def check_updates(self):
        ids = [m.mod_id for m in self.mods if m.source == "Steam" and m.mod_id.isdigit()]
        if not ids:
            QMessageBox.information(self, "没有 Steam Mod", "没有找到可检查的 Steam Workshop Mod。")
            return
        self.update_btn.setEnabled(False)
        self.update_btn.setText("检查中…")
        self.worker = SteamWorker(ids)
        self.worker.completed.connect(self._updates_checked)
        self.worker.failed.connect(self._updates_failed)
        self.worker.start()

    def _updates_checked(self, data: dict):
        cache = self._cache()
        for mid, d in data.items():
            old = cache.get(mid, {})
            old["title"] = d.get("title") or old.get("title", "")
            old["time_updated"] = int(d.get("time_updated", 0) or 0)
            old["file_size"] = int(d.get("file_size", 0) or 0)
            old["preview_url"] = d.get("preview_url") or old.get("preview_url", "")
            old["workshop_url"] = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}"
            cache[mid] = old
        self.meta_cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        self.update_btn.setEnabled(True)
        self.update_btn.setText("检查 Mod 更新")
        self._apply_cache()
        self._mark_updates()
        self.populate()
        updates = [m for m in self.mods if m.update_available]
        if not updates:
            self.statusBar().showMessage("所有 Steam Workshop Mod 都是最新版本。", 6000)
            return
        self.update_dialog = UpdatesDialog(updates, self)
        self.update_dialog.download_requested.connect(self.download_updates)
        self.update_dialog.show()

    def _updates_failed(self, msg: str):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("检查 Mod 更新")
        QMessageBox.warning(self, "检查失败", msg)

    def download_updates(self, ids: list[str]):
        if not self.paths.workshop_cache:
            QMessageBox.warning(self, "未找到 Steam Workshop 缓存", "没有检测到 Steam 客户端的 Workshop 缓存。请确认 Steam 已登录并至少订阅过一个缺氧 Mod。")
            return
        if not self.paths.workshop_mods:
            QMessageBox.warning(self, "未找到 ONI Mod 目录", "没有检测到缺氧实际加载的 mods\\Steam 目录。")
            return
        self.progress = QProgressDialog("正在请求 Steam 更新…", "隐藏", 0, 0, self)
        self.progress.setWindowTitle("Steam 客户端 Mod 更新")
        self.progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.progress.setMinimumDuration(0)
        self.progress.show()
        self.download_worker = DownloadWorker(ids, self.paths.workshop_cache, self.paths.workshop_mods)
        self.download_worker.progress.connect(self.progress.setLabelText)
        self.download_worker.completed.connect(self._download_done)
        self.download_worker.failed.connect(self._download_failed)
        self.download_worker.start()

    def _download_done(self, success: list, failed: list, log: str):
        self.progress.close()

        # Record the Workshop revision synchronized from the logged-in Steam client.
        if success:
            cache = self._cache()
            state = self._installed_state()
            for mid in success:
                remote_time = int((cache.get(str(mid), {}) or {}).get("time_updated", 0) or 0)
                if remote_time:
                    state[str(mid)] = remote_time
            self._save_installed_state(state)

        # Full rescan: discard every old ModEntry and rebuild only from folders
        # currently present under ONI's Steam/Local directories.
        self.refresh()
        if failed:
            QMessageBox.warning(
                self, "更新完成（部分失败）",
                f"成功：{len(success)} 个\n失败：{len(failed)} 个\n\n失败 ID：{', '.join(failed)}\n\n"
                "Steam 未在等待时间内完成这些项目。\n"
                "请保持 Steam 客户端登录、在线并确认这些 Mod 已订阅；可在 Steam 下载页面点继续。"
            )
        else:
            QMessageBox.information(self, "更新完成", f"已通过 Steam 客户端同步 {len(success)} 个 Workshop Mod。")

    def _download_failed(self, msg: str):
        self.progress.close()
        QMessageBox.critical(self, "下载失败", msg)

    def populate(self):
        for lst in (self.disabled,self.enabled): lst.clear()
        for m in self.mods: self._add(self.enabled if m.enabled else self.disabled,m)
        self.filter()
    def _add(self,lst,m):
        item=QListWidgetItem(); item.setData(ROLE_MOD,m); item.setSizeHint(QSize(320,74)); lst.addItem(item); lst.setItemWidget(item,ModCard(m))
    def filter(self):
        q=self.search.text().strip().casefold(); src=self.source.currentText(); vf=self.view_filter.currentText()
        for lst in (self.disabled,self.enabled):
            for i in range(lst.count()):
                item=lst.item(i); m=item.data(ROLE_MOD)
                blob=" ".join([m.display_title,m.title,m.mod_id,m.config_id,m.author,*m.all_tags]).casefold()
                hidden=bool(q and q not in blob) or (src!="全部来源" and m.source!=src)
                if vf=="收藏" and not m.favorite: hidden=True
                elif vf=="有配置" and not m.config_files: hidden=True
                elif vf=="有更新" and not m.update_available: hidden=True
                elif vf=="已退订残留" and not m.orphaned: hidden=True
                item.setHidden(hidden)
    def toggle_item(self,item):
        source=item.listWidget()
        source.clearSelection(); item.setSelected(True); source.setCurrentItem(item)
        if source is self.disabled: self.enable_selected()
        else: self.disable_selected()
    def changed(self):
        self.dirty = True
        self.save_state.setText("● Mod 列表或顺序已修改，尚未保存")
        self.save_state.setObjectName("dirtyState")
        self.save_state.style().unpolish(self.save_state); self.save_state.style().polish(self.save_state)
        self.save_btn.setEnabled(True)
        self.save_btn.setText("💾 保存修改")
        self.save_btn.setObjectName("saveDirty")
        self.save_btn.style().unpolish(self.save_btn); self.save_btn.style().polish(self.save_btn)
        self.statusBar().showMessage("有未保存的 Mod 启用状态或排序。按 Ctrl+S 保存。")
    def ordered(self):
        out=[]
        for lst,enabled in ((self.enabled,True),(self.disabled,False)):
            for i in range(lst.count()): m=lst.item(i).data(ROLE_MOD); m.enabled=enabled; out.append(m)
        return out
    def save(self):
        try:
            backup=save_config(self.paths.mods_json,self.root_config,self.ordered())
            # Reload the just-written root so later saves preserve the current structure.
            self.mods,self.root_config=scan_mods(self.paths); self._apply_cache(); self._apply_user_data(); self._index_configs(); self._mark_updates(); self.populate()
            self.dirty = False
            self.save_state.setText("✓ 当前 Mod 列表已保存")
            self.save_state.setObjectName("savedState")
            self.save_state.style().unpolish(self.save_state); self.save_state.style().polish(self.save_state)
            self.save_btn.setEnabled(False)
            self.save_btn.setText("✓ 已保存")
            self.save_btn.setObjectName("saveClean")
            self.save_btn.style().unpolish(self.save_btn); self.save_btn.style().polish(self.save_btn)
            self.statusBar().showMessage("✓ Mod 启用状态和排序已保存。", 8000)
        except Exception as exc: QMessageBox.critical(self,"保存失败",str(exc))
    def restore(self):
        p=restore_latest(self.paths.mods_json)
        if p: QMessageBox.information(self,"恢复完成",f"已恢复 {p.name}"); self.refresh()
        else: QMessageBox.warning(self,"没有备份","未找到 ONI Hub 创建的备份。")
    def show_details(self,current,*_):
        if not current:return
        m=current.data(ROLE_MOD); self.detail_title.setText(m.display_title)
        updated=datetime.fromtimestamp(m.time_updated).strftime("%Y-%m-%d %H:%M") if m.time_updated else "未知"
        size=f"{m.file_size/1024/1024:.2f} MB" if m.file_size else "未知"
        subscription = "已退订残留" if m.orphaned else ("已订阅" if m.steam_subscribed else "未知")
        self.detail_meta.setText(f"原名称: {m.title if m.alias else '—'}\n作者: {m.author or '未知'}\nSteam 状态: {subscription}\nWorkshop ID: {m.mod_id}\n缺氧配置 ID: {m.config_id or m.mod_id}\n来源: {m.source}\n版本: {m.version or '未知'}\n更新时间: {updated}\n文件大小: {size}\n路径: {m.path}\n标签: {', '.join(m.all_tags) or '无'}")
        config_files=m.config_files or discover_config_files(m)
        self.config_btn.setEnabled(bool(config_files))
        self.config_btn.setText(f"配置 ({len(config_files)})" if config_files else "无配置")
        self.favorite_btn.setText("★ 已收藏" if m.favorite else "☆ 收藏")
        self.description.setText(m.description or "暂无描述。本地文件通常不包含作者和简介，请点击“同步 Workshop 信息”。")
        if m.preview and m.preview.exists():
            pix=QPixmap(str(m.preview)); self.big_preview.setPixmap(pix.scaled(self.big_preview.size(),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        else: self.big_preview.setPixmap(QPixmap()); self.big_preview.setText("无预览图\n点击同步 Workshop 信息")
    def selected(self):
        for lst in (self.disabled,self.enabled):
            if lst.currentItem(): return lst.currentItem().data(ROLE_MOD)
        return None
    def open_mods(self): self._open(self.paths.local_mods)
    def open_selected(self):
        m=self.selected(); self._open(m.path) if m else None
    def open_workshop(self):
        m=self.selected(); webbrowser.open(m.workshop_url) if m and m.workshop_url else None
    def _open(self,p):
        if not p or not Path(p).exists(): QMessageBox.warning(self,"路径不存在",str(p)); return
        os.startfile(str(p)) if sys.platform.startswith("win") else subprocess.Popen(["xdg-open",str(p)])
    def launch(self):
        webbrowser.open(f"steam://run/{APP_ID}")
    def sync_steam(self):
        ids=[m.mod_id for m in self.mods if m.source=="Steam" and m.mod_id.isdigit()]
        if not ids: QMessageBox.information(self,"没有项目","没有找到可同步的 Steam Workshop Mod。"); return
        self.sync_btn.setEnabled(False); self.sync_btn.setText("同步中…"); self.worker=SteamWorker(ids); self.worker.completed.connect(self._sync_done); self.worker.failed.connect(self._sync_fail); self.worker.start()
    def _sync_done(self,data):
        cache=self._cache()
        for mid,d in data.items():
            tags=[x.get("tag","") for x in d.get("tags",[]) if isinstance(x,dict) and x.get("tag")]
            entry={"title":d.get("title", ""),"author":str(d.get("creator", "")),"description":html.unescape(d.get("description", "")),"preview_url":d.get("preview_url", ""),"workshop_url":f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}","time_updated":d.get("time_updated",0),"file_size":d.get("file_size",0),"tags":tags}
            cache[mid]=entry
            if entry["preview_url"]: download(entry["preview_url"],self.preview_cache/f"{mid}.jpg")
        self.meta_cache_file.write_text(json.dumps(cache,ensure_ascii=False,indent=2),encoding="utf-8"); self.sync_btn.setEnabled(True); self.sync_btn.setText("同步 Workshop 信息"); self.refresh(); QMessageBox.information(self,"同步完成",f"已更新 {len(data)} 个 Workshop Mod。\n注意：Steam 接口通常只返回创建者 Steam ID，不一定返回昵称。")
    def _sync_fail(self,msg): self.sync_btn.setEnabled(True); self.sync_btn.setText("同步 Workshop 信息"); QMessageBox.warning(self,"同步失败",msg)
    @property
    def presets_dir(self) -> Path:
        path = self.app_dir / "presets"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _safe_preset_name(name: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', name).strip(' .')
        return cleaned[:100] or "Preset"

    def _preset_path(self, name: str) -> Path:
        return self.presets_dir / f"{self._safe_preset_name(name)}.onihub"

    def _preset_files(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for path in self.presets_dir.glob("*.onihub"):
            try:
                with zipfile.ZipFile(path, "r") as archive:
                    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                display = str(manifest.get("name") or path.stem)
                result[display] = path
            except Exception:
                continue
        return result

    PRESET_PLACEHOLDER = "— 选择预设（仅载入时应用）—"

    def _load_preset_names(self, keep_selection: str = ""):
        if not hasattr(self, "preset"):
            return
        self.preset.blockSignals(True)
        try:
            self.preset.clear()
            self.preset.addItem(self.PRESET_PLACEHOLDER)
            self.preset.addItems(sorted(self._preset_files(), key=str.casefold))
            if keep_selection and keep_selection in self._preset_files():
                self.preset.setCurrentText(keep_selection)
            else:
                self.preset.setCurrentIndex(0)
        finally:
            self.preset.blockSignals(False)

    def _build_preset_manifest(self, name: str) -> tuple[dict, list[tuple[Path, str]]]:
        ordered = self.ordered()
        enabled = [self._mod_key(m) for m in ordered if m.enabled]
        disabled = [self._mod_key(m) for m in ordered if not m.enabled]
        files: list[tuple[Path, str]] = []
        mods_payload = []
        used_members: set[str] = set()
        for mod in ordered:
            key = self._mod_key(mod)
            config_entries = []
            config_files = mod.config_files or discover_json_configs(mod)
            for config_path in config_files:
                try:
                    relative = config_path.relative_to(mod.path)
                    json.loads(config_path.read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                member = f"configs/{self._safe_preset_name(key)}/{relative.as_posix()}"
                if member in used_members:
                    continue
                used_members.add(member)
                files.append((config_path, member))
                config_entries.append({"relative_path": relative.as_posix(), "member": member})
            mods_payload.append({
                "key": key,
                "workshop_id": mod.mod_id if mod.source == "Steam" else "",
                "static_id": mod.config_id,
                "title": mod.title,
                "source": mod.source,
                "enabled": bool(mod.enabled),
                "configs": config_entries,
            })
        manifest = {
            "format": "ONIHubPreset",
            "format_version": 1,
            "name": name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "app_id": APP_ID,
            "enabled_order": enabled,
            "disabled_order": disabled,
            "mods": mods_payload,
        }
        return manifest, files

    def _write_preset(self, path: Path, name: str) -> tuple[int, int]:
        manifest, files = self._build_preset_manifest(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for source, member in files:
                archive.write(source, member)
        temp.replace(path)
        return len(manifest["mods"]), len(files)

    def save_preset(self):
        name, ok = QInputDialog.getText(self, "保存预设", "预设名称:", text=self.preset.currentText())
        if not ok or not name.strip():
            return
        name = name.strip()
        path = self._preset_path(name)
        if path.exists() and QMessageBox.question(self, "覆盖预设", f"预设“{name}”已存在，是否覆盖？") != QMessageBox.StandardButton.Yes:
            return
        try:
            mod_count, config_count = self._write_preset(path, name)
            # Saving is snapshot-only. It must never activate or apply the preset.
            self._load_preset_names()
            QMessageBox.information(self, "预设已保存", f"已保存 {mod_count} 个 Mod 的状态和顺序。\n已打包 {config_count} 个 JSON 配置文件。\n\n保存预设不会改变当前 Mod。只有点击“载入”时才会应用。\n\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "保存预设失败", str(exc))

    def _read_preset(self, path: Path) -> tuple[dict, zipfile.ZipFile]:
        archive = zipfile.ZipFile(path, "r")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != "ONIHubPreset" or int(manifest.get("format_version", 0)) != 1:
                raise ValueError("不是受支持的 ONI Hub 预设文件。")
            if str(manifest.get("app_id")) != APP_ID:
                raise ValueError("该预设不属于《缺氧》。")
            return manifest, archive
        except Exception:
            archive.close()
            raise

    def _apply_preset_file(self, path: Path) -> None:
        manifest, archive = self._read_preset(path)
        try:
            by_key: dict[str, ModEntry] = {}
            for mod in self.mods:
                for key in {self._mod_key(mod), str(mod.mod_id), str(mod.config_id), mod.path.name}:
                    if key:
                        by_key.setdefault(key, mod)
            enabled_order = [str(x) for x in manifest.get("enabled_order", [])]
            disabled_order = [str(x) for x in manifest.get("disabled_order", [])]
            rank_enabled = {key: i for i, key in enumerate(enabled_order)}
            rank_disabled = {key: i for i, key in enumerate(disabled_order)}
            known_preset_keys = set(rank_enabled) | set(rank_disabled)
            # Preserve the current state/order of Mods that did not exist when the
            # snapshot was created. A preset is a one-shot overlay, not a lock.
            current_order = {self._mod_key(mod): i for i, mod in enumerate(self.ordered())}
            missing: list[str] = []
            missing_workshop_ids: list[str] = []
            restored_configs = 0
            backup_root = self.portable["config_backups"] / f"preset-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

            for entry in manifest.get("mods", []):
                if not isinstance(entry, dict):
                    continue
                keys = [str(entry.get("key", "")), str(entry.get("static_id", "")), str(entry.get("workshop_id", ""))]
                mod = next((by_key.get(k) for k in keys if k and by_key.get(k)), None)
                if mod is None:
                    missing.append(str(entry.get("title") or entry.get("workshop_id") or entry.get("key") or "未知 Mod"))
                    workshop_id = str(entry.get("workshop_id") or "")
                    if workshop_id.isdigit():
                        missing_workshop_ids.append(workshop_id)
                    continue
                for config in entry.get("configs", []):
                    if not isinstance(config, dict):
                        continue
                    relative_text = str(config.get("relative_path", ""))
                    member = str(config.get("member", ""))
                    relative = Path(relative_text)
                    if not relative_text or relative.is_absolute() or ".." in relative.parts or not member.startswith("configs/"):
                        continue
                    target = (mod.path / relative).resolve()
                    mod_root = mod.path.resolve()
                    if target != mod_root and mod_root not in target.parents:
                        continue
                    raw = archive.read(member)
                    json.loads(raw.decode("utf-8-sig"))
                    if target.exists():
                        backup = backup_root / self._safe_preset_name(self._mod_key(mod)) / relative
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, backup)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp = target.with_suffix(target.suffix + ".onihub_tmp")
                    temp.write_bytes(raw)
                    temp.replace(target)
                    restored_configs += 1

            for mod in self.mods:
                key = self._mod_key(mod)
                if key in rank_enabled:
                    mod.enabled = True
                elif key in rank_disabled:
                    mod.enabled = False
                # Mods absent from the snapshot keep their current state. This
                # allows newly installed Mods to be enabled/disabled normally.
            self.mods.sort(key=lambda m: (
                0,
                0 if self._mod_key(m) in rank_enabled else 1,
                rank_enabled.get(self._mod_key(m), current_order.get(self._mod_key(m), 10**9)),
                m.title.casefold(),
            ) if m.enabled else (
                1,
                0 if self._mod_key(m) in rank_disabled else 1,
                rank_disabled.get(self._mod_key(m), current_order.get(self._mod_key(m), 10**9)),
                m.title.casefold(),
            ))
            self.populate()
            backup = save_config(self.paths.mods_json, self.root_config, self.ordered())
            self.mods, self.root_config = scan_mods(self.paths)
            self._apply_cache(); self._apply_user_data(); self._index_configs(); self._mark_updates(); self.populate()
            summary = f"已载入预设：{manifest.get('name', path.stem)}\n已恢复 {restored_configs} 个 Mod 配置文件。\n启用状态与排序已写入 mods.json。\n备份：{backup or '无'}"
            if missing:
                summary += "\n\n缺少的 Mod：\n" + "\n".join(missing[:50])
                if len(missing) > 50:
                    summary += f"\n……另有 {len(missing)-50} 个"
                QMessageBox.warning(self, "预设已载入（存在缺失 Mod）", summary)
                if missing_workshop_ids:
                    answer = QMessageBox.question(
                        self, "同步缺失 Mod",
                        f"预设中有 {len(missing_workshop_ids)} 个缺失的 Steam Mod。\n如果它们已在当前 Steam 账号订阅，是否立即刷新订阅并自动下载？"
                    )
                    if answer == QMessageBox.StandardButton.Yes:
                        QTimer.singleShot(0, self.refresh_subscriptions)
            else:
                QMessageBox.information(self, "预设已载入", summary)
        finally:
            archive.close()

    def load_preset(self):
        name = self.preset.currentText()
        path = self._preset_files().get(name)
        if not path:
            return
        if QMessageBox.question(self, "载入预设", "将覆盖当前 Mod 启用状态、排序和预设内包含的 JSON 配置。\n覆盖前会自动备份。是否继续？") != QMessageBox.StandardButton.Yes:
            return
        try:
            self._apply_preset_file(path)
            # Applying is one-shot; do not leave an "active preset" selected.
            self._load_preset_names()
        except Exception as exc:
            QMessageBox.critical(self, "载入预设失败", str(exc))

    def export_preset(self):
        name = self.preset.currentText()
        source = self._preset_files().get(name)
        if not source:
            QMessageBox.information(self, "没有预设", "请先保存或选择一个预设。")
            return
        target, _ = QFileDialog.getSaveFileName(self, "导出 ONI Hub 预设", f"{self._safe_preset_name(name)}.onihub", "ONI Hub 预设 (*.onihub)")
        if not target:
            return
        try:
            target_path = Path(target)
            if target_path.suffix.casefold() != ".onihub":
                target_path = target_path.with_suffix(".onihub")
            shutil.copy2(source, target_path)
            QMessageBox.information(self, "导出完成", f"预设包已导出：\n{target_path}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def import_preset(self):
        source, _ = QFileDialog.getOpenFileName(self, "导入 ONI Hub 预设", "", "ONI Hub 预设 (*.onihub);;所有文件 (*)")
        if not source:
            return
        try:
            source_path = Path(source)
            manifest, archive = self._read_preset(source_path)
            archive.close()
            name = str(manifest.get("name") or source_path.stem)
            target = self._preset_path(name)
            if target.exists() and QMessageBox.question(self, "覆盖预设", f"预设“{name}”已存在，是否覆盖？") != QMessageBox.StandardButton.Yes:
                return
            shutil.copy2(source_path, target)
            self._load_preset_names(name)
            answer = QMessageBox.question(self, "导入完成", f"预设“{name}”已导入。\n导入本身不会应用预设。\n是否现在载入一次 Mod 列表、排序和配置？")
            if answer == QMessageBox.StandardButton.Yes:
                self._apply_preset_file(target)
            self._load_preset_names()
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))

    def delete_preset(self):
        name = self.preset.currentText()
        path = self._preset_files().get(name)
        if not path:
            return
        if QMessageBox.question(self, "删除预设", f"确定删除预设“{name}”？") != QMessageBox.StandardButton.Yes:
            return
        try:
            path.unlink()
            self._load_preset_names()
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))

    def closeEvent(self, event):
        if not self.dirty:
            event.accept(); return
        box = QMessageBox(self)
        box.setWindowTitle("还有修改未保存")
        box.setText("Mod 启用状态或排序尚未保存。")
        box.setInformativeText("保存后退出、放弃修改，还是返回继续编辑？")
        save_btn = box.addButton("保存并退出", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is save_btn:
            self.save()
            event.accept() if not self.dirty else event.ignore()
        elif box.clickedButton() is discard_btn:
            event.accept()
        else:
            event.ignore()

STYLE="""
QMainWindow,QWidget{background:#10151c;color:#eef3fb;font-family:'Microsoft YaHei UI';font-size:14px}
QFrame#panel,QFrame#switchPanel{background:#151c25;border:1px solid #293442;border-radius:9px}
QFrame#saveBar{background:#171f29;border:1px solid #344355;border-radius:9px}
QLabel#panelTitle{font-size:18px;font-weight:700;padding:6px}
QLabel#summaryLine{color:#b7c5d7;background:#151c25;border:1px solid #293442;border-radius:7px;padding:7px 10px}
QLabel#savedState{color:#74d69e;font-weight:700} QLabel#dirtyState{color:#ffbe68;font-weight:800}
QPushButton{background:#252f3c;border:1px solid #3b4859;border-radius:7px;padding:9px 14px}
QPushButton:hover{background:#303d4c} QPushButton#primary,QPushButton#toolbarPrimary{background:#18794e;border-color:#2aa76d;font-weight:700}
QPushButton#saveDirty{background:#159257;border:1px solid #38c47c;font-size:16px;font-weight:800;padding:11px 24px}
QPushButton#saveClean{background:#202934;color:#8290a1;border-color:#303b49;padding:11px 24px}
QLineEdit,QComboBox{background:#1c2531;border:1px solid #374557;border-radius:7px;padding:9px}
QListWidget{background:#111820;border:none;outline:0;padding:6px} QListWidget::item{border:none}
QListWidget::item:selected QWidget#modCard{background:#30445e;border:1px solid #6489b8}
QWidget#modCard{background:#1c2530;border:1px solid #293746;border-radius:8px}
QLabel#thumb{background:#0e1319;border:1px solid #344152;border-radius:5px;color:#8fa2b7;font-weight:800}
QLabel#cardTitle{font-size:15px;font-weight:700} QLabel#cardMeta,QLabel#cardId{color:#a8b5c5;font-size:12px}
QLabel#updateBadge{background:#6b3c16;border:1px solid #d58b35;border-radius:5px;padding:3px 7px;color:#ffe1b3;font-weight:700}
QLabel#badge{background:#25344a;border:1px solid #526d94;border-radius:5px;padding:3px 7px;color:#dcecff}
QLabel#bigPreview{background:#0d1218;border:1px solid #303b48;border-radius:7px;color:#8796a8}
QLabel#detailTitle{font-size:23px;font-weight:800;padding-top:5px} QLabel#pathline{color:#8f9daf;padding:3px}
QScrollArea{background:transparent} QStatusBar{background:#0b1016;color:#b2bfce}
"""

def run():
    app=QApplication(sys.argv); app.setApplicationName("ONI Hub"); w=MainWindow(); w.show(); return app.exec()
