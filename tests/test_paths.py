from pathlib import Path

from onihub.paths import normalize_workshop_root


def test_normalize_workshop_root_accepts_content_folder(tmp_path: Path) -> None:
    target = tmp_path / "steamapps" / "workshop" / "content" / "457140"
    target.mkdir(parents=True)
    assert normalize_workshop_root(target) == target
