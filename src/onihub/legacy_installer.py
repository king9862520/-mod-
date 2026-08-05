from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable


class LegacyInstallError(RuntimeError):
    """Raised when a Steam Workshop package cannot be installed safely."""


def find_legacy_package(source: Path) -> Path | None:
    """Find the newest ONI legacy package.

    Steamworks GetItemInstallInfo may return either:
    - the Workshop item directory; or
    - the actual ``*_legacy.bin`` file.

    ONI Hub therefore accepts both forms and never assumes the returned path is
    a directory.
    """
    source = Path(source)

    if source.is_file():
        if source.name.lower().endswith("_legacy.bin") or zipfile.is_zipfile(source):
            return source
        return None

    if not source.is_dir():
        return None

    candidates: list[Path] = []
    try:
        candidates.extend(
            child
            for child in source.glob("*_legacy.bin")
            if child.is_file()
        )
        # Tolerate future/odd naming while still requiring a real ZIP package.
        if not candidates:
            candidates.extend(
                child
                for child in source.glob("*.bin")
                if child.is_file() and zipfile.is_zipfile(child)
            )
    except OSError:
        return None

    if not candidates:
        return None

    def mtime(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    return max(candidates, key=mtime)


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        name = member.filename.replace("\\", "/")
        if not name or name.startswith("/"):
            raise LegacyInstallError(f"压缩包包含非法路径：{member.filename}")

        output = (destination / name).resolve()
        if output != root and root not in output.parents:
            raise LegacyInstallError(f"压缩包包含越界路径：{member.filename}")

        # Reject Unix symbolic links stored in ZIP metadata.
        mode = member.external_attr >> 16
        if (mode & 0o170000) == 0o120000:
            raise LegacyInstallError(f"压缩包包含不允许的符号链接：{member.filename}")

    archive.extractall(destination)


def _metadata_exists(root: Path) -> bool:
    return (root / "mod.yaml").is_file() or (root / "mod_info.yaml").is_file()


def _has_metadata_anywhere(root: Path) -> bool:
    return (
        _metadata_exists(root)
        or next(root.rglob("mod.yaml"), None) is not None
        or next(root.rglob("mod_info.yaml"), None) is not None
    )


def _choose_payload_root(extract_dir: Path) -> Path:
    """Choose the directory installed as ``mods/Steam/<WorkshopID>``.

    ONI legacy packages may be:
    - a normal mod at archive root;
    - wrapped in one outer folder; or
    - a multi-version package containing several metadata files.

    Multi-version layouts must be preserved intact.
    """
    if _metadata_exists(extract_dir):
        return extract_dir

    entries = [entry for entry in extract_dir.iterdir() if entry.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir() and _metadata_exists(entries[0]):
        return entries[0]

    metadata = list(extract_dir.rglob("mod.yaml")) + list(extract_dir.rglob("mod_info.yaml"))
    if not metadata:
        raise LegacyInstallError("解压成功，但没有找到 mod.yaml 或 mod_info.yaml。")

    # More than one metadata file generally means archived/latest version
    # layout. Preserve the complete archive so ONI can select the right one.
    return extract_dir


def _replace_directory_atomic(staging: Path, target_dir: Path) -> None:
    backup = target_dir.parent / f".{target_dir.name}.onihub-old"
    shutil.rmtree(backup, ignore_errors=True)

    if target_dir.exists():
        os.replace(target_dir, backup)

    try:
        os.replace(staging, target_dir)
    except Exception:
        if backup.exists() and not target_dir.exists():
            os.replace(backup, target_dir)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    else:
        shutil.rmtree(backup, ignore_errors=True)


def install_legacy_package(
    package: Path,
    target_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Safely extract one ``*_legacy.bin`` ZIP and atomically install it."""
    package = Path(package)
    target_dir = Path(target_dir)

    if not package.is_file():
        raise LegacyInstallError(f"找不到 Workshop 包文件：{package}")
    if not zipfile.is_zipfile(package):
        raise LegacyInstallError(f"文件不是有效 ZIP 格式的 legacy 包：{package.name}")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = target_dir.parent / f".{target_dir.name}.onihub-new"
    shutil.rmtree(staging, ignore_errors=True)

    with tempfile.TemporaryDirectory(
        prefix=f".onihub-{target_dir.name}-",
        dir=target_dir.parent,
    ) as temp_name:
        extracted = Path(temp_name)
        if progress:
            progress(f"正在解压 Mod {target_dir.name}：{package.name}")

        try:
            with zipfile.ZipFile(package, "r") as archive:
                damaged = archive.testzip()
                if damaged:
                    raise LegacyInstallError(f"压缩包校验失败，损坏文件：{damaged}")
                _safe_extract(archive, extracted)
        except zipfile.BadZipFile as exc:
            raise LegacyInstallError(f"legacy 包损坏：{package.name}") from exc

        payload = _choose_payload_root(extracted)
        shutil.copytree(payload, staging)

    if not _has_metadata_anywhere(staging):
        shutil.rmtree(staging, ignore_errors=True)
        raise LegacyInstallError("安装暂存目录缺少 Mod 元数据，已取消替换。")

    _replace_directory_atomic(staging, target_dir)


def install_unpacked_folder(
    source_dir: Path,
    target_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> None:
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)

    if not source_dir.is_dir():
        raise LegacyInstallError(f"Workshop 目录不存在：{source_dir}")
    if not _has_metadata_anywhere(source_dir):
        raise LegacyInstallError("Workshop 项目没有 legacy 包，也没有可识别的 Mod 文件。")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = target_dir.parent / f".{target_dir.name}.onihub-new"
    shutil.rmtree(staging, ignore_errors=True)

    if progress:
        progress(f"正在复制普通 Workshop Mod：{target_dir.name}")
    shutil.copytree(source_dir, staging)
    _replace_directory_atomic(staging, target_dir)


def install_workshop_item(
    source: Path,
    target_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> str:
    """Install a Steam Workshop item from either a file or a directory.

    Returns ``"legacy"`` when a ``*_legacy.bin`` ZIP was extracted, otherwise
    ``"folder"`` for an already-unpacked Workshop item.
    """
    source = Path(source)
    package = find_legacy_package(source)
    if package is not None:
        install_legacy_package(package, target_dir, progress)
        return "legacy"

    if source.is_file():
        raise LegacyInstallError(f"Workshop 文件不是有效 legacy ZIP：{source}")

    install_unpacked_folder(source, target_dir, progress)
    return "folder"
