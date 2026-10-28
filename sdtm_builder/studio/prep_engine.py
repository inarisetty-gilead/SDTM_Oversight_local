# Vendored from sdtm-studio sdtm_api/src/prep_engine.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""Data-Prep pipeline — faithful port of SDTM Designer's `_run_prep` (KNIME-style stack of
dataset operations that run BEFORE the domain build, producing named intermediate datasets
the mapping can then read from).

Adapted only at the I/O boundary to sdtm-studio: sources load via build_engine._load_source
(synth/rawdata parquet) / _load_built (sdtm_output); DERIVED outputs persist to S3
`prep/{study}/{name}.parquet` (NOT rawdata/ — they are temp datasets, kept out of the raw source
folders) and the build still resolves them as `raw_<name>` because _load_source also searches prep/;
plus a `prep/{study}/manifest.json` + `steps.json`.

Ops: stack · merge · rename · drop · select · filter · derive (if/then) · split ·
transpose_long · transpose_findings · transpose_wide · aggregate · date_extreme.
"""
from __future__ import annotations

import contextvars
import io
import json
import re

import pandas as pd

from .build_engine import _load_built, _load_source, _s3
from .build_exec import clean
from . import sas_expr, sas_runtime
from .config import settings


def _read_raw(study, name):
    df = _load_source(study, str(name))
    if df is None:
        raise ValueError(f"dataset '{name}' not found in this study's data")
    return df


def _read_built(study, dom):
    return _load_built(study, str(dom).upper())


# {step name: error} for the steps of the current run_prep that failed. A step reading a failed
# step's output must fail too: falling through to _load_source would serve that name's prep/
# parquet from an earlier run (stale, or another study's rows) as if it were fresh.
_FAILED_STEPS = contextvars.ContextVar("prep_failed_steps", default=None)


def _prep_load(study, name, ns):
    """Load a raw dataset, a built SDTM domain ('sdtm.dm'), or a prior prep output (from ns)."""
    key = str(name).strip().lower()
    if key in ns:
        return ns[key]
    failed = _FAILED_STEPS.get()
    if failed and key in failed:
        raise ValueError(f"upstream step '{key}' failed — {failed[key]}")
    if key.startswith("sdtm."):
        dom = key.split(".", 1)[1].upper()
        df = _read_built(study, dom)
        if df is None:
            raise ValueError(f"SDTM {dom} not built yet — build it first")
    else:
        # tolerate the spec's `raw.<name>` convention for raw datasets (the mapping spec and the AI
        # prep planner both reference sources as raw.consents, raw.enroll, …); the raw file itself
        # is named without the prefix.
        raw_key = key[4:] if key.startswith("raw.") else key
        df = _read_raw(study, raw_key)
    ns[key] = df
    return df


def _ci_col(df, col):
    c = str(col or "")
    if c in df.columns:
        return c
    return {str(x).upper(): x for x in df.columns}.get(c.upper())


_FN_VALUE = re.compile(r"^\s*(upper|upcase|lower|lowcase|strip|trim)\s*\(\s*([A-Za-z0-9_]+)\s*\)\s*$", re.I)


def _derive_value(df, value):
    """Resolve a derive-step value against the frame so it can be a column TRANSFORM, not just a
    literal. Supports UPPER(col)/LOWER(col)/STRIP(col) and a bare column reference — e.g. the value
    'UPPER(DSDECOD)' uppercases the DSDECOD column per row instead of writing the text 'UPPER(DSDECOD)'.
    Returns a pandas Series (per-row) for a transform/column ref, or the original scalar for a literal
    (multi-word CT terms like 'INFORMED CONSENT OBTAINED' stay literal — only single-token values that
    exactly name a column are treated as a column reference)."""
    s = "" if value is None else str(value)
    m = _FN_VALUE.match(s)
    if m:
        col = _ci_col(df, m.group(2))
        if col is not None:
            ser = df[col].astype("string")
            fn = m.group(1).lower()
            if fn in ("upper", "upcase"):
                return ser.str.upper()
            if fn in ("lower", "lowcase"):
                return ser.str.lower()
            return ser.str.strip()   # strip / trim
    if re.match(r"^[A-Za-z0-9_]+$", s):     # a bare token that names an existing column → copy it
        col = _ci_col(df, s)
        if col is not None:
            return df[col]
    return s   # plain literal


def _self_upper_col(step):
    """If `step` is a PURE self-uppercase derive, return the column name (upper); else None.
    Pure self-uppercase = op 'derive' with exactly one rule whose single cond is {column: X,
    operator '!='/'<>'/'ne', value: ''}, rule value exactly UPPER(X)/UPCASE(X), else_value '',
    and the target is also X — i.e. `if X != '' then X = UPPER(X)` with no other logic and no
    cross-column source. Anything with extra conds, a non-UPPER value, a different source/target,
    or a non-empty else is rejected (returns None) so it is never merged."""
    if (step.get("op") or "") != "derive":
        return None
    p = step.get("params") or {}
    if str(p.get("else_value") or "") != "":
        return None
    rules = p.get("rules") or []
    if len(rules) != 1:
        return None
    conds = (rules[0] or {}).get("conds") or []
    if len(conds) != 1:
        return None
    c = conds[0] or {}
    opr = str(c.get("operator", "")).strip().lower().replace(" ", "")
    if opr not in ("!=", "<>", "ne") or str(c.get("value") or "") != "":
        return None
    m = _FN_VALUE.match(str((rules[0] or {}).get("value") or ""))
    if not m or m.group(1).lower() not in ("upper", "upcase"):
        return None
    col = str(c.get("column") or "").strip().upper()
    tgt = str(p.get("target") or "").strip().upper()
    arg = str(m.group(2)).strip().upper()
    if col and col == tgt == arg:
        return col
    return None


def consolidate_prep_steps(steps):
    """Collapse RUNS (>=2) of consecutive pure self-uppercase `derive` steps that chain
    dataset->name into a single `uppercase` batch op, re-threading the chain so downstream steps are
    untouched. Build output is unchanged (the `uppercase` op reproduces each column's transform
    exactly) and the pass is idempotent (an already-merged `uppercase` op is left alone). Every
    non-mergeable step is kept verbatim and in order."""
    steps = list(steps or [])
    n = len(steps)
    out = []
    i = 0
    while i < n:
        if _self_upper_col(steps[i]) is not None:
            run = [i]
            j = i + 1
            while j < n and _self_upper_col(steps[j]) is not None:
                prev_name = str(steps[run[-1]].get("name") or "").strip().lower()
                ds_j = str(((steps[j].get("params") or {}).get("dataset")) or "").strip().lower()
                if not prev_name or ds_j != prev_name:
                    break
                run.append(j)
                j += 1
            if len(run) >= 2:
                cols = []
                for k in run:
                    cc = _self_upper_col(steps[k])
                    if cc not in cols:
                        cols.append(cc)
                # keep the run's LAST name as the merged output so any later step that reads it
                # (including the immediate next step) still resolves; input = the run's first input.
                out.append({"op": "uppercase",
                            "name": str(steps[run[-1]].get("name") or "").strip().lower(),
                            "params": {"dataset": (steps[run[0]].get("params") or {}).get("dataset"),
                                       "columns": cols}})
                i = j
                continue
        out.append(steps[i])
        i += 1
    return out


def _rename_plan(renames):
    """({from: first target}, [(first target, further target)]) for a rename step. A column listed with
    two targets is renamed to the first and COPIED to the others — a plain mapping kept only the last
    target, silently (TEST-001: FOLDERNAME → VISIT and VISITNUM; EC's ECTPTREF for EX)."""
    ren, copies = {}, []
    for x in renames or []:
        f, t = x.get("from"), x.get("to")
        if not (f and t):
            continue
        if f in ren:
            if t != ren[f]:
                copies.append((ren[f], t))
        else:
            ren[f] = t
    return ren, copies


def _prep_op(operator):
    """A prep condition's operator in its one canonical spelling. The planner writes SAS (`=`, `ge`)
    and SQL (`not_null`) spellings too; an operator outside this list is refused — it used to be
    skipped, which kept EVERY row (TEST-001: `=` in SV VISITNUM, CE, EC; `>=` in AE EPOCH)."""
    o = str("==" if operator is None else operator).strip().lower().replace(" ", "").replace("_", "")
    o = {"=": "==", "eq": "==", "<>": "!=", "^=": "!=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<",
         "le": "<=", "isnull": "missing", "null": "missing", "ismissing": "missing", "empty": "missing",
         "notnull": "notmissing", "isnotnull": "notmissing", "isnotmissing": "notmissing",
         "notempty": "notmissing", "nin": "notin"}.get(o, o)
    if o not in ("==", "!=", ">", ">=", "<", "<=", "contains", "in", "notin", "missing", "notmissing"):
        raise ValueError(f"operator {operator!r} is not supported — use = ne > >= < <= contains in "
                         "notin missing notmissing")
    return o


def _prep_cond_mask(df, conds):
    """Row mask for ANDed conditions [{column, operator, value}]; case-insensitive, trimmed.
    > >= < <= compare with SAS rules: against another column when the value names one, as numbers
    when the value is a number (missing sorts lowest), else as text."""
    m = pd.Series(True, index=df.index)
    for c in conds or []:
        col = c.get("column")
        opr = _prep_op(c.get("operator", "=="))
        vraw = c.get("value", "")
        val = ",".join(str(x) for x in vraw) if isinstance(vraw, list) else str(vraw)
        if not col:
            continue
        col = _ci_col(df, col) or col
        if col not in df.columns:
            raise ValueError(f"column '{c.get('column')}' not found "
                             f"(dataset has: {', '.join(list(df.columns)[:8])}…)")
        s = df[col].astype(str).str.strip()
        su, vu = s.str.upper(), val.strip().upper()
        blank = s.eq("") | s.str.lower().isin(["nan", "none", "nat"])
        if opr in ("==", "!=") and vu == "":
            # SAS: a missing value equals '' — the text of an empty cell ("None", "nan") must not count
            # as a value (TEST-001 SS kept 24 records with no result through `SSORRES ne ''`)
            m &= blank if opr == "==" else ~blank
        elif opr == "==":
            m &= su.eq(vu)
        elif opr == "!=":
            m &= ~su.eq(vu)
        elif opr == "contains":
            m &= s.str.contains(val, case=False, na=False)
        elif opr == "in":
            m &= su.isin([x.strip().upper() for x in val.split(",") if x.strip()])
        elif opr == "notin":
            m &= ~su.isin([x.strip().upper() for x in val.split(",") if x.strip()])
        elif opr == "missing":
            m &= blank
        elif opr == "notmissing":
            m &= ~blank
        else:   # > >= < <=
            vcol = _ci_col(df, val.strip()) if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", val.strip()) else None
            try:
                num = None if vcol else float(val)
            except ValueError:
                num = None
            if num is not None:
                lhs, rhs = pd.to_numeric(s.where(~blank), errors="coerce").fillna(float("-inf")), num
            else:
                lhs = s.where(~blank, "")
                rhs = df[vcol].astype(str).str.strip().where(lambda x: ~x.str.lower().isin(["nan", "none", "nat"]), "") \
                    if vcol else val.strip()
            m &= {">": lhs.gt, ">=": lhs.ge, "<": lhs.lt, "<=": lhs.le}[opr](rhs)
    return m


def _resolve_col(want, columns):
    """Resolve a transpose measure's column to a REAL column when the plan's name is slightly off (a
    common AI-planning slip): exact (case-insensitive) → suffix-stripped (_RAW/_STD/_STD_UN/_UN) →
    re-suffixed variant (DANUMRT_RAW→DANUMRT, DANUMDP_RAW→DANUMDP2) → a unique short prefix match.
    Returns the actual column name or None (so a genuinely-absent measure is still skipped)."""
    if not want:
        return None
    up = {str(c).upper(): c for c in columns}
    w = str(want).strip().upper()
    if w in up:
        return up[w]
    base = re.sub(r"_(RAW|STD_UN|STD|UN)$", "", w)
    if base and base in up:
        return up[base]
    for suf in ("2", "_RAW", "_STD", "_STD_UN"):
        if base + suf in up:
            return up[base + suf]
    cands = [c for k, c in up.items() if base and k.startswith(base) and len(k) - len(base) <= 2]
    return cands[0] if len(cands) == 1 else None


def _persist_frame(study, name, df):
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)
    # Prep outputs are DERIVED (temp) datasets — keep them in prep/ so they never pollute the raw
    # source folders (synth/ generated data, rawdata/ uploaded raw). The build still resolves them as
    # raw_<name> because _load_source also searches prep/.
    _s3().put_object(Bucket=settings.S3_BUCKET, Key=f"prep/{study}/{name}.parquet", Body=buf.getvalue())


