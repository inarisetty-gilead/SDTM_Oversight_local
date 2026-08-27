"""Studies: a named piece of work that survives closing the application.

Everything a reader decides about a study — which spec, which raw folder, every hand-edited
mapping, every preparation pipeline, every per-domain override — is kept in one JSON file
next to that study. Reopening it restores the work exactly.

JSON rather than a database on purpose: it is readable, diffable, survives this application,
and can be committed alongside the study documentation. A build is reproducible from the spec
and the raw data; a study is the record of the judgements applied on top.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

STUDY_FILE = "study.json"
SCHEMA = 1


def slug(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "-", str(name or "")).strip("-").lower()
    return s or "study"


@dataclass
class Study:
    """The persistent part of a study — everything except the data itself."""
    id: str
    name: str
    schema: int = SCHEMA
    created: str = ""
    updated: str = ""
    spec_path: str = ""
    raw_path: str = ""
    vendor_path: str = ""
    studyid: str = ""
    fmt: str = "xpt"
    include_unbuilt: bool = True
    name_match: int = 70
    notes: str = ""
    # the judgements: these are the work, and the reason a study is worth saving
    overrides: dict = field(default_factory=dict)
    edits: dict = field(default_factory=dict)
    pipelines: dict = field(default_factory=dict)
    draft_pipelines: dict = field(default_factory=dict)   # prep steps still being edited
    custom_fns: dict = field(default_factory=dict)        # the user's function library
    acrf_path: str = ""
    ecrf_path: str = ""
    standards_path: str = ""
    ta_path: str = ""
    acrf_report: dict | None = None
    template_overrides: dict = field(default_factory=dict)
    dedups: dict = field(default_factory=dict)
    last_run: str = ""

    def counts(self) -> dict:
        return {
            "edits": sum(len(v) for v in self.edits.values()),
            "pipelines": sum(len(v) for v in self.pipelines.values()),
            "overrides": len([k for k, v in self.overrides.items() if any(v.values())]),
            "domains_touched": len(set(self.edits) | set(self.pipelines) | set(self.overrides)),
        }


class StudyStore:
    """The folder of studies on disk."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, study_id: str) -> Path:
        return self.root / study_id / STUDY_FILE

    def list(self) -> list[dict]:
        out = []
        for folder in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            f = folder / STUDY_FILE
            if not f.is_file():
                continue
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            study = Study(**{k: v for k, v in d.items() if k in Study.__dataclass_fields__})
            out.append({**asdict(study), "counts": study.counts(),
                        "spec_exists": bool(study.spec_path) and Path(study.spec_path).exists(),
                        "raw_exists": bool(study.raw_path) and Path(study.raw_path).is_dir()})
        return sorted(out, key=lambda s: s.get("updated") or "", reverse=True)

    def create(self, name: str) -> Study:
        base = slug(name)
        study_id, n = base, 2
        while (self.root / study_id).exists():
            study_id, n = f"{base}-{n}", n + 1
        now = datetime.now().isoformat(timespec="seconds")
        study = Study(id=study_id, name=(name or study_id).strip(), created=now, updated=now)
        self.save(study)
        return study

    def load(self, study_id: str) -> Study | None:
        f = self.path(study_id)
        if not f.is_file():
            return None
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            return None
        return Study(**{k: v for k, v in d.items() if k in Study.__dataclass_fields__})

    def save(self, study: Study) -> Study:
        study.updated = datetime.now().isoformat(timespec="seconds")
        folder = self.root / study.id
        folder.mkdir(parents=True, exist_ok=True)
        # write to a temporary file first: a half-written study.json would lose the work
        tmp = folder / (STUDY_FILE + ".tmp")
        tmp.write_text(json.dumps(asdict(study), indent=2))
        tmp.replace(folder / STUDY_FILE)
        return study

    def delete(self, study_id: str) -> bool:
        folder = self.root / study_id
        if not (folder / STUDY_FILE).is_file():
            return False
        shutil.rmtree(folder, ignore_errors=True)
        return True

    def runs_dir(self, study_id: str) -> Path:
        d = self.root / study_id / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d
