"""FastAPI application: load a spec, build every SDTM domain from raw data, then compare
against a vendor delivery.

Binds to 127.0.0.1 only. Makes no outbound network calls. Reads the filesystem the user
points it at and writes only into the run's output folder.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sdtm_builder import __version__, acrf as acrf_module, prep as prep_module, report
from sdtm_builder.build import build_domain, build_study
from sdtm_builder.compare import compare_study, discover_vendor
from sdtm_builder.rawio import RawStore
from sdtm_builder.spec import analyse as analyse_spec, load_spec
from sdtm_builder import templates as templates_registry
from sdtm_builder.translate import raw_refs
from sdtm_builder.ops import SAS_FUNCTIONS
from sdtm_builder.rawio import norm_key
from sdtm_builder.util import s, upper
from sdtm_builder.writers import write_dataset, write_manifest

from .jobs import JobRunner
from .studies import Study, StudyStore

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
RUNS = ROOT / "runs"
STUDIES = StudyStore(ROOT / "studies")


# ── session state (single-user desktop app) ─────────────────────────────────
@dataclass
class Session:
    spec_path: str = ""
    raw_path: str = ""
    vendor_path: str = ""
    out_dir: str = ""
    studyid: str = ""
    spec: object = None
    store: object = None
    results: dict = field(default_factory=dict)
    comps: dict = field(default_factory=dict)
    build_meta: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)      # label -> path
    overrides: dict = field(default_factory=dict)    # DOMAIN -> {base, sort, prep_mode, prep, keys}
    edits: dict = field(default_factory=dict)        # DOMAIN -> {VARIABLE -> block override}
    dedups: dict = field(default_factory=dict)       # DOMAIN -> {enabled, keys, keep}
    pipelines: dict = field(default_factory=dict)    # DOMAIN -> [prep step, ...]
    draft_pipelines: dict = field(default_factory=dict)  # DOMAIN -> steps being edited, not yet applied
    custom_fns: dict = field(default_factory=dict)       # name -> user-defined derivation
    acrf_path: str = ""                              # annotated CRF PDF
    ecrf_path: str = ""                              # eCRF spec (question text source)
    std_acrf_path: str = ""                          # the internal standards aCRF
    std_ecrf_path: str = ""                          # its eCRF spec (question source)
    crf_cmp: dict | None = None                      # last vendor-vs-standards CRF comparison
    standards_path: str = ""                         # standards mapping workbook
    ta_path: str = ""                                # therapeutic-area spec workbook
    acrf_report: dict | None = None                  # last aCRF check
    template_overrides: dict = field(default_factory=dict)  # VARIABLE -> {"enabled": False}
    preview_outputs: set = field(default_factory=set)  # prepared datasets from an unapplied run
    study_id: str = ""
    study_name: str = ""
    open_problems: list = field(default_factory=list)
    synthetic: dict | None = None
    spec_sig: tuple = ()                             # (path, mtime, size) of the loaded spec
    raw_sig: tuple = ()                              # (path, file count, newest mtime) of the raw folder
    fmt: str = "xpt"
    include_unbuilt: bool = True
    name_match: int = 70


SESSION = Session()
RUNNER = JobRunner()

app = FastAPI(title="SDTM Oversight", version=__version__)


# ── models ──────────────────────────────────────────────────────────────────
class PathIn(BaseModel):
    path: str


class BuildIn(BaseModel):
    domains: list[str] | None = None
    studyid: str = ""
    fmt: str = "xpt"
    include_unbuilt: bool = True
    name_match: int = 70
    bases: dict[str, str] = {}


class EditIn(BaseModel):
    mtype: str                              # assign | constant | sequence | drop | derived
    dataset: str = ""
    column: str = ""
    value: str = ""
    recipe: str = ""
    codelist: str = ""
    args: dict = {}
    note: str = ""


class SynthIn(BaseModel):
    out: str = ""
    subjects: int = 40
    visits: int = 5
    events: int = 3
    studyid: str = "SYNTH-001"
    seed: str = "sdtm-oversight"


class PipelineIn(BaseModel):
    steps: list[dict] = []
    # which output the domain's records (and matching mappings) follow:
    # None = leave the current choice alone, "" = follow the LAST step (the default),
    # a name = pin to that output, so adding prep2 later never moves prep1's mappings
    base: str | None = None


class DedupIn(BaseModel):
    enabled: bool = False
    keys: list[str] = []
    keep: str = "first"


class OverrideIn(BaseModel):
    base: str = ""
    sort: list[str] = []
    prep_mode: str = "auto"                # auto | off | custom
    prep: dict | None = None               # a custom prep step when prep_mode == "custom"
    keys: list[str] = []                   # record-matching keys for the comparison


class CompareIn(BaseModel):
    path: str
    domains: list[str] = []          # empty = every built domain
    ignore_case: bool = False
    numeric_tolerance: float = 1e-9
    ignore_vars: list[str] = []
    keys: dict[str, list[str]] = {}



# ── session persistence ─────────────────────────────────────────────────────
# A build can take minutes. Restarting the application should not throw it away, and neither
# should closing the browser tab. The built frames are written beside the run's outputs and
# reloaded on start. Persistence is best-effort: any failure leaves the app fully usable and
# is reported, never raised.
SESSION_FILE = ".session.pkl"
SESSION_MAX_MB = 750


def _save_session() -> None:
    if not SESSION.out_dir:
        return
    try:
        rows = sum(len(r.dataset) for r in SESSION.results.values() if r.ok)
        if rows > 5_000_000:            # very large study: the datasets on disk are the record
            print(f"  session not cached ({rows:,} rows) — rebuild after a restart")
            return
        import pickle
        payload = {
            "tool_version": __version__,
            "results": SESSION.results, "comps": SESSION.comps,
            "build_meta": SESSION.build_meta, "outputs": SESSION.outputs,
            "overrides": SESSION.overrides, "studyid": SESSION.studyid,
            "fmt": SESSION.fmt, "include_unbuilt": SESSION.include_unbuilt,
            "spec_path": SESSION.spec_path, "raw_path": SESSION.raw_path,
            "vendor_path": SESSION.vendor_path, "out_dir": SESSION.out_dir,
            "spec_sig": SESSION.spec_sig, "raw_sig": SESSION.raw_sig,
        }
        path = Path(SESSION.out_dir) / SESSION_FILE
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        if tmp.stat().st_size > SESSION_MAX_MB * 1024 * 1024:
            tmp.unlink()
            print(f"  session not cached (over {SESSION_MAX_MB} MB)")
            return
        tmp.replace(path)
    except Exception as exc:                                  # noqa: BLE001 - never fatal
        print(f"  could not cache the session: {exc}")


def _load_run_cache(run_dir: Path) -> bool:
    """Restore a run's build and comparison into the session — only when the study's inputs
    are still the ones that made it. A build presented against a changed spec or changed raw
    data would be a stale answer wearing a current face."""
    cache = run_dir / SESSION_FILE
    if not cache.exists():
        return False
    try:
        import pickle
        with open(cache, "rb") as fh:
            d = pickle.load(fh)
    except Exception:                                            # noqa: BLE001
        return False
    if d.get("spec_sig") != SESSION.spec_sig or d.get("raw_sig") != SESSION.raw_sig:
        return False                       # inputs changed since this run — rebuild, don't resume
    if d.get("tool_version") != __version__:
        # the engine changed since this run was made. Its datasets on disk are still the
        # record, but resuming them into a newer engine would present old results as the new
        # code's output — and may not even unpickle. One rebuild after an upgrade is the cost
        # of never showing a stale answer with a current face.
        return False
    for key in ("results", "comps", "build_meta", "outputs"):
        if key in d:
            setattr(SESSION, key, d[key])
    SESSION.out_dir = d.get("out_dir", str(run_dir))
    SESSION.vendor_path = d.get("vendor_path", SESSION.vendor_path)
    # prep outputs are in-memory frames — rebuild them so the domain views resolve
    for res in SESSION.results.values():
        if res.ok and res.prep_step is not None and SESSION.store is not None \
                and res.base_dataset not in SESSION.store.refs:
            try:
                SESSION.store.put(res.prep_step.name,
                                  prep_module.apply_step(res.prep_step, SESSION.store))
            except Exception:                                    # noqa: BLE001
                pass
    return True


def _restore_session() -> str:
    """Reload the most recent run, so a restart resumes where the user left off."""
    candidates = []
    if RUNS.is_dir():
        candidates += [d for d in RUNS.iterdir() if d.is_dir()]
    if STUDIES.root.is_dir():
        for study_dir in STUDIES.root.iterdir():
            runs = study_dir / "runs"
            if runs.is_dir():
                candidates += [d for d in runs.iterdir() if d.is_dir()]
    for run in sorted(candidates, key=lambda d: d.name, reverse=True):
        cache = run / SESSION_FILE
        if not cache.exists():
            continue
        # A run that belongs to a study: reopen the WHOLE study, not just the run's
        # frames. The preparation pipelines, hand edits and prepared datasets live in
        # study.json — and a session without the study open cannot even autosave the
        # reader's next decision. Resuming only the run made them all vanish on restart.
        sid = (run.parent.parent.name
               if run.parent.name == "runs" and run.parent.parent.parent == STUDIES.root
               else "")
        if sid:
            study = STUDIES.load(sid)
            if study is not None:
                try:
                    _apply_study(study)
                    n_prep = sum(len(v) for v in SESSION.pipelines.values()) \
                        + sum(len(v) for v in SESSION.draft_pipelines.values())
                    return (f"reopened study '{study.name}': {len(SESSION.results)} "
                            f"domain(s) built"
                            + (f", {n_prep} preparation step(s) restored" if n_prep else ""))
                except Exception as exc:                       # noqa: BLE001
                    print(f"  could not reopen study {sid}: {exc}")
                    continue
        try:
            import pickle
            with open(cache, "rb") as fh:
                d = pickle.load(fh)
            spec_path, raw_path = d.get("spec_path", ""), d.get("raw_path", "")
            if not (spec_path and Path(spec_path).exists() and raw_path and Path(raw_path).is_dir()):
                continue
            SESSION.spec = load_spec(spec_path)
            SESSION.store = RawStore.discover(raw_path)
            for key in ("results", "comps", "build_meta", "outputs", "overrides", "studyid",
                        "fmt", "include_unbuilt", "spec_sig", "raw_sig"):
                if key in d:
                    setattr(SESSION, key, d[key])
            SESSION.spec_path, SESSION.raw_path = spec_path, raw_path
            SESSION.vendor_path, SESSION.out_dir = d.get("vendor_path", ""), d.get("out_dir", "")
            # prep outputs are in-memory frames — put them back so the domain view resolves
            for res in SESSION.results.values():
                if res.ok and res.prep_step is not None and res.base_dataset not in SESSION.store.refs:
                    try:
                        SESSION.store.put(res.prep_step.name,
                                          prep_module.apply_step(res.prep_step, SESSION.store))
                    except Exception:                          # noqa: BLE001
                        pass
            return f"resumed {run.name}: {len(SESSION.results)} domain(s) built"
        except Exception as exc:                               # noqa: BLE001
            print(f"  could not resume {run.name}: {exc}")
            continue
    return ""


def _autosave() -> None:
    """Persist the open study after anything that changes it.

    Saving on a button is saving that will one day not be pressed. Everything the reader
    decides is written as it is decided, so closing the application loses nothing."""
    if not SESSION.study_id:
        return
    study = STUDIES.load(SESSION.study_id)
    if study is None:
        return
    study.name = SESSION.study_name or study.name
    study.spec_path = SESSION.spec_path
    study.raw_path = SESSION.raw_path
    study.vendor_path = SESSION.vendor_path
    study.studyid = SESSION.studyid
    study.fmt = SESSION.fmt
    study.include_unbuilt = SESSION.include_unbuilt
    study.name_match = SESSION.name_match
    study.overrides = SESSION.overrides
    study.edits = SESSION.edits
    study.pipelines = SESSION.pipelines
    study.draft_pipelines = SESSION.draft_pipelines
    study.custom_fns = SESSION.custom_fns
    study.template_overrides = SESSION.template_overrides
    study.acrf_path, study.standards_path, study.ta_path = (
        SESSION.acrf_path, SESSION.standards_path, SESSION.ta_path)
    study.ecrf_path = SESSION.ecrf_path
    study.std_acrf_path, study.std_ecrf_path = SESSION.std_acrf_path, SESSION.std_ecrf_path
    study.crf_cmp = SESSION.crf_cmp
    study.acrf_report = SESSION.acrf_report
    study.dedups = SESSION.dedups
    if SESSION.out_dir:
        study.last_run = SESSION.out_dir
    STUDIES.save(study)


def _apply_study(study: Study) -> None:
    """Restore a study into the live session."""
    global SESSION
    SESSION = Session()
    SESSION.study_id, SESSION.study_name = study.id, study.name
    SESSION.spec_path, SESSION.raw_path = study.spec_path, study.raw_path
    SESSION.vendor_path, SESSION.studyid = study.vendor_path, study.studyid
    SESSION.fmt, SESSION.include_unbuilt = study.fmt, study.include_unbuilt
    SESSION.name_match = study.name_match
    SESSION.overrides = dict(study.overrides)
    SESSION.edits = dict(study.edits)
    SESSION.pipelines = dict(study.pipelines)
    SESSION.draft_pipelines = dict(study.draft_pipelines)
    SESSION.custom_fns = dict(study.custom_fns)
    SESSION.template_overrides = dict(study.template_overrides)
    SESSION.acrf_path, SESSION.standards_path, SESSION.ta_path = (
        study.acrf_path, study.standards_path, study.ta_path)
    SESSION.ecrf_path = study.ecrf_path
    SESSION.std_acrf_path, SESSION.std_ecrf_path = study.std_acrf_path, study.std_ecrf_path
    SESSION.crf_cmp = study.crf_cmp or None
    SESSION.acrf_report = study.acrf_report or None
    SESSION.dedups = dict(study.dedups)
    # reopen the inputs so the reader lands where they left off, not on an empty form
    SESSION.open_problems = []
    if study.spec_path:
        try:
            SESSION.spec = load_spec(study.spec_path)
            SESSION.spec_sig = _file_sig(Path(study.spec_path).resolve())
        except Exception as exc:                                 # noqa: BLE001
            SESSION.spec = None
            SESSION.open_problems.append(f"the mapping spec could not be reopened: {_fs_hint(exc)}")
    if study.raw_path and Path(study.raw_path).is_dir():
        try:
            SESSION.store = RawStore.discover(study.raw_path)
            SESSION.raw_sig = _folder_sig(Path(study.raw_path).resolve())
            from sdtm_builder.synth import read_marker
            SESSION.synthetic = read_marker(study.raw_path)
        except Exception as exc:                                 # noqa: BLE001
            SESSION.store = None
            SESSION.open_problems.append(f"the raw data could not be reopened: {_fs_hint(exc)}")
    # Re-materialise the prepared datasets. The pipeline STEPS were restored above, but
    # their OUTPUTS were in-memory frames — without re-running them, a reopened study
    # lists the steps while every dataset picker is missing the prepared datasets,
    # which reads as "my preparation is gone".
    if SESSION.store is not None:
        for dom, steps in list(SESSION.pipelines.items()):
            try:
                outputs, _reports = prep_module.run_pipeline(steps, SESSION.store, dom)
                for name, frame in outputs.items():
                    SESSION.store.put(name, frame)
            except Exception as exc:                             # noqa: BLE001
                SESSION.open_problems.append(
                    f"{dom}: the prepared datasets could not be rebuilt: {exc}")
        for dom, steps in list(SESSION.draft_pipelines.items()):
            try:
                outputs, _reports = prep_module.run_pipeline(steps, SESSION.store, dom)
                for name, frame in outputs.items():
                    SESSION.store.put(name, frame)
                    SESSION.preview_outputs.add(name)
            except Exception:                                    # noqa: BLE001
                pass          # a draft may be half-finished — restoring the steps is enough
    # resume the last build, so reopening a study lands where the reader left it —
    # built domains, comparison and all — rather than on an empty build view
    if study.last_run and Path(study.last_run).is_dir():
        _load_run_cache(Path(study.last_run))


def _fs_hint(exc_or_msg) -> str:
    """Turn a bare filesystem error into something the reader can act on. 'Operation not
    permitted' on macOS is the OS withholding folder access from this app — nothing is wrong
    with the file, and no retry will fix it."""
    msg = str(exc_or_msg)
    if "Operation not permitted" in msg or "Errno 1]" in msg or "Errno 13]" in msg:
        return (msg + " — macOS is blocking this app's access to that folder (common for "
                "Downloads/Desktop/Documents). Either move the file somewhere unrestricted "
                "such as a folder under ~/Developer, or grant access in System Settings → "
                "Privacy & Security → Files and Folders.")
    return msg


# ── studies ─────────────────────────────────────────────────────────────────
class StudyIn(BaseModel):
    name: str = ""


@app.get("/api/studies")
def list_studies():
    return {"studies": STUDIES.list(), "open": SESSION.study_id}


@app.post("/api/studies")
def create_study(body: StudyIn):
    if not s(body.name):
        raise HTTPException(400, "give the study a name")
    study = STUDIES.create(body.name)
    _apply_study(study)
    return {"study": STUDIES.list()[0] if STUDIES.list() else None, "id": study.id}


@app.post("/api/studies/{study_id}/open")
def open_study(study_id: str):
    if RUNNER.busy:
        raise HTTPException(409, "a job is running")
    study = STUDIES.load(study_id)
    if study is None:
        raise HTTPException(404, f"no study '{study_id}'")
    _apply_study(study)
    return {"id": study.id, "name": study.name, "spec": study.spec_path,
            "raw": study.raw_path, "vendor": study.vendor_path,
            "restored": study.counts(),
            "problems": SESSION.open_problems,
            "built": sorted(SESSION.results), "compared": sorted(SESSION.comps)}


@app.post("/api/studies/{study_id}/close")
def close_study(study_id: str):
    global SESSION
    _autosave()
    SESSION = Session()
    return {"ok": True}


@app.delete("/api/studies/{study_id}")
def delete_study(study_id: str):
    global SESSION
    if SESSION.study_id == study_id:
        SESSION = Session()
    if not STUDIES.delete(study_id):
        raise HTTPException(404, f"no study '{study_id}'")
    return {"ok": True}


# ── filesystem browser ──────────────────────────────────────────────────────
DATA_EXT = {".sas7bdat", ".xpt", ".csv", ".parquet", ".xlsx", ".xls", ".xlsm", ".txt", ".tsv",
            ".pdf"}   # the aCRF check reads annotated CRF PDFs


@app.get("/api/browse")
def browse(path: str = ""):
    """List a directory so the user can pick folders and files without typing paths."""
    p = Path(os.path.expanduser(path)) if path else Path.home()
    try:
        p = p.resolve()
        if p.is_file():
            p = p.parent
        if not p.is_dir():
            raise HTTPException(400, f"not a folder: {p}")
        dirs, files = [], []
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
                elif child.suffix.lower() in DATA_EXT:
                    files.append({"name": child.name, "path": str(child),
                                  "size": child.stat().st_size})
            except OSError:
                continue
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
                "dirs": dirs, "files": files,
                "shortcuts": [{"name": n, "path": str(q)} for n, q in (
                    ("Home", Path.home()), ("Desktop", Path.home() / "Desktop"),
                    ("Documents", Path.home() / "Documents"),
                    ("Downloads", Path.home() / "Downloads")) if q.is_dir()]}
    except PermissionError:
        raise HTTPException(403, f"no permission to read {p}")



def _file_sig(path: Path) -> tuple:
    # nanosecond mtime: whole seconds would miss a spec edited in the same second it loaded
    st = path.stat()
    return (str(path), st.st_mtime_ns, st.st_size)


def _folder_sig(path: Path) -> tuple:
    """Cheap fingerprint of a data folder: enough to notice a re-delivery, cheap enough to
    take on every load."""
    newest, count, total = 0, 0, 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.name.startswith("."):
                st = child.stat()
                count += 1
                total += st.st_size
                newest = max(newest, st.st_mtime_ns)
        except OSError:
            continue
    return (str(path), count, total, newest)


def _discard_build(reason: str) -> bool:
    """Drop a build whose inputs have changed. Returns True if anything was actually dropped,
    so the interface can clear the stale tables instead of leaving them on screen."""
    had = bool(SESSION.results or SESSION.comps)
    SESSION.results, SESSION.comps, SESSION.outputs = {}, {}, {}
    if had:
        print(f"  build discarded: {reason}")
    return had


# ── step 1: the mapping spec ────────────────────────────────────────────────
@app.post("/api/spec")
def set_spec(body: PathIn):
    try:
        spec = load_spec(os.path.expanduser(body.path))
    except (FileNotFoundError, ValueError, PermissionError) as exc:
        raise HTTPException(400, _fs_hint(exc))
    resolved = Path(os.path.expanduser(body.path)).resolve()
    sig = _file_sig(resolved)
    # Re-reading the SAME, UNCHANGED spec must not throw away a build that took minutes.
    # A different file, or the same path with new contents, invalidates it and says so.
    cleared = False
    if SESSION.spec_sig and sig != SESSION.spec_sig:
        cleared = _discard_build("the mapping spec changed")
    SESSION.spec_path, SESSION.spec, SESSION.spec_sig = str(resolved), spec, sig
    _autosave()
    return {"path": SESSION.spec_path, "cleared": cleared, "domains": spec.domain_names,
            "variables": sum(len(r) for r in spec.domains.values()),
            "codelists": len(spec.codelists),
            "coverage": analyse_spec(spec),
            "toc": spec.toc, "active": spec.active_domains, "inactive": spec.inactive_domains,
            "skipped": [{"sheet": s, "why": w} for s, w in spec.skipped_sheets]}


# ── synthetic raw data from the spec ────────────────────────────────────────
@app.post("/api/synth")
def make_synthetic(body: SynthIn):
    """Generate a raw folder from the spec's own Input Variables, for studies with no extract
    yet. The data is invented; the folder is marked so the rest of the app can say so."""
    if SESSION.spec is None:
        raise HTTPException(400, "load the mapping spec first")
    from sdtm_builder.synth import SynthOptions, generate
    default_dir = (STUDIES.root / SESSION.study_id / "synthetic_raw"
                   if SESSION.study_id else RUNS / "synthetic_raw")
    out = Path(os.path.expanduser(body.out)) if body.out else default_dir
    try:
        res = generate(SESSION.spec, out, SynthOptions(
            subjects=max(1, min(body.subjects, 2000)), visits=max(1, min(body.visits, 30)),
            events_per_subject=max(1, min(body.events, 20)),
            studyid=body.studyid, seed=body.seed))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return res


# ── reviewing the spec ──────────────────────────────────────────────────────
@app.get("/api/spec/domains")
def spec_domains():
    """Every domain sheet, with its TOC standing and row counts — the spec's own table of
    contents, reviewable before anything is built."""
    if SESSION.spec is None:
        raise HTTPException(400, "load the mapping spec first")
    spec = SESSION.spec
    out = []
    for dom in spec.domain_names:
        rows = spec.rows(dom)
        toc = spec.toc.get(dom, {})
        out.append({
            "domain": dom, "variables": len(rows),
            "supp": sum(1 for r in rows if r.is_supp),
            "active": spec.is_active(dom), "in_toc": dom in spec.toc,
            "label": toc.get("label", ""), "class": toc.get("class", ""),
            "structure": toc.get("structure", ""),
        })
    return {"domains": out, "has_toc": bool(spec.toc),
            "toc_only": sorted(set(spec.toc) - set(spec.domains))}


@app.get("/api/spec/{domain}/rows")
def spec_rows(domain: str):
    """One domain's spec sheet, row by row, exactly as written."""
    if SESSION.spec is None:
        raise HTTPException(400, "load the mapping spec first")
    dom = upper(domain)
    rows = SESSION.spec.rows(dom)
    if not rows:
        raise HTTPException(404, f"{dom} is not in the mapping spec")
    return {"domain": dom, "active": SESSION.spec.is_active(dom),
            "rows": [{
                "variable": r.variable, "label": r.label, "action": upper(r.action),
                "input_variables": r.input_variables, "mapping_rule": r.mapping_rule,
                "sas_code": r.sas_code, "codelist": upper(r.codelist), "role": r.role,
                "origin": r.origin, "dataset": r.dataset, "type": r.type,
                "length": r.length, "supp": r.is_supp, "sheet_row": r.row_number,
            } for r in rows]}


