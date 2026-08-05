from __future__ import annotations
import json
from pathlib import Path
from .models import ModEntry

EXCLUDE_DIRS={'.git','__pycache__','translations','translation','localization','localisations','strings','assets','sprites','images','preview','previews','backup','backups','onihub_backups'}
EXCLUDE_FILES={'mod_info.json','modinfo.json','manifest.json','metadata.json','package.json','workshop.json','preview.json','strings.json'}
HINTS=('config','setting','settings','option','options','preference','preferences','prefs')

def score_config(path: Path, root: Path) -> int:
    rel=path.relative_to(root)
    parts=[x.casefold() for x in rel.parts]
    stem=path.stem.casefold()
    score=0
    if any(h in stem for h in HINTS): score+=100
    if any(any(h in part for h in HINTS) for part in parts[:-1]): score+=60
    score+=max(0,20-(len(parts)-1)*3)
    try:
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(data,dict):
            score+=20
            score+=min(20,sum(isinstance(v,(str,int,float,bool)) or v is None for v in data.values()))
        elif isinstance(data,list):
            score-=15
    except Exception:
        return -999
    return score

def discover_json_configs(mod: ModEntry) -> list[Path]:
    root=mod.path
    if not root.is_dir(): return []
    found=[]
    for path in root.rglob('*.json'):
        if not path.is_file(): continue
        try: rel=path.relative_to(root)
        except ValueError: continue
        parts=[x.casefold() for x in rel.parts]
        if any(x in EXCLUDE_DIRS for x in parts[:-1]): continue
        if path.name.casefold() in EXCLUDE_FILES: continue
        score=score_config(path,root)
        if score <= -999: continue
        found.append((score,str(rel).casefold(),path))
    found.sort(key=lambda x:(-x[0],x[1]))
    return [x[2] for x in found]
