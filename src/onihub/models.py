from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass(slots=True)
class ModEntry:
    mod_id: str
    title: str
    source: str
    path: Path
    config_id: str = ""
    enabled: bool = False
    description: str = ""
    version: str = ""
    author: str = ""
    preview: Path | None = None
    preview_url: str = ""
    workshop_url: str = ""
    time_updated: int = 0
    local_time_updated: int = 0
    update_available: bool = False
    file_size: int = 0
    tags: list[str] = field(default_factory=list)
    raw_config: dict[str, Any] = field(default_factory=dict)
    alias: str = ""
    user_tags: list[str] = field(default_factory=list)
    favorite: bool = False
    config_files: list[Path] = field(default_factory=list)
    steam_subscribed: bool = False
    orphaned: bool = False

    @property
    def display_title(self) -> str:
        return self.alias.strip() or self.title

    @property
    def all_tags(self) -> list[str]:
        seen: list[str] = []
        for tag in [*self.user_tags, *self.tags]:
            text = str(tag).strip()
            if text and text not in seen:
                seen.append(text)
        return seen

    @property
    def subtitle(self) -> str:
        author = self.author or "未知作者"
        return f"{author}  ·  {self.source}"
