"""Make a run reproducible years after it was made.

An oversight finding may be questioned long after the fact — at a filing, an audit, an
inspection. "The tool said so" is not an answer if the tool cannot be made to say it again.

So every run records what went into it: the exact inputs by content hash, the engine version,
the interpreter, and the versions of the libraries that did the arithmetic. `verify` re-runs
the build from that record and reports whether the output is bit-for-bit what it was.

Hashes are of CONTENT, not paths. A file that moved is still the same file; a file that was
edited is not, whatever it is called.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CHUNK = 1 << 20
MANIFEST = "provenance.json"


def file_digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def dataset_digest(path: str | Path) -> str:
    """Hash a dataset by its CONTENT, not its bytes.

    A SAS transport file carries its creation timestamp in the header, so writing the same
    data twice produces different bytes. Comparing bytes would report every re-run as a
    difference, which is worse than useless — it trains the reader to ignore the check. What
    reproducibility means for a dataset is that the records are the same."""
    from .rawio import read_dataset

    frame = read_dataset(path)
    h = hashlib.sha256()
    h.update(("\x1f".join(str(c) for c in frame.columns) + "\n").encode())
    h.update(f"{len(frame)}\n".encode())
    for row in frame.astype(object).where(frame.notna(), "").itertuples(index=False, name=None):
        h.update(("\x1f".join(str(v) for v in row) + "\n").encode())
    return h.hexdigest()


def outputs_digest(folder: str | Path) -> dict:
    """Content digests for every dataset a run produced."""
    root = Path(folder)
    files: dict[str, str] = {}
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                try:
                    files[str(p.relative_to(root))] = dataset_digest(p)
                except Exception:                                # noqa: BLE001
                    files[str(p.relative_to(root))] = f"unreadable:{file_digest(p)[:16]}"
    combined = hashlib.sha256(
        "\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()).hexdigest()
    return {"root": str(root), "files": files, "digest": combined, "count": len(files),
            "basis": "dataset content, not file bytes"}


def folder_digest(folder: str | Path, suffixes: tuple[str, ...] | None = None) -> dict:
    """Hash every data file in a folder, and the folder as a whole.

    Per-file digests matter as much as the total: when a re-run differs, the useful question
    is *which extract changed*, and that is answerable only if each was recorded."""
    root = Path(folder)
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if suffixes and p.suffix.lower() not in suffixes:
            continue
        files[str(p.relative_to(root))] = file_digest(p)
    combined = hashlib.sha256(
        "\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()).hexdigest()
    return {"root": str(root), "files": files, "digest": combined, "count": len(files)}


def environment() -> dict:
    """The versions that did the arithmetic. A pandas upgrade can change a rounding edge."""
    versions = {}
    for name in ("pandas", "numpy", "pyreadstat", "openpyxl", "pyarrow"):
        try:
            versions[name] = __import__(name).__version__
        except Exception:                                        # noqa: BLE001
            versions[name] = None
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "libraries": versions,
    }


@dataclass
class RunRecord:
    """Everything needed to reproduce one build."""
    tool_version: str
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    spec: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    vendor: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    env: dict = field(default_factory=environment)

    def as_dict(self) -> dict:
        return {
            "tool_version": self.tool_version, "created": self.created,
            "spec": self.spec, "raw": self.raw, "vendor": self.vendor,
            "options": self.options, "outputs": self.outputs, "environment": self.env,
        }


def record_run(out_dir: str | Path, tool_version: str, spec_path: str, raw_path: str,
               options: dict, vendor_path: str = "") -> dict:
    """Write provenance.json beside a run's outputs."""
    rec = RunRecord(tool_version=tool_version, options=dict(options))
    if spec_path and Path(spec_path).exists():
        rec.spec = {"path": str(Path(spec_path).resolve()),
                    "digest": file_digest(spec_path),
                    "size": Path(spec_path).stat().st_size}
    if raw_path and Path(raw_path).is_dir():
        rec.raw = folder_digest(raw_path)
    if vendor_path and Path(vendor_path).is_dir():
        rec.vendor = folder_digest(vendor_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST).write_text(json.dumps(rec.as_dict(), indent=2))
    return rec.as_dict()


def record_outputs(out_dir: str | Path) -> dict:
    """Hash what the run produced, so a later re-run can be compared against it."""
    out = Path(out_dir)
    path = out / MANIFEST
    rec = json.loads(path.read_text()) if path.exists() else {"outputs": {}}
    datasets = out / "datasets"
    rec["outputs"] = outputs_digest(datasets) if datasets.is_dir() else {}
    path.write_text(json.dumps(rec, indent=2))
    return rec


def load(out_dir: str | Path) -> dict | None:
    p = Path(out_dir) / MANIFEST
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def compare_to(record: dict, out_dir: str | Path) -> dict:
    """Compare a fresh run's outputs against what the record says they were."""
    was = (record.get("outputs") or {}).get("files") or {}
    now = outputs_digest(Path(out_dir) / "datasets")["files"]
    changed = sorted(k for k in set(was) & set(now) if was[k] != now[k])
    return {
        "reproduced": not changed and set(was) == set(now),
        "identical": sorted(k for k in set(was) & set(now) if was[k] == now[k]),
        "changed": changed,
        "missing": sorted(set(was) - set(now)),
        "added": sorted(set(now) - set(was)),
    }


def describe_drift(record: dict) -> list[str]:
    """What about this machine differs from the one that made the run."""
    then, now = record.get("environment", {}), environment()
    notes = []
    if then.get("python") and then["python"] != now["python"]:
        notes.append(f"Python {then['python']} then, {now['python']} now")
    for lib, was in (then.get("libraries") or {}).items():
        is_now = now["libraries"].get(lib)
        if was and is_now and was != is_now:
            notes.append(f"{lib} {was} then, {is_now} now")
    return notes
