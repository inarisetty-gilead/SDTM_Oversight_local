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

import re
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from .blocks import Block
from .translate import raw_refs
from . import ops as ops_module
from .util import as_float, blank_mask, norm_key, s, str_series, upper

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


def is_raw_file(store, name: str) -> bool:
    """True when `name` is a dataset read from the raw folder (not a prepared, in-memory one)."""
    ref = (getattr(store, "refs", None) or {}).get(norm_key(name))
    return ref is not None and getattr(ref, "kind", "") != "memory"


def output_name(store, domain: str) -> str:
    """The name an automatic step's output takes: the domain's own name, unless a raw form is
    called that — a prepared dataset must never replace a raw one, or every other domain (and
    the next rebuild of this one) reads the prepared records as if they were the raw form."""
    key = norm_key(domain)
    return f"{key}_prep" if is_raw_file(store, key) else key


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
        op="stack", name=output_name(store, domain),
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
        op="transpose_findings", name=output_name(store, domain),
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
        op="stack", name=output_name(store, domain), params={"datasets": keys},
        note=(f"{domain} is collected across {len(keys)} forms — they are stacked into one "
              f"record source: {', '.join(keys)}."),
    )


def detect(blocks: list[Block], store, domain: str) -> PrepStep | None:
    """The step this domain needs, if any.

    A transpose is tried first and regardless of whether a dataset is named after the domain:
    the spec often references `raw.eg.*` while the study collected the form as `egperf`, so
    the wide form is found by which dataset actually holds the measurement columns. Stacking
    is only considered when the domain has no dataset of its own."""
    from . import spec_transpose
    step = spec_transpose.detect(blocks, store, domain)   # the spec's own rule wins
    if step is not None:
        return step
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
    out = pd.concat(frames, ignore_index=True, sort=False)
    # stacking forms with different columns can otherwise land a column only the SECOND
    # frame introduces after __SOURCE_DATASET, even though it was assigned last in every
    # individual frame — always put it back at the end, so it is a reliable landmark
    cols = [c for c in out.columns if c != "__SOURCE_DATASET"] + ["__SOURCE_DATASET"]
    return out[cols]


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
    if step.op == "transpose_rule":
        from . import spec_transpose
        return spec_transpose.apply(step, store)
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
    if step.op == "transpose_rule":
        from . import spec_transpose
        srcs = {store.resolve(x) or x for x in spec_transpose.sources(step)}
        made = [c for c in spec_transpose.produced(step) if c in upper_cols]
        why = f"produced by the transpose the {p['testcd_col']} Mapping Rule describes"
    else:
        srcs = {store.resolve(p["dataset"]) or p["dataset"]}
        made = [p["testcd_col"], p["test_col"], p["orres_col"], p["orresu_col"]]
        why = f"produced by the wide-to-long transpose of '{next(iter(srcs))}'"
    orres, orresu = p["orres_col"], p["orresu_col"]
    handled = set(made)

    for var in made:
        b = next((x for x in blocks if x.variable == var), None)
        if b is None:
            continue
        b.mtype, b.dataset, b.column, b.recipe, b.args = "assign", out_name, var, "", {}
        b.reason = why

    for b in blocks:
        if b.variable in handled:
            continue
        reads_src = (store.resolve(b.dataset) or b.dataset) in srcs or any(
            (store.resolve(ds) or ds) in srcs for ds, _c in raw_refs(b.input_variables))
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
    "merge": "Merge — join datasets on key columns (a colliding column keeps ONE name; "
             "the later dataset's value wins, as in a SAS MERGE)",
    "filter": "Filter — keep only the records that match",
    "select": "Select — keep only these columns",
    "drop": "Drop — remove these columns",
    "rename": "Rename — change column names",
    "constant": "Add columns — set one or more new columns to a fixed value, no condition needed",
    "derive": "Derive — set a column with if/then rules",
    "compute": "Compute — make a column from other columns (fix a partial date, year, join text)",
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
    "between": "between (low, high)",
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