# ── step 2: the raw data ────────────────────────────────────────────────────
@app.post("/api/raw")
def set_raw(body: PathIn):
    if SESSION.spec is None:
        raise HTTPException(400, "load the mapping spec first")
    try:
        store = RawStore.discover(os.path.expanduser(body.path))
    except (NotADirectoryError, PermissionError) as exc:
        raise HTTPException(400, _fs_hint(exc))
    resolved = Path(os.path.expanduser(body.path)).resolve()
    sig = _folder_sig(resolved)
    cleared = False
    if SESSION.raw_sig and sig != SESSION.raw_sig:
        cleared = _discard_build("the raw data folder changed")
    SESSION.raw_path, SESSION.store, SESSION.raw_sig = str(resolved), store, sig

    datasets = []
    for name in sorted(store.refs):
        ref = store.refs[name]
        try:
            df = store.get(name)
            datasets.append({"name": name, "rows": len(df), "cols": len(df.columns),
                             "file": ref.path.name, "error": ""})
        except Exception as exc:                                  # noqa: BLE001
            datasets.append({"name": name, "rows": None, "cols": None,
                             "file": ref.path.name, "error": str(exc)})

    # which domains can actually be built, and which spec sources are missing
    missing: dict[str, set] = {}
    coverage = []
    for dom in SESSION.spec.domain_names:
        refs = set()
        for r in SESSION.spec.rows(dom):
            refs |= set(raw_refs(r.input_variables))
        have = sum(1 for ds, col in refs if store.has(ds) and col in store.columns(store.resolve(ds)))
        for ds, col in refs:
            if not store.has(ds):
                missing.setdefault(ds, set()).add(f"{dom}.{col}")
            elif col not in store.columns(store.resolve(ds)):
                missing.setdefault(f"{ds}.{col}", set()).add(dom)
        coverage.append({"domain": dom, "sources": len(refs), "resolved": have,
                         "has_own_dataset": bool(store.resolve(dom))})

    from sdtm_builder.synth import read_marker
    SESSION.synthetic = read_marker(resolved)
    _autosave()
    return {"path": SESSION.raw_path, "cleared": cleared, "datasets": datasets,
            "synthetic": SESSION.synthetic, "coverage": coverage,
            "built": sorted(SESSION.results),
            "missing": [{"source": k, "used_by": sorted(v)[:12], "count": len(v)}
                        for k, v in sorted(missing.items())]}


