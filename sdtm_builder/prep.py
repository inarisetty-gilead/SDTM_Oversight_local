"""Data preparation: reshape raw forms into the record grain an SDTM domain needs.

Two shapes come up constantly and neither can be handled by mapping columns one at a time:

  * **stack** — a domain whose records come from several raw forms (DS from consent,
    enrolment and completion). The forms union into one record source.
  * **transpose_findings** — a wide findings form where one raw record holds several
    measurements in separate columns (SYSBP, DIABP, PULSE). SDTM needs one row per test.

Both are detected from the spec, both are reported, and both can be overridden or turned
off per domain. Detection is ported from SDTM Designer's `_auto_stack_step` and
`_auto_transpose_step`; the transforms here are executed directly rather than as generated
code, and the tool always says which step it applied and why.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from .blocks import Block
from .translate import raw_refs
from .util import as_float, blank_mask, norm_key, s, upper

UNIT_SUFFIXES = ("U", "_U", "_UN", "_UNIT")
VALUE_SUFFIXES = ("_RAW", "_STD")


@dataclass
class PrepStep:
    op: str                       # 'stack' | 'transpose_findings'
    name: str                     # the output dataset name the domain will build on
    params: dict = field(default_factory=dict)
    note: str = ""                # why this step was proposed, shown to the user

    def as_dict(self) -> dict:
        return {"op": self.op, "name": self.name, "params": self.params, "note": self.note}


# ── detection ───────────────────────────────────────────────────────────────
def detect_stack(blocks: list[Block], store, domain: str) -> PrepStep | None:
    """A raw dataset referenced by two or more of the domain's variables is a RECORD source;
    one referenced by a single variable is a per-subject lookup. Two or more record sources
    means the forms stack."""
    tally: Counter = Counter()
    for b in blocks:
        seen = set()
        for ds, _col in raw_refs(b.input_variables):
            key = store.resolve(ds)
            if key and key not in seen:
                seen.add(key)
                tally[key] += 1
    members = sorted(d for d, n in tally.items() if n >= 2)
    if len(members) < 2:
        return None
    return PrepStep(
        op="stack", name=norm_key(domain),
        params={"datasets": members},
        note=(f"{domain} has no raw dataset of its own and {len(members)} forms each supply "
              f"two or more of its variables — they are unioned into one record source: "
              f"{', '.join(members)}."),
    )


def _unit_sibling(col_upper: str, cols: dict[str, str]) -> str:
    for suf in UNIT_SUFFIXES:
        if col_upper + suf in cols:
            return cols[col_upper + suf]
    return ""


def _value_stem(token: str) -> str:
    t = upper(token)
    for suf in VALUE_SUFFIXES:
        if t.endswith(suf):
            return t[: -len(suf)]
    return t


def detect_transpose(blocks: list[Block], store, domain: str) -> PrepStep | None:
    """Wide findings: --TESTCD and --ORRES both list the SAME set of two or more raw result
    columns. Those columns are measurements, and they melt into one row per test."""
    dom = upper(domain)

    def find(suffix, exclude=()):
        for b in blocks:
            v = b.variable
            if v.endswith(suffix) and not any(v.endswith(e) for e in exclude):
                return b
        return None

    tc, orres = find("TESTCD"), find("ORRES", exclude=("ORRESU",))
    if not (tc and orres):
        return None

    # ORDER MATTERS and must be stable: the melted records are numbered by --SEQ, so the
    # measurement order has to be reproducible across runs and machines. Take it from the
    # order the spec lists the columns in, de-duplicated, never from a set.
    sdtm_vars = {b.variable for b in blocks}
    candidates: list[str] = []
    for b in (tc, orres):
        for _ds, col in raw_refs(b.input_variables):
            stem = _value_stem(col)
            if stem and stem not in sdtm_vars and stem not in candidates:
                candidates.append(stem)
    if len(candidates) < 2:
        return None

    # which collected dataset actually holds these columns? (the spec may say raw.eg.* while
    # the study collected egperf) — the one containing the most candidates wins. Ties break on
    # the dataset name so the choice is reproducible.
    best, best_hits = "", []
    for name in sorted(store.refs):
        cols = {upper(c): c for c in store.columns(name)}
        hits = [cols[c] for c in candidates if c in cols]     # follows the spec's order
        if len(hits) > len(best_hits):
            best, best_hits = name, hits
    if not best or len(best_hits) < 2:
        return None

    src_cols = {upper(c): c for c in store.columns(best)}
    measures = []
    for value_col in best_hits:
        cu = upper(value_col)
        unit = _unit_sibling(cu, src_cols)
        # a genuine measurement carries a unit sibling or a numeric raw sibling. This excludes
        # performed-flags, free-text descriptions and overall-result columns.
        if not (unit or (cu + "_RAW") in src_cols):
            continue
        code = value_col[len(dom):] if cu.startswith(dom) and len(value_col) > len(dom) else value_col
        measures.append({"testcd": upper(code), "test": upper(code),
                         "value_col": value_col, "unit_col": unit})
    if len(measures) < 2:
        return None

    te = find("TEST", exclude=("TESTCD",))
    orresu = find("ORRESU")
    testcd_col = tc.variable
    test_col = te.variable if te else dom + "TEST"
    orres_col = orres.variable
    orresu_col = orresu.variable if orresu else dom + "ORRESU"

    measure_cols = {m["value_col"] for m in measures} | {m["unit_col"] for m in measures if m["unit_col"]}
    id_vars = [c for c in store.columns(best) if c not in measure_cols]

    return PrepStep(
        op="transpose_findings", name=norm_key(domain),
        params={"dataset": best, "id_vars": id_vars, "measures": measures,
                "testcd_col": testcd_col, "test_col": test_col,
                "orres_col": orres_col, "orresu_col": orresu_col},
        note=(f"'{best}' holds {len(measures)} measurements in separate columns — melted to "
              f"one record per test. The test codes are taken from the column names "
              f"({', '.join(m['testcd'] for m in measures)}); check them against the codelist "
              f"and override the step if the submission values differ."),
    )


def detect_split_forms(store, domain: str) -> PrepStep | None:
    """A collection captured across several forms — rawlb1, rawlb2, rawlb3 for LB — is one
    record source. Stack them, so the domain builds instead of failing for want of a dataset
    called 'lb'."""
    keys = store.resolve_all(domain)
    if len(keys) < 2:
        return None
    return PrepStep(
        op="stack", name=norm_key(domain), params={"datasets": keys},
        note=(f"{domain} is collected across {len(keys)} forms — they are stacked into one "
              f"record source: {', '.join(keys)}."),
    )


def detect(blocks: list[Block], store, domain: str) -> PrepStep | None:
    """The step this domain needs, if any.

    A transpose is tried first and regardless of whether a dataset is named after the domain:
    the spec often references `raw.eg.*` while the study collected the form as `egperf`, so
    the wide form is found by which dataset actually holds the measurement columns. Stacking
    is only considered when the domain has no dataset of its own."""
    step = detect_transpose(blocks, store, domain)
    if step is not None:
        return step
    split = detect_split_forms(store, domain)
    if split is not None:
        return split
    if store.resolve(domain):
        return None
    return detect_stack(blocks, store, domain)


# ── execution ───────────────────────────────────────────────────────────────
def apply_stack(step: PrepStep, store) -> pd.DataFrame:
    names = [store.resolve(d) or d for d in step.params.get("datasets", [])]
    frames = []
    for name in names:
        try:
            df = store.get(name).copy()
        except KeyError:
            continue
        df["__SOURCE_DATASET"] = name          # provenance: which form each record came from
        frames.append(df)
    if not frames:
        raise ValueError(f"none of the datasets to stack are available: {', '.join(names)}")
    return pd.concat(frames, ignore_index=True, sort=False)


def apply_transpose(step: PrepStep, store) -> pd.DataFrame:
    p = step.params
    src = store.get(p["dataset"])
    id_vars = [c for c in p.get("id_vars", []) if c in src.columns]
    testcd, test = p["testcd_col"], p["test_col"]
    orres, orresu = p["orres_col"], p["orresu_col"]

    parts = []
    for order, m in enumerate(p.get("measures", [])):
        vcol = m.get("value_col")
        if not vcol or vcol not in src.columns:
            continue
        part = src[id_vars].copy()
        part["__ROW"] = range(len(src))        # keep the source record together after melting
        part["__M"] = order
        part[testcd] = upper(m.get("testcd"))
        part[test] = s(m.get("test")) or upper(m.get("testcd"))
        part[orres] = src[vcol]
        ucol = m.get("unit_col")
        part[orresu] = src[ucol] if ucol and ucol in src.columns else ""
        parts.append(part)
    if not parts:
        raise ValueError(f"no measurement columns found in '{p['dataset']}' to transpose")

    out = pd.concat(parts, ignore_index=True, sort=False)
    # a measurement that was not taken is not a record — SDTM findings carry results
    out = out[~blank_mask(out[orres])]
    out = out.sort_values(["__ROW", "__M"], kind="stable").drop(columns=["__ROW", "__M"])
    return out.reset_index(drop=True)


def apply_step(step: PrepStep, store) -> pd.DataFrame:
    if step.op == "stack":
        return apply_stack(step, store)
    if step.op == "transpose_findings":
        return apply_transpose(step, store)
    raise ValueError(f"unknown preparation step '{step.op}'")


# ── retargeting ─────────────────────────────────────────────────────────────
def retarget(step: PrepStep, blocks: list[Block], store, out_cols: list[str]) -> None:
    """Point the domain's mappings at the prepared dataset instead of the raw forms."""
    out_name = step.name
    upper_cols = {upper(c): c for c in out_cols}

    if step.op == "stack":
        members = {store.resolve(d) or d for d in step.params.get("datasets", [])}
        for b in blocks:
            if b.mtype == "assign" and (store.resolve(b.dataset) or b.dataset) in members:
                b.dataset = out_name
                b.reason = b.reason or f"reads the stacked record source '{out_name}'"
            for key in ("dataset",):                       # iso_date and friends carry their own
                if b.args.get(key) and (store.resolve(b.args[key]) or b.args[key]) in members:
                    b.args[key] = out_name
        return

    p = step.params
    src = store.resolve(p["dataset"]) or p["dataset"]
    testcd, test = p["testcd_col"], p["test_col"]
    orres, orresu = p["orres_col"], p["orresu_col"]
    handled = {testcd, test, orres, orresu}

    for var, col in ((testcd, testcd), (test, test), (orres, orres), (orresu, orresu)):
        b = next((x for x in blocks if x.variable == var), None)
        if b is None:
            continue
        b.mtype, b.dataset, b.column, b.recipe, b.args = "assign", out_name, col, "", {}
        b.reason = f"produced by the wide-to-long transpose of '{src}'"

    for b in blocks:
        if b.variable in handled:
            continue
        reads_src = (store.resolve(b.dataset) or b.dataset) == src or any(
            (store.resolve(ds) or ds) == src for ds, _c in raw_refs(b.input_variables))
        if not reads_src:
            continue
        v = b.variable
        if v.endswith(("STRESC", "STRESN")):
            b.mtype, b.dataset, b.column, b.recipe, b.args = "assign", out_name, orres, "", {}
        elif v.endswith("STRESU"):
            b.mtype, b.dataset, b.column, b.recipe, b.args = "assign", out_name, orresu, "", {}
        elif upper(b.column) in upper_cols:                # its column survived as an id_var
            b.dataset, b.column = out_name, upper_cols[upper(b.column)]
            if b.args.get("dataset"):
                b.args["dataset"] = out_name
        else:                                              # measure-specific column is gone
            b.dataset = out_name
            if b.args.get("dataset"):
                b.args["dataset"] = out_name
        b.reason = b.reason or f"reads the transposed base '{out_name}'"