_DERIVE_REF = re.compile(r"^=\s*(?:([A-Za-z_]\w*)\s*\(\s*)?([A-Za-z_][\w.]*)\s*\)?\s*$")


def _derive_value(out: pd.DataFrame, val: str):
    """A derive rule's value is normally a literal — `_TRSTAT='NOT DONE'`. But SAS often sets
    one variable FROM another instead — `_trstresc=_trorres`, `_trorres=upcase(LSSTATUS)` —
    which a fixed string can never express. '=COLUMN' or '=FUNC(COLUMN)' (one function, one
    column — what the Compute step already offers) reads the data instead of typing it;
    anything else is taken as the literal text it looks like."""
    txt = s(val)
    m = _DERIVE_REF.match(txt) if txt.startswith("=") else None
    if not m:
        return txt
    func, colname = m.group(1), m.group(2)
    col = _ci_col(out, colname)
    if not col:
        raise PrepError(f"'{colname}' is not a column in this dataset")
    if not func:
        return out[col]
    func = func.lower()
    if func not in ops_module.SAS_FUNCTIONS:
        raise PrepError(f"'{func}' is not a supported function")
    try:
        return ops_module.apply_sas_function(func, [str_series(out[col])], {}, raw=[out[col]])
    except ops_module.OpError as exc:
        raise PrepError(str(exc)) from exc