# ── step 3: build ───────────────────────────────────────────────────────────
@app.post("/api/build")
def start_build(body: BuildIn):
    if SESSION.spec is None or SESSION.store is None:
        raise HTTPException(400, "load the mapping spec and the raw data folder first")
    if RUNNER.busy:
        raise HTTPException(409, "a job is already running")

    if body.domains:
        targets = [upper(d) for d in body.domains]
    elif SESSION.spec.toc:
        targets = SESSION.spec.active_domains
    else:
        targets = SESSION.spec.domain_names
    bad = [d for d in targets if d not in SESSION.spec.domains]
    if bad:
        raise HTTPException(400, f"not in the mapping spec: {', '.join(bad)}")

    # A run belongs to the study that made it. Everything about a study — its judgements in
    # study.json, its builds, its reports — lives under that one folder, so archiving or
    # handing over a study is copying a single directory.
    runs_root = STUDIES.runs_dir(SESSION.study_id) if SESSION.study_id else RUNS
    out = runs_root / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    SESSION.out_dir, SESSION.studyid = str(out), body.studyid
    SESSION.fmt, SESSION.include_unbuilt = body.fmt, body.include_unbuilt
    SESSION.name_match = body.name_match
    SESSION.comps, SESSION.outputs = {}, {}
    for dom, ov in body.bases.items():                    # --base style overrides from the UI
        SESSION.overrides.setdefault(upper(dom), {})["base"] = ov
    ovr = SESSION.overrides

    def work(progress):
        done = {"n": 0}

        def tick(dom):
            done["n"] += 1
            progress(f"building {dom}", step=done["n"])

        results = build_study(
            SESSION.spec, SESSION.store, domains=targets, studyid=body.studyid,
            base_overrides={d: o["base"] for d, o in ovr.items() if o.get("base")},
            sort_overrides={d: o["sort"] for d, o in ovr.items() if o.get("sort")},
            prep_modes={d: o["prep_mode"] for d, o in ovr.items() if o.get("prep_mode")},
            prep_overrides={d: o["prep"] for d, o in ovr.items()
                            if o.get("prep_mode") == "custom" and o.get("prep")},
            prep_pipelines=SESSION.pipelines, edits=SESSION.edits, dedups=SESSION.dedups,
            custom_fns=SESSION.custom_fns, template_overrides=SESSION.template_overrides,
            name_match_threshold=body.name_match,
            include_unbuilt=body.include_unbuilt, progress=tick)
        # Building a chosen subset ACCUMULATES: DM now, AE next, one by one — each build adds
        # to the session rather than wiping the domains built before it. Only a full build
        # (no selection) replaces everything.
        if body.domains:
            SESSION.results = {**SESSION.results, **results}
        else:
            SESSION.results = results

        if body.fmt != "none":
            data_dir = out / "datasets"
            n = len([r for r in results.values() if r.ok])
            for i, (dom, res) in enumerate(sorted(results.items()), start=1):
                if not res.ok:
                    continue
                progress(f"writing {dom}.{body.fmt}", step=len(targets), total=len(targets))
                labels = {b.variable: b.label for b in res.blocks}
                write_dataset(res.dataset, data_dir, dom, body.fmt, labels)
                if res.supp is not None and len(res.supp):
                    write_dataset(res.supp, data_dir, f"SUPP{dom}", body.fmt)
            SESSION.outputs["datasets"] = str(data_dir)

        progress("writing the build manifest")
        meta = {"spec": SESSION.spec_path, "raw": SESSION.raw_path,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "tool": f"sdtm_builder {__version__}", "studyid": body.studyid,
                "domains": sorted(SESSION.results)}
        SESSION.build_meta = meta
        jpath, xpath = write_manifest(SESSION.results, out, meta)
        SESSION.outputs["manifest"] = str(xpath)
        SESSION.outputs["manifest_json"] = str(jpath)
        html = report.write_html_report(SESSION.results, None, out / "build_report.html", meta)
        SESSION.outputs["report"] = str(html)
        _save_session()
        _autosave()

    RUNNER.start("build", work, total=len(targets))
    return {"started": True, "domains": targets, "out_dir": str(out)}


@app.get("/api/build/results")
def build_results():
    if not SESSION.results:
        return {"domains": [], "not_built": [], "out_dir": SESSION.out_dir}
    domains, not_built = [], []
    for dom in sorted(SESSION.results):
        r = SESSION.results[dom]
        c = r.counts
        domains.append({
            "domain": dom, "ok": r.ok, "error": r.error, "base": r.base_dataset,
            "rows": 0 if r.dataset is None else len(r.dataset),
            "supp_rows": 0 if r.supp is None else len(r.supp),
            "built": c["built"], "dropped": c["dropped"],
            "not_built": c["not_built"] + c["error"],
            "empty": c.get("empty", 0),
            "warnings": r.warnings,
            "prep": r.prep_step.as_dict() if r.prep_step else None,
            "edited": c.get("edited", 0),
            "name_matched": c.get("name_matched", 0),
            "override": SESSION.overrides.get(dom, {}),
        })
        for b in r.blocks:
            if b.status in ("not_built", "error", "empty"):
                not_built.append({"domain": dom, "variable": b.variable, "label": b.label,
                                  "why": b.error or b.reason or "no deterministic rule in the spec",
                                  "spec_rule": b.mapping_rule, "spec_row": b.sheet_row})
    # Group the reasons, so "why is coverage low?" has an answer rather than a long list.
    buckets: dict[str, dict] = {}
    for row in not_built:
        why = row["why"]
        if "narrative mapping rule" in why:
            key = "The spec states the rule in prose, with no machine-readable Input Variables"
        elif "no Input Variables and no Mapping Action" in why:
            key = "The spec row is blank — no source and no rule"
        elif "no resolvable source" in why:
            key = "The spec names a Mapping Action but no source this build could resolve"
        elif "is not in raw dataset" in why or "not found" in why:
            key = "The source the spec names is not in the raw data"
        elif "no deterministic rule" in why:
            key = "The derivation has no deterministic rule in this engine"
        elif "not built in this domain" in why or "not built yet" in why:
            key = "It depends on another variable that was not built"
        else:
            key = "Other"
        b = buckets.setdefault(key, {"reason": key, "count": 0, "examples": []})
        b["count"] += 1
        if len(b["examples"]) < 6:
            b["examples"].append(f"{row['domain']}.{row['variable']}")
    return {"domains": domains, "not_built": not_built, "out_dir": SESSION.out_dir,
            "synthetic": SESSION.synthetic,
            "not_built_reasons": sorted(buckets.values(), key=lambda x: -x["count"]),
            "name_match": SESSION.name_match, "outputs": SESSION.outputs}


# One request can carry a whole domain. Above this the reader gets a window and is told so —
# a browser holding a million rows helps nobody.
MAX_ROWS = 100_000


def _page(df, offset: int, limit: int):
    """A window of a frame as JSON-safe rows."""
    offset = max(0, offset)
    limit = max(1, min(limit, MAX_ROWS))
    view = df.iloc[offset:offset + limit]
    return view.astype(object).where(view.notna(), "").values.tolist()


# a column with few enough distinct values is offered as a dropdown filter rather than a box
DISTINCT_FILTER_MAX = 40


def _shape(df, sort: str, direction: str, filters: str):
    """Apply the reader's sort and column filters to the WHOLE dataset before paging.

    Filtering a single page would be a lie — you would be filtering what happens to be on
    screen, not the data. Both are applied here, then the page is cut."""
    notes = []
    if filters:
        try:
            wanted = json.loads(filters)
        except ValueError:
            wanted = {}
            notes.append("the column filter could not be read and was ignored")
        for col, needle in (wanted or {}).items():
            if col not in df.columns or str(needle) == "":
                continue
            text = df[col].astype("string").fillna("")
            df = df[text.str.contains(str(needle), case=False, regex=False, na=False)]
    if sort and sort in df.columns:
        df = df.sort_values(by=sort, ascending=(direction != "desc"),
                            kind="stable", na_position="last")
    return df, notes


def _distinct_for(frame, name):
    """The distinct values of a low-cardinality column, for a dropdown filter."""
    vals = frame[name].astype("string").fillna("").unique().tolist()
    if len(vals) > DISTINCT_FILTER_MAX:
        return None
    return sorted(str(v) for v in vals if str(v) != "")


@app.get("/api/build/preview/{domain}")
def build_preview(domain: str, limit: int = 25):
    res = SESSION.results.get(upper(domain))
    if res is None or not res.ok:
        raise HTTPException(404, f"{upper(domain)} has not been built")
    return {"domain": upper(domain), "columns": list(res.dataset.columns),
            "nrows": len(res.dataset), "rows": _page(res.dataset, 0, limit)}