def run_prep(study, steps, persist=True):
    """Execute an ordered list of dataset operations; each output is reusable by later steps
    and (when persisted) by the build as raw_<name>. Returns {steps, outputs, frames}."""
    failed: dict = {}
    token = _FAILED_STEPS.set(failed)
    try:
        return _run_prep(study, steps, persist, failed)
    finally:
        _FAILED_STEPS.reset(token)



def _prep_sas(expr: str, frame, what: str = "expression"):
    """Parse a prep step's SAS expression against a frame's columns -> the AST. A column the
    expression names but the dataset lacks is an error naming it (SAS would quietly make it
    an uninitialised variable), and so is anything the SAS grammar does not understand."""
    try:
        tree = sas_expr.parse(expr)
    except sas_expr.SasExprError as e:
        raise ValueError(f"SAS {what} not understood: {e}") from None
    have = {str(c).upper() for c in frame.columns}
    missing = [n for n in sas_expr.names(tree) if n not in have]
    if missing:
        raise ValueError(f"the {what} reads {', '.join(missing)}, which the dataset does not have")
    return tree


def _prep_upper(frame):
    out = frame.copy()
    out.columns = [str(c).upper() for c in out.columns]
    return out


def _prep_names(xs):
    xs = re.split(r"[\s,]+", xs) if isinstance(xs, str) else (xs or [])
    return [str(x).strip().upper() for x in xs if str(x).strip()]