def complete_partial_date(series: pd.Series) -> pd.Series:
    """ISO text with missing month/day filled with 01: 1962 -> 1962-01-01, 1962-05 ->
    1962-05-01. Full dates pass through; anything that is not a real date comes out blank
    rather than guessed."""
    text = series.astype("string").str.strip()
    m = text.str.extract(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")
    completed = (m[0] + "-" + m[1].fillna("1").str.zfill(2)
                 + "-" + m[2].fillna("1").str.zfill(2))
    parsed = pd.to_datetime(completed, errors="coerce", format="%Y-%m-%d")
    return parsed.dt.strftime("%Y-%m-%d").fillna("")


# every spelling a mapping spec or a SAS programmer writes, onto the one the engine runs
OP_ALIASES = {
    "=": "==", "eq": "==", "ne": "!=", "<>": "!=", "^=": "!=", "~=": "!=",
    "gt": ">", "ge": ">=", "lt": "<", "le": "<=",
    "starts": "startswith", "ends": "endswith", "nin": "notin", "not_in": "notin",
    "isnull": "missing", "null": "missing", "ismissing": "missing", "empty": "missing",
    "notnull": "notmissing", "not_null": "notmissing", "isnotnull": "notmissing",
    "isnotmissing": "notmissing", "notempty": "notmissing",
}


def norm_op(operator) -> str:
    """A condition's operator in its one canonical spelling. Anything else is refused: an
    operator the engine does not know must never quietly keep (or drop) every record."""
    raw = "==" if operator is None else operator
    o = str(raw).strip().lower().replace(" ", "")
    o = OP_ALIASES.get(o, o)
    if o not in COND_OPS:
        raise PrepError(f"unknown condition '{raw}' — use one of: = ne > >= < <= contains "
                        "startswith endswith in notin between missing notmissing")
    return o


def cond_mask(df: pd.DataFrame, conds) -> pd.Series:
    """Row mask for a list of ANDed conditions [{column, operator, value}].
    Text comparison is trimmed and case-insensitive, as SAS character comparison behaves.
    A missing cell equals '' (SAS: `x = ''` is true for a missing value), and > >= < <=
    follow SAS too: against another column when the value names one, as numbers when the
    value is a number (a missing value sorts lowest), otherwise as text."""
    m = pd.Series(True, index=df.index)
    for c in conds or []:
        col_req = c.get("column")
        if not col_req:
            continue
        col = _ci_col(df, col_req)
        if not col:
            raise PrepError(f"column '{col_req}' is not in the dataset "
                            f"(it has: {', '.join(str(x) for x in list(df.columns)[:8])}…)")
        opr = norm_op(c.get("operator", "=="))
        raw = c.get("value", "")
        val = ",".join(str(x) for x in raw) if isinstance(raw, list) else str(raw)
        val = _unquote(val)
        text = df[col].astype("string").str.strip()
        tu, vu = text.str.upper(), val.strip().upper()
        blank = blank_mask(df[col])
        items = [_unquote(x).strip().upper() for x in val.split(",") if x.strip()]
        if opr in ("==", "!=") and vu == "":
            m &= blank if opr == "==" else ~blank
        elif opr == "==":
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
        elif opr == "between":
            num = pd.to_numeric(df[col], errors="coerce")
            bounds = [as_float(x) for x in val.split(",")[:2]] if "," in val else None
            if not bounds:
                raise PrepError("between needs two numbers: low, high")
            m &= num.between(bounds[0], bounds[1]).fillna(False)
        else:                                                     # > >= < <=
            m &= _compare(df, col, opr, val, blank)
    return m


def _unquote(v: str) -> str:
    """'Y' and "Y" are the literal Y — a spec writes values the way SAS code quotes them."""
    t = str(v).strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        return t[1:-1]
    return t


def _compare(df: pd.DataFrame, col: str, opr: str, val: str, blank: pd.Series) -> pd.Series:
    vtxt = val.strip()
    vcol = _ci_col(df, vtxt) if re.fullmatch(r"[A-Za-z_]\w*", vtxt) else ""
    num = None if vcol else as_float(vtxt, None)
    if num is not None:
        # SAS: a missing numeric value is smaller than every number
        lhs = pd.to_numeric(df[col].where(~blank), errors="coerce").fillna(float("-inf"))
        rhs = num
    elif vcol:
        lnum = pd.to_numeric(df[col].where(~blank), errors="coerce")
        rblank = blank_mask(df[vcol])
        rnum = pd.to_numeric(df[vcol].where(~rblank), errors="coerce")
        if (lnum.notna() | blank).all() and (rnum.notna() | rblank).all():
            lhs, rhs = lnum.fillna(float("-inf")), rnum.fillna(float("-inf"))
        else:                                       # text (ISO dates compare correctly as text)
            lhs = df[col].astype("string").str.strip().where(~blank, "").fillna("")
            rhs = df[vcol].astype("string").str.strip().where(~rblank, "").fillna("")
    else:
        lhs = df[col].astype("string").str.strip().where(~blank, "").fillna("")
        rhs = vtxt
    return {">": lhs.gt, ">=": lhs.ge, "<": lhs.lt, "<=": lhs.le}[opr](rhs).fillna(False).astype(bool)


def rename_plan(columns, renames) -> tuple[dict, list]:
    """(rename map, copies) for [{from, to}]. A column listed twice is renamed once and COPIED
    for the other target(s) — a dict keeps only the last target, which silently lost one.
    Renaming onto a column the dataset already has (and keeps) is refused: it makes two
    columns with one name, and every reader then picks one of them at random."""
    cols = {upper(c): c for c in columns}
    mapping: dict = {}
    copies: list = []
    for r in renames or []:
        frm = cols.get(upper(r.get("from")))
        to = upper(r.get("to"))
        if not frm or not to or to == upper(frm):
            continue
        if frm in mapping:
            copies.append((mapping[frm], to))
        else:
            mapping[frm] = to
    targets = list(mapping.values()) + [t for _, t in copies]
    dupes = sorted({t for t in targets if targets.count(t) > 1})
    if dupes:
        raise PrepError(f"two columns are renamed to {', '.join(dupes)}")
    kept = {upper(c) for c in columns if c not in mapping}
    clash = sorted(t for t in targets if t in kept)
    if clash:
        raise PrepError(f"the dataset already has a column named {', '.join(clash)} — drop it "
                        "first, or rename onto a different name")
    return mapping, copies


def apply_renames(df: pd.DataFrame, renames) -> pd.DataFrame:
    mapping, copies = rename_plan(df.columns, renames)
    out = df.rename(columns=mapping)
    for have, to in copies:
        out[to] = out[have]
    return out


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
        out = pd.concat(frames, ignore_index=True, sort=False)
        # keep __SOURCE_DATASET as the last column even when the stacked datasets have
        # different schemas — otherwise a column only a LATER frame introduces lands after it
        cols = [c for c in out.columns if c != "__SOURCE_DATASET"] + ["__SOURCE_DATASET"]
        return out[cols], extra

    if op == "merge":
        specs = p.get("inputs") or []
        if len(specs) < 2:
            raise PrepError("merge needs at least two datasets")
        how = str(p.get("how", "left")).lower()
        on = [upper(x) for x in (p.get("on") or [])]
        canon = on[0] if on else ""
        frames = []
        for spec in specs:
            d = _load(spec.get("dataset"), store, ns)
            # a dataset may carry the first join key under a different name — DM's USUBJID
            # is AE's X_SUBJID — 'key' renames it to that name before the merge, so the two
            # line up instead of silently finding no column in common
            key_col = upper(s(spec.get("key")))
            if key_col and canon and key_col != canon and key_col in d.columns:
                if canon in d.columns:
                    raise PrepError(f"{spec.get('dataset')} already has a column named "
                                     f"{canon} — remove the join key override or drop it")
                d = d.rename(columns={key_col: canon})
            keep = [(canon if key_col and upper(c) == key_col else c)
                    for c in (spec.get("columns") or [])]
            keep = [c for c in keep if c in d.columns]
            if keep:                    # always retain join and subject keys alongside the pick
                keep = list(dict.fromkeys(
                    keep + [k for k in on if k in d.columns]
                         + [k for k in KEYISH if k in d.columns]))
                d = d[keep]
            frames.append(d)
        out = frames[0]
        for spec, d in zip(specs[1:], frames[1:]):
            if on:
                # a named key missing on either side is an error, never a quiet fallback to
                # the subject alone — that multiplies every record by the other side's visits
                for side, frame, label in (("left", out, "the merged records so far"),
                                           ("right", d, spec.get("dataset"))):
                    gone = [k for k in on if k not in frame.columns]
                    if gone:
                        raise PrepError(f"merge key {', '.join(gone)} is not in {label} "
                                        f"({side} side) — fix the join keys or add the column")
            keys = list(on) if on else []
            if not keys:
                common = [c for c in out.columns if c in d.columns]
                keys = [c for c in KEYISH if c in common] or common
            if not keys:
                raise PrepError("merge found no column in common to join on — name the join keys")
            out = out.merge(d, on=keys, how=how, suffixes=("", "_r"))
            # SAS MERGE semantics for a colliding column: the later dataset's value wins
            # wherever it has one. Never a silent COUNTRY_r twin — the reader maps COUNTRY,
            # finds the twin instead, and gets a column of nothing.
            twins = [c for c in out.columns if c.endswith("_r") and c[:-2] in out.columns]
            for c in twins:
                out[c[:-2]] = out[c].combine_first(out[c[:-2]])
            if twins:
                out = out.drop(columns=twins)
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
        return apply_renames(src, p.get("renames") or []), extra

    if op == "constant":
        src = _load(p.get("dataset"), store, ns)
        out = src.copy()
        for item in p.get("columns") or []:
            name = upper(s(item.get("name")))
            if name:
                out[name] = s(item.get("value"))
        return out, extra

    if op == "derive":
        # One condition often has to set SEVERAL columns at once — a SAS IF/DO/END block
        # assigning _TRSTAT and _TRREASND together under the same IF is the same rule
        # applying to both, not two unrelated rules that happen to share a condition.
        # `targets` (a list) plus each rule's `values` (a {column: value} map) covers that;
        # the older single-column shape (`target` + `rules[].value`) still works unchanged.
        src = _load(p.get("dataset"), store, ns)
        out = src.copy()
        targets = [upper(t) for t in (p.get("targets") or []) if s(t)]
        if not targets and s(p.get("target")):
            targets = [upper(p.get("target"))]
        if not targets:
            raise PrepError("name at least one column to set")
        else_values = p.get("else_values") if isinstance(p.get("else_values"), dict) else {}
        for t in targets:
            raw_ev = s(else_values.get(t)) if t in else_values else (
                s(p.get("else_value")) if len(targets) == 1 else "")
            if raw_ev:
                out[t] = _derive_value(out, raw_ev)
            elif t not in out.columns:
                out[t] = ""
        for rule in p.get("rules") or []:            # sequential if/then, first write wins last
            mask = cond_mask(out, rule.get("conds"))
            values = rule.get("values") if isinstance(rule.get("values"), dict) else None
            if values is None:                        # legacy single-value rule
                out.loc[mask, targets[0]] = _derive_value(out, s(rule.get("value")))
                continue
            for t in targets:
                raw_val = s(values.get(t))
                if raw_val:
                    out.loc[mask, t] = _derive_value(out, raw_val)
        return out, extra

    if op == "compute":
        src = _load(p.get("dataset"), store, ns)
        out = src.copy()
        # the first Compute release used its own names for three of these
        func = {"concat": "catx", "trim": "strip", "upcase": "upcase"}.get(
            str(p.get("func", "")).strip().lower(), str(p.get("func", "")).strip().lower())
        cols = [c for c in (_ci_col(src, x) for x in (p.get("columns") or [])) if c]
        if not cols:
            raise PrepError("compute needs at least one source column that exists in the dataset")
        target = upper(p.get("out_col")) or upper(cols[0])
        if func == "complete_date":
            out[target] = complete_partial_date(src[cols[0]])
        elif func == "year":
            out[target] = (src[cols[0]].astype("string").str.strip()
                           .str.extract(r"^(\d{4})")[0].fillna(""))
        elif func in ops_module.SAS_FUNCTIONS:
            parts = [str_series(src[c]) for c in cols]
            raw = [src[c] for c in cols]
            try:
                result = ops_module.apply_sas_function(func, parts, p, raw=raw)
            except ops_module.OpError as exc:
                raise PrepError(str(exc)) from exc
            out[target] = result.astype("string").fillna("") \
                if result.dtype != object else result
        else:
            raise PrepError("pick what to compute — a SAS function, complete a date, "
                            "or take the year")
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

    if op == "transpose_rule":
        from . import spec_transpose
        return spec_transpose.apply(PrepStep(op=op, name=step.get("name", ""), params=p), store), extra

    raise PrepError(f"unknown preparation step '{op}'")


class _StoreWithBuilt:
    """A raw store view that ALSO resolves 'built_<domain>' to an already-built SDTM
    domain's own output — so one domain's prep pipeline can merge in another domain that
    has already been built (e.g. AE merging in DM). Kept as a distinct 'built_' name rather
    than the domain's raw dataset name (often the SAME letters, 'dm') so a step can never
    silently switch between "the raw EDC extract" and "the built SDTM domain" — the reader
    always picked one of the two on purpose."""

    def __init__(self, store, built: dict | None = None):
        self._store = store
        self._built = built or {}

    def _built_frame(self, name: str) -> pd.DataFrame | None:
        key = norm_key(name)
        if not key.startswith("built_"):
            return None
        return self._built.get(key[len("built_"):].upper())

    def resolve(self, name: str) -> str | None:
        if self._built_frame(name) is not None:
            return norm_key(name)
        return self._store.resolve(name)

    def resolve_all(self, name: str) -> list[str]:
        r = self.resolve(name)
        return [r] if r else self._store.resolve_all(name)

    def get(self, name: str) -> pd.DataFrame:
        frame = self._built_frame(name)
        if frame is not None:
            return frame
        return self._store.get(name)

    def columns(self, name: str):
        try:
            return set(self.get(name).columns)
        except (KeyError, OSError, ValueError):
            return set()

    def schema(self):
        return self._store.schema()

    @property
    def refs(self):
        return self._store.refs


def run_pipeline(steps: list[dict], store, domain: str = "",
                 built: dict | None = None) -> tuple[dict, list[dict]]:
    """Run an ordered pipeline. Returns ({output name: frame}, [per-step report]).

    `built` is {DOMAIN: its built dataset} for every domain already built this session —
    a step may name 'built_<domain>' as its dataset to read that instead of the raw data."""
    if built:
        store = _StoreWithBuilt(store, built)
    ns: dict[str, pd.DataFrame] = {}
    reports: list[dict] = []
    for i, step in enumerate(steps or [], start=1):
        name = norm_key(step.get("name") or f"prep{i}")
        try:
            if is_raw_file(store, name):
                raise PrepError(f"step {i} is named '{name}', which is a raw dataset — a prepared "
                                f"dataset must not replace it (other domains read it, and a "
                                f"rebuild would run the steps over their own output). Name the "
                                f"step '{name}_prep' or similar")
            out, extra = _apply_one(step, store, ns)
            clash = [k for k in extra if is_raw_file(store, k)]
            if clash:
                raise PrepError(f"step {i} would replace the raw dataset(s) {', '.join(clash)} "
                                "— give its outputs other names")
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


def retarget_to_output(blocks, store, out_name: str) -> int:
    """Point the domain's mappings at a prepared dataset wherever its columns match.

    A hand-built pipeline is the reader saying "THIS is the data" — merged, filtered,
    deduplicated. Leaving mappings on their spec-named raw forms after that means the
    preparation applied to the records but not to the values, which is never what was meant.
    Any source whose column(s) the output carries is repointed to it — including sources
    nested inside derived recipes (a SAS pipeline's steps, a function's inputs, a date's
    component columns). A column the output lacks keeps its raw source.

    Runs BEFORE hand edits are applied, so an explicit per-variable choice still wins."""
    key = store.resolve(out_name)
    if not key:
        return 0
    cols = {upper(c) for c in store.columns(key)}
    moved = 0

    def columns_of(node: dict) -> list[str]:
        out = [upper(node.get(f)) for f in
               ("column", "date_col", "time_col", "y_col", "m_col", "d_col") if s(node.get(f))]
        out += [upper(c) for c in (node.get("columns") or []) if s(c)]
        return out

    def visit(node) -> int:
        n = 0
        if isinstance(node, dict):
            if s(node.get("dataset")) and (store.resolve(node["dataset"]) or node["dataset"]) != key:
                referenced = columns_of(node)
                if referenced and all(c in cols for c in referenced):
                    node["dataset"] = key
                    n += 1
            for v in node.values():
                n += visit(v)
        elif isinstance(node, list):
            for v in node:
                n += visit(v)
        return n

    for b in blocks:
        # not only plain assigns: a derived block keeps its original source in dataset/column
        # and the interface displays it — it must follow the retarget too
        if b.mtype in ("assign", "derived") and b.column and upper(b.column) in cols \
                and (store.resolve(b.dataset) or b.dataset) != key:
            b.dataset = key
            b.reason = (b.reason + " · " if b.reason else "") + \
                f"reads the prepared dataset '{key}'"
            moved += 1
        if b.args:
            inner = visit(b.args)
            if inner:
                moved += inner
                if "prepared dataset" not in (b.reason or ""):
                    b.reason = (b.reason + " · " if b.reason else "") + \
                        f"reads the prepared dataset '{key}'"
    return moved