@app.get("/api/domain/{domain}/data")
def domain_data(domain: str, offset: int = 0, limit: int = 50, part: str = "parent",
                sort: str = "", dir: str = "asc", filters: str = "", only: str = ""):
    """The built records themselves, a page at a time.

    Column metadata travels with the rows so the grid can mark which variables were guessed
    by name matching, set by hand, or are present-but-empty because they could not be built —
    the reader should never have to guess why a column is blank."""
    dom = upper(domain)
    res = SESSION.results.get(dom)
    if res is None or not res.ok:
        raise HTTPException(404, f"{dom} has not been built")
    frame = res.supp if part == "supp" else res.dataset
    if frame is None:
        raise HTTPException(404, f"{dom} has no SUPP{dom} dataset")

    # `only` narrows the table to a few columns — the record keys plus one variable, so a
    # single mapping can be read row by row in the same table as everything else
    if only:
        wanted = [upper(c) for c in only.split(",") if s(c)]
        keep = [c for c in frame.columns if upper(c) in wanted]
        if keep:
            frame = frame[keep]

    by_var = {b.variable: b for b in res.blocks}
    shaped, notes = _shape(frame, sort, dir, filters)
    columns = []
    for c in frame.columns:
        b = by_var.get(str(c))
        populated = int(frame[c].astype("string").str.strip().ne("").fillna(False).sum())
        columns.append({
            "name": str(c),
            "label": (b.label if b else ""),
            "status": (b.status if b else "built"),
            "method_source": (b.method_source if b else "spec"),
            "confidence": (b.confidence if b else 100),
            "populated": populated,
            "numeric": bool(pd.api.types.is_numeric_dtype(frame[c])),
            "distinct": _distinct_for(frame, c),
        })
    return {"domain": dom, "part": part, "nrows": int(len(shaped)), "total": int(len(frame)),
            "offset": max(0, offset), "limit": max(1, min(limit, MAX_ROWS)),
            "sort": sort, "dir": dir, "notes": notes,
            "columns": columns, "rows": _page(shaped, offset, limit),
            "has_supp": res.supp is not None and len(res.supp) > 0}


@app.get("/api/raw-datasets")
def raw_datasets():
    """Every dataset the raw folder scan found (prepared outputs included) — the raw
    browser's list. Names and files only: nothing is loaded until a dataset is opened."""
    if SESSION.store is None:
        raise HTTPException(400, "scan the raw data folder first")
    out = []
    for name in sorted(SESSION.store.refs):
        ref = SESSION.store.refs[name]
        path = getattr(ref, "path", None)
        out.append({"name": name,
                    "file": path.name if path is not None else "",
                    "kind": getattr(ref, "kind", "") or ("prepared" if path is None else ""),
                    "label": getattr(ref, "label", "") or ""})
    return {"datasets": out}


@app.get("/api/raw/{dataset}/data")
def raw_data(dataset: str, offset: int = 0, limit: int = 50,
             sort: str = "", dir: str = "asc", filters: str = ""):
    """A page of a raw or prepared dataset — the input side, for checking a mapping against
    what actually came in."""
    if SESSION.store is None:
        raise HTTPException(400, "scan the raw data folder first")
    key = SESSION.store.resolve(dataset)
    if not key:
        raise HTTPException(404, f"no raw dataset '{dataset}'")
    frame = SESSION.store.get(key)
    shaped, notes = _shape(frame, sort, dir, filters)
    return {"dataset": key, "nrows": int(len(shaped)), "total": int(len(frame)),
            "offset": max(0, offset), "limit": max(1, min(limit, MAX_ROWS)),
            "sort": sort, "dir": dir, "notes": notes,
            "columns": [{"name": str(c), "label": "", "status": "built",
                         "method_source": "spec", "confidence": 100,
                         "populated": int(frame[c].astype("string").str.strip().ne("").fillna(False).sum()),
                         "numeric": bool(pd.api.types.is_numeric_dtype(frame[c])),
                         "distinct": _distinct_for(frame, c)}
                        for c in frame.columns],
            "rows": _page(shaped, offset, limit)}


# ── domain detail: inspect and rebuild one domain ───────────────────────────
def _samples(res, variable: str, n: int = 4) -> list[str]:
    """A few real values, so a mapping can be judged rather than just read."""
    if res.dataset is None or variable not in res.dataset.columns:
        return []
    col = res.dataset[variable].astype(object)
    vals, seen = [], set()
    for v in col:
        t = "" if v is None else str(v).strip()
        if not t or t.lower() in ("nan", "nat", "none", "<na>") or t in seen:
            continue
        seen.add(t)
        vals.append(t if len(t) <= 40 else t[:37] + "…")
        if len(vals) >= n:
            break
    return vals


def _domain_payload(dom: str) -> dict:
    res = SESSION.results.get(dom)
    if res is None:
        if SESSION.results:
            raise HTTPException(404, f"{dom} was not part of the last build")
        raise HTTPException(
            409, "this build is no longer loaded — the spec or raw data was reloaded, or the "
                 "application was restarted. Run the build again in step 3.")
    blank = sum_blank = 0
    variables = []
    for b in res.blocks:
        populated = None
        if res.dataset is not None and b.variable in res.dataset.columns:
            col = res.dataset[b.variable]
            populated = int(col.notna().sum() - (col.astype("string").str.strip() == "").sum())
        variables.append({
            "variable": b.variable, "label": b.label, "status": b.status,
            "target": f"SUPP{dom}" if b.supp else dom,
            "how": b.method or b.describe_source(),
            "mapping_type": b.mtype, "recipe": b.recipe,
            # a hand edit away from a direct assign no longer reads that column — showing
            # the old source would tell the reader a mapping that is no longer true
            "source": (f"{b.dataset}.{b.column}"
                       if b.dataset and b.column and not (b.edited and b.mtype != "assign")
                       else ""),
            "constant": b.value, "codelist": b.codelist, "origin": b.origin, "role": b.role,
            "spec_action": b.action, "spec_input": b.input_variables,
            "spec_rule": b.mapping_rule, "spec_sas": b.sas_code, "spec_row": b.sheet_row,
            "reason": b.reason, "error": b.error,
            "edited": b.edited, "edit_note": b.edit_note, "spec_method": b.spec_method,
            "method_source": b.method_source, "confidence": b.confidence,
            "args": b.args, "supp": b.supp,
            "populated": populated, "samples": _samples(res, b.variable),
        })
    return {
        "domain": dom, "ok": res.ok, "error": res.error, "base": res.base_dataset,
        "rows": 0 if res.dataset is None else len(res.dataset),
        "supp_rows": 0 if res.supp is None else len(res.supp),
        "columns": [] if res.dataset is None else list(res.dataset.columns),
        "prep": res.prep_step.as_dict() if res.prep_step else None,
        "warnings": res.warnings, "counts": res.counts,
        "override": SESSION.overrides.get(dom, {}),
        "dedup": SESSION.dedups.get(dom, {}),
        "pipeline": SESSION.pipelines.get(dom, []),
        "pipeline_draft": SESSION.draft_pipelines.get(dom),
        "codelists": sorted(SESSION.spec.codelists) if SESSION.spec else [],
        "prep_reports": res.prep_reports,
        "prep_outputs": res.prep_outputs,
        "edits": SESSION.edits.get(dom, {}),
        "datasets": sorted(SESSION.store.refs) if SESSION.store else [],
        "prepared_datasets": sorted(
            {norm_key(st.get("name", "")) for steps in SESSION.pipelines.values()
             for st in steps if st.get("name")} | set(SESSION.preview_outputs)),
        "unapplied_datasets": sorted(SESSION.preview_outputs),
        "built_domains": sorted(d for d in SESSION.results if d != dom),
        "variables": variables,
    }


@app.get("/api/domain/{domain}")
def domain_detail(domain: str):
    return _domain_payload(upper(domain))


@app.post("/api/domain/{domain}/settings")
def set_override(domain: str, body: OverrideIn):
    dom = upper(domain)
    if dom not in (SESSION.spec.domains if SESSION.spec else {}):
        raise HTTPException(404, f"{dom} is not in the mapping spec")
    SESSION.overrides[dom] = {
        "base": body.base.strip(), "sort": [upper(x) for x in body.sort],
        "prep_mode": body.prep_mode, "prep": body.prep,
        "keys": [upper(k) for k in body.keys],
    }
    _autosave()
    return {"domain": dom, "override": SESSION.overrides[dom]}


@app.post("/api/domain/{domain}/build")
def rebuild_domain(domain: str):
    """Rebuild one domain with its current overrides, leaving the others alone."""
    dom = upper(domain)
    if SESSION.spec is None or SESSION.store is None:
        raise HTTPException(400, "load the mapping spec and the raw data folder first")
    if dom not in SESSION.spec.domains:
        raise HTTPException(404, f"{dom} is not in the mapping spec")
    if RUNNER.busy:
        raise HTTPException(409, "a job is already running")
    if not SESSION.out_dir:
        raise HTTPException(400, "run a full build first, so there is an output folder")

    ov = SESSION.overrides.get(dom, {})

    def work(progress):
        progress(f"rebuilding {dom}", step=1, total=3)
        # other domains stay available as cross-domain references (DM reference dates, etc.)
        others = {d: r.dataset for d, r in SESSION.results.items() if d != dom and r.ok}
        res = build_domain(SESSION.spec, SESSION.store, dom, built=others,
                           studyid=SESSION.studyid, base_override=ov.get("base", ""),
                           sort_by=ov.get("sort"), include_unbuilt=SESSION.include_unbuilt,
                           prep_mode=ov.get("prep_mode", "auto"),
                           prep_override=ov.get("prep") if ov.get("prep_mode") == "custom" else None,
                           prep_steps=SESSION.pipelines.get(dom),
                           edits=SESSION.edits.get(dom), dedup=SESSION.dedups.get(dom),
                           custom_fns=SESSION.custom_fns, template_overrides=SESSION.template_overrides,
                           name_match_threshold=SESSION.name_match)
        SESSION.results[dom] = res

        out = Path(SESSION.out_dir)
        if res.ok and SESSION.fmt != "none":
            progress(f"writing {dom}", step=2)
            labels = {b.variable: b.label for b in res.blocks}
            write_dataset(res.dataset, out / "datasets", dom, SESSION.fmt, labels)
            if res.supp is not None and len(res.supp):
                write_dataset(res.supp, out / "datasets", f"SUPP{dom}", SESSION.fmt)
        progress("refreshing the manifest", step=3)
        _, xpath = write_manifest(SESSION.results, out, SESSION.build_meta)
        SESSION.outputs["manifest"] = str(xpath)
        # the comparison no longer matches the rebuilt data — make that visible, not stale
        SESSION.comps = {}
        report.write_html_report(SESSION.results, None, out / "build_report.html",
                                 SESSION.build_meta)
        _save_session()

    RUNNER.start("rebuild", work, total=3)
    return {"started": True, "domain": dom}


