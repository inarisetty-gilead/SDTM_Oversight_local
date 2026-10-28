"""SDTM Studio studies inside Oversight: import an export, build with the studio's engine, and
hand the results to the rest of the application (domain view, compare, report, writers).

A studio study lives in one folder (studies/<id>/studio): the export's store and reference
outputs, a SQLite database loaded from its rows, and a mapping-spec workbook written from the
studio's spec so Oversight's spec, label and comparison features read it like any other spec.
The mapping itself — blocks, saved Domain Build edits, prep steps — stays the studio's: it is
built by the studio's code and reviewed here, not edited here.
"""
from __future__ import annotations

import gzip
import io
import json
import shutil
import zipfile
from pathlib import Path

import pandas as pd

from . import configure, engine, load_study_tables, STANDARDS
from ..blocks import Block
from ..util import s, upper


def import_export(src: str | Path, dest: str | Path) -> dict:
    """Unpack a studio export (zip or folder) into `dest`, replacing what was there."""
    src, dest = Path(src).expanduser(), Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if src.is_file() and zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                if n.startswith("/") or ".." in Path(n).parts:
                    raise ValueError(f"unsafe path in the export: {n}")
            z.extractall(dest)
    elif src.is_dir():
        for part in ("store", "reference"):
            if (src / part).is_dir():
                shutil.copytree(src / part, dest / part)
        for f in ("db.json.gz", "manifest.json", "report.json"):
            if (src / f).is_file():
                shutil.copy2(src / f, dest / f)
    else:
        raise ValueError(f"not a studio export (a .zip or its folder): {src}")
    if not (dest / "db.json.gz").is_file():
        raise ValueError("the export has no db.json.gz — make it with tools/export_studio_study.py")
    db = json.loads(gzip.decompress((dest / "db.json.gz").read_bytes()))
    configure(dest)
    load_study_tables(db["tables"])
    study = db["study_id"]
    st = (db["tables"].get("studies") or [{}])[0]
    spec_path = write_spec(dest, db)
    raw = next((dest / "store" / p / study for p in ("synth", "rawdata")
                if (dest / "store" / p / study).is_dir()), dest / "store")
    manifest = json.loads((dest / "manifest.json").read_text()) if (dest / "manifest.json").is_file() else {}
    return {"study_id": study, "study_name": st.get("study_name") or "",
            "protocol_number": st.get("protocol_number") or "", "spec_path": str(spec_path),
            "raw_path": str(raw), "domains": manifest.get("domains") or saved_domains(dest, study),
            "exported": manifest.get("exported", "")}


def saved_domains(dest: Path, study: str) -> list[str]:
    d = Path(dest) / "store" / "sdtm_build" / study
    return sorted(p.stem.upper() for p in d.glob("*.json")) if d.is_dir() else []


def write_spec(dest: Path, db: dict) -> Path:
    """The studio's spec as a mapping-spec workbook (TOC, one sheet per domain, Codelist), so
    Oversight's spec views and comparisons read the same rows the studio's engine builds from."""
    run = (db["tables"].get("mapping_run") or [{}])[0]
    data = run.get("data")
    data = json.loads(data) if isinstance(data, str) else (data or {})
    specs = data.get("specs") or {}
    toc = pd.DataFrame(data.get("toc") or [{"Dataset": d, "in_study": "Y"} for d in specs])
    path = Path(dest) / "studio_spec.xlsx"
    with pd.ExcelWriter(path) as xw:
        toc.to_excel(xw, sheet_name="TOC", index=False)
        for dom in sorted(specs):
            rows = [{k: v for k, v in r.items() if not str(k).startswith("_")} for r in specs[dom] or []]
            if rows:
                pd.DataFrame(rows).to_excel(xw, sheet_name=dom[:31], index=False)
        core = STANDARDS / "specs" / "core_spec.xlsx"
        if core.is_file():
            try:
                pd.read_excel(core, sheet_name="Codelist", dtype=object).to_excel(
                    xw, sheet_name="Codelist", index=False)
            except ValueError:
                pass
    return path


def build_order(study: str, domains: list[str]) -> list[str]:
    """The studio's build order (DM first, then dependencies)."""
    be = engine("build_engine")
    try:
        order = be.build_order({d: [] for d in domains})
        return [d for d in order if d in domains] + [d for d in domains if d not in order]
    except Exception:  # noqa: BLE001
        return sorted(domains, key=lambda d: (d != "DM", d))


