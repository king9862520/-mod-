from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS mod_user_data (
  mod_key TEXT PRIMARY KEY,
  alias TEXT NOT NULL DEFAULT '',
  tags_json TEXT NOT NULL DEFAULT '[]',
  favorite INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS config_index (
  mod_key TEXT NOT NULL,
  path TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0,
  mtime REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (mod_key, path)
);
"""

class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def get_user_data(self, mod_key: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM mod_user_data WHERE mod_key=?", (mod_key,)).fetchone()
        if not row:
            return {"alias":"", "tags":[], "favorite":False, "notes":""}
        try:
            tags = json.loads(row["tags_json"])
        except Exception:
            tags = []
        return {"alias":row["alias"] or "", "tags":tags if isinstance(tags,list) else [], "favorite":bool(row["favorite"]), "notes":row["notes"] or ""}

    def set_user_data(self, mod_key: str, *, alias: str, tags: list[str], favorite: bool, notes: str="") -> None:
        payload = json.dumps(tags, ensure_ascii=False)
        with self.connect() as db:
            db.execute(
                """INSERT INTO mod_user_data(mod_key,alias,tags_json,favorite,notes,updated_at)
                VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(mod_key) DO UPDATE SET alias=excluded.alias,tags_json=excluded.tags_json,
                favorite=excluded.favorite,notes=excluded.notes,updated_at=CURRENT_TIMESTAMP""",
                (mod_key, alias, payload, 1 if favorite else 0, notes),
            )

    def replace_config_index(self, mod_key: str, files: list[tuple[str,str,int,float]]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM config_index WHERE mod_key=?", (mod_key,))
            db.executemany(
                "INSERT INTO config_index(mod_key,path,relative_path,score,mtime) VALUES(?,?,?,?,?)",
                [(mod_key, *row) for row in files],
            )