# ── editing a variable's mapping ────────────────────────────────────────────
# The recipe catalogue the editor renders its forms from. Keeping it here means the
# interface can never offer an operation the engine does not implement.
RECIPES = [
    {"id": "iso_date",
     "label": "ISO 8601 date/time",
     "desc": "Build a --DTC from the date the form collected. Name the date column and that is "
             "all — if the form split it into year/month/day parts, they are found and used "
             "automatically, which is what keeps a partial date partial (a known month with an "
             "unknown day stays 1962-11, and is never padded out to a day nobody recorded).",
     "fields": [
         {"k": "dataset", "t": "dataset", "label": "Raw dataset",
          "help": "which raw form the date comes from"},
         {"k": "date_col", "t": "column", "label": "Date column",
          "help": "e.g. AESTDAT. Its _YYYY / _MM / _DD parts and its time column are picked up "
                  "automatically when the form has them."},
         {"k": "time_col", "t": "column", "label": "Time column", "advanced": True,
          "help": "only if the automatic match found the wrong one"},
         {"k": "y_col", "t": "column", "label": "Year column", "advanced": True,
          "help": "override the automatic match"},
         {"k": "m_col", "t": "column", "label": "Month column", "advanced": True,
          "help": "override the automatic match"},
         {"k": "d_col", "t": "column", "label": "Day column", "advanced": True,
          "help": "override the automatic match"},
     ]},
    {"id": "study_day",
     "label": "Study day (--DY)",
     "desc": "Days from a reference date in DM to this record's date. Adds 1 on and after the "
             "reference, so there is no day 0, as SDTM requires.",
     "fields": [
         {"k": "dtc_var", "t": "sdtmvar", "label": "Event date variable",
          "help": "the --DTC in this domain the day is counted from, e.g. AESTDTC"},
         {"k": "ref_var", "t": "text", "label": "DM reference date",
          "help": "RFSTDTC by default; RFXSTDTC counts from first exposure instead"},
     ]},
    {"id": "lobxfl",
     "label": "Last observation before exposure flag",
     "desc": "Marks 'Y' on the last non-missing result on or before the subject's first "
             "exposure date, and leaves every other record blank.",
     "fields": [
         {"k": "testcd_var", "t": "sdtmvar", "label": "Test code variable", "help": "e.g. VSTESTCD"},
         {"k": "dtc_var", "t": "sdtmvar", "label": "Date variable", "help": "e.g. VSDTC"},
         {"k": "result_var", "t": "sdtmvar", "label": "Result variable",
          "help": "a record with no result cannot be the last observation, e.g. VSORRES"},
         {"k": "ref_var", "t": "text", "label": "DM exposure date", "help": "RFXSTDTC by default"},
         {"k": "group_vars", "t": "list", "label": "Flag within",
          "help": "one flag per group, normally USUBJID and the test code"},
     ]},
    {"id": "date_extreme",
     "label": "Earliest / latest date across datasets",
     "desc": "Takes the minimum or maximum date per subject across ANY number of raw datasets. "
             "This is how RFSTDTC, RFENDTC and similar reference dates are built — the first "
             "dose across every exposure form, the last contact across every visit form.",
     "fields": [
         {"k": "func", "t": "choice", "options": ["min", "max"], "label": "Take the",
          "help": "min = earliest date, max = latest date"},
         {"k": "sources", "t": "sources", "label": "Datasets and their date columns",
          "help": "add one row per dataset to search; every date found is pooled per subject "
                  "and the earliest or latest is taken"},
         {"k": "group_by", "t": "list", "label": "Per", "help": "USUBJID by default"},
     ]},
    {"id": "concat",
     "label": "Join columns with a separator",
     "desc": "Glue several raw columns together, in order, with a separator between them.",
     "fields": [
         {"k": "dataset", "t": "dataset", "label": "Raw dataset"},
         {"k": "columns", "t": "list", "label": "Columns", "help": "in the order they should join"},
         {"k": "sep", "t": "text", "label": "Separator", "help": "a space if left empty"},
     ]},
    {"id": "copy_var",
     "label": "Copy another variable in this domain",
     "desc": "Take the value of a variable built earlier in this same domain.",
     "fields": [{"k": "source_var", "t": "sdtmvar", "label": "Variable to copy",
                 "help": "it must appear earlier in the spec than this one"}]},
    {"id": "sdtm_ref",
     "label": "Take a value from another built domain",
     "desc": "Look the value up in another SDTM domain, matched on USUBJID. That domain must "
             "be built first.",
     "fields": [
         {"k": "source_domain", "t": "domain", "label": "Domain", "help": "e.g. DM"},
         {"k": "source_var", "t": "text", "label": "Variable", "help": "e.g. RFSTDTC"},
     ]},
    {"id": "fn",
     "label": "One SAS function",
     "desc": "Apply a single SAS character or numeric function. Chain several with the "
             "pipeline derivation instead.",
     "fields": [
         {"k": "fn", "t": "choice", "options": sorted(SAS_FUNCTIONS), "label": "Function"},
         {"k": "sources", "t": "json", "label": "Inputs",
          "help": 'a raw column [{"dataset": "ae", "column": "AETERM"}], another variable '
                  '[{"kind": "var", "var": "AETERM"}], the running value [{"kind": "self"}], '
                  'or a literal [{"kind": "text", "text": "X"}]'},
         {"k": "start", "t": "text", "label": "Start position", "help": "SUBSTR — 1-based"},
         {"k": "len", "t": "text", "label": "Length", "help": "SUBSTR — leave empty for the rest"},
         {"k": "delim", "t": "text", "label": "Delimiter", "help": "SCAN"},
         {"k": "word", "t": "text", "label": "Word number",
          "help": "SCAN — negative counts from the end, so -1 is the last"},
         {"k": "find", "t": "text", "label": "Find", "help": "TRANWRD / INDEX"},
         {"k": "replace", "t": "text", "label": "Replace with", "help": "TRANWRD"},
         {"k": "chars", "t": "text", "label": "Characters to remove", "help": "COMPRESS"},
         {"k": "sep", "t": "text", "label": "Separator", "help": "CATX"},
         {"k": "width", "t": "text", "label": "Width", "help": "ZEROPAD"},
     ]},
    {"id": "ct",
     "label": "Apply controlled terminology (assign_ct)",
     "desc": "Normalise a raw column, fixed text or another variable to a codelist's "
             "submission values ('male', 'M', 'Male' → M), sdtm.oak style. Unmatched "
             "values pass through for validation to flag — never dropped, never invented.",
     "fields": [
         {"k": "sources", "t": "json", "label": "Input",
          "help": '[{"dataset": "dm", "column": "SEXCD"}] or [{"kind": "text", "text": "male"}]'},
         {"k": "codelist", "t": "text", "label": "Codelist",
          "help": "a name from the spec's Codelist sheet"},
     ]},
    {"id": "cond",
     "label": "If / else-if / else",
     "desc": "Sequential rules; the first one that matches wins, as in a SAS IF-THEN-ELSE chain.",
     "fields": [
         {"k": "rules", "t": "json", "label": "Rules",
          "help": '[{"src": {"dataset":"ae","column":"AESEVCD"}, "op": "eq", "value": "1", '
                  '"then": {"kind":"text","text":"MILD"}}] — ops: eq, ne, in, notin, contains, '
                  'starts, ends, gt, lt, ge, le, between, missing, notmissing'},
         {"k": "else", "t": "json", "label": "Otherwise",
          "help": '{"kind": "missing"} leaves it blank, or {"kind": "text", "text": "N"}'},
     ]},
    {"id": "pipeline",
     "label": "Several steps on one variable",
     "desc": "Chain operations: copy a column, then transform it, then apply a rule. Each step "
             "can read the running value with {\"kind\": \"self\"}.",
     "fields": [
         {"k": "steps", "t": "json", "label": "Steps",
          "help": '[{"op":"assign","dataset":"ae","column":"AETERM"}, '
                  '{"op":"fn","args":{"fn":"upcase","sources":[{"kind":"self"}]}}]'},
     ]},
    {"id": "age",
     "label": "Age at the reference date",
     "desc": "The company DM template's AGE: the reported age when the study collected one, "
             "otherwise whole years from the birth date to the reference start date on the "
             "anniversary rule — never a fraction, never a birthday-eve off-by-one.",
     "fields": [
         {"k": "age_col", "t": "column", "label": "Reported age column",
          "help": "used as-is when present, e.g. AGE_REP; leave empty to always derive"},
         {"k": "birth_var", "t": "sdtmvar", "label": "Birth date variable", "help": "BRTHDTC"},
         {"k": "ref_var", "t": "sdtmvar", "label": "Reference date variable", "help": "RFSTDTC"},
     ]},
    {"id": "studyid", "label": "Study identifier", "hidden": True,
     "desc": "Taken from the STUDYID override, or whichever spelling of a study key the raw "
             "data carries.", "fields": []},
    {"id": "usubjid", "label": "Unique subject identifier", "hidden": True,
     "desc": "Carried from the raw data when present, otherwise composed as STUDYID-SUBJID.",
     "fields": []},
]


@app.get("/api/domain/{domain}/program/{lang}")
def domain_program(domain: str, lang: str):
    """The build, written out as a standalone program — Python/pandas or house-style SAS.
    Generated from the same structured blocks the build executed (hand edits included),
    with clearly-marked TODOs where a recipe has no standalone translation."""
    from sdtm_builder import programs
    dom = upper(domain)
    res = SESSION.results.get(dom)
    if res is None:
        raise HTTPException(409, "build the domain first — the program is generated from "
                                 "the executed build, so it says exactly what the tool did")
    lang = lang.lower()
    if lang not in ("python", "sas"):
        raise HTTPException(400, "lang must be python or sas")
    # the distinct raw values behind every codelisted variable, so the program inlines
    # only the CT terms this study's data holds (5 entries, not the codelist's 100)
    observed: dict[str, list[str]] = {}
    for b in res.blocks:
        if not (b.codelist or (b.args or {}).get("codelist")):
            continue
        vals = [d["value"].upper() for d in _ct_raw_values(b)]
        if vals:
            observed[b.variable] = vals
    kwargs = dict(
        domain=dom, blocks=res.blocks, base_dataset=res.base_dataset,
        prep_step=res.prep_step.as_dict() if res.prep_step else None,
        pipeline=SESSION.pipelines.get(dom, []),
        sort_by=(SESSION.overrides.get(dom, {}) or {}).get("sort") or [],
        dedup=SESSION.dedups.get(dom, {}),
        codelists=SESSION.spec.codelists if SESSION.spec else {},
        raw_path=SESSION.raw_path, studyid=SESSION.studyid, version=__version__,
        observed=observed)
    if lang == "sas":
        # dataset -> columns, so the SAS emitter can pick the subject key each
        # cross-dataset input shares with the base and pre-merge it deterministically
        source_columns: dict[str, list[str]] = {}
        if SESSION.store is not None:
            for name in list(SESSION.store.refs):
                try:
                    source_columns[name] = [upper(c) for c in SESSION.store.columns(name)]
                except Exception:                                # noqa: BLE001
                    continue
            # a block may name a dataset by an alias the store resolves (e.g. 'inv' for
            # 'inv_20260504') — register the alias too so the emitter's lookup matches
            alias_names = {res.base_dataset}
            for b in res.blocks:
                if b.dataset:
                    alias_names.add(b.dataset)
                if (b.args or {}).get("dataset"):
                    alias_names.add(b.args["dataset"])
                for src_d in (b.args or {}).get("sources") or []:
                    if isinstance(src_d, dict) and src_d.get("dataset"):
                        alias_names.add(src_d["dataset"])
            for alias in alias_names:
                key = SESSION.store.resolve(alias)
                if key and key in source_columns:
                    source_columns.setdefault(norm_key(alias), source_columns[key])
        kwargs["source_columns"] = source_columns
    text = programs.python_program(**kwargs) if lang == "python" else programs.sas_program(**kwargs)
    ext = "py" if lang == "python" else "sas"
    return {"domain": dom, "lang": lang, "program": text,
            "filename": f"{dom.lower()}_build.{ext}"}


@app.get("/api/recipes")
def recipes():
    return {"mtypes": ["assign", "constant", "sequence", "derived", "drop"], "recipes": RECIPES}


def _rebuild_one(dom: str, progress=None) -> "object":
    """Build a single domain with the session's current overrides, prep and edits."""
    ov = SESSION.overrides.get(dom, {})
    others = {d: r.dataset for d, r in SESSION.results.items() if d != dom and r.ok}
    return build_domain(
        SESSION.spec, SESSION.store, dom, built=others, studyid=SESSION.studyid,
        base_override=ov.get("base", ""), sort_by=ov.get("sort"),
        include_unbuilt=SESSION.include_unbuilt,
        prep_mode=ov.get("prep_mode", "auto"),
        prep_override=ov.get("prep") if ov.get("prep_mode") == "custom" else None,
        prep_steps=SESSION.pipelines.get(dom),
        edits=SESSION.edits.get(dom), dedup=SESSION.dedups.get(dom),
        custom_fns=SESSION.custom_fns, template_overrides=SESSION.template_overrides,
        name_match_threshold=SESSION.name_match)