# ── the full preparation pipeline ───────────────────────────────────────────
# An ordered list of dataset operations. Each step names its output, and any later step (or
# any variable mapping) can read that output by name. This is the same operation set SDTM
# Designer's Domain Studio offers, executed here by named functions rather than generated
# code, so a prepared dataset is as reproducible as a mapped one.

PREP_OPS = {
    "stack": "Stack — append the records of several datasets",
    "merge": "Merge — join datasets on key columns",
    "filter": "Filter — keep only the records that match",
    "select": "Select — keep only these columns",
    "drop": "Drop — remove these columns",
    "rename": "Rename — change column names",
    "derive": "Derive — set a column with if/then rules",
    "aggregate": "Aggregate — group and summarise",
    "date_extreme": "Earliest / latest date per group across datasets",
    "sort": "Sort — order the records",
    "dedup": "De-duplicate — keep the first or last record per group",
    "split": "Split — route records into separate outputs",
    "transpose_long": "Transpose — melt chosen columns into name/value records",
    "transpose_findings": "Transpose findings — value+unit columns into one record per test",
}

KEYISH = ("USUBJID", "STUDYID", "SUBJID", "X_SUBJID", "SUBJECTID", "SCRNID")

COND_OPS = {
    "==": "equals", "!=": "does not equal", "contains": "contains",
    "startswith": "starts with", "endswith": "ends with",
    "in": "is one of", "notin": "is not one of",
    "missing": "is missing", "notmissing": "is not missing",
    ">": "greater than", "<": "less than", ">=": "at least", "<=": "at most",
}


