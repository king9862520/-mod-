from pathlib import Path
from zipfile import ZipFile

from onihub.legacy_installer import find_legacy_package


def test_find_latest_legacy_package(tmp_path: Path) -> None:
    old = tmp_path / "1_legacy.bin"
    new = tmp_path / "2_legacy.bin"
    with ZipFile(old, "w") as archive:
        archive.writestr("mod.yaml", "title: old")
    with ZipFile(new, "w") as archive:
        archive.writestr("mod.yaml", "title: new")
    old.touch()
    new.touch()
    assert find_legacy_package(tmp_path) in {old, new}