def _profile_series(ser) -> dict:
    """Describe one column: how full it is, what is in it, and its range.

    A distribution is what actually exposes a mapping problem — a severity that should have
    three levels arriving with two, a date column that is 40% blank, a code that is really
    free text."""
    text = ser.astype("string").str.strip()
    blank = text.isna() | text.eq("")
    populated = int((~blank).sum())
    present = text[~blank]

    counts = present.value_counts()
    top = [{"value": str(v), "count": int(n)} for v, n in counts.head(25).items()]

    out = {
        "n": int(len(ser)), "populated": populated, "blank": int(blank.sum()),
        "distinct": int(counts.size), "top": top,
        "truncated": bool(counts.size > 25),
    }

    numeric = pd.to_numeric(present, errors="coerce")
    if len(present) and numeric.notna().mean() > 0.9:
        out["numeric"] = {
            "min": float(numeric.min()), "median": float(numeric.median()),
            "max": float(numeric.max()), "mean": round(float(numeric.mean()), 4),
        }

    # ISO 8601 dates, including the partial forms SDTM allows
    iso_full = present.str.match(r"^\d{4}-\d{2}-\d{2}")
    iso_any = present.str.match(r"^\d{4}(-\d{2}(-\d{2})?)?")
    if len(present) and iso_any.mean() > 0.7:
        partial = int((iso_any & ~iso_full).sum())
        dates = present[iso_full.fillna(False)].str[:10]
        out["dates"] = {
            "complete": int(iso_full.sum()), "partial": partial,
            "unparseable": int((~iso_any.fillna(False)).sum()),
            "earliest": str(dates.min()) if len(dates) else "",
            "latest": str(dates.max()) if len(dates) else "",
        }
    return out


@app.get("/api/domain/{domain}/variable/{variable}/profile")
def variable_profile(domain: str, variable: str):
    """What this one variable actually contains, next to the raw column it came from."""
    dom, var = upper(domain), upper(variable)
    res = SESSION.results.get(dom)
    if res is None or not res.ok:
        raise HTTPException(404, f"{dom} has not been built")
    blk = next((b for b in res.blocks if b.variable == var), None)
    if blk is None:
        raise HTTPException(404, f"{var} is not a variable of {dom}")

    frame = res.supp if blk.supp else res.dataset
    out: dict = {
        "domain": dom, "variable": var, "label": blk.label, "status": blk.status,
        "method": blk.method or blk.describe_source(), "method_source": blk.method_source,
        "confidence": blk.confidence, "reason": blk.error or blk.reason,
        "codelist": blk.codelist, "source": (f"{blk.dataset}.{blk.column}"
                                             if blk.dataset and blk.column else ""),
        "built": None, "input": None, "ct": None,
    }
    if frame is not None and var in frame.columns:
        out["built"] = _profile_series(frame[var])

    # the raw column this variable reads, profiled the same way — the comparison between the
    # two is where a lost level or a dropped record shows up
    if blk.dataset and blk.column and SESSION.store is not None:
        key = SESSION.store.resolve(blk.dataset)
        if key:
            try:
                raw = SESSION.store.get(key)
                if blk.column in raw.columns:
                    out["input"] = {"dataset": key, "column": blk.column,
                                    **_profile_series(raw[blk.column])}
            except Exception:                                    # noqa: BLE001
                pass

    # values the spec's codelist does not contain
    terms = SESSION.spec.codelists.get(blk.codelist) if (SESSION.spec and blk.codelist) else None
    if terms and out["built"]:
        allowed = {str(v).upper() for v in terms.values()} | {str(k).upper() for k in terms}
        bad = [t for t in out["built"]["top"] if str(t["value"]).upper() not in allowed]
        out["ct"] = {"codelist": blk.codelist, "allowed": sorted({str(v) for v in terms.values()})[:40],
                     "violations": bad[:20], "violating_records": sum(t["count"] for t in bad)}
    return out


def _ct_block_and_codelist(dom: str, var: str):
    res = SESSION.results.get(dom)
    if res is None:
        raise HTTPException(409, "build the domain first")
    blk = next((b for b in res.blocks if b.variable == var), None)
    if blk is None:
        raise HTTPException(404, f"{var} is not a variable of {dom}")
    cl = upper(blk.codelist) or upper((blk.args or {}).get("codelist"))
    if not cl:
        raise HTTPException(404, f"{var} has no codelist")
    return res, blk, cl


def _ct_raw_values(blk) -> list[dict]:
    """Distinct values of the raw column this block reads, with counts — the DATA side
    of the CT inspector. Empty when the source is not a plain column."""
    pairs = []
    if blk.dataset and blk.column:
        pairs.append((blk.dataset, blk.column))
    for src in (blk.args or {}).get("sources") or []:
        if isinstance(src, dict) and src.get("dataset") and src.get("column"):
            pairs.append((src["dataset"], src["column"]))
    out: dict[str, int] = {}
    if SESSION.store is None:
        return []
    for ds, col in pairs:
        key = SESSION.store.resolve(ds)
        if not key:
            continue
        try:
            fr = SESSION.store.get(key)
        except Exception:                                        # noqa: BLE001
            continue
        c = upper(col)
        if c not in fr.columns:
            continue
        vv = fr[c].astype("string").str.strip()
        for val, n in vv[vv.notna() & (vv != "")].value_counts().head(200).items():
            out[str(val)] = out.get(str(val), 0) + int(n)
    return [{"value": v, "count": n}
            for v, n in sorted(out.items(), key=lambda kv: -kv[1])]


def _ct_overrides_for(dom: str, var: str, blk) -> dict:
    edit = SESSION.edits.get(dom, {}).get(var) or {}
    return dict((edit.get("args") or {}).get("ct_overrides")
                or (blk.args or {}).get("ct_overrides") or {})


@app.get("/api/domain/{domain}/variable/{variable}/ct")
def variable_ct(domain: str, variable: str):
    """The CT inspector: the codelist as the spec states it (terms, decodes, extensible),
    and every value the DATA holds with what it normalises to — unmatched values named,
    manual mappings shown. The Designer-style click-through on a codelist chip."""
    dom, var = upper(domain), upper(variable)
    _res, blk, cl = _ct_block_and_codelist(dom, var)
    cmap = dict(SESSION.spec.codelists.get(cl, {})) if SESSION.spec else {}
    meta = (SESSION.spec.codelist_meta.get(cl, {}) if SESSION.spec else {}) or {}
    overrides = _ct_overrides_for(dom, var, blk)
    eff = {**cmap, **{upper(k): v for k, v in overrides.items()}}
    submissions = sorted({str(v) for v in cmap.values()})
    data = []
    for d in _ct_raw_values(blk):
        mapped = eff.get(upper(d["value"]))
        data.append({**d, "maps_to": mapped or "",
                     "matched": mapped is not None,
                     "manual": upper(d["value"]) in {upper(k) for k in overrides}})
    return {"domain": dom, "variable": var, "codelist": cl,
            "label": meta.get("label", ""),
            "extensible": bool(meta.get("extensible")),
            "terms": (meta.get("terms") or
                      [{"value": v, "decode": ""} for v in submissions])[:500],
            "n_terms": len(meta.get("terms") or submissions),
            "submission_values": submissions[:500],
            "data": data,
            "overrides": overrides,
            "unmatched_records": sum(d["count"] for d in data if not d["matched"])}


class CtMapIn(BaseModel):
    raw_value: str
    ct_value: str = ""          # empty removes the manual mapping


@app.post("/api/domain/{domain}/variable/{variable}/ct-map")
def variable_ct_map(domain: str, variable: str, body: CtMapIn):
    """Manually map one raw value to a CT value. On a NON-extensible codelist the target
    must be one of its submission values (mapping a spelling is normalisation); an
    extensible codelist also accepts a new value. Stored as a hand edit's ct_overrides,
    so it survives reloads and is labelled as the reader's decision."""
    dom, var = upper(domain), upper(variable)
    _res, blk, cl = _ct_block_and_codelist(dom, var)
    raw = s(body.raw_value)
    if not raw:
        raise HTTPException(400, "name the raw value to map")
    target = s(body.ct_value)
    meta = (SESSION.spec.codelist_meta.get(cl, {}) if SESSION.spec else {}) or {}
    subs = {str(v) for v in (SESSION.spec.codelists.get(cl, {}) if SESSION.spec else {}).values()}
    if target and target not in subs and not meta.get("extensible"):
        raise HTTPException(400, f"codelist {cl} is not extensible — pick one of its "
                                 f"submission values, or extend the spec's Codelist sheet")
    overrides = _ct_overrides_for(dom, var, blk)
    if target:
        overrides[raw] = target
    else:
        overrides = {k: v for k, v in overrides.items() if upper(k) != upper(raw)}
    edit = SESSION.edits.get(dom, {}).get(var)
    if edit is None:                       # start from the block's CURRENT mapping
        edit = {"mtype": blk.mtype, "dataset": blk.dataset, "column": blk.column,
                "value": blk.value, "recipe": blk.recipe, "codelist": blk.codelist,
                "args": dict(blk.args or {}),
                "note": f"manual CT mapping on {cl}"}
    edit.setdefault("args", {})
    if overrides:
        edit["args"]["ct_overrides"] = overrides
    else:
        edit["args"].pop("ct_overrides", None)
    SESSION.edits.setdefault(dom, {})[var] = edit
    _autosave()
    return {"domain": dom, "variable": var, "codelist": cl, "overrides": overrides}


@app.get("/api/domain/{domain}/variable/{variable}/suggest")
def suggest_arguments(domain: str, variable: str, recipe: str):
    """What the spec already says about this derivation's arguments.

    The Input Variables column lists the datasets and columns. Asking someone to retype them
    into a form is asking them to copy out what the tool can already read."""
    dom, var = upper(domain), upper(variable)
    res = SESSION.results.get(dom)
    if res is None:
        raise HTTPException(404, f"{dom} has not been built")
    blk = next((b for b in res.blocks if b.variable == var), None)
    if blk is None:
        raise HTTPException(404, f"{var} is not a variable of {dom}")
    from sdtm_builder import automap
    return {"domain": dom, "variable": var, "recipe": recipe,
            "args": automap.suggest_args(blk, SESSION.store, recipe),
            "input_variables": blk.input_variables}


@app.post("/api/domain/{domain}/variable/{variable}/preview")
def preview_edit(domain: str, variable: str, body: EditIn):
    """Try an edit WITHOUT keeping it: build the domain with it and return what the variable
    would contain. Nothing is committed, so a mapping can be judged before it is adopted."""
    dom, var = upper(domain), upper(variable)
    if SESSION.spec is None or SESSION.store is None:
        raise HTTPException(400, "load the mapping spec and the raw data folder first")
    if RUNNER.busy:
        raise HTTPException(409, "a job is already running")

    saved = SESSION.edits.get(dom, {}).copy()
    trial = dict(saved)
    trial[var] = body.model_dump()
    SESSION.edits[dom] = trial
    try:
        res = _rebuild_one(dom)
    finally:
        SESSION.edits[dom] = saved                      # a preview never changes the session
    if res.error:
        return {"ok": False, "error": res.error}
    blk = next((b for b in res.blocks if b.variable == var), None)
    if blk is None:
        return {"ok": False, "error": f"{var} is not a variable in {dom}"}
    return {
        "ok": blk.status == "built", "status": blk.status,
        "error": blk.error, "reason": blk.reason, "how": blk.method or blk.describe_source(),
        "rows": 0 if res.dataset is None else len(res.dataset),
        "populated": int(res.dataset[var].astype("string").str.strip().ne("").sum())
                     if res.dataset is not None and var in res.dataset.columns else 0,
        "samples": _samples(res, var, n=10),
        "warnings": res.warnings,
    }