class PrepError(Exception):
    """A preparation step could not run. The message names the step and what was wrong."""


def _ci_col(df: pd.DataFrame, name) -> str:
    """Resolve a column name case-insensitively."""
    n = upper(name)
    if not n:
        return ""
    for c in df.columns:
        if upper(c) == n:
            return c
    return ""


def cond_mask(df: pd.DataFrame, conds) -> pd.Series:
    """Row mask for a list of ANDed conditions [{column, operator, value}].
    Text comparison is trimmed and case-insensitive, as SAS character comparison behaves."""
    m = pd.Series(True, index=df.index)
    for c in conds or []:
        col_req = c.get("column")
        if not col_req:
            continue
        col = _ci_col(df, col_req)
        if not col:
            raise PrepError(f"column '{col_req}' is not in the dataset "
                            f"(it has: {', '.join(str(x) for x in list(df.columns)[:8])}…)")
        opr = str(c.get("operator", "==")).strip().lower().replace(" ", "")
        opr = {"eq": "==", "ne": "!=", "<>": "!=", "starts": "startswith",
               "ends": "endswith"}.get(opr, opr)
        raw = c.get("value", "")
        val = ",".join(str(x) for x in raw) if isinstance(raw, list) else str(raw)
        text = df[col].astype("string").str.strip()
        tu, vu = text.str.upper(), val.strip().upper()
        blank = blank_mask(df[col])
        items = [x.strip().upper() for x in val.split(",") if x.strip()]
        if opr == "==":
            m &= tu.eq(vu).fillna(False)
        elif opr == "!=":
            m &= ~tu.eq(vu).fillna(False)
        elif opr == "contains":
            m &= tu.str.contains(vu, na=False, regex=False)
        elif opr == "startswith":
            m &= tu.fillna("").str.startswith(vu)
        elif opr == "endswith":
            m &= tu.fillna("").str.endswith(vu)
        elif opr == "in":
            m &= tu.isin(items).fillna(False)
        elif opr == "notin":
            m &= ~tu.isin(items).fillna(False)
        elif opr == "missing":
            m &= blank
        elif opr == "notmissing":
            m &= ~blank
        elif opr in (">", "<", ">=", "<="):
            num = pd.to_numeric(df[col], errors="coerce")
            f = as_float(val)
            m &= {">" : num > f, "<": num < f, ">=": num >= f, "<=": num <= f}[opr].fillna(False)
        else:
            raise PrepError(f"unknown condition '{opr}'")
    return m