def build_domain(study: str, domain: str):
    """The studio's Domain Build for one domain, as the studio runs it: the saved blocks,
    settings and prep steps, the prep pipeline, then the domain. Nothing is saved back, so every
    domain reads the other domains as the study held them when it was exported — the same
    inputs a tester sees in the studio, whatever order domains are built in here.
    Returns (DomainResult, studio result)."""
    from ..build import DomainResult
    be = engine("build_exec")
    dom = upper(domain)
    comp = be.compose_blocks(study, dom)
    pre = be._prep_frames(study, comp["prep_steps"], persist=False)
    res = be._run_domain(study, dom, comp["blocks"], preloaded=pre, settings=comp["settings"],
                         row_limit=200, prep_steps=comp["prep_steps"])
    df, supp = res.pop("df", None), res.pop("supp_df", None)
    base = s((comp.get("settings") or {}).get("base")) or _base_of(comp)
    out = DomainResult(domain=dom, dataset=df, supp=supp if supp is not None and len(supp) else None,
                       blocks=to_blocks(dom, comp["blocks"], res, base), base_dataset=base)
    if df is None:
        out.error = s(res.get("error")) or "the studio's build produced no dataset"
        if res.get("load_errors"):
            out.error += " — " + "; ".join(f"{k}: {v}" for k, v in list(res["load_errors"].items())[:3])
    n_steps = len(comp["prep_steps"] or [])
    out.warnings = [f"built by SDTM Studio's engine{f' after {n_steps} prep step(s)' if n_steps else ''}"]
    out.studio_prep = comp["prep_steps"] or []
    return out, res


def _base_of(comp: dict) -> str:
    steps = comp.get("prep_steps") or []
    return s(steps[-1].get("name")) if steps else ""


def to_blocks(domain: str, blocks: list[dict], res: dict, base: str = "") -> list[Block]:
    """The studio's blocks as Oversight variables, for the domain view: what each variable is
    mapped to, the code the studio generated for it, and whether it built."""
    ran = {upper(b.get("variable")): b for b in res.get("blocks") or []}
    out = []
    for i, b in enumerate(blocks):
        v = upper(b.get("variable"))
        r = ran.get(v, {})
        mtype = s(b.get("mtype")) or "unmapped"
        blk = Block(variable=v, domain=upper(domain), mtype={"seq": "sequence"}.get(mtype, mtype),
                    dataset=s(b.get("dataset")), column=s(b.get("column")), value=s(b.get("value")),
                    recipe=s(b.get("recipe")), args=dict(b.get("args") or {}),
                    label=s(b.get("label")), codelist=s(b.get("codelist")), role=s(b.get("role")),
                    origin=s(b.get("origin")), input_variables=s(b.get("input_variables")),
                    order=i, supp=bool(b.get("supp")))
        blk.method_source = "studio"
        blk.reason = s(r.get("code"))
        if not blk.reason and r.get("op") == "prep-base":
            blk.reason = (f"taken as it is from the prepared dataset{f' {base}' if base else ''} — "
                          "the prep steps set it")
        if mtype == "drop":
            blk.status = "dropped"
        elif mtype == "unmapped":
            blk.status = "not_built"
            blk.reason = blk.reason or "not mapped in the studio"
        elif r.get("error") or r.get("ok") is False:
            blk.status, blk.error = "error", s(r.get("error")) or "failed in the studio's build"
        else:
            blk.status = "built"
        out.append(blk)
    return out


def programs(study: str, domain: str) -> dict:
    """The studio's own Python and SAS programs for a domain."""
    return engine("program_gen").generate_for_domain(study, upper(domain))


def reference_match(dest: str | Path, domain: str, df: pd.DataFrame | None) -> dict:
    """Does this build equal what the studio's Domain Build produced when the study was exported?"""
    p = Path(dest) / "reference" / f"{upper(domain)}.parquet"
    if not p.is_file():
        return {"checked": False}
    want = _norm(pd.read_parquet(p))
    got = _norm(df if df is not None else pd.DataFrame())
    if list(got.columns) != list(want.columns):
        return {"checked": True, "same": False, "why": "the columns differ"}
    if len(got) != len(want):
        return {"checked": True, "same": False, "why": f"{len(got)} records vs the studio's {len(want)}"}
    bad = [c for c in want.columns if not got[c].equals(want[c])]
    return {"checked": True, "same": not bad, "why": f"values differ in {', '.join(bad[:5])}" if bad else ""}


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        out[c] = out[c].map(lambda v: "" if v is None or (isinstance(v, float) and v != v) else str(v))
    return out.reset_index(drop=True)


def prep_summary(steps: list[dict]) -> list[dict]:
    """The studio's prep steps, one line each, for the read-only Prepare the data view."""
    out = []
    for i, st in enumerate(steps or [], start=1):
        p = st.get("params") or {}
        reads = [x for x in [p.get("dataset"), p.get("left"), p.get("right"), *(p.get("datasets") or []),
                             *[d.get("dataset") for k in ("inputs", "sources") for d in (p.get(k) or [])
                               if isinstance(d, dict)]] if x]
        out.append({"step": i, "op": s(st.get("op")), "name": s(st.get("name")), "reads": reads,
                    "params": json.loads(json.dumps(p, default=str))})
    return out


def frame_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()