@app.post("/api/domain/{domain}/variable/{variable}")
def set_edit(domain: str, variable: str, body: EditIn):
    dom, var = upper(domain), upper(variable)
    if SESSION.spec is None or dom not in SESSION.spec.domains:
        raise HTTPException(404, f"{dom} is not in the mapping spec")
    SESSION.edits.setdefault(dom, {})[var] = body.model_dump()
    _autosave()
    return {"domain": dom, "variable": var, "edits": sorted(SESSION.edits[dom])}


@app.delete("/api/domain/{domain}/variable/{variable}")
def clear_edit(domain: str, variable: str):
    """Put a variable back under the mapping spec."""
    dom, var = upper(domain), upper(variable)
    SESSION.edits.get(dom, {}).pop(var, None)
    _autosave()
    return {"domain": dom, "variable": var, "edits": sorted(SESSION.edits.get(dom, {}))}


@app.delete("/api/domain/{domain}/edits")
def clear_domain_edits(domain: str):
    dom = upper(domain)
    SESSION.edits.pop(dom, None)
    _autosave()
    return {"domain": dom, "edits": []}


@app.post("/api/domain/{domain}/dedup")
def set_dedup(domain: str, body: DedupIn):
    """Keep only the first or last record per group — a dataset-level preparation."""
    dom = upper(domain)
    if body.enabled and not body.keys:
        raise HTTPException(400, "choose at least one variable to group by")
    SESSION.dedups[dom] = body.model_dump()
    _autosave()
    return {"domain": dom, "dedup": SESSION.dedups[dom]}


@app.get("/api/domain/{domain}/columns/{dataset}")
def dataset_columns(domain: str, dataset: str):
    """Columns of a raw dataset, for the editor's column picker."""
    if SESSION.store is None:
        raise HTTPException(400, "scan the raw data folder first")
    key = SESSION.store.resolve(dataset)
    if not key:
        raise HTTPException(404, f"no raw dataset '{dataset}'")
    return {"dataset": key, "columns": sorted(SESSION.store.columns(key))}


# ── the data-preparation pipeline ───────────────────────────────────────────
# ── the annotated-CRF check ─────────────────────────────────────────────────
class AcrfIn(BaseModel):
    acrf: str
    standards: str
    ta: str = ""
    ecrf: str = ""


@app.get("/api/acrf")
def get_acrf():
    return {"acrf": SESSION.acrf_path, "standards": SESSION.standards_path,
            "ta": SESSION.ta_path, "ecrf": SESSION.ecrf_path,
            "std_acrf": SESSION.std_acrf_path, "std_ecrf": SESSION.std_ecrf_path,
            "report": SESSION.acrf_report, "cmp": SESSION.crf_cmp}


@app.post("/api/acrf")
def run_acrf(body: AcrfIn):
    """Extract the SDTM annotations from the aCRF and judge each against the standards
    mapping (and the TA spec) — plus the reverse look at what was never annotated."""
    for label, path in (("annotated CRF", body.acrf), ("standards mapping", body.standards)):
        if not s(path):
            raise HTTPException(400, f"point at the {label} first")
        if not Path(path).expanduser().exists():
            raise HTTPException(400, _fs_hint(f"the {label} was not found at {path}"))
    for label, path in (("TA spec", body.ta), ("eCRF spec", body.ecrf)):
        if s(path) and not Path(path).expanduser().exists():
            raise HTTPException(400, _fs_hint(f"the {label} was not found at {path}"))
    try:
        report = acrf_module.check(Path(body.acrf).expanduser(),
                                   Path(body.standards).expanduser(),
                                   Path(body.ta).expanduser() if s(body.ta) else None,
                                   Path(body.ecrf).expanduser() if s(body.ecrf) else None)
    except acrf_module.AcrfError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:                       # noqa: BLE001 — surface, never a bare 500
        raise HTTPException(400, f"the check failed on these files: {exc}")
    SESSION.acrf_path, SESSION.standards_path = body.acrf, body.standards
    SESSION.ta_path, SESSION.ecrf_path = body.ta, body.ecrf
    SESSION.acrf_report = report
    _autosave()
    return {"ok": True, "report": report}


class CrfCmpIn(BaseModel):
    vendor: str
    standard: str
    vendor_ecrf: str = ""
    standard_ecrf: str = ""
    standards: str = ""            # mapping workbook — helps question filling, optional


@app.post("/api/acrf/compare")
def compare_crfs_api(body: CrfCmpIn):
    """The vendor's aCRF against the internal standards aCRF — questions aligned
    (worded differently still pairs, with the score shown) and their mappings compared."""
    for label, path, required in (("vendor aCRF", body.vendor, True),
                                  ("standards aCRF", body.standard, True),
                                  ("vendor eCRF spec", body.vendor_ecrf, False),
                                  ("standards eCRF spec", body.standard_ecrf, False),
                                  ("standards mapping", body.standards, False)):
        if required and not s(path):
            raise HTTPException(400, f"point at the {label} first")
        if s(path) and not Path(path).expanduser().exists():
            raise HTTPException(400, _fs_hint(f"the {label} was not found at {path}"))
    try:
        cmp_ = acrf_module.compare_crfs(
            Path(body.vendor).expanduser(), Path(body.standard).expanduser(),
            vendor_ecrf=body.vendor_ecrf or None, standard_ecrf=body.standard_ecrf or None,
            standards_path=body.standards or None)
    except acrf_module.AcrfError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:                       # noqa: BLE001 — surface, never a bare 500
        raise HTTPException(400, f"the comparison failed on these files: {exc}")
    SESSION.acrf_path = body.vendor
    SESSION.ecrf_path = body.vendor_ecrf
    SESSION.std_acrf_path, SESSION.std_ecrf_path = body.standard, body.standard_ecrf
    if s(body.standards):
        SESSION.standards_path = body.standards
    SESSION.crf_cmp = cmp_
    _autosave()
    return {"ok": True, "cmp": cmp_}


@app.get("/api/acrf/export")
def export_acrf():
    """The aCRF check as an Excel workbook — one sheet per table, ready to share."""
    if not SESSION.acrf_report:
        raise HTTPException(400, "run the aCRF check first")
    import tempfile

    import pandas as pd
    rep = SESSION.acrf_report
    ann = pd.DataFrame([{
        "Page": r.get("page"), "Form": r.get("form", ""),
        "CRF question": r.get("question", ""),
        "Annotation": (f"{r['domain']}.{r['variable']}" if r.get("domain") and r.get("variable")
                       else (r.get("variable") or r.get("value", ""))),
        "Value": r.get("value", ""), "Verdict": r.get("verdict", ""),
        "What to do": r.get("advice", ""), "As annotated": r.get("text", ""),
    } for r in rep.get("rows", [])])
    miss = pd.DataFrame([{
        "Domain": m.get("domain"), "Variable": m.get("variable"),
        "Label": m.get("label", ""), "Origin": m.get("origin", ""),
        "What to do": m.get("advice", ""),
    } for m in rep.get("missing", [])])
    summary = pd.DataFrame([{"Measure": k.replace("_", " "), "Count": v}
                            for k, v in rep.get("counts", {}).items()])
    out = Path(tempfile.mkdtemp(prefix="acrf_")) / "acrf_check.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        (ann if len(ann) else pd.DataFrame({"note": ["no annotations found"]})
         ).to_excel(xw, sheet_name="Annotations", index=False)
        (miss if len(miss) else pd.DataFrame({"note": ["nothing missing"]})
         ).to_excel(xw, sheet_name="Never annotated", index=False)
        if SESSION.crf_cmp:
            cmpr = SESSION.crf_cmp
            pd.DataFrame([{
                "Standard question": x["standard_question"], "Vendor question": x["vendor_question"],
                "Match": x["match"], "Similarity": x["similarity"],
                "Standard form": x["standard_form"], "Vendor form": x["vendor_form"],
                "Standard mapping": x["standard_mapping"], "Vendor mapping": x["vendor_mapping"],
                "Verdict": x["verdict"].replace("_", " "), "What to do": x["advice"],
            } for x in cmpr.get("pairs", [])] or [{"note": "no aligned questions"}]
            ).to_excel(xw, sheet_name="CRF vs CRF", index=False)
            pd.DataFrame(cmpr.get("standard_only", []) or [{"note": "none"}]
            ).to_excel(xw, sheet_name="Standards CRF only", index=False)
            pd.DataFrame(cmpr.get("vendor_only", []) or [{"note": "none"}]
            ).to_excel(xw, sheet_name="Vendor CRF only", index=False)
    return FileResponse(out, filename="acrf_check.xlsx")


# ── the function library: template derivations + the user's own ────────────
class FnIn(BaseModel):
    name: str
    description: str = ""
    variable: str
    domains: list[str] = []
    steps: list[dict] = []
    override: bool = False
    enabled: bool = True


@app.get("/api/functions")
def list_functions():
    """The whole function library: the built-in SAS-template derivations (with their
    switches) and every function the user has written."""
    templates = []
    for t in templates_registry.REGISTRY:
        ov = SESSION.template_overrides.get(t.variable, {})
        resolved = None
        # a '--' template applies in any domain — look across everything built
        doms = [upper(d) for d in t.domains] or sorted(SESSION.results)
        for dom in doms:
            res = SESSION.results.get(upper(dom))
            if res is None or not res.ok:
                continue
            name = dom + t.variable[2:] if t.variable.startswith("--") else t.variable
            b = next((x for x in res.blocks if x.variable == name), None)
            if b is not None and b.method_source == "template":
                resolved = {"domain": upper(dom), "mtype": b.mtype, "dataset": b.dataset,
                            "column": b.column, "value": b.value, "recipe": b.recipe,
                            "args": b.args or {}, "reason": b.reason}
                break
        templates.append({
            "variable": t.variable, "domains": list(t.domains), "source": t.source,
            "describe": t.describe, "enabled": ov.get("enabled", True) is not False,
            "resolved": resolved, "edit": ov.get("edit") or None,
        })
    return {"templates": templates,
            "custom": sorted(SESSION.custom_fns.values(), key=lambda f: f.get("name", ""))}