def _load(name: str, store, ns: dict) -> pd.DataFrame:
    """A dataset by name — an earlier step's output first, then the raw folder."""
    key = norm_key(name)
    if key in ns:
        return ns[key]
    resolved = store.resolve(name)
    if not resolved:
        raise PrepError(f"dataset '{name}' is neither an earlier step's output nor a raw dataset")
    return store.get(resolved)


def _apply_one(step: dict, store, ns: dict) -> tuple[pd.DataFrame, dict]:
    """Run one step. Returns (output frame, extra named outputs)."""
    op = str(step.get("op") or "").strip().lower()
    p = step.get("params") or {}
    extra: dict[str, pd.DataFrame] = {}

    if op == "stack":
        names = p.get("datasets") or []
        if len(names) < 1:
            raise PrepError("stack needs at least one dataset")
        frames = []
        for n in names:
            d = _load(n, store, ns).copy()
            d["__SOURCE_DATASET"] = norm_key(n)
            frames.append(d)
        return pd.concat(frames, ignore_index=True, sort=False), extra

    if op == "merge":
        specs = p.get("inputs") or []
        if len(specs) < 2:
            raise PrepError("merge needs at least two datasets")
        how = str(p.get("how", "left")).lower()
        on = [upper(x) for x in (p.get("on") or [])]
        frames = []
        for spec in specs:
            d = _load(spec.get("dataset"), store, ns)
            keep = [c for c in (spec.get("columns") or []) if c in d.columns]
            if keep:                    # always retain join and subject keys alongside the pick
                keep = list(dict.fromkeys(
                    keep + [k for k in on if k in d.columns]
                         + [k for k in KEYISH if k in d.columns]))
                d = d[keep]
            frames.append(d)
        out = frames[0]
        for d in frames[1:]:
            keys = [c for c in on if c in out.columns and c in d.columns]
            if not keys:
                common = [c for c in out.columns if c in d.columns]
                keys = [c for c in KEYISH if c in common] or common
            if not keys:
                raise PrepError("merge found no column in common to join on — name the join keys")
            out = out.merge(d, on=keys, how=how, suffixes=("", "_r"))
        return out, extra

    if op == "filter":
        src = _load(p.get("dataset"), store, ns)
        conds = p.get("conds") or [{"column": p.get("column"),
                                    "operator": p.get("operator", "=="),
                                    "value": p.get("value", "")}]
        return src[cond_mask(src, conds)].reset_index(drop=True), extra

    if op == "select":
        src = _load(p.get("dataset"), store, ns)
        cols = [c for c in (_ci_col(src, x) for x in (p.get("columns") or [])) if c]
        if not cols:
            raise PrepError("select needs at least one column that exists in the dataset")
        return src[cols].copy(), extra

    if op == "drop":
        src = _load(p.get("dataset"), store, ns)
        cols = [c for c in (_ci_col(src, x) for x in (p.get("columns") or [])) if c]
        return src.drop(columns=cols), extra

    if op == "rename":
        src = _load(p.get("dataset"), store, ns)
        mapping = {}
        for r in p.get("renames") or []:
            frm = _ci_col(src, r.get("from"))
            if frm and s(r.get("to")):
                mapping[frm] = upper(r.get("to"))
        return src.rename(columns=mapping), extra

    if op == "derive":
        src = _load(p.get("dataset"), store, ns)
        out = src.copy()
        target = upper(p.get("target"))
        if not target:
            raise PrepError("name the column to set")
        if s(p.get("else_value")):
            out[target] = s(p.get("else_value"))
        elif target not in out.columns:
            out[target] = ""
        for rule in p.get("rules") or []:            # sequential if/then, first write wins last
            out.loc[cond_mask(out, rule.get("conds")), target] = s(rule.get("value"))
        return out, extra

    if op == "aggregate":
        src = _load(p.get("dataset"), store, ns)
        group = [c for c in (_ci_col(src, x) for x in (p.get("group_by") or [])) if c]
        col = _ci_col(src, p.get("column"))
        func = str(p.get("func", "min")).lower()
        if not (group and col):
            raise PrepError("aggregate needs group-by column(s) and a column to summarise")
        out_col = upper(p.get("out_col")) or col
        if func in ("min", "max"):                   # date-aware, like SAS min/max on dates
            dt = pd.to_datetime(src[col], errors="coerce", format="mixed")
            if dt.notna().any():
                g = src.assign(__v=dt).groupby(group)["__v"].agg(func).dt.strftime("%Y-%m-%d")
                return g.reset_index().rename(columns={"__v": out_col}), extra
        g = src.groupby(group)[col].agg(func)
        return g.reset_index().rename(columns={col: out_col}), extra

    if op == "date_extreme":
        group = [upper(g) for g in (p.get("group_by") or ["USUBJID"])]
        func = str(p.get("func", "min")).lower()
        out_col = upper(p.get("out_col")) or "DATE"
        frames = []
        for src_spec in p.get("sources") or []:
            d = _load(src_spec.get("dataset"), store, ns)
            dcol = _ci_col(d, src_spec.get("date_col"))
            keys = [k for k in group if _ci_col(d, k)]
            if not (dcol and keys):
                continue
            frames.append(pd.DataFrame({
                **{k: d[_ci_col(d, k)] for k in keys},
                "__d": pd.to_datetime(d[dcol], errors="coerce", format="mixed")}))
        if not frames:
            raise PrepError("none of the given datasets have both the group key and the date column")
        allrows = pd.concat(frames, ignore_index=True, sort=False)
        keys = [k for k in group if k in allrows.columns]
        g = allrows.groupby(keys)["__d"].agg(func).dt.strftime("%Y-%m-%d")
        return g.reset_index().rename(columns={"__d": out_col}), extra

    if op == "sort":
        src = _load(p.get("dataset"), store, ns)
        cols = [c for c in (_ci_col(src, x) for x in (p.get("columns") or [])) if c]
        if not cols:
            raise PrepError("sort needs at least one column")
        asc = [str(x).lower() != "desc" for x in (p.get("directions") or [])]
        asc = (asc + [True] * len(cols))[:len(cols)]
        return src.sort_values(by=cols, ascending=asc, kind="stable",
                               na_position="last").reset_index(drop=True), extra

    if op == "dedup":
        src = _load(p.get("dataset"), store, ns)
        keys = [c for c in (_ci_col(src, x) for x in (p.get("keys") or [])) if c]
        if not keys:
            raise PrepError("de-duplicate needs at least one grouping column")
        keep = "last" if str(p.get("keep", "first")).lower() == "last" else "first"
        return src.drop_duplicates(subset=keys, keep=keep).reset_index(drop=True), extra

    if op == "split":
        src = _load(p.get("dataset"), store, ns)
        branches = p.get("branches") or []
        if not branches:
            raise PrepError("split needs at least one branch")
        base_name = norm_key(step.get("name") or "split")
        remaining = pd.Series(True, index=src.index)
        outs = []
        for i, br in enumerate(branches):
            bname = norm_key(br.get("name") or (base_name if i == 0 else f"{base_name}_{i + 1}"))
            m = cond_mask(src, br.get("conds")) & remaining
            outs.append((bname, src[m].reset_index(drop=True)))
            remaining &= ~m              # first matching branch wins, as in a SAS select block
        rest = norm_key(p.get("other_name") or f"{base_name}_rest")
        outs.append((rest, src[remaining].reset_index(drop=True)))
        for name, frame in outs[1:]:
            extra[name] = frame
        return outs[0][1], extra

    if op == "transpose_long":
        src = _load(p.get("dataset"), store, ns)
        id_vars = [c for c in (_ci_col(src, x) for x in (p.get("id_vars") or [])) if c]
        value_vars = [c for c in (_ci_col(src, x) for x in (p.get("value_vars") or [])) if c]
        out = src.melt(id_vars=id_vars, value_vars=value_vars or None,
                       var_name=upper(p.get("var_name")) or "TESTCD",
                       value_name=upper(p.get("value_name")) or "ORRES")
        if p.get("drop_blank", True):
            out = out[~blank_mask(out[upper(p.get("value_name")) or "ORRES"])]
        return out.reset_index(drop=True), extra

    if op == "transpose_findings":
        return apply_transpose(PrepStep(op=op, name=step.get("name", ""), params=p), store), extra

    raise PrepError(f"unknown preparation step '{op}'")


def run_pipeline(steps: list[dict], store, domain: str = "") -> tuple[dict, list[dict]]:
    """Run an ordered pipeline. Returns ({output name: frame}, [per-step report])."""
    ns: dict[str, pd.DataFrame] = {}
    reports: list[dict] = []
    for i, step in enumerate(steps or [], start=1):
        name = norm_key(step.get("name") or f"prep{i}")
        try:
            out, extra = _apply_one(step, store, ns)
        except PrepError as exc:
            reports.append({"step": i, "name": name, "op": step.get("op"),
                            "ok": False, "error": str(exc)})
            raise
        except (KeyError, ValueError, TypeError) as exc:
            reports.append({"step": i, "name": name, "op": step.get("op"), "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
            raise PrepError(f"step {i} ({step.get('op')}): {exc}") from exc
        ns[name] = out.reset_index(drop=True)
        for k, v in extra.items():
            ns[k] = v
        reports.append({"step": i, "name": name, "op": step.get("op"), "ok": True,
                        "rows": int(len(out)), "columns": [str(c) for c in out.columns],
                        "extra_outputs": sorted(extra)})
    return ns, reports