def _prep_carry(frame, p):
    """RETAIN / LAG / DIF of one column into a target, within BY groups in BY + ORDER order."""
    out = _prep_upper(frame)
    tgt, col = str(p.get("target") or "").strip().upper(), str(p.get("column") or "").strip().upper()
    if not tgt or not col:
        raise ValueError("pick the column to carry and name the target column")
    by, order = _prep_names(p.get("by", "USUBJID")), _prep_names(p.get("order"))
    absent = [c for c in [col, *by, *order] if c not in out.columns]
    if absent:
        raise ValueError(f"the dataset has no column {', '.join(absent)}")
    mode = str(p.get("mode") or "retain").lower()
    if mode in ("lag", "dif"):
        out[tgt] = sas_runtime._sx_lag(out, out[col], by, order, max(int(float(p.get("n") or 1)), 1), mode == "dif")
    else:
        out[tgt] = sas_runtime._sx_retain(out, out[col], by, order, "first" if mode == "first" else "locf")
    return out

def _run_prep(study, steps, persist, failed):
    ns, results, manifest = {}, [], {}
    # keep the source cache fresh so a re-persisted prep output isn't served stale
    from .build_engine import _SOURCE_CACHE
    _SOURCE_CACHE.clear()
    for k, st in enumerate(steps or []):
        op = st.get("op")
        name = (st.get("name") or f"prep{k + 1}").strip().lower()
        p = st.get("params", {}) or {}
        _extra_out = None
        _warn = None
        try:
            if op == "stack":
                dfs, _missing = [], []
                for d in p.get("datasets", []):
                    if not d:
                        continue
                    try:
                        dfs.append(_prep_load(study, d, ns))
                    except Exception:
                        _missing.append(str(d))
                if _missing and not dfs:
                    raise ValueError("dataset(s) not found: " + ", ".join(_missing)
                                     + " — remove them from this stack step or fix the name")
                if _missing:
                    # assemble-from-many-sources is resilient: stack what exists and flag the rest
                    # (e.g. a source the plan referenced that this study doesn't have) rather than
                    # collapsing the whole pipeline.
                    _up = [m for m in _missing if m.strip().lower() in failed]
                    _nf = [m for m in _missing if m not in _up]
                    _warn = "; ".join(
                        ([f"skipped source(s) not in this study: {', '.join(_nf)}"] if _nf else [])
                        + ([f"skipped failed upstream step(s): {', '.join(_up)}"] if _up else []))
                out = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
            elif op == "merge":
                how = p.get("how", "left")
                on_all = list(p.get("on", []) or [])
                on_map = [m for m in (p.get("on_map") or [])
                          if isinstance(m, dict) and m.get("left") and m.get("right")]
                _KEYISH = ("USUBJID", "STUDYID", "SUBJID", "X_SUBJID", "SUBJECTID")
                specs = p.get("inputs")
                if not specs:
                    specs = [{"dataset": p.get("left"), "columns": []},
                             {"dataset": p.get("right"), "columns": []}]
                frames = []
                for spec in specs:
                    ds = spec.get("dataset")
                    if not ds:
                        continue
                    d = _prep_load(study, ds, ns)
                    keep = [c for c in (spec.get("columns") or []) if c in d.columns]
                    if keep:
                        _mapk = [m["left"] for m in on_map] + [m["right"] for m in on_map]
                        keep = list(dict.fromkeys(
                            keep + [c for c in on_all if c in d.columns]
                                 + [c for c in _mapk if c in d.columns]
                                 + [c for c in _KEYISH if c in d.columns]))
                        d = d[keep]
                    frames.append(d)
                if not frames:
                    raise ValueError("merge needs at least one dataset")
                if len(frames) == 1:
                    _warn = "merge has a single input — nothing is joined, so this step only passes it through"
                out = frames[0]
                for d in frames[1:]:
                    lmap = [m["left"] for m in on_map if m["left"] in out.columns and m["right"] in d.columns]
                    rmap = [m["right"] for m in on_map if m["left"] in out.columns and m["right"] in d.columns]
                    if lmap and rmap:
                        out = out.merge(d, left_on=lmap, right_on=rmap, how=how, suffixes=("", "_r"))
                        continue
                    keys = [c for c in on_all if c in out.columns and c in d.columns]
                    if on_all and len(keys) < len(on_all):
                        # a named key one side lacks used to be dropped, joining by the rest — by subject
                        # alone, every record repeated once per match (TEST-001 EG: 156 tests → 2508)
                        gone = [c for c in on_all if c not in keys]
                        raise ValueError(f"merge key(s) {', '.join(gone)} not in both datasets — the join would "
                                         "repeat records; merge on keys both sides have")
                    if not keys:
                        common = [c for c in out.columns if c in d.columns]
                        keys = [c for c in _KEYISH if c in common] or common
                    if keys:
                        out = out.merge(d, on=keys, how=how, suffixes=("", "_r"))
                    else:
                        lk = next((c for c in _KEYISH if c in out.columns), None)
                        rk = next((c for c in _KEYISH if c in d.columns), None)
                        if lk and rk:
                            out = out.merge(d, left_on=lk, right_on=rk, how=how, suffixes=("", "_r"))
                        else:
                            raise ValueError(f"merge: no join key — left has {list(out.columns)[:8]}, "
                                             f"right has {list(d.columns)[:8]}")
            elif op == "rename":
                src = _prep_load(study, p["dataset"], ns)
                ren, copies = _rename_plan(p.get("renames"))
                after = [str(ren.get(c, c)).upper() for c in src.columns] + [str(t).upper() for _f, t in copies]
                dup = sorted({c for c in after if after.count(c) > 1})
                if dup:   # SAS refuses this too ("variable already exists"); pandas would keep both
                    raise ValueError(f"rename would leave two columns named {', '.join(dup)} — drop the "
                                     "existing one first or rename into a different name")
                out = src.rename(columns=ren)
                for first, extra in copies:
                    if first in out.columns:
                        out[extra] = out[first]
            elif op == "drop":
                src = _prep_load(study, p["dataset"], ns)
                out = src.drop(columns=[c for c in p.get("columns", []) if c in src.columns])
            elif op == "select":
                src = _prep_load(study, p["dataset"], ns)
                out = src[[c for c in p.get("columns", []) if c in src.columns]]
            elif op == "filter" and str(p.get("expression") or "").strip():
                # subsetting IF with a SAS expression: keep rows where it is true (non-zero, non-missing)
                src = _prep_upper(_prep_load(study, p["dataset"], ns))
                tree = _prep_sas(p["expression"], src, "WHERE expression")
                keep = sas_runtime._sx_n(src.index, sas_expr.evaluate(tree, dict(src.items()), src.index))
                out = src[keep.fillna(0).ne(0).values]
            elif op == "filter":
                src = _prep_load(study, p["dataset"], ns)
                out = src[_prep_cond_mask(src, [{"column": p.get("column"),
                                                 "operator": p.get("operator", "=="),
                                                 "value": p.get("value", "")}])]
            elif op == "compute":
                # assignment statement: TARGET = <SAS expression over this dataset's columns>;
                out = _prep_upper(_prep_load(study, p["dataset"], ns))
                tgt = str(p.get("target") or "").strip().upper()
                if not tgt:
                    raise ValueError("name the target column")
                tree = _prep_sas(p.get("expression") or "", out)
                out[tgt] = sas_expr.evaluate(tree, dict(out.items()), out.index)
            elif op == "carry":
                out = _prep_carry(_prep_load(study, p["dataset"], ns), p)
            elif op == "derive":
                src = _prep_load(study, p["dataset"], ns)
                out = src.copy()
                tgt = str(p.get("target") or "").strip().upper()
                if not tgt:
                    raise ValueError("name the target column (e.g. VISIT_)")
                if str(p.get("else_value") or "") != "":
                    out[tgt] = _derive_value(out, p["else_value"])
                elif tgt not in out.columns:
                    out[tgt] = ""
                for r in p.get("rules", []):
                    m = _prep_cond_mask(out, r.get("conds"))
                    val = _derive_value(out, r.get("value", ""))
                    out.loc[m, tgt] = (val[m] if isinstance(val, pd.Series) else str(val))
            elif op == "uppercase":
                # Batch self-uppercase: UPPER() a LIST of columns in one pass. Per column, byte-identical
                # to a trivial `derive` {rules:[{conds:[{col X,!=,''}], value:'UPPER(X)'}], else:''} — i.e.
                # `if X != '' then UPPER(X) else leave as-is` (blanks stay blank). `consolidate_prep_steps`
                # collapses runs of such derives into this op; the columns are independent, so a one-pass
                # batch equals the original chained steps.
                src = _prep_load(study, p["dataset"], ns)
                out = src.copy()
                for col in p.get("columns", []):
                    c = _ci_col(out, col)
                    if not c:
                        continue
                    m = _prep_cond_mask(out, [{"column": c, "operator": "!=", "value": ""}])
                    val = out[c].astype("string").str.upper()
                    out.loc[m, c] = val[m]
            elif op == "split":
                src = _prep_load(study, p["dataset"], ns)
                branches = p.get("branches") or [{"conds": p.get("conds") or []}]
                remaining = pd.Series(True, index=src.index)
                _outs = []
                for bx, br in enumerate(branches):
                    bname = str(br.get("name") or (name if bx == 0 else f"{name}_{bx + 1}")).strip().lower()
                    m = _prep_cond_mask(src, br.get("conds")) & remaining
                    _outs.append((bname, src[m].reset_index(drop=True)))
                    remaining &= ~m
                _rest_name = str(p.get("other_name") or f"{name}_rest").strip().lower()
                _outs.append((_rest_name, src[remaining].reset_index(drop=True)))
                out = _outs[0][1]
                _extra_out = []
                for oname, odf in _outs[1:]:
                    for c in odf.columns:
                        if odf[c].dtype == object:
                            odf[c] = odf[c].astype(str).where(odf[c].notna(), None)
                    ns[oname] = odf
                    if persist:
                        _persist_frame(study, oname, odf)
                    manifest[oname] = [str(c).upper() for c in odf.columns]
                    _extra_out.append({"name": oname, "rows": int(len(odf)),
                                       "columns": [str(c) for c in odf.columns][:200]})
            elif op == "transpose_long":
                src = _prep_load(study, p["dataset"], ns)
                out = src.melt(id_vars=[c for c in p.get("id_vars", []) if c in src.columns],
                               value_vars=([c for c in p.get("value_vars", []) if c in src.columns] or None),
                               var_name=p.get("var_name", "TESTCD"),
                               value_name=p.get("value_name", "ORRES"))
            elif op == "transpose_findings":
                src = _prep_load(study, p["dataset"], ns)
                idv = [c for c in p.get("id_vars", []) if c in src.columns]
                tcol = p.get("testcd_col", "TESTCD")
                tname = p.get("test_col", "TEST")
                ocol = p.get("orres_col", "ORRES")
                ucol = p.get("orresu_col", "ORRESU")
                dtraw = p.get("date_raw_col")
                frames = []
                for m in p.get("measures", []):
                    vc = _resolve_col(m.get("value_col"), src.columns)   # tolerate slightly-off names
                    if not vc:
                        continue
                    f = src[idv].copy()
                    f[tcol] = m.get("testcd", "")
                    f[tname] = m.get("test") or m.get("testcd", "")
                    f[ocol] = src[vc].astype(object)
                    uc = _resolve_col(m.get("unit_col"), src.columns)
                    f[ucol] = src[uc].astype(object) if uc else ""
                    if dtraw:
                        dc = _resolve_col(m.get("date_col"), src.columns)
                        f[dtraw] = src[dc].astype(object) if dc else ""
                    catc = p.get("cat_col")
                    if catc and m.get("cat") is not None:
                        f[catc] = m.get("cat", "")
                    frames.append(f)
                out = pd.concat(frames, ignore_index=True) if frames else src
                if frames and ocol in out.columns:
                    keep = out[ocol].astype(str).str.strip().replace({"nan": "", "None": ""}) != ""
                    out = out[keep].reset_index(drop=True)
                # Per-row ISO --DTC from an assessment-date column carried through the transpose, so
                # each test row keeps its own date (the deterministic Findings transpose relies on this).
                dtc_col = p.get("dtc_col")
                dtc_src = p.get("dtc_source_col")
                if dtc_col and dtc_src and dtc_src in out.columns and dtc_col not in out.columns:
                    from .build_engine import _iso
                    out[dtc_col] = _iso(out[dtc_src]).values
            elif op == "transpose_wide":
                src = _prep_load(study, p["dataset"], ns)
                out = src.pivot_table(index=[c for c in p.get("index", []) if c in src.columns],
                                      columns=p.get("columns"), values=p.get("values"),
                                      aggfunc="first").reset_index()
                out.columns = [str(c).upper() for c in out.columns]
            elif op == "aggregate":
                src = _prep_load(study, p["dataset"], ns)
                gb = [x for x in (_ci_col(src, c) for c in p.get("group_by", [])) if x]
                col = _ci_col(src, p.get("column")) or p.get("column")
                fn = p.get("func", "min")
                series = src[col]
                if fn in ("min", "max"):
                    dt = pd.to_datetime(series, errors="coerce")
                    if dt.notna().any():
                        g = src.assign(_v=dt).groupby(gb)["_v"].agg(fn).dt.strftime("%Y-%m-%d")
                    else:
                        g = src.groupby(gb)[col].agg(fn)
                else:
                    g = src.groupby(gb)[col].agg(fn)
                out = g.reset_index().rename(columns={col: p.get("out_col") or col,
                                                      "_v": p.get("out_col") or col})
            elif op == "date_extreme":
                grp_req = p.get("group_by") or [str(p.get("subject") or "SUBJID").strip() or "SUBJID"]
                grp_req = [str(g).strip().upper() for g in grp_req if str(g).strip()]
                fn = p.get("func", "min")
                frames, _issues = [], []
                _SUBJ_ALTS = ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID", "SCRNID")
                for s in p.get("sources", []):
                    dsn = s.get("dataset", "")
                    if not dsn:
                        continue
                    d = _prep_load(study, dsn, ns)
                    cu = {str(c).upper(): c for c in d.columns}
                    resolved, ok = [], True
                    for gi, g in enumerate(grp_req):
                        want = str(s.get("subject_col") or g).upper() if gi == 0 else g
                        hit = cu.get(want)
                        if hit is None and gi == 0:
                            hit = next((cu[a] for a in _SUBJ_ALTS if a in cu), None)
                        if hit is None:
                            _issues.append(f"{dsn}: group column '{g}' not found")
                            ok = False
                            break
                        resolved.append(hit)
                    dc = s.get("date_col")
                    if ok and (not dc or dc not in d.columns):
                        _issues.append(f"{dsn}: date column '{dc or '(none picked)'}' not found")
                        ok = False
                    if not ok:
                        continue
                    t = d[resolved + [dc]].copy()
                    t.columns = [f"__g{gi}" for gi in range(len(resolved))] + ["__d"]
                    frames.append(t)
                if not frames:
                    raise ValueError("; ".join(_issues) or "pick at least one dataset + date column")
                alld = pd.concat(frames, ignore_index=True)
                alld["__d"] = pd.to_datetime(alld["__d"], errors="coerce")
                gcols = [f"__g{gi}" for gi in range(len(grp_req))]
                agg = alld.groupby(gcols)["__d"].agg(fn)
                out = agg.dt.strftime("%Y-%m-%d").reset_index()
                out.columns = grp_req + [p.get("out_col") or ("RFSTDTC" if fn == "min" else "RFENDTC")]
            else:
                out = _prep_load(study, p.get("dataset", ""), ns)
            for c in out.columns:
                if out[c].dtype == object:
                    out[c] = out[c].astype(str).where(out[c].notna(), None)
            out.columns = [str(c).upper() for c in out.columns]
            ns[name] = out
            if persist:
                _persist_frame(study, name, out)
            manifest[name] = [str(c).upper() for c in out.columns]
            results.append({"name": name, "op": op, "ok": True,
                            "rows": int(len(out)), "cols": len(out.columns),
                            "columns": [str(c) for c in out.columns][:200],
                            "preview": [{kk: clean(vv) for kk, vv in r.items()}
                                        for r in out.head(100).to_dict(orient="records")],
                            **({"extra_output": _extra_out} if _extra_out else {}),
                            **({"warn": _warn} if _warn else {})})
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "NoSuchKey" in msg or "GetObject" in msg:
                msg = "a referenced dataset was not found in this study's data — check the dataset names in this step"
            results.append({"name": name, "op": op, "ok": False, "error": msg})
            failed[name] = msg
            ns.pop(name, None)
            if persist:
                # this step's output from an earlier run is now stale — remove it so neither a later
                # step nor the build (which resolves prep/<name> as raw_<name>) can pick it up
                try:
                    _s3().delete_object(Bucket=settings.S3_BUCKET, Key=f"prep/{study}/{name}.parquet")
                except Exception:  # noqa: BLE001
                    pass
    if persist:
        try:
            _s3().put_object(Bucket=settings.S3_BUCKET, Key=f"prep/{study}/manifest.json",
                             Body=json.dumps(manifest).encode())
            _s3().put_object(Bucket=settings.S3_BUCKET, Key=f"prep/{study}/steps.json",
                             Body=json.dumps({"steps": steps}).encode())
        except Exception:
            pass
    return {"steps": results, "outputs": manifest, "frames": ns}


def prep_manifest(study):
    """{name: [COLS]} of persisted prep outputs (so pickers/sources include them)."""
    try:
        obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key=f"prep/{study}/manifest.json")
        return json.loads(obj["Body"].read()) or {}
    except Exception:
        return {}