@app.post("/api/functions")
def save_function(body: FnIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name the function")
    if not body.variable.strip():
        raise HTTPException(400, "name the variable the function fills")
    SESSION.custom_fns[name] = {
        "name": name, "description": body.description.strip(),
        "variable": upper(body.variable), "domains": [upper(d) for d in body.domains],
        "steps": body.steps, "override": body.override, "enabled": body.enabled,
    }
    _autosave()
    return {"ok": True, "count": len(SESSION.custom_fns)}


@app.delete("/api/functions/{name}")
def delete_function(name: str):
    SESSION.custom_fns.pop(name, None)
    _autosave()
    return {"ok": True, "count": len(SESSION.custom_fns)}


class TemplateSave(BaseModel):
    enabled: bool | None = None
    edit: dict | None = None          # {dataset?, column?, value?, args?} applied on top
    clear_edit: bool = False


@app.post("/api/functions/template/{variable}")
def save_template(variable: str, body: TemplateSave):
    var = upper(variable)
    if var not in {t.variable for t in templates_registry.REGISTRY}:
        raise HTTPException(404, f"no template derivation for {var}")
    ov = dict(SESSION.template_overrides.get(var, {}))
    if body.enabled is not None:
        ov["enabled"] = body.enabled
    if body.clear_edit:
        ov.pop("edit", None)
    elif body.edit is not None:
        ov["edit"] = body.edit
    SESSION.template_overrides[var] = ov
    _autosave()
    return {"ok": True, "override": ov}


@app.get("/api/functions/context/{domain}")
def fn_context(domain: str):
    """Just enough of a domain's shape to drive the function editor's pickers — works
    before any build: datasets from the raw folder, variables from the spec."""
    dom = upper(domain)
    if SESSION.spec is None or dom not in SESSION.spec.domains:
        raise HTTPException(404, f"{dom} is not in the mapping spec")
    datasets = sorted(SESSION.store.refs) if SESSION.store else []
    variables = [{"variable": upper(r.variable)}
                 for r in SESSION.spec.rows(dom) if r.variable]
    return {"domain": dom, "datasets": datasets,
            "prepared_datasets": sorted(SESSION.preview_outputs),
            "variables": variables,
            "codelists": sorted(SESSION.spec.codelists) if SESSION.spec else []}


@app.get("/api/prep/ops")
def prep_ops():
    """The operations the pipeline editor can offer — taken from the engine, so the
    interface can never present a step the engine cannot run."""
    return {"ops": [{"id": k, "label": v} for k, v in sorted(prep_module.PREP_OPS.items())],
            "conditions": [{"id": k, "label": v} for k, v in prep_module.COND_OPS.items()]}


@app.get("/api/domain/{domain}/pipeline")
def get_pipeline(domain: str):
    dom = upper(domain)
    return {"domain": dom, "steps": SESSION.pipelines.get(dom, []),
            "datasets": sorted(SESSION.store.refs) if SESSION.store else []}


@app.post("/api/domain/{domain}/pipeline/preview")
def preview_pipeline(domain: str, body: PipelineIn):
    """Run a pipeline without keeping it, and report each step's output — rows, columns and
    a sample — so a preparation can be checked step by step before it is adopted."""
    if SESSION.store is None:
        raise HTTPException(400, "scan the raw data folder first")
    # The draft persists as it is typed — half-finished steps included — so a refresh or a
    # reopened study lands back in the editor exactly as it was left, not on an empty form.
    dom = upper(domain)
    if body.steps:
        SESSION.draft_pipelines[dom] = body.steps
    else:
        SESSION.draft_pipelines.pop(dom, None)
    _autosave()
    try:
        outputs, reports = prep_module.run_pipeline(body.steps, SESSION.store, upper(domain))
    except prep_module.PrepError as exc:
        return {"ok": False, "error": str(exc)}
    # Register what the preview produced, so a prepared dataset can be used as a source the
    # moment it exists. Watching a dataset being built and then not finding it in the variable
    # editor is a trap the reader has no way to diagnose.
    preview = {}
    for name, frame in outputs.items():
        SESSION.store.put(name, frame)
        SESSION.preview_outputs.add(name)
        head = frame.head(8)
        preview[name] = {
            "rows": int(len(frame)), "columns": [str(c) for c in frame.columns],
            "sample": head.astype(object).where(head.notna(), "").values.tolist(),
        }
    return {"ok": True, "reports": reports, "outputs": preview}


@app.post("/api/domain/{domain}/pipeline")
def set_pipeline(domain: str, body: PipelineIn):
    dom = upper(domain)
    if SESSION.spec is None or dom not in SESSION.spec.domains:
        raise HTTPException(404, f"{dom} is not in the mapping spec")
    SESSION.draft_pipelines.pop(dom, None)       # applying (or clearing) supersedes the draft
    if body.steps:
        SESSION.preview_outputs -= {norm_key(st.get("name", "")) for st in body.steps}
        SESSION.pipelines[dom] = body.steps
        SESSION.overrides.setdefault(dom, {})["prep_mode"] = "custom"
        if body.base is not None:
            SESSION.overrides[dom]["base"] = norm_key(body.base) if body.base else ""
    else:
        SESSION.pipelines.pop(dom, None)
        if SESSION.overrides.get(dom, {}).get("prep_mode") == "custom":
            SESSION.overrides[dom]["prep_mode"] = "auto"
    _autosave()
    return {"domain": dom, "steps": SESSION.pipelines.get(dom, [])}


@app.post("/api/domain/{domain}/pipeline/from-auto")
def pipeline_from_auto(domain: str):
    """Seed an editable pipeline from whatever the automatic detection proposed, so the
    detected stack or transpose becomes a starting point rather than a black box."""
    dom = upper(domain)
    res = SESSION.results.get(dom)
    if res is None or res.prep_step is None:
        raise HTTPException(404, f"no preparation was detected for {dom}")
    step = res.prep_step.as_dict()
    SESSION.pipelines[dom] = [{"op": step["op"], "name": step["name"], "params": step["params"]}]
    SESSION.overrides.setdefault(dom, {})["prep_mode"] = "custom"
    _autosave()
    return {"domain": dom, "steps": SESSION.pipelines[dom]}


# ── step 4: compare ─────────────────────────────────────────────────────────
@app.post("/api/compare")
def start_compare(body: CompareIn):
    if not SESSION.results:
        raise HTTPException(400, "build the SDTM datasets first")
    if RUNNER.busy:
        raise HTTPException(409, "a job is already running")
    vendor = Path(os.path.expanduser(body.path))
    try:
        found = discover_vendor(vendor)
    except NotADirectoryError as exc:
        raise HTTPException(400, str(exc))
    if not found:
        raise HTTPException(400, f"no SDTM datasets (.xpt/.sas7bdat/.csv/.parquet) in {vendor}")
    SESSION.vendor_path = str(vendor.resolve())

    # record-matching keys set per domain in the domain view, plus anything sent with the call
    keys = {d: o["keys"] for d, o in SESSION.overrides.items() if o.get("keys")}
    keys.update({upper(k): v for k, v in body.keys.items()})

    # a restricted comparison covers exactly the named domains — and their SUPP-- —
    # with no "delivered but not built" noise from everything else in the vendor folder
    wanted = {upper(d) for d in body.domains if s(d)}
    missing = sorted(wanted - set(SESSION.results))
    if missing:
        raise HTTPException(400, f"not built in this session: {', '.join(missing)}")
    targets = ({k: v for k, v in SESSION.results.items() if k in wanted}
               if wanted else SESSION.results)

    def work(progress):
        progress(f"reading {len(found)} vendor dataset(s)", step=1, total=3)
        comps = compare_study(targets, vendor,
                              keys=keys,
                              ignore_case=body.ignore_case,
                              numeric_tol=body.numeric_tolerance,
                              ignore_vars=set(body.ignore_vars))
        if wanted:
            allowed = wanted | {f"SUPP{d}" for d in wanted}
            comps = {k: v for k, v in comps.items() if k in allowed}
        SESSION.comps = comps
        out = Path(SESSION.out_dir)
        progress("writing the comparison workbook", step=2)
        wb = report.write_comparison_workbook(comps, out / "vendor_comparison.xlsx")
        SESSION.outputs["comparison"] = str(wb)
        progress("writing the report", step=3)
        meta = dict(SESSION.build_meta, vendor=SESSION.vendor_path)
        html = report.write_html_report(SESSION.results, comps, out / "build_report.html", meta)
        SESSION.outputs["report"] = str(html)
        _save_session()

    RUNNER.start("compare", work, total=3)
    return {"started": True, "vendor_datasets": sorted(found)}


@app.get("/api/compare/results")
def compare_results():
    out = []
    for dom in sorted(SESSION.comps):
        c = SESSION.comps[dom]
        out.append({
            "domain": dom,
            "status": "error" if c.error else ("identical" if c.clean else "differences"),
            "error": c.error, "keys": c.keys, "key_note": c.key_note, "notes": c.notes,
            "rows_built": c.rows_built, "rows_vendor": c.rows_vendor, "matched": c.matched,
            "only_built": c.only_built, "only_vendor": c.only_vendor,
            "value_differences": c.total_differences,
            "vars_only_built": c.vars_only_built, "vars_only_vendor": c.vars_only_vendor,
            "not_built": c.not_built,
            "variables": [{"variable": d.variable, "compared": d.compared,
                           "differing": d.differing, "agreement": round(d.agreement, 1),
                           "only_built_nonblank": d.only_built_nonblank,
                           "only_vendor_nonblank": d.only_vendor_nonblank,
                           "examples": d.examples}
                          for d in c.diffs if d.differing],
        })
    return {"domains": out, "vendor_path": SESSION.vendor_path,
            "synthetic": SESSION.synthetic, "outputs": SESSION.outputs}


# ── job + session ───────────────────────────────────────────────────────────
@app.get("/api/job")
def job_state():
    return RUNNER.state()


@app.get("/api/state")
def state():
    return {
        "version": __version__,
        "study_id": SESSION.study_id, "study_name": SESSION.study_name,
        "spec": SESSION.spec_path, "raw": SESSION.raw_path, "vendor": SESSION.vendor_path,
        "out_dir": SESSION.out_dir, "studyid": SESSION.studyid,
        "domains": SESSION.spec.domain_names if SESSION.spec else [],
        "built": sorted(SESSION.results), "compared": sorted(SESSION.comps),
        "outputs": SESSION.outputs, "job": RUNNER.state(),
    }


@app.post("/api/reset")
def reset():
    global SESSION
    if RUNNER.busy:
        raise HTTPException(409, "a job is running")
    SESSION = Session()
    return {"ok": True}


# ── downloads ───────────────────────────────────────────────────────────────
@app.get("/api/report")
def serve_report():
    path = SESSION.outputs.get("report")
    if not path or not Path(path).exists():
        raise HTTPException(404, "no report yet — build first")
    return HTMLResponse(Path(path).read_text(encoding="utf-8"))


@app.get("/api/download")
def download(name: str):
    path = SESSION.outputs.get(name)
    if not path or not Path(path).exists():
        raise HTTPException(404, f"no output named '{name}'")
    p = Path(path)
    if p.is_dir():
        raise HTTPException(400, f"'{name}' is a folder: {p}")
    return FileResponse(p, filename=p.name)


@app.get("/api/reveal")
def reveal(name: str = "out_dir"):
    """Open the run folder in Finder — the local-app equivalent of a download-all."""
    import subprocess
    target = SESSION.out_dir if name == "out_dir" else SESSION.outputs.get(name)
    if not target or not Path(target).exists():
        raise HTTPException(404, "nothing to open yet")
    try:
        subprocess.run(["open", "-R" if Path(target).is_file() else "", str(target)],
                       check=False, capture_output=True)
    except OSError as exc:
        raise HTTPException(500, str(exc))
    return {"opened": str(target)}


# ── static UI (mounted last so /api/* wins) ─────────────────────────────────
DIST = HERE / "static_dist"


@app.get("/")
def index():
    """The entry page must never be cached.

    Asset filenames are content-hashed, so they can be cached forever — but only if the page
    that names them is fresh. A cached index.html keeps loading whichever bundle was current
    when it was stored, so the application silently stops updating and the reader is looking
    at an old build while being told it is the new one."""
    page = DIST / "index.html" if (DIST / "index.html").exists() else STATIC / "index.html"
    return FileResponse(page, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/api/build-id")
def build_id():
    """Which interface build is being served — so a stale page can be spotted, not guessed."""
    page = DIST / "index.html" if (DIST / "index.html").exists() else STATIC / "index.html"
    import re as _re
    html = page.read_text(encoding="utf-8", errors="ignore")
    assets = _re.findall(r"assets/[A-Za-z0-9._-]+\.(?:js|css)", html)
    return {"version": __version__, "assets": assets,
            "built": datetime.fromtimestamp(page.stat().st_mtime).isoformat(timespec="seconds")}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


def main():
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(prog="sdtm-oversight",
                                 description="Local SDTM build and vendor-comparison application")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--host", default="127.0.0.1", help="loopback only by default")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="start empty instead of resuming the most recent run")
    args = ap.parse_args()
    RUNS.mkdir(exist_ok=True)
    url = f"http://{args.host}:{args.port}"
    print(f"\n  SDTM Oversight {__version__}")
    resumed = "" if args.fresh else _restore_session()
    if resumed:
        print(f"  {resumed}")
    print(f"  {url}\n  local only — no network, no AI\n")
    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
