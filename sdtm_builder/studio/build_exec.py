# Vendored from sdtm-studio sdtm_api/src/build_exec.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""SDTM Domain Studio — faithful port of SDTM Designer's exec()-based build engine.

This is the *literal* SDTM Designer mapping engine (structured blocks compiled to pandas
snippets and executed with `exec()`), ported into sdtm-studio and adapted only at the I/O
boundary:

    SDTM Designer                         sdtm-studio (here)
    ─────────────────────────────         ──────────────────────────────────────────
    _read_raw  (synth/real S3)       →     build_engine._load_source (synth/rawdata parquet)
    _read_built (S3 sdtm/{study})    →     build_engine._load_built  (sdtm_output table)
    _codelists (CACHE core_spec)     →     build_engine._core_codelists (S3 specs/core_spec)
    resolved spec (specs_resolved)   →     build_engine.load_spec (mapping_run.data.specs)
    _study_avail                     →     build_engine._meta_columns (study_metadata)
    persist                          →     build_engine._persist (sdtm_output)

Everything else — the recipe compiler (`_code_from_struct`), the SAS-function/conditional/
pipeline code generators, ISO-date/study-day/LOBXFL derivations, SUPP-- transposition,
finalize (sort/dedup), and the `exec()` runtime (`_run_domain`) — is the SDTM Designer code
verbatim, so the Domain Studio behaves exactly like the app the user preferred.

SECURITY NOTE: `_run_domain` runs `exec()` on generated + user/LLM-authored snippets with a
pandas/numpy namespace. This mirrors SDTM Designer (the user explicitly chose the faithful
exec() engine). Trusted users only until sandboxed — same posture as the Domain Studio there.
"""
from __future__ import annotations

import json
import math
import re

import numpy as np
import pandas as pd

from . import sas_expr, sas_functions

from .build_engine import (
    _auto_ct_map,
    _core_codelists,
    _ct_terms,
    _load_built,
    _load_source,
    _meta_columns,
    _persist,
    _studyid,
    domain_sources,
    merge_temps,
    parse_keyed_lookup,
    parse_seq_pivot,
    temp_is_plain_copy,
    temp_stem,
    load_spec,
    variable_ct,
)

# ── JSON-safe cleaners (SDTM Designer main.py) ───────────────────────────────
_NAN_STRINGS = {"nan", "none", "nat", "n/a", "na", "—", "--"}


def clean(val):
    """NaN/Inf/NA/NaT → None for JSON serialization."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    return val


def clean_str(val):
    v = clean(val)
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in _NAN_STRINGS else s


def _as_int(x, d=0):
    try:
        return int(float(str(x)))
    except Exception:
        return d


def _as_float(x, d=0.0):
    try:
        return float(str(x))
    except Exception:
        return d


# ── Constants ────────────────────────────────────────────────────────────────
_REF_DATE_VARS = {"RFSTDTC", "RFXSTDTC", "RFICDTC", "RFENDTC", "RFXENDTC", "RFPENDTC"}
_ISO_BAD_TOKENS = {"", "NAN", "NONE", "NAT", "UN", "UNK", "UNKN", "UNKNOWN", ".", "--", "NA"}
_SUPP_COLS = ["STUDYID", "RDOMAIN", "USUBJID", "IDVAR", "IDVARVAL",
              "QNAM", "QLABEL", "QVAL", "QORIG", "QEVAL"]

# The Transform-function picker: every SAS function in the library (sas_functions), keyed by
# name, with whether it takes several inputs and which constant parameters it asks for.
SAS_FUNCS = {f["name"]: {"label": f"{f['name'].upper()} — {f['summary']}", "category": f["category"],
                         "multi": any(p["kind"] == "srcs" for p in f["params"]),
                         "fields": [p["name"] for p in f["params"] if p["kind"] not in ("src", "srcs")]}
             for f in [*sas_functions.FUNCTIONS, *sas_functions.LEGACY.values()]}

COND_OPS = ["eq", "ne", "in", "notin", "contains", "starts", "ends",
            "gt", "lt", "ge", "le", "between", "missing", "notmissing"]


# ── I/O adapters (the only thing changed from SDTM Designer) ─────────────────
def _read_raw(study, ds):
    """Load a raw source dataset by name. Raises when the study never collected it
    (so _run_domain's phantom-neutralization can tell a phantom from a real load failure)."""
    df = _load_source(study, str(ds))
    if df is None:
        raise KeyError(f"raw source '{ds}' not available for this study")
    return df


def _read_built(study, dom):
    """Load a previously-built SDTM domain (for cross-domain refs). None if not built."""
    return _load_built(study, str(dom).upper())


_CODELIST_NORM_CACHE: dict = {}


def _codelists():
    """codelist name (UPPER) -> {term_upper: submission_value}, mapping every value / decode
    to the CT submission value. Built from the core-spec Codelist sheet (via build_engine)."""
    if _CODELIST_NORM_CACHE.get("map") is not None:
        return _CODELIST_NORM_CACHE["map"]
    out = {}
    for name, terms in (_core_codelists() or {}).items():
        m = {}
        for t in terms:
            val = str(t.get("value", "") or "").strip()
            if not val or val.lower() == "nan":
                continue
            m[val.upper()] = val
            dec = str(t.get("decode", "") or "").strip()
            if dec and dec.lower() != "nan":
                for piece in dec.split(";"):
                    piece = piece.strip()
                    if piece:
                        m.setdefault(piece.upper(), val)
        if m:
            out[str(name).strip().upper()] = m
    _CODELIST_NORM_CACHE["map"] = out
    return out


def _study_collected(study):
    """{(dataset_lower, COL_UPPER)} the study actually collected — from parsed metadata.
    Replaces SDTM Designer's _study_avail for phantom-source detection."""
    pairs = set()
    for f, cols in (_meta_columns(study) or {}).items():
        for c in cols:
            pairs.add((str(f).lower(), str(c).upper()))
    return pairs


def _spec_rows(study, domain):
    """Resolved spec rows for a domain, from the latest mapping_run."""
    specs = (load_spec(study) or {}).get("specs") or {}
    return specs.get(str(domain).upper()) or specs.get(str(domain)) or []


# ── Deterministic auto-derivations (SDTM Designer's _known_dm_dates / iso_date) ──
_SUBJ_KEYS = {"SUBJID", "X_SUBJID", "SUBJECTID", "USUBJID", "SCRNID"}
_DATE_SUFFIXES = ("DAT", "DAT_RAW", "_DT", "DTM", "DATE")


def _date_catalog(sources):
    """{dataset: [date columns]} for datasets that have a subject key."""
    cat = {}
    for ds, cols in (sources or {}).items():
        cu = [str(c).upper() for c in cols]
        if not any(c in _SUBJ_KEYS for c in cu):
            continue
        dates = [c for c in cu if c.endswith("DAT") or c.endswith("DAT_RAW") or c.endswith("_DT")]
        if dates:
            cat[str(ds).lower()] = dates
    return cat


def _exposure_dates(cat, suffix):
    """date_extreme sources from exposure datasets (ex*/ec*), start or end dates."""
    out = []
    for ds, dates in cat.items():
        if ds.startswith(("ex", "ec")) or "_ex" in ds:
            out += [{"dataset": ds, "date_col": c} for c in dates if c.endswith(suffix)]
    return out


def _known_dm_dates(sources):
    """Deterministic derivations for the standard DM reference-date variables — RFSTDTC/
    RFXSTDTC (earliest exposure start), RFENDTC/RFXENDTC (latest exposure end), RFPENDTC
    (latest date on record = end of participation), RFICDTC (informed consent)."""
    cat = _date_catalog(sources)
    known = {}
    starts, ends = _exposure_dates(cat, "STDAT"), _exposure_dates(cat, "ENDAT")
    if starts:
        m = {"mtype": "derived", "recipe": "date_extreme",
             "ai_rationale": "first study treatment date (earliest exposure start)",
             "args": {"func": "min", "sources": starts}}
        known["RFSTDTC"] = known["RFXSTDTC"] = m
    if ends:
        m = {"mtype": "derived", "recipe": "date_extreme",
             "ai_rationale": "last study treatment date (latest exposure end)",
             "args": {"func": "max", "sources": ends}}
        known["RFENDTC"] = known["RFXENDTC"] = m
    all_dates = [{"dataset": ds, "date_col": c} for ds, ds_cols in cat.items() for c in ds_cols][:40]
    if all_dates:
        known["RFPENDTC"] = {"mtype": "derived", "recipe": "date_extreme",
                             "ai_rationale": "end of participation (latest date on record)",
                             "args": {"func": "max", "sources": all_dates}}
    for ds in (sources or {}):
        low = str(ds).lower()
        if "consent" in low or "icf" in low:
            ic = next((str(c).upper() for c in sources[ds] if str(c).upper().endswith("DAT")), None)
            if ic:
                known["RFICDTC"] = {"mtype": "derived", "recipe": "iso_date",
                                    "ai_rationale": "informed consent date",
                                    "args": {"dataset": low, "date_col": ic}}
            break
    return known


def _is_date_col(col):
    c = str(col or "").upper()
    return c.endswith(_DATE_SUFFIXES)


_RAW_TOK_RE = re.compile(r"raw\.([a-z0-9_]+)\.([a-z0-9_]+)", re.I)
# `if <col> ^= "" then <var> = "Y"` — presence flag (source non-missing → a literal)
_FLAG_PRESENCE_RE = re.compile(
    r"""\bif\b\s*\(?\s*(?:strip\()?\s*[a-z0-9_]+\)?\s*(?:\^=|~=|ne|not\s*=|<>)\s*(?:""|'')\s+then\b"""
    r"""[^=]*=\s*["']([^"']+)["']""", re.I)
# `if strip(<col>) = 'X' then <var> = 'Y'` — value-equals flag (source == literal → a literal)
_FLAG_EQ_RE = re.compile(
    r"""\bif\b\s*\(?\s*(?:strip\()?\s*[a-z0-9_]+\)?\s*(?:=|eq)\s*["']([^"']+)["']\s+then\b"""
    r"""[^=]*=\s*["']([^"']+)["']""", re.I)


def _first_raw_tok(iv):
    m = _RAW_TOK_RE.search(str(iv or ""))
    return (m.group(1).lower(), m.group(2).upper()) if m else (None, None)


def _parse_flag_sas(sas, iv):
    """Parse the common CODE-action flag patterns from Implemented SAS Code + the raw Input
    Variable → {ds, col, op, value, result} or None. `op` is 'notmissing' or 'eq'."""
    ds, col = _first_raw_tok(iv)
    if not (ds and col):
        return None
    me = _FLAG_EQ_RE.search(sas or "")
    if me:
        return {"ds": ds, "col": col, "op": "eq", "value": me.group(1), "result": me.group(2)}
    mp = _FLAG_PRESENCE_RE.search(sas or "")
    if mp:
        return {"ds": ds, "col": col, "op": "notmissing", "value": "", "result": mp.group(1)}
    return None


def _spec_if_chain(sas, iv, dfn, sources, prep_steps=None):
    """Implemented SAS that is an IF / ELSE chain assigning one variable from columns of this
    domain's own raw dataset → sas_if recipe args, keeping EVERY branch (the first-IF regexes
    above drop the ELSE: DM.RACE's `else race = strip(race_std)` left every normal race blank)."""
    ds, _col = _first_raw_tok(iv)
    if not ds:
        return None
    try:
        target, chains = sas_expr.parse_if_chain(sas)
    except sas_expr.SasExprError:
        return None
    if not any(c["if"] for c in chains):
        return None
    names = sas_expr.chain_names(chains)
    if not set(names) <= {str(c).upper() for c in (sources or {}).get(ds, [])}:
        return None
    args = {"dataset": ds, "chains": sas_expr.chains_to_json(chains)}
    if prep_steps:
        # a prepared base that carries the form's columns through unchanged (rename/derive/stack …)
        # is row-aligned with the output — read them there (CE.CEPRESP from raw.ce_irr via ce_base)
        from .program_gen import _prep_base_carries
        if _prep_base_carries(prep_steps, dfn, ds, names):
            return {**args, "on_base": True}
    return args if ds == dfn else None


def _is_legacy_flag_block(b, sas, iv, dfn):
    """A saved block that is exactly what the old first-IF flag parser produced from this spec
    row — an engine output saved along with the domain, not a person's edit — so it may be rebuilt."""
    if b.get("recipe") != "cond" or b.get("ai_rationale") != "conditional flag from the spec logic":
        return False
    f = _parse_flag_sas(sas, iv)
    if not f or f["ds"] != dfn:
        return False
    rule = {"src": {"kind": "column", "dataset": f["ds"], "column": f["col"]}, "op": f["op"],
            "then": {"kind": "text", "text": f["result"]}}
    if f["op"] == "eq":
        rule["value"] = f["value"]
    return (b.get("args") or {}) == {"rules": [rule], "else": {"kind": "missing"}}


def _flag_cross_code(dfn, v, f):
    """Per-subject presence/value flag when the source dataset differs from the domain base
    (e.g. DM.DTHFL from the `death` dataset) — set the flag by SUBJECT, never by row order."""
    ds, col, result = f["ds"], f["col"], f["result"]
    if f["op"] == "eq":
        hit = f"_m.str.upper() == {f['value'].upper()!r}"
    else:
        hit = "_m.notna() & ~_m.str.lower().isin(['', 'nan', 'none', 'nat'])"
    # Join by a subject key shared between the source and this domain's RAW BASE frame
    # (raw_<dom>, row-aligned with the output) — the built output frame's keys may be remapped
    # (e.g. DM.SUBJID comes from enroll, not the raw subject id).
    return (
        f"_SK = ['USUBJID', 'X_SUBJID', 'SUBJID', 'SUBJECTID']\n"
        f"_k = next((k for k in _SK if k in raw_{ds}.columns and k in raw_{dfn}.columns), None)\n"
        f"if _k is not None:\n"
        f"    _m = raw_{ds}['{col}'].astype('string').str.strip()\n"
        f"    _subj = raw_{ds}.loc[{hit}, _k].astype(str).unique()\n"
        f"    {dfn}['{v}'] = np.where(raw_{dfn}[_k].astype(str).isin(_subj), {result!r}, '')\n"
        f"else:\n"
        f"    {dfn}['{v}'] = ''")


def _usubjid_source(dfn, blocks, sources, prep_steps=()):
    """(dataset, column) for a USUBJID the spec gives no raw source: the dataset the domain's records
    come from, so USUBJID stays row-aligned with it — the prepared <dom>_base when the prep builds one
    (its USUBJID when a step names it, e.g. EX built from SDTM EC, else its X_SUBJID), else the
    domain's own raw dataset, else the one most of its variables copy from (the base Domain Build and
    the SAS program pick)."""
    from collections import Counter
    from .program_gen import _base_lineage_cols
    names = {str(st.get("name") or "").strip().lower() for st in prep_steps or []}
    for nm in (f"{dfn}_base", f"{dfn}base"):
        if nm in names:
            return nm, ("USUBJID" if "USUBJID" in _base_lineage_cols(prep_steps, dfn) else "X_SUBJID")
    cols = {str(k).lower(): {str(c).upper() for c in v} for k, v in (sources or {}).items()}
    used = Counter(str(x.get("dataset") or "").lower() for x in blocks
                   if x.get("mtype") == "assign" and x.get("dataset"))
    for ds in [dfn, *(d for d, _n in used.most_common())]:
        if "X_SUBJID" in cols.get(ds, ()):
            return ds, "X_SUBJID"
    return None


def _apply_auto_derivations(study, domain, blocks, locked):
    """Auto-pick the derivations SDTM Designer configures deterministically, for blocks the
    user hasn't edited (`locked`): DM reference dates (RFSTDTC/RFENDTC/RFPENDTC/… →
    date_extreme), and any --DTC assigned from a raw date column → partial-aware iso_date."""
    sources = _meta_columns(study) or {}
    dom = str(domain).upper()
    known = _known_dm_dates(sources) if dom == "DM" else {}
    dfn = dom.lower()
    prep_steps = (_load_saved(study, dom) or {}).get("prep_steps") or []
    _lit_re = re.compile(r'^"([^"]*)"$')
    _dm_ref_re = re.compile(r"sdtm\.dm\.([a-z0-9_]+)", re.I)
    for b in blocks:
        v = str(b.get("variable", "")).upper()
        if v in locked:   # a user-saved edit — never override
            b.pop("_sas", None)
            b.pop("_action", None)
            continue
        sas = str(b.pop("_sas", "") or "")
        act = str(b.pop("_action", None) or "").upper()
        iv = str(b.get("input_variables") or "")
        recipe = str(b.get("recipe") or "")
        generic = b.get("mtype") in ("unmapped", "", None) or (b.get("mtype") == "derived" and not recipe)
        # USUBJID with no raw source in the spec (blank Input Variables, or sdtm.<dom>.usubjid) — the
        # Gilead rule is "assign USUBJID from x_subjid", so take it from the record dataset's X_SUBJID.
        if v == "USUBJID" and generic:
            src = _usubjid_source(dfn, blocks, sources, prep_steps)
            if src:
                ds, col = src
                b.update({"mtype": "assign", "dataset": ds, "column": col, "recipe": "", "args": {},
                          "ai_rationale": f"USUBJID from {ds}.{col} — the mapping rule; the spec names no raw source"})
                continue
        # 0) common CODE-action conditional FLAGS — `if <col> ^= "" then "Y"` / `if <col>='X'
        #    then 'Y'`. Fixes flags like DM.DTHFL that must NOT be a plain date copy. Same-file
        #    source → a row-aligned `cond`; a DIFFERENT dataset → a per-subject presence `code`.
        # 0) the spec's own IF / ELSE chain over this domain's raw dataset — every branch kept
        if sas and (generic or (act == "CODE" and b.get("mtype") == "assign")):
            ch = _spec_if_chain(sas, iv, dfn, sources, prep_steps)
            if ch:
                b.update({"mtype": "derived", "recipe": "sas_if", "args": ch, "dataset": "", "column": "",
                          "ai_rationale": "the spec's IF / ELSE logic"})
                continue
        if generic and sas:
            f = _parse_flag_sas(sas, iv)
            if f:
                if f["ds"] == dfn:
                    src = {"kind": "column", "dataset": f["ds"], "column": f["col"]}
                    rule = {"src": src, "op": f["op"], "then": {"kind": "text", "text": f["result"]}}
                    if f["op"] == "eq":
                        rule["value"] = f["value"]
                    b.update({"mtype": "derived", "recipe": "cond",
                              "args": {"rules": [rule], "else": {"kind": "missing"}},
                              "dataset": "", "column": "",
                              "ai_rationale": "conditional flag from the spec logic"})
                else:
                    b.update({"mtype": "derived", "recipe": "code",
                              "args": {"code": _flag_cross_code(dfn, v, f)},
                              "dataset": "", "column": "",
                              "ai_rationale": f"per-subject flag: {f['col']} in {f['ds']}"})
                continue
        # 1) DM reference dates → the deterministic date_extreme / iso_date recipe
        if v in known:
            b.update({"mtype": known[v]["mtype"], "recipe": known[v].get("recipe", ""),
                      "args": known[v].get("args", {}), "dataset": "", "column": "",
                      "ai_rationale": known[v].get("ai_rationale", "")})
            continue
        # 2) a --DTC assigned from a raw date column → ISO 8601 (partial-aware) derivation
        if v.endswith("DTC") and len(v) > 3 and b.get("mtype") == "assign" and _is_date_col(b.get("column")):
            b.update({"mtype": "derived", "recipe": "iso_date",
                      "args": {"dataset": str(b.get("dataset") or "").lower(),
                               "date_col": str(b.get("column")).upper(), "time_col": ""},
                      "dataset": "", "column": "",
                      "ai_rationale": "ISO 8601 date from the raw date column"})
            continue
        # 2b) a --DTC left as an empty derived shell (spec Mapping Action == CODE) whose Input Variables
        #     names a single raw date column → the SAME ISO 8601 derivation. Fixes collected dates like
        #     DM.DTHDTC (from raw.death.DEATHDAT) that were staying unmapped in Domain Build even though
        #     the source exists — a plain --DTC is a direct date copy, not a complex CODE derivation.
        if v.endswith("DTC") and len(v) > 3 and generic:
            mrt = _RAW_TOK_RE.search(iv)
            if mrt and _is_date_col(mrt.group(2)):
                b.update({"mtype": "derived", "recipe": "iso_date",
                          "args": {"dataset": mrt.group(1).lower(), "date_col": mrt.group(2).upper(), "time_col": ""},
                          "dataset": "", "column": "",
                          "ai_rationale": "ISO 8601 date from the collected raw date column"})
                continue
        # 3) a fixed-literal Implemented SAS Code ("VALUE") → constant (all domains)
        m = _lit_re.match(sas)
        if m and generic and v not in ("STUDYID", "DOMAIN"):
            b.update({"mtype": "constant", "value": m.group(1), "recipe": "", "args": {},
                      "dataset": "", "column": "", "ai_rationale": "fixed value from the spec"})
            continue
        # 4) a per-subject value carried from the built DM domain (sdtm.dm.X) → row-safe lookup.
        #    DM is one-row-per-subject, so mapping by USUBJID is correct (unlike record-level
        #    cross-domain refs, which need a prep base and are left for Prepare data / AI).
        if generic:
            dmrefs = _dm_ref_re.findall(iv)
            if dmrefs and "raw." not in iv.lower() and dom != "DM":
                col = dmrefs[0].upper()
                code = (f"{dfn}['{v}'] = {dfn}['USUBJID'].map(_sref('DM','{col}'))"
                        if v != "USUBJID" else f"{dfn}['{v}'] = {dfn}['USUBJID']")
                b.update({"mtype": "derived", "recipe": "code",
                          "args": {"code": code}, "dataset": "", "column": "",
                          "ai_rationale": f"per-subject value from DM.{col}"})
                continue
    # Knowledge-driven STANDARD derivations (CDISC IG + company) — applied for every variable that has
    # one, so the user need not hand-type a standard rule (e.g. DTHFL='Y' when the subject died). A
    # user-saved edit (`locked`) always wins. These diverge from the spec's stated mapping → the block
    # is flagged `changed_from_spec` so the reviewer sees "built ≠ spec" (the spec is never rewritten).
    try:
        from .sdtm_knowledge import domain_derivations, compile_cond_args
        kbderivs = domain_derivations(dom)
        if kbderivs:
            spec_vars = {str(b.get("variable", "")).upper() for b in blocks}
            for b in blocks:
                v = str(b.get("variable", "")).upper()
                if v in locked or v not in kbderivs:
                    continue
                args, reads_var = compile_cond_args(kbderivs[v], spec_vars)
                if not args["rules"]:
                    continue
                b.update({"mtype": "derived", "recipe": "cond", "args": args,
                          "dataset": "", "column": "", "value": "",
                          "ai_rationale": kbderivs[v].get("rationale", "CDISC standard derivation"),
                          "changed_from_spec": True})
                if reads_var:
                    b["_late"] = True
    except Exception:  # noqa: BLE001 — knowledge must never break the compose
        pass
    return blocks


def _qnam_meta(study, domain):
    """{QNAM var (UPPER): {qlabel, qorig}} from spec rows tagged Dataset == 'QNAM'."""
    out = {}
    for r in _spec_rows(study, domain):
        if str(r.get("Dataset") or r.get("dataset") or "").strip().upper() == "QNAM":
            v = str(r.get("Variable") or r.get("variable") or "").strip().upper()
            if v:
                out[v] = {"qlabel": str(r.get("Label") or r.get("label") or "").strip(),
                          "qorig": str(r.get("Origin") or r.get("origin") or "").strip()}
    return out


# ── input Series expression (SDTM Designer main.py:4480) ─────────────────────
def _src_series(s, df, cast="string", selfvar=None):
    s = s or {}
    kind = (s.get("kind") or "").lower()
    wrap = (lambda e: f"({e}).astype('string')") if cast == "string" else (lambda e: e)
    if kind == "self" and selfvar:
        return wrap(f"{df}[{str(selfvar).upper()!r}]")
    if kind == "step":
        try:
            n = int(s.get("step") or 0)
        except Exception:
            n = 0
        if n > 0:
            return wrap(f"{df}['__pipe_step{n}']")
    if kind == "var" or (not kind and s.get("var")):
        return wrap(f"{df}[{str(s.get('var', '')).upper()!r}]")
    if kind == "text" or (not kind and s.get("text") is not None and not s.get("column")):
        return f"pd.Series({str(s.get('text', ''))!r}, index={df}.index, dtype='string')"
    ds, col = str(s.get("dataset", "")), str(s.get("column", ""))
    if ds and col:
        return wrap(f"raw_{ds}[{col!r}]")
    return f"pd.Series(pd.NA, index={df}.index, dtype='string')"


def _assign_coalesce_code(df, v, sources, cmap=None):
    """Code for an ASSIGN whose value is coalesced (first non-blank) across several value columns,
    each resolved onto the base: row-aligned when it IS the base dataset, else mapped by a shared
    subject key (USUBJID/X_SUBJID/SUBJID). A source with no shared key contributes NA (skipped).
    `sources` is a list of (dataset, column). Referencing raw_<ds> makes the loader load each one."""
    L = ["_SK = ['USUBJID', 'X_SUBJID', 'SUBJID', 'SUBJECTID']",
         f"_val = pd.Series(pd.NA, index={df}.index, dtype='string')"]
    for ds, col in sources:
        L += [
            f"try:\n    _rs = raw_{ds}\nexcept NameError:\n    _rs = None",
            f"if _rs is not None and '{col}' in getattr(_rs, 'columns', []):",
            f"    if _rs is raw_{df} or _rs.index.equals(raw_{df}.index):",
            f"        _s = _rs['{col}']",
            f"    else:",
            f"        _k = next((k for k in _SK if k in _rs.columns and k in raw_{df}.columns), None)",
            f"        if _k is not None:",
            f"            _m = _rs.dropna(subset=[_k]).drop_duplicates(_k)",
            f"            _s = raw_{df}[_k].map(pd.Series(_m['{col}'].values, index=_m[_k].values))",
            f"        else:",
            f"            _s = None",
            f"    if _s is not None:",
            f"        _s = _s.astype('string').str.strip().replace('', pd.NA)",
            f"        _val = _val.fillna(_s)",
        ]
    if cmap and len(cmap) <= 600:
        L.append(f"_ct = {cmap!r}")
        L.append(f"{df}[{v!r}] = _val.str.upper().map(_ct).fillna(_val)"
                 f"   # normalized to controlled terminology")
    else:
        L.append(f"{df}[{v!r}] = _val")
    return "\n".join(L)


# ── xx-merge `_temp` derivations (recipes keyed_lookup / seq_pivot) ──────────
# The consolidated program does both halves of the legacy pair: it rebuilds the temp the way
# xx-merge.sas did, then applies the spec's Implemented SAS Code to it (`strip(upcase(_atc1pt2))`).
# `args.final` holds that final assign's character functions, outermost first.
_TEMP_FINAL_FUNCS = ("strip", "trim", "left", "upcase", "lowcase", "propcase", "compbl")
_TEMP_FINAL_PY = {"strip": ".str.strip()", "trim": ".str.rstrip()", "left": ".str.lstrip()",
                  "upcase": ".str.upper()", "lowcase": ".str.lower()", "propcase": ".str.title()",
                  "compbl": ".str.replace(r' {2,}', ' ', regex=True)"}


def _temp_final_funcs(sas, temp):
    """`strip(upcase(_cmtrt))` → ['strip', 'upcase']; a bare `_visitnum` → []. None when the
    Implemented SAS Code is anything other than character functions wrapped around the temp."""
    s = str(sas or "").strip().rstrip(";").strip().lower()
    funcs = []
    while True:
        m = re.fullmatch(r"([a-z]+)\s*\((.*)\)", s, re.S)
        if not m or m.group(1) not in _TEMP_FINAL_FUNCS:
            break
        funcs.append(m.group(1))
        s = m.group(2).strip()
    return funcs if s == str(temp).lower() else None


def _temp_final_py(temp, funcs):
    if not funcs:
        return temp
    return f"{temp}.astype('string')" + "".join(_TEMP_FINAL_PY[f] for f in reversed(funcs))


def _record_keys(cols):
    """Columns identifying one CRF record in a vertical (one row per sequence) raw dataset."""
    cu = [str(c).upper() for c in (cols or [])]
    if "RECORDID" in cu:
        return ["RECORDID"]
    subj = next((k for k in ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID") if k in cu), None)
    rest = [c for c in ("DATAPAGEID", "INSTANCEID", "RECORDPOSITION") if c in cu]
    return [subj] + rest if subj and rest else None


def _keyed_lookup_code(b, df):
    """xx-merge lookup: the value of raw.<dataset>.<column> merged onto the domain base on
    <key_col> = <base_col> (optionally scan()-derived), per subject from the highest-screening
    record when the Mapping Rule says so — DM.INVNAM = raw.inv.INVNAME by
    INVID = scan(X_SUBJID, -2, '-')."""
    v = str(b.get("variable", "")).strip().upper()
    a = b.get("args") or {}
    ds, col, kc = a.get("dataset"), str(a.get("column", "")).upper(), str(a.get("key_col", "")).upper()
    bds, bcol = a.get("base_dataset") or df, str(a.get("base_col", "")).upper()
    temp = a.get("temp") or f"_{v.lower()}"
    if not (ds and col and kc and bcol):
        return f"# {v}: keyed lookup needs a lookup dataset, value column and key"
    sc, dd = a.get("scan") or {}, a.get("dedup") or {}
    keydesc = f"scan({bcol}, {sc['n']}, {sc['sep']!r})" if sc else bcol
    L = [f"# xx-merge {temp}: raw.{ds}.{col} merged on {kc} = {keydesc}"
         + (f" — highest {dd.get('order_col')} record per subject" if dd else ""),
         "_SK = ['USUBJID', 'X_SUBJID', 'SUBJID', 'SUBJECTID']",
         f"_rows = globals().get('_ROWS', raw_{df})   # the frame whose rows {df} follows",
         f"_k = next((k for k in _SK if k in raw_{bds}.columns and k in _rows.columns), None)",
         f"_kb = raw_{bds}"]
    if dd.get("order_col"):
        oc = str(dd["order_col"]).upper()
        L += [f"if _k is not None and {oc!r} in _kb.columns:",
              f"    _kb = _kb.sort_values({oc!r}, kind='mergesort').drop_duplicates(_k, keep='last')"]
    if sc:
        n = int(sc.get("n", 1))
        sep = "[" + re.escape(str(sc.get("sep") or " ")) + "]+"
        # SAS scan(): runs of delimiters count as one; leading/trailing ones are ignored.
        L.append(f"_kv = (_kb[{bcol!r}].astype('string').str.replace(r'^{sep}|{sep}$', '', regex=True)"
                 f".str.split(r'{sep}', regex=True).str[{n - 1 if n > 0 else n}])")
    else:
        L.append(f"_kv = _kb[{bcol!r}].astype('string').str.strip()")
    L += [f"_lk = raw_{ds}.assign(__k=raw_{ds}[{kc!r}].astype('string').str.strip()).drop_duplicates('__k')",
          f"_lk = pd.Series(_lk[{col!r}].values, index=_lk['__k'].values)",
          "_vv = _kv.map(_lk)"]
    na = f"pd.Series(pd.NA, index={df}.index, dtype='string')"
    if dd.get("order_col"):
        L += ["if _k is not None:   # one value per subject",
              f"    {temp} = pd.Series(_rows[_k].map(pd.Series(_vv.values, index=_kb[_k].values)).values,"
              f" index={df}.index)",
              f"elif _kb is _rows:",
              f"    {temp} = pd.Series(_vv.values, index={df}.index)",
              "else:",
              f"    {temp} = {na}"]
    else:
        L += ["if _kb is _rows:   # the key lives on this domain's own records",
              f"    {temp} = pd.Series(_vv.values, index={df}.index)",
              "else:              # record grain unknown — left blank, not guessed",
              f"    {temp} = {na}"]
    L.append(f"{df}['{v}'] = {_temp_final_py(temp, a.get('final'))}")
    return "\n".join(L)


def _seq_pivot_code(b, df):
    """xx-merge vertical→wide pivot: <column> from the row whose <seq_col> == <seq>, carried onto
    every row of the same record — CM.ATC1PT2 = WHOATC1 where ATCSEQ = 2. The base is collapsed to
    one row per record afterwards (_seq_collapse_code)."""
    v = str(b.get("variable", "")).strip().upper()
    a = b.get("args") or {}
    ds, col, sq = a.get("dataset"), str(a.get("column", "")).upper(), str(a.get("seq_col", "")).upper()
    rk, n = list(a.get("record_keys") or []), a.get("seq")
    temp = a.get("temp") or f"_{v.lower()}"
    if not (ds and col and sq and rk and n is not None):
        return f"# {v}: sequence pivot needs a dataset, value column, sequence column and record keys"
    return "\n".join([
        f"# xx-merge {temp}: {col} where {sq} = {n}, pivoted vertical→wide per {'+'.join(rk)} record",
        f"_rows = globals().get('_ROWS', raw_{df})   # the frame whose rows {df} follows",
        f"_src = raw_{ds}",
        f"_rk = [c for c in {rk!r} if c in _src.columns]",
        "def _rkey(_f):   # record keys compared as text (1 / 1.0 / '1' are the same record)",
        "    return _f[_rk].apply(lambda _c: _c.astype('string').str.strip().str.replace(r'\\.0$', '', regex=True))",
        f"_pv = _src.loc[pd.to_numeric(_src[{sq!r}], errors='coerce') == {int(n)}]",
        f"_pv = _rkey(_pv).assign(__v=_pv[{col!r}].values).drop_duplicates(_rk)",
        "if _rk and all(_c in _rows.columns for _c in _rk):",
        f"    {temp} = pd.Series(_rkey(_rows).merge(_pv, on=_rk, how='left')['__v'].values, index={df}.index)",
        "else:   # the domain's rows carry no record key — left blank, not guessed",
        f"    {temp} = pd.Series(pd.NA, index={df}.index, dtype='string')",
        f"{df}['{v}'] = {_temp_final_py(temp, a.get('final'))}"])


def _seq_collapse_code(df, args):
    """Keep one row per record (its lowest sequence) once the other sequences of a vertical raw
    dataset have been pivoted into wide columns — exactly the grain the xx-merge pivot produced."""
    sq, rk = str(args.get("seq_col", "")).upper(), list(args.get("record_keys") or [])
    return "\n".join([
        f"# vertical→wide: one row per {'+'.join(rk)} record (lowest {sq}); other {sq} rows are pivoted",
        f"_rows = globals().get('_ROWS', raw_{df})",
        "_rk = [c for c in " + repr(rk) + " if c in _rows.columns]",
        f"if _rk and {sq!r} in _rows.columns:",
        f"    _sq = pd.Series(pd.to_numeric(_rows[{sq!r}], errors='coerce').values, index={df}.index)",
        f"    _g = [pd.Series(_rows[c].values, index={df}.index) for c in _rk]",
        "    _keep = _sq.isna() | (_sq == _sq.groupby(_g, dropna=False).transform('min'))",
        f"    {df} = {df}.loc[_keep]"])


def _sas_expr_code(b, df):
    """The spec's own Implemented SAS, when it reads only other variables of this domain
    (DM.SUBJID = scan(usubjid,-2,'-')||'-'||scan(usubjid,-1,'-')), as pandas via sas_expr."""
    v = str(b.get("variable", "")).strip().upper()
    src = str((b.get("args") or {}).get("sas") or "")
    try:
        tree = sas_expr.parse(src)
    except sas_expr.SasExprError as e:
        return f"# {v}: spec SAS not understood ({e})\n{df}['{v}'] = pd.NA"
    py = sas_expr.to_py(tree, lambda n: f"{df}.get('{n}')")
    return (f"# {v} = {sas_expr.to_sas(tree)}   (spec SAS, over this domain's variables)\n"
            f"_ix = {df}.index\n"
            f"{df}['{v}'] = _sx_res(_ix, {py})")


def _seq_collapse_args(blocks, df):
    """args of the seq_pivot block that reads the domain's own raw base (collapse it), else None."""
    return next(((b.get("args") or {}) for b in blocks
                 if b.get("recipe") == "seq_pivot" and str((b.get("args") or {}).get("dataset") or "").lower() == df
                 and (b.get("args") or {}).get("record_keys")), None)


# ── SAS functions (recipe="fn") ──────────────────────────────────────────────
def _fn_code(b, df):
    """A Transform-function recipe as the SAS expression it stands for (sas_functions.fn_tree),
    evaluated with SAS semantics by the sas_runtime helpers — the same value the SAS program gets."""
    v = str(b.get("variable", "")).strip().upper()
    a = b.get("args", {}) or {}
    if not str(a.get("fn") or "").strip():
        return f"# {v}: pick a function"
    try:
        tree, n = sas_functions.fn_tree(a)
    except ValueError as e:
        return f"# {v}: {e}\n{df}[{v!r}] = pd.NA"
    srcs = list(a.get("sources") or [])
    if n and not srcs:
        return f"# {v}: add at least one input"
    refs = {f"__S{i}": _src_series(srcs[i] if i < len(srcs) else {}, df, cast="raw", selfvar=v)
            for i in range(max(n, len(srcs)))}
    sas = sas_expr.to_sas(tree, lambda nm: _src_label(srcs[int(nm[3:])]) if int(nm[3:]) < len(srcs) else "''")
    return (f"# {v} = {sas}\n"
            f"_ix = {df}.index\n"
            f"{df}[{v!r}] = _sx_res(_ix, {sas_expr.to_py(tree, lambda nm: refs.get(nm, repr(''))) })")


def _src_label(s):
    """How an input reads in a SAS comment: raw.ds.COL / VAR / 'text' / running value / step n."""
    s = s or {}
    kind = (s.get("kind") or "").lower()
    if kind == "self":
        return "<value so far>"
    if kind == "step":
        return f"<step {s.get('step')}>"
    if kind == "var" or (not kind and s.get("var")):
        return str(s.get("var", "")).upper()
    if kind == "text" or (not kind and s.get("text") is not None and not s.get("column")):
        return "'" + str(s.get("text", "")).replace("'", "''") + "'"
    return f"{s.get('dataset', '')}.{str(s.get('column', '')).upper()}"


# ── the spec's IF / ELSE chain (recipe="sas_if") ─────────────────────────────
def _sas_if_code(b, df):
    """IF / ELSE IF / ELSE assignments over the domain's raw dataset, with SAS semantics."""
    v = str(b.get("variable", "")).strip().upper()
    a = b.get("args", {}) or {}
    ds = str(a.get("dataset") or df).lower()
    try:
        chains = sas_expr.chains_from_json(a.get("chains"))
    except sas_expr.SasExprError as e:
        return f"# {v}: IF / ELSE not understood ({e})\n{df}[{v!r}] = pd.NA"
    if not chains:
        return f"# {v}: add an IF / ELSE"
    head = "\n".join("# " + ln for ln in sas_expr.chains_to_sas(chains, v))
    if a.get("on_base"):
        # the prepared base's rows ARE the output's rows: read its columns by position
        ref = lambda n: f"_rb({n!r})"  # noqa: E731
        pre = ("_rbf = globals().get('_ROWS')\n"
               f"_rb = lambda c: (pd.Series(_rbf[c].values, index={df}.index) "
               "if _rbf is not None and c in _rbf.columns and len(_rbf) == len(" + df + ") else None)\n")
    else:
        ref, pre = (lambda n: f"raw_{ds}.get({n!r})"), ""
    return (f"{head}\n{pre}_ix = {df}.index\n"
            f"{df}[{v!r}] = _sx_res(_ix, {sas_expr.chains_to_py(chains, ref)})")


# ── BY-group carry-forward (recipe="retain" | "lag") ─────────────────────────
CARRY_RECIPES = ("retain", "lag")


def _carry_args(a):
    """(source, BY vars, ORDER vars) of a retain/lag recipe, names upper-cased."""
    def up(xs):   # a list, or the text as typed ("USUBJID VSTESTCD" / "USUBJID, VSTESTCD")
        xs = re.split(r"[\s,]+", xs) if isinstance(xs, str) else (xs or [])
        return [str(x).strip().upper() for x in xs if str(x).strip()]
    by = up(a.get("by")) if a.get("by") is not None else ["USUBJID"]
    src = a.get("source") or {}
    return {"kind": "var", "var": str(src.get("var") or "").strip().upper()}, by, up(a.get("order"))


def _carry_code(b, df):
    """RETAIN carry-forward / LAG / DIF within BY groups, in BY + ORDER order (sas_runtime), after
    every other variable of the domain is built (they run with the late blocks)."""
    v = str(b.get("variable", "")).strip().upper()
    a = b.get("args", {}) or {}
    src, by, order = _carry_args(a)
    if not src["var"]:
        return f"# {v}: pick the variable to carry"
    s = f"{df}.get({src['var']!r})"
    what = src["var"]
    if b.get("recipe") == "retain":
        mode = "first" if str(a.get("mode") or "").lower() == "first" else "locf"
        return (f"# {v}: RETAIN {'first non-missing' if mode == 'first' else 'last non-missing'} {what} "
                f"BY {' '.join(by) or '(all rows)'} ORDER {' '.join(order) or '(as is)'}\n"
                f"{df}[{v!r}] = _sx_retain({df}, {s}, {by!r}, {order!r}, {mode!r})")
    n = max(_as_int(a.get("n", 1), 1), 1)
    dif = str(a.get("fn") or "lag").lower() == "dif"
    return (f"# {v}: {'DIF' if dif else 'LAG'}{n}({what}) BY {' '.join(by) or '(all rows)'} "
            f"ORDER {' '.join(order) or '(as is)'}\n"
            f"{df}[{v!r}] = _sx_lag({df}, {s}, {by!r}, {order!r}, {n}, {dif!r})")


# ── Conditional logic (recipe="cond") ────────────────────────────────────────
def _result_series(r, df, selfvar=None):
    if (r or {}).get("kind") == "missing":
        return f"pd.Series(pd.NA, index={df}.index, dtype='string')"
    return _src_series(r, df, cast="string", selfvar=selfvar)


def _cond_expr(rule, df, selfvar=None):
    op = str((rule or {}).get("op", "eq")).lower()
    s_str = _src_series((rule or {}).get("src"), df, cast="string", selfvar=selfvar)
    s_num = f"pd.to_numeric({_src_series((rule or {}).get('src'), df, cast='raw', selfvar=selfvar)}, errors='coerce')"
    val = (rule or {}).get("value", "")
    blank = f"(({s_str}).isna() | (({s_str}).str.strip() == ''))"
    if op == "missing":
        return blank
    if op == "notmissing":
        return f"~{blank}"
    if op in ("in", "notin"):
        items = [x.strip() for x in str(val).split(",") if x.strip()]
        base = f"({s_str}).isin({items!r})"
        return f"~{base}" if op == "notin" else base
    if op == "contains":
        return f"({s_str}).str.contains({str(val)!r}, na=False, regex=False)"
    if op == "starts":
        return f"({s_str}).fillna('').str.startswith({str(val)!r})"
    if op == "ends":
        return f"({s_str}).fillna('').str.endswith({str(val)!r})"
    if op in ("gt", "lt", "ge", "le"):
        sym = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}[op]
        return f"({s_num} {sym} {_as_float(val)})"
    if op == "between":
        return f"({s_num}.between({_as_float(val)}, {_as_float((rule or {}).get('value2', ''))}))"
    if op == "ne":
        return f"(({s_str}) != {str(val)!r})"
    return f"(({s_str}) == {str(val)!r})"


def _cond_code(b, df):
    v = str(b.get("variable", "")).strip().upper()
    a = b.get("args", {}) or {}
    rules = a.get("rules") or []
    els = a.get("else") or {"kind": "missing"}
    if not rules:
        return f"# {v}: add at least one IF…THEN rule\n{df}[{v!r}] = {_result_series(els, df, v)}"
    lines = [f"{df}[{v!r}] = {_result_series(els, df, v)}"]
    for r in reversed(rules):
        lines.append(f"{df}[{v!r}] = {df}[{v!r}].mask("
                     f"({_cond_expr(r, df, v)}).fillna(False), {_result_series(r.get('then'), df, v)})")
    return "\n".join(lines)


# ── Multi-step pipeline (recipe="pipeline") ──────────────────────────────────
def _pipeline_code(b, df):
    v = str(b.get("variable", "")).strip().upper()
    steps = (b.get("args", {}) or {}).get("steps") or []
    if not steps:
        return f"# {v}: add at least one step"
    out = []
    for idx, st in enumerate(steps):
        op = str(st.get("op", "fn")).lower()
        if op == "assign":
            sb = {"variable": v, "mtype": "assign",
                  "dataset": st.get("dataset", ""), "column": st.get("column", ""), "args": {}}
        elif op == "constant":
            sb = {"variable": v, "mtype": "constant", "value": st.get("value", ""), "args": {}}
        else:
            sb = {"variable": v, "mtype": "derived", "recipe": op, "args": st.get("args", {}) or {}}
        out.append(_code_from_struct(sb, df))
        out.append(f"{df}['__pipe_step{idx + 1}'] = {df}[{v!r}]")
    if steps:
        _regs = ", ".join(repr(f"__pipe_step{n + 1}") for n in range(len(steps)))
        out.append(f"{df}.drop(columns=[c for c in [{_regs}] if c in {df}.columns], "
                   f"inplace=True, errors='ignore')")
    return "\n".join(out)


# ── ISO date(time), partial-aware (SDTM Designer main.py:5508) ───────────────
def _iso_parts(y=None, m=None, d=None, t=None):
    def _norm(s, width):
        if s is None:
            return None
        s = s.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)
        s = s.where(~(s.isna() | s.str.upper().isin(_ISO_BAD_TOKENS)), "")
        return s.mask(s != "", s.str.zfill(width))
    Y, M, D = _norm(y, 4), _norm(m, 2), _norm(d, 2)
    idx = next((p.index for p in (Y, M, D) if p is not None), None)
    if Y is None or idx is None:
        return pd.Series([""] * (len(idx) if idx is not None else 0), index=idx)
    res = Y.copy()
    if M is not None:
        res = res.mask((Y != "") & (M != ""), Y + "-" + M)
        if D is not None:
            res = res.mask((Y != "") & (M != "") & (D != ""), Y + "-" + M + "-" + D)
    if t is not None:
        T = t.astype("string").str.strip()
        T = T.where(~(T.isna() | T.str.upper().isin(_ISO_BAD_TOKENS)), "")
        res = res.mask((res.str.len() >= 10) & (T != ""), res + "T" + T)
    return res.fillna("")


# ── study-day args from Input Variables (SDTM Designer main.py:4364) ─────────
def _study_day_args(var, iv):
    v = str(var).strip().upper()
    dtc, ref = "", ""
    for e in str(iv or "").replace("\n", " ").replace("\r", " ").split(","):
        e = e.strip()
        if not e.lower().startswith("sdtm."):
            continue
        parts = e.split(".")
        if len(parts) < 3:
            continue
        dom, col = parts[1].strip().lower(), parts[2].strip().upper()
        if col in _REF_DATE_VARS or dom == "dm":
            ref = ref or col
        elif col.endswith("DTC"):
            dtc = dtc or col
    if not dtc and v.endswith("DY") and len(v) > 2:
        dtc = v[:-2] + "DTC"
    return {"dtc_var": dtc, "ref_var": ref or "RFSTDTC"}


# ── SDTM cross-domain reference repair (SDTM Designer main.py:4710) ──────────
_SDTM_REF_RE = re.compile(r"sdtm\.([a-z0-9_]+)\.([a-z0-9_]+)")
_RAW_COL_RE = re.compile(r"(raw_[A-Za-z0-9_]+\[)(['\"])([^'\"\]]+)\2\]")


def _upper_raw_cols(code):
    if not code:
        return code
    code = re.sub(r"\braw_raw\.([A-Za-z][A-Za-z0-9_]*)", r"raw_\1", code)
    return _RAW_COL_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3).upper()}{m.group(2)}]", code)


def _repair_sdtm_refs(code, domain, df, input_variables):
    if not code:
        return code
    this = domain.upper()
    for dom, var in _SDTM_REF_RE.findall(str(input_variables or "").lower()):
        DOM, VAR = dom.upper(), var.upper()
        repl = (f"{df}['{VAR}']" if DOM == this
                else f"{df}['USUBJID'].map(_sref('{DOM}','{VAR}'))")
        code = re.sub(r"raw_[A-Za-z0-9_]+\[(['\"])" + VAR + r"\1\]", repl, code, flags=re.IGNORECASE)
        code = re.sub(r"\braw_[A-Za-z0-9_]+\." + VAR + r"\b", repl, code, flags=re.IGNORECASE)
    return code


# ── The recipe compiler (SDTM Designer main.py:4798) ─────────────────────────
def _code_from_struct(b, domain="AE"):
    """Generate the Python snippet from a structured block (single source of truth)."""
    v = str(b.get("variable", "")).strip().upper()
    t = b.get("mtype", "unmapped")
    df = domain.lower()
    if t == "drop":
        return f"# {v}: excluded (Drop)"
    if t == "constant":
        return f"{df}['{v}'] = {b.get('value', '')!r}"
    if t == "sequence":
        grp = str((b.get("args", {}) or {}).get("group", "USUBJID")).upper() or "USUBJID"
        return (f"{df}['{v}'] = ({df}.groupby('{grp}').cumcount() + 1\n"
                f"               if '{grp}' in {df} else range(1, len({df}) + 1))")
    if t == "assign":
        ds, col = b.get("dataset", ""), b.get("column", "")
        if not (ds and col):
            return f"# {v}: choose a source dataset and column"
        clname = str(b.get("codelist") or "").strip().upper()
        cmap = _codelists().get(clname) if clname else None
        _ovr = {str(k).strip().upper(): vv
                for k, vv in ((b.get("args") or {}).get("ct_overrides") or {}).items() if str(k).strip()}
        if _ovr:
            cmap = {**(cmap or {}), **_ovr}
        # Multi-source value: coalesce (first non-blank) across the value columns, resolving each onto
        # the base by subject key when it comes from another dataset (COUNTRY ← ixrsrand.country;
        # inv.country, which shares no subject key, yields NA and is skipped).
        vsrc = [(str(x.get("dataset", "")).lower(), str(x.get("column", "")).upper())
                for x in ((b.get("args") or {}).get("value_sources") or [])
                if x.get("dataset") and x.get("column")]
        if len(vsrc) > 1:
            return _assign_coalesce_code(df, v, vsrc, cmap)
        if ds != df:
            src = (f"if raw_{ds} is raw_{df} or raw_{ds}.index.equals(raw_{df}.index):\n"
                   f"    _val = raw_{ds}['{col}']   # {ds} is this domain's base — row-aligned copy\n"
                   f"elif '{ds}' in globals().get('_PREP_SRC', ()) and '{col}' in raw_{df}.columns:\n"
                   f"    _val = raw_{df}['{col}']   # {ds} built the prepared base, which carries {col} on every record\n"
                   f"else:\n"
                   f"    _SK = ['USUBJID', 'X_SUBJID', 'SUBJID', 'SUBJECTID']\n"
                   f"    _k = next((k for k in _SK if k in raw_{ds}.columns and k in raw_{df}.columns), None)\n"
                   f"    if _k is not None:\n"
                   f"        _src = raw_{ds}.dropna(subset=[_k]).drop_duplicates(_k)\n"
                   f"        _lk = pd.Series(_src['{col}'].values, index=_src[_k].values)   # works even when {col}==key\n"
                   f"        _val = raw_{df}[_k].map(_lk)\n"
                   f"    else:\n"
                   f"        _val = raw_{ds}['{col}']\n")
        else:
            src = f"_val = raw_{ds}['{col}']\n"
        if cmap and len(cmap) <= 600:
            return (src + f"_ct = {cmap!r}\n"
                    f"{df}['{v}'] = _val.astype('string').str.strip().str.upper().map(_ct).fillna(_val)"
                    f"   # normalized to {clname or 'CT'} controlled terminology")
        return (src +
                f"{df}['{v}'] = (_val.astype('string').str.strip()\n"
                f"              if (pd.api.types.is_string_dtype(_val) or pd.api.types.is_object_dtype(_val))\n"
                f"              else _val)   # direct assign (strip if character, numeric as-is)")
    if t == "derived":
        rc = b.get("recipe", "")
        a = b.get("args", {}) or {}
        if rc == "iso_date":
            ds = a.get("dataset", df)
            dcol, tcol = a.get("date_col", ""), a.get("time_col", "")
            ycol, mcol, ddcol = a.get("y_col", ""), a.get("m_col", ""), a.get("d_col", "")
            if not (dcol or ycol):
                return f"# {v}: pick the raw date column (ISO date recipe)"
            if ycol:
                parts = [f"y=raw_{ds}['{ycol}']"]
                if mcol:
                    parts.append(f"m=raw_{ds}['{mcol}']")
                if ddcol:
                    parts.append(f"d=raw_{ds}['{ddcol}']")
                if tcol:
                    parts.append(f"t=raw_{ds}['{tcol}']")
                return f"{df}['{v}'] = _iso_parts({', '.join(parts)})"
            code = f"{df}['{v}'] = pd.to_datetime(raw_{ds}['{dcol}'], errors='coerce').dt.strftime('%Y-%m-%d')"
            if tcol:
                code += (f"\n_t = raw_{ds}['{tcol}'].astype('string').str.strip()"
                         f"\n_t = _t.where(~(_t.isna() | _t.str.upper().isin(['','NAN','NONE','NAT','UN','UNK'])), '')"
                         f"\n{df}['{v}'] = {df}['{v}'].where({df}['{v}'].isna() | (_t == ''), {df}['{v}'] + 'T' + _t)")
            return code
        if rc == "study_day":
            _sd = _study_day_args(v, b.get("input_variables", ""))
            dtc = str(a.get("dtc_var") or _sd["dtc_var"] or "").strip().upper().split(".")[-1]
            if not dtc.endswith("DTC"):
                dtc = (_sd["dtc_var"] or (v[:-2] + "DTC" if v.endswith("DY") and len(v) > 2 else dtc)).upper()
            ref_var = (str(a.get("ref_var") or _sd["ref_var"]).strip().upper().split(".")[-1]
                       or "RFSTDTC")
            ref_dt = str(a.get("ref_date", "") or "").strip()
            if not dtc:
                return f"# {v}: pick the event --DTC variable (study day recipe)"
            fallback = f"pd.to_datetime('{ref_dt}')" if ref_dt else "pd.NaT"
            return (
                f"_ev = pd.to_datetime({df}['{dtc}'].astype('string').str[:10],\n"
                f"                       format='%Y-%m-%d', errors='coerce')   "
                f"# strict: a partial --DTC has no day, so no study day\n"
                f"_rs = _DMREF.get('{ref_var}')                       # DM reference date per USUBJID\n"
                f"_rfraw = ({df}['USUBJID'].map(_rs) if (_rs is not None and 'USUBJID' in {df})\n"
                f"          else ({df}['{ref_var}'] if '{ref_var}' in {df} "
                f"else pd.Series({fallback}, index={df}.index)))\n"
                f"_rf = pd.to_datetime(_rfraw.astype('string').str[:10],\n"
                f"                       format='%Y-%m-%d', errors='coerce')\n"
                f"_d  = (_ev - _rf).dt.days\n"
                f"{df}['{v}'] = _d.where(_d < 0, _d + 1).astype('Int64')   "
                f"# +1 on/after RFSTDTC (from DM), no day 0; empty if either date missing")
        if rc == "lobxfl":
            tc = str(a.get("testcd_var") or "").upper()
            dtc = str(a.get("dtc_var") or "").upper()
            res = str(a.get("result_var") or "").upper()
            ref = str(a.get("ref_var") or "RFXSTDTC").upper()
            raw_gv = a.get("group_vars") or ["USUBJID", tc]
            gv = [str(g).upper() for g in raw_gv if g]
            if not gv:
                gv = ["USUBJID", tc]
            if not (tc and dtc):
                return f"# {v}: needs --TESTCD and --DTC (LOBXFL recipe)"
            gv_repr = repr(gv)
            return (
                f"{df}['{v}'] = ''\n"
                f"_gv_{v} = [c for c in {gv_repr} if c in {df}.columns]\n"
                f"if all(c in {df} for c in ({tc!r}, {dtc!r})) and 'USUBJID' in {df} and _gv_{v}:\n"
                f"    _ref = _DMREF.get('{ref}')                       # first-exposure date per USUBJID\n"
                f"    _rfx = ({df}['USUBJID'].map(_ref) if _ref is not None else pd.Series(pd.NaT, index={df}.index))\n"
                f"    _rfx = pd.to_datetime(_rfx.astype('string').str[:10],\n"
                f"                           format='%Y-%m-%d', errors='coerce')\n"
                f"    _obs = pd.to_datetime({df}['{dtc}'].astype('string').str[:10],\n"
                f"                           format='%Y-%m-%d', errors='coerce')\n"
                f"    _res = ({df}['{res}'].astype('string').str.strip() if {res!r} in {df} "
                f"else pd.Series('x', index={df}.index))\n"
                f"    _elig = _obs.notna() & _rfx.notna() & (_obs <= _rfx) & _res.fillna('').ne('')\n"
                f"    _lb = {df}[_gv_{v}].copy(); _lb['__obs'] = _obs; _lb['__elig'] = _elig\n"
                f"    _e = _lb[_lb['__elig']]\n"
                f"    if len(_e):\n"
                f"        {df}.loc[_e.sort_values('__obs').groupby(_gv_{v}).tail(1).index, '{v}'] = 'Y'   "
                f"# last non-missing obs on/before DM.{ref}")
        if rc == "concat":
            ds, cols, sep = a.get("dataset", df), a.get("columns", []), a.get("sep", " ")
            cols = [c for c in cols if c]
            if not cols:
                return f"# {v}: pick columns to concatenate"
            expr = f" + {sep!r} + ".join(f"raw_{ds}['{c}'].astype(str)" for c in cols)
            return f"{df}['{v}'] = {expr}"
        if rc == "date_extreme":
            fn = a.get("func", "min")
            srcs = [s for s in a.get("sources", []) if s.get("dataset") and s.get("date_col")]
            if not srcs:
                return f"# {v}: add at least one dataset + date column"
            picks = ", ".join(f"_pick(raw_{s['dataset']}, '{s['date_col']}')" for s in srcs)
            return (
                "_SK = ['SUBJID','X_SUBJID','SUBJECTID','USUBJID']\n"
                "def _pick(_df,_dc):\n"
                "    _k = next((k for k in _SK if k in _df.columns), None)\n"
                "    return _df[[_k,_dc]].rename(columns={_k:'__s',_dc:'__d'}) if _k and _dc in _df.columns else None\n"
                f"_fr = [x for x in [{picks}] if x is not None]\n"
                "_t = pd.concat(_fr, ignore_index=True)\n"
                "_t['__d'] = pd.to_datetime(_t['__d'], errors='coerce')\n"
                f"_agg = _t.groupby('__s')['__d'].{fn}()\n"
                f"_tk = next((k for k in _SK if k in raw_{df}.columns), None)\n"
                f"{df}['{v}'] = raw_{df}[_tk].map(_agg).dt.strftime('%Y-%m-%d')")
        if rc == "constant":
            return f"{df}['{v}'] = {a.get('value', '')!r}"
        if rc == "code":
            code = (a.get("code") or "").strip()
            code = _repair_sdtm_refs(code, domain, df, b.get("input_variables", ""))
            return code if code else f"# {v}: complex rule — not generated yet\n{df}['{v}'] = ''"
        if rc == "fn":
            return _fn_code(b, df)
        if rc == "keyed_lookup":
            return _keyed_lookup_code(b, df)
        if rc == "seq_pivot":
            return _seq_pivot_code(b, df)
        if rc == "sas_expr":
            return _sas_expr_code(b, df)
        if rc in CARRY_RECIPES:
            return _carry_code(b, df)
        if rc == "sas_if":
            return _sas_if_code(b, df)
        if rc == "cond":
            return _cond_code(b, df)
        if rc == "pipeline":
            return _pipeline_code(b, df)
        return (f"# {v}: custom derivation — needs a SAS/stat programmer\n"
                f"{df}['{v}'] = ''   # TODO")
    return f"# {v}: not mapped"


# ── structured config from one spec row (SDTM Designer main.py:4386) ─────────
def _struct_from_spec(var, domain, action, iv, studyid="STUDYID", sas=""):
    v = str(var).strip().upper()
    act = str(action or "").strip().upper()
    iv = str(iv or "").strip()
    s = {"mtype": "unmapped", "dataset": "", "column": "", "value": "",
         "recipe": "", "args": {}}

    if v == "DOMAIN":
        return {**s, "mtype": "constant", "value": domain.upper()}
    if v == "STUDYID":
        return {**s, "mtype": "constant", "value": studyid}
    if act == "DROP":
        return {**s, "mtype": "drop"}
    if v.endswith("SEQ"):
        return {**s, "mtype": "sequence"}
    if v.endswith("DY") and len(v) > 2:
        return {**s, "mtype": "derived", "recipe": "study_day", "args": _study_day_args(v, iv)}
    if v == "USUBJID":
        for tok in iv.replace("\n", ",").split(","):
            p = tok.strip().replace(" ", "").split(".")
            if len(p) >= 3 and p[0].lower() == "raw":
                return {**s, "mtype": "assign", "dataset": p[1].lower(), "column": ".".join(p[2:]).upper()}
        return {**s, "mtype": "derived"}
    if act == "ASSIGN" and (not iv or iv.lower() == "nan"):
        return {**s, "mtype": "drop"}
    # Mapping Action == CODE is a COMPLEX derivation (conditional / multi-step SAS), never a
    # plain column copy. Leave it as a generic derived block — the auto-derivation pass then
    # parses the common patterns (presence/value flags) into a `cond` recipe; anything it can't
    # decide stays derived for the user / AI (rather than a WRONG assign, e.g. DTHFL=DEATHDAT).
    if act == "CODE":
        return {**s, "mtype": "derived"}

    def _all_raw(_iv):
        out = []
        for tok in _iv.replace("\n", ",").split(","):
            p = tok.strip().replace(" ", "").split(".")
            if len(p) >= 3 and p[0].lower() == "raw":
                out.append((p[1].lower(), ".".join(p[2:]).upper()))
        return out

    if iv and iv.lower() != "nan":
        raws = _all_raw(iv)
        # Pick the VALUE source, not a merge key (mirrors build_engine.compile_recipe). Input Variables
        # often lists a join key first and the value column(s) after (COUNTRY: raw.dm.x_subjid, …,
        # raw.inv.country, raw.ixrsrand.country). Priority, grounded in the Implemented SAS Code:
        #   1. SAS-NAMED columns (the authority — CMCAT's strip(upcase(datapagename)) → datapagename,
        #      NOT the mislabeled *.cmcat columns; COUNTRY's strip(_country) → country → *.country).
        #   2. NAME-MATCH (column == the variable).
        #   3. LEGACY (first token / same-dataset coalesce).
        cand = []
        if sas:
            try:
                from .build_engine import _sas_src_cols
                cand = _sas_src_cols(sas, v)
            except Exception:  # noqa: BLE001
                cand = []
        sas_toks = [t for c in cand for t in raws if t[1].lower() == c]
        name_match = [t for t in raws if t[1].upper() == v]
        value = sas_toks or name_match
        if value:
            primary = value[0]
            # value co-sources + same-dataset siblings that are variants of THIS var (LBFAST_NSV,
            # CMDOSE_RAW) — never a different, shorter variable that only shares the dataset.
            sib = [t for t in raws if t not in value and t[0] == primary[0]
                   and t[1].upper().startswith(v)]
            srcs = value + sib
            if len(srcs) > 1:
                return {**s, "mtype": "assign", "dataset": primary[0], "column": primary[1],
                        "args": {"value_sources": [{"dataset": d, "column": c} for d, c in srcs]}}
            return {**s, "mtype": "assign", "dataset": primary[0], "column": primary[1]}
        # The SAS reads an xx-merge `_temp` (strip(_invnam)) that no Input Variable is named for — its
        # value comes from the merge/pivot/lookup in the Mapping Rule, so neither legacy fallback below
        # applies (they made DM.INVNAM a copy of raw.dm.x_subjid). Leave it an unresolved derivation;
        # compose_blocks reproduces the derivations it recognises and flags the rest for review.
        temps = merge_temps(sas) if raws else []
        if temps:
            return {**s, "mtype": "derived", "merge_temps": temps, "needs_review": True,
                    "review_reason": (f"Implemented SAS reads the xx-merge temporary {', '.join(temps)}, "
                                      "whose derivation (Mapping Rule) was not reproduced — needs a "
                                      "Domain Build / AI derivation, not a copy of an Input Variable")}
        # Legacy: more than one Input Variable from ONE dataset → coalesce; else first token.
        if len(raws) > 1 and len({d for d, _c in raws}) == 1:
            return {**s, "mtype": "derived", "recipe": "fn",
                    "args": {"fn": "coalesce",
                             "sources": [{"dataset": d, "column": c} for d, c in raws]}}
        if raws:
            return {**s, "mtype": "assign", "dataset": raws[0][0], "column": raws[0][1]}
        if iv.split(",")[0].strip().lower().startswith("sdtm."):
            return {**s, "mtype": "derived"}
    return s


# ── dataset-level finalize (SDTM Designer main.py:6247) ──────────────────────
def _finalize_df(d, settings):
    settings = settings or {}
    sort = [s for s in (settings.get("sort") or []) if s.get("var") in d.columns]
    if sort:
        d = d.sort_values(by=[s["var"] for s in sort],
                          ascending=[str(s.get("dir", "asc")).lower() != "desc" for s in sort],
                          kind="mergesort", na_position="last")
    dd = settings.get("dedup") or {}
    keys = [k for k in (dd.get("keys") or []) if k in d.columns]
    if dd.get("enabled") and keys:
        d = d.drop_duplicates(subset=keys, keep=(dd.get("keep") or "first"))
    return d


def _dm_ref_frame(study):
    try:
        dm = _read_built(study, "DM")
        if dm is not None and "USUBJID" in dm.columns:
            return dm.drop_duplicates("USUBJID").set_index("USUBJID")
    except Exception:
        pass
    return None


def _sdtm_domain_frame(study, dom):
    try:
        d = _read_built(study, dom)
        if d is not None and "USUBJID" in d.columns:
            return d.drop_duplicates("USUBJID").set_index("USUBJID")
    except Exception:
        pass
    return None


# ── SUPP-- transposition (SDTM Designer main.py:6298) ────────────────────────
def _supp_from_built(domain, df, blocks):
    qnam = [b for b in blocks if b.get("supp") and b.get("variable") in df.columns]
    if not qnam:
        return None
    dom = domain.upper()
    cols = {str(c).upper(): c for c in df.columns}
    usub = cols.get("USUBJID")
    if not usub:
        return pd.DataFrame(columns=_SUPP_COLS)
    seqcol = cols.get(f"{dom}SEQ")
    studyc = cols.get("STUDYID")
    idvar = f"{dom}SEQ" if seqcol else ""

    def _blank(val):
        s = "" if val is None else str(val).strip()
        return s in ("", "nan", "none", "None", "<NA>", "NaN", "NaT")

    out = []
    for _, row in df.iterrows():
        u = row[usub]
        if _blank(u):
            continue
        sid = "" if not studyc or _blank(row[studyc]) else str(row[studyc])
        idval = "" if not seqcol or _blank(row[seqcol]) else str(row[seqcol]).split(".")[0]
        for b in qnam:
            val = row[b["variable"]]
            if _blank(val):
                continue
            out.append({"STUDYID": sid, "RDOMAIN": dom, "USUBJID": str(u),
                        "IDVAR": idvar, "IDVARVAL": idval,
                        "QNAM": b["variable"], "QLABEL": b.get("qlabel") or b.get("label") or "",
                        "QVAL": val, "QORIG": b.get("qorig") or b.get("origin") or "CRF",
                        "QEVAL": b.get("qeval") or ""})
    sd = pd.DataFrame(out, columns=_SUPP_COLS)
    if len(sd):
        sd = sd.sort_values(["USUBJID", "QNAM", "IDVARVAL"], kind="stable").reset_index(drop=True)
    return sd


# ── The exec() runtime (SDTM Designer main.py:6341) ──────────────────────────
def _prep_sources(prep_steps) -> set:
    """The raw forms a Prep pipeline reads (names without `raw.`), not its own intermediate outputs."""
    from .prep_orchestrator import _step_inputs
    made = {str(st.get("name") or "").strip().lower() for st in prep_steps or []}
    return {n for st in prep_steps or [] for n in _step_inputs(st)
            if n not in made and not n.startswith("sdtm.")}


def _run_domain(study, domain, blocks, preloaded=None, settings=None, row_limit=100, prep_steps=None):
    """Execute structured blocks for a domain → built dataframe + per-block results."""
    import re as _re
    preloaded = preloaded or {}
    dfn = domain.lower()
    qmeta = _qnam_meta(study, domain)
    if qmeta:
        for b in blocks:
            if not b.get("supp") and str(b.get("variable", "")).upper() in qmeta:
                m = qmeta[str(b["variable"]).upper()]
                b["supp"] = True
                b.setdefault("qlabel", m["qlabel"])
                b.setdefault("qorig", m["qorig"])
    for b in blocks:
        b["code"] = _upper_raw_cols(_code_from_struct(b, domain))
    full = "\n".join(b.get("code", "") for b in blocks)
    needed = set(_re.findall(r"\braw_([A-Za-z0-9_]+)\b", full))
    try:
        _collected = {d for (d, _c) in (_study_collected(study) or set())}
    except Exception:
        _collected = set()
    _prep_names = {_re.sub(r"[^0-9a-zA-Z_]", "_", str(k)).lower() for k in (preloaded or {})}
    _phantoms = []
    ns, load_err = {"pd": pd, "np": np, "pandas": pd, "numpy": np}, {}
    for ds in needed:
        if ds in preloaded:
            ns[f"raw_{ds}"] = preloaded[ds]
            continue
        try:
            ns[f"raw_{ds}"] = _read_raw(study, ds)
        except Exception as e:
            if ds not in _collected and ds not in _prep_names:
                ns[f"raw_{ds}"] = pd.DataFrame()
                _phantoms.append(ds)
            else:
                load_err[ds] = str(e)
    _RESERVED = {"pd", "np", "pandas", "numpy", dfn, "range", "len", "str", "dt",
                 "loc", "iloc", "groupby", "merge", "where", "apply", "astype",
                 "fillna", "map", "isin", "values", "index", "columns", "print"}
    for ds, frame in (preloaded or {}).items():
        key = _re.sub(r"[^0-9a-zA-Z_]", "_", str(ds))
        ns.setdefault(f"raw_{key}", frame)
        if key not in _RESERVED:
            ns.setdefault(key, frame)
    for ds in set(_re.findall(r"\b([a-z][a-z0-9_]{2,})\s*[\[\.]", full)):
        if ds in _RESERVED or ds in ns or ds.startswith(("raw_", "sdtm_")):
            continue
        if f"raw_{ds}" in ns:
            ns[ds] = ns[f"raw_{ds}"]
        else:
            try:
                frame = _read_raw(study, ds)
                ns[ds] = frame
                ns.setdefault(f"raw_{ds}", frame)
            except Exception:
                pass
    for _sdom in set(_re.findall(r"\bsdtm_([a-z0-9]+)\b", full)):
        if f"sdtm_{_sdom}" in ns:
            continue
        try:
            _bdf = _read_built(study, _sdom.upper())
            if _bdf is not None:
                ns[f"sdtm_{_sdom}"] = _bdf
        except Exception as e:
            load_err[f"sdtm_{_sdom}"] = str(e)

    base = None
    from collections import Counter as _Cnt
    _prep_keys = {_re.sub(r"[^0-9a-zA-Z_]", "_", str(k)).lower() for k in preloaded}
    if _prep_keys:
        cnt0 = _Cnt(str(b.get("dataset") or "").lower()
                    for b in blocks if b.get("mtype") == "assign" and b.get("dataset"))
        for cand, _n in cnt0.most_common():
            if cand in _prep_keys and ns.get(f"raw_{cand}") is not None:
                base = ns[f"raw_{cand}"]
                ns[f"raw_{dfn}"] = base
                load_err.pop(dfn, None)
                break
    if base is None:
        base = ns.get(f"raw_{dfn}")
        # a domain with no raw dataset of its own (e.g. LB assembled from a prep base, or DS
        # from consents) leaves raw_<dom> as an empty PHANTOM frame — never use that as the
        # base, or the record grain collapses to 0 rows. Fall through to the assign source.
        if base is not None and len(base) == 0:
            base = None
    if base is None:
        cnt = _Cnt(str(b.get("dataset") or "").lower()
                   for b in blocks if b.get("mtype") == "assign" and b.get("dataset"))
        for cand, _n in cnt.most_common():
            if ns.get(f"raw_{cand}") is not None and len(ns[f"raw_{cand}"]):
                base = ns[f"raw_{cand}"]
                break
        if base is None:
            base = next((ns[k] for k in ns if k.startswith("raw_")), None)
        if base is not None:
            ns[f"raw_{dfn}"] = base
            load_err.pop(dfn, None)
    # Non-destructive Prep integration (mirrors build_engine._assembled_base): if Prep assembled a
    # <dom>_base whose UPPERCASE columns are SDTM variable names, build FROM it — take each variable
    # the base supplies verbatim and skip its block, so Domain Build matches Auto-Build and the spec
    # is never rewritten.
    # an assembled base with no records (the prep ran, nothing matched) makes an empty domain — not a
    # fallback to a raw form, which would invent records with no test or result (TEST-001 DA)
    _abase = next((preloaded[n] for n in (f"{dfn}_base", f"{dfn}base")
                   if n in (preloaded or {}) and preloaded[n] is not None), None)
    _base_cols = set()
    if _abase is not None:
        base = _abase
        # raw_<dom> is "this domain's base" to every block (a copy is row-aligned with it, a lookup maps
        # onto its rows); it must be the assembled base, not the raw form chosen before it
        ns[f"raw_{dfn}"] = _abase
        # a copy from a form the base was built from reads the base's own column, record by record —
        # not a per-subject lookup (TEST-001 VS: VISIT ← vsperf.FOLDERNAME was 'Screening' on all 421)
        if prep_steps is None:
            prep_steps = (_load_saved(study, domain) or {}).get("prep_steps") or []
        ns["_PREP_SRC"] = _prep_sources(prep_steps)
        _bmap = {str(c).upper(): c for c in _abase.columns}
        ns[dfn] = pd.DataFrame(index=_abase.index)
        for _b in blocks:
            _v = str(_b.get("variable", "")).upper()
            if _v in _bmap and _v not in ("STUDYID", "DOMAIN") and not _v.endswith("SEQ"):
                ns[dfn][_v] = _abase[_bmap[_v]].values
                _base_cols.add(_v)
    else:
        ns[dfn] = pd.DataFrame(index=(base.index if base is not None else range(20)))
    # the frame whose rows the output follows (record-grain derivations such as the xx-merge
    # pivot/lookup join onto it by key instead of assuming a raw dataset is row-aligned)
    ns["_ROWS"] = base if base is not None else ns.get(f"raw_{dfn}")

    ns["_DMREF"] = {}
    need_refs = {str((b.get("args", {}) or {}).get("ref_var") or "RFSTDTC").upper()
                 for b in blocks if b.get("recipe") == "study_day"}
    need_refs |= {str((b.get("args", {}) or {}).get("ref_var") or "RFXSTDTC").upper()
                  for b in blocks if b.get("recipe") == "lobxfl"}
    if need_refs and domain.upper() != "DM":
        dmf = _dm_ref_frame(study)
        if dmf is not None:
            for rv in need_refs:
                if rv in dmf.columns:
                    ns["_DMREF"][rv] = dmf[rv]

    ref_doms = set()
    for b in blocks:
        for m in _SDTM_REF_RE.findall(str(b.get("input_variables") or "").lower()):
            ref_doms.add(m[0].upper())
    ref_doms.discard(domain.upper())
    _SDTM = {}
    for rdom in ref_doms:
        f = _sdtm_domain_frame(study, rdom)
        if f is not None:
            _SDTM[rdom] = f
    ns["_SDTM"] = _SDTM
    ns["_DM"] = _SDTM.get("DM")

    def _sref(dom, var, _S=_SDTM):
        f = _S.get(str(dom).upper())
        if f is not None and var in f.columns:
            return f[var]
        return pd.Series(dtype="object")
    ns["_sref"] = _sref
    ns["_iso_parts"] = _iso_parts
    ns.update({f.__name__: f for f in sas_expr.HELPERS})

    res = {}

    def _do(b):
        var = b.get("variable")
        code = b.get("code", "")
        try:
            exec(code, ns)  # noqa: S102 (dev tool — faithful SDTM Designer engine)
            res[var] = {"variable": var, "ok": True, "code": code}
            return
        except Exception as e:
            err = e
        if "'numpy.ndarray' object has no attribute 'str'" in str(err):
            fixed = _re.sub(r"\.str\.(strip|upper|lower|title)\(\)", "", code)
            if fixed != code:
                try:
                    exec(fixed, ns)  # noqa: S102
                    b["code"] = fixed
                    res[var] = {"variable": var, "ok": True, "code": fixed}
                    return
                except Exception as e2:
                    err = e2
        msg = str(err)
        if isinstance(err, NameError):
            m = _re.search(r"name '([^']+)'", msg)
            if m:
                nm = m.group(1).replace("raw_", "")
                msg = (f"source dataset '{nm}' is not available in this build — "
                       f"check that a prep step named '{nm}' exists and produces data")
        elif isinstance(err, KeyError):
            msg = (f"column {msg} was not found in the source data — it may have "
                   "been dropped by a prep step or is spelled differently")
        elif isinstance(err, AttributeError) and "numpy.ndarray" in msg:
            msg = ("the generated logic mixes NumPy and pandas text functions — "
                   "click ✎ and re-generate, or remove the .str… call on the "
                   "np.where(...) result")
        elif "Length of values" in msg or "Length mismatch" in msg:
            msg = ("the source has a different number of rows than this domain's "
                   "records — map this variable from the domain's prepared base "
                   "dataset (see Prepare data) instead of a raw form")
        elif isinstance(err, (SyntaxError, IndentationError)):
            msg = "the generated code is malformed — click ✎ and re-generate this logic"
        res[var] = {"variable": var, "ok": False, "error": msg, "preview": [], "code": code}

    seq_blocks = [b for b in blocks if b.get("mtype") == "sequence"]
    # --DY (study_day) and LOBXFL read a BUILT --DTC / --TESTCD, which the spec may order LATER
    # than them — so run these after all other column derivations (a --DY always depends on its
    # --DTC). This diverges from SDTM Designer's exact order but is a strict correctness win.
    _LATE = ("lobxfl", "study_day", *CARRY_RECIPES)
    # a block flagged `_late` (e.g. a deterministic-Apply conditional flag that reads another SDTM
    # variable in THIS domain, like DTHFL from DTHDTC) must run after the columns it depends on.
    def _is_late(b):
        return b.get("recipe") in _LATE or bool(b.get("_late"))
    # RETAIN / LAG read the finished values of the variables they carry, so they go last of all
    late_blocks = sorted((b for b in blocks if _is_late(b)), key=lambda b: b.get("recipe") in CARRY_RECIPES)
    def _do_or_base(b):
        v = str(b.get("variable", "")).upper()
        if v in _base_cols:   # value already taken from the assembled prep base — don't overwrite
            res[b.get("variable")] = {"variable": b.get("variable"), "ok": True, "op": "prep-base", "code": ""}
            return
        _do(b)
    for b in blocks:
        if b.get("mtype") != "sequence" and not _is_late(b):
            _do_or_base(b)
    for b in late_blocks:
        _do_or_base(b)
    # A vertical→wide pivot of this domain's raw dataset: the other sequence rows now live in the
    # wide columns, so keep one row per record. Applies to a Prep-assembled base too when it carries
    # the record key and sequence column through (the collapse is a no-op otherwise).
    _pv_args = _seq_collapse_args(blocks, dfn)
    if _pv_args and ns.get("_ROWS") is not None:
        try:
            exec(_seq_collapse_code(dfn, _pv_args), ns)  # noqa: S102
        except Exception:  # noqa: BLE001 — a failed collapse keeps every row (nothing is lost)
            pass
    # Construction Standards drive the build here too (parity with Auto-Build): fill any variable the
    # study spec left empty from the Gilead IG-CORE standard (e.g. SITEID/INVID = scan(USUBJID,-2,'-')).
    try:
        from .build_engine import _apply_construction_standards, _apply_ct_standard
        for _sv in _apply_construction_standards(study, domain, ns[dfn]):
            res[_sv] = {"variable": _sv, "ok": True, "op": "standard", "code": ""}
        _apply_ct_standard(ns[dfn], _spec_rows(study, domain))   # coded vars → CT submission value
    except Exception:  # noqa: BLE001
        pass
    if _abase is not None and len(_abase) == 0:
        ns[dfn] = ns[dfn].iloc[0:0]   # the assembled base has no records — nor does the domain
    ns[dfn] = _finalize_df(ns[dfn], settings)
    for b in seq_blocks:
        _do(b)

    d_full = ns[dfn]
    supp_df = _supp_from_built(domain, d_full, blocks)
    for var, r in res.items():
        if r.get("ok"):
            r["preview"] = ([clean(x) for x in d_full[var].head(8).tolist()]
                            if var in d_full.columns else [])
    order = [b["variable"] for b in blocks
             if b.get("variable") in d_full.columns and not b.get("supp")]
    d = d_full[order].reset_index(drop=True) if order else d_full.reset_index(drop=True)
    ns[dfn] = d
    results = [res[b["variable"]] for b in blocks if b.get("variable") in res]
    row_limit = max(1, min(int(row_limit or 100), 200000))
    rows = [{k: clean(v) for k, v in r.items()} for r in d.head(row_limit).to_dict(orient="records")]
    out = {"columns": list(d.columns), "nrows": int(len(d)), "rows": rows,
           "blocks": results, "load_errors": load_err, "df": d}
    if supp_df is not None:
        srows = [{k: clean(v) for k, v in r.items()} for r in supp_df.head(row_limit).to_dict(orient="records")]
        out["supp"] = {"domain": f"SUPP{domain.upper()}", "columns": list(supp_df.columns),
                       "nrows": int(len(supp_df)), "rows": srows}
        out["supp_df"] = supp_df
    return out


# ── Block composition (spec rows → editable blocks) ──────────────────────────
def _reproduce_merge_temps(study, dom, blocks, rows, locked=()):
    """The consolidation half of the legacy xx-merge.sas + xx.sas pair. For every block whose
    Implemented SAS reads a `_temp` no Input Variable is named for (flagged by _struct_from_spec),
    rebuild the temp from the derivation its Mapping Rule states, then apply the Implemented SAS:
      • vertical→wide pivot  `… (where atcseq = 2)`            → seq_pivot     (CM.ATC1PT2)
      • lookup merge          `Merge raw.inv … by … INVID …`     → keyed_lookup  (DM.INVNAM)
      • sole input            one raw Input Variable, no logic   → assign        (CM.CMDOSFRQ)
    Anything else keeps needs_review, loses the spec's SPEC/100 stamp (so it is neither shown as
    'no change needed' nor bulk-approved) and is left for Domain Build / the AI tier."""
    flagged = [b for b in blocks if b.get("needs_review") and b.get("merge_temps")
               and str(b.get("variable", "")).upper() not in locked]
    if not flagged:
        return
    try:
        colmap = {str(f).lower(): [str(c).upper() for c in cs] for f, cs in (_meta_columns(study) or {}).items()}
    except Exception:  # noqa: BLE001
        colmap = {}
    usub = next((b for b in blocks if str(b.get("variable", "")).upper() == "USUBJID"
                 and b.get("mtype") == "assign" and b.get("dataset")), None)
    base = str(usub["dataset"]).lower() if usub else dom.lower()
    by_var, stems = {}, {}
    for r in rows or []:
        rv = str(r.get("Variable") or r.get("variable") or "").strip().upper()
        by_var[rv] = r
        for t in merge_temps(r.get("Implemented SAS Code") or r.get("implemented_sas_code") or ""):
            if temp_stem(t):
                stems[temp_stem(t)] = stems.get(temp_stem(t), 0) + 1
    subj_keys = {"USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID"}
    # DM reference dates have their own standard derivation (_known_dm_dates, applied next) —
    # that IS the reproduction of their merge temps, so they are not review items.
    std = set(_known_dm_dates(_meta_columns(study) or {})) if dom == "DM" else set()
    for b in flagged:
        v = str(b.get("variable", "")).upper()
        if v in std:
            b.pop("needs_review", None)
            b.pop("review_reason", None)
            continue
        r = by_var.get(v, {})
        rule = str(r.get("Mapping Rule") or r.get("mapping_rule") or "")
        sas = str(r.get("Implemented SAS Code") or r.get("implemented_sas_code") or "")
        iv = str(b.get("input_variables") or "")
        raws = list(dict.fromkeys((d.lower(), c.upper()) for d, c in _RAW_TOK_RE.findall(iv)))
        temps = b["merge_temps"]
        final = _temp_final_funcs(sas, temps[0]) if len(temps) == 1 else None
        done = None
        if final is not None:
            pv = parse_seq_pivot(rule, raws, base)
            rk = _record_keys(colmap.get(pv["dataset"])) if pv else None
            if pv and rk:
                done = {"mtype": "derived", "recipe": "seq_pivot",
                        "args": {**pv, "record_keys": rk, "temp": temps[0], "final": final},
                        "ai_rationale": f"xx-merge {temps[0]}: {pv['column']} where {pv['seq_col']} = "
                                        f"{pv['seq']}, pivoted vertical→wide per record"}
            kl = None if done else parse_keyed_lookup(rule, raws, base, colmap)
            if kl:
                done = {"mtype": "derived", "recipe": "keyed_lookup",
                        "args": {**kl, "temp": temps[0], "final": final},
                        "ai_rationale": f"xx-merge {temps[0]}: raw.{kl['dataset']}.{kl['column']} merged on "
                                        f"{kl['key_col']}" + (" (derived by scan)" if kl.get("scan") else "")}
        if done is None:
            positional = any(stems.get(temp_stem(t), 0) > 1 for t in temps)
            n_sdtm = len(_SDTM_REF_RE.findall(iv.lower()))
            if temp_is_plain_copy(rule, raws, n_sdtm, positional):
                d, c = raws[0]
                if d == base or subj_keys & set(colmap.get(d) or []):
                    done = {"mtype": "assign", "dataset": d, "column": c, "recipe": "", "args": {},
                            "ai_rationale": f"xx-merge {temps[0]} is its sole input raw.{d}.{c}"}
        if done is not None:
            b.update({"dataset": "", "column": "", **done})
            b.pop("needs_review", None)
            b.pop("review_reason", None)
            continue
        b["method"] = "REVIEW"
        b["confidence"] = None


def _apply_sas_expressions(dom, blocks, rows, locked=()):
    """Build a variable from its spec SAS when that SAS reads only OTHER variables of this domain
    and is fully understood by sas_expr — DM.SUBJID = scan(usubjid,-2,'-')||'-'||scan(usubjid,-1,'-').
    Its raw Input Variables are then provenance, not the value: the legacy fallback built SUBJID from
    raw.enroll.ENROLNUM. Applies only to a block that is an empty derivation or an assign from a
    column the SAS never mentions; runs after the variables it reads (`_late`)."""
    domain_vars = {str(b.get("variable", "")).upper() for b in blocks}
    by_var = {str(r.get("Variable") or r.get("variable") or "").strip().upper(): r for r in rows or []}
    for b in blocks:
        v = str(b.get("variable", "")).upper()
        if v in locked or b.get("needs_review"):
            continue
        r = by_var.get(v) or {}
        sas = str(r.get("Implemented SAS Code") or r.get("implemented_sas_code") or "").strip()
        hit = sas_expr.own_domain_expr(sas, domain_vars - {v}, v) if sas else None
        if not hit:
            continue
        mt, col = b.get("mtype"), str(b.get("column") or "").upper()
        generic = mt in ("unmapped", "", None) or (mt == "derived" and not b.get("recipe"))
        unrelated_assign = mt == "assign" and col and col.lower() not in sas.lower()
        if not (generic or unrelated_assign):
            continue
        tree, refs = hit
        b.update({"mtype": "derived", "recipe": "sas_expr", "dataset": "", "column": "", "value": "",
                  "args": {"sas": sas_expr.to_sas(tree), "refs": refs}, "_late": True,
                  "ai_rationale": f"spec SAS over {', '.join(refs)}"})


def compose_blocks(study, domain, rows=None):
    """Build the editable block list for a domain from the mapping_run spec, overlaid with
    any saved Domain Studio edits (S3 sdtm_build/{study}/{DOM}.json). Mirrors SDTM Designer's
    /sdtm/build compose: each spec row → a structured block (via _struct_from_spec), enriched
    with codelist / label / role / origin / input_variables."""
    dom = str(domain).upper()
    if rows is None:
        rows = _spec_rows(study, dom)
    sid = _studyid(study)
    saved = _load_saved(study, dom)
    saved_by = {str(b.get("variable", "")).upper(): b for b in (saved.get("blocks") or [])}
    stale: set = set()

    blocks = []
    for r in rows:
        var = str(r.get("Variable") or r.get("variable") or "").strip().upper()
        if not var:
            continue
        act = r.get("Mapping Action") or r.get("mapping_action") or r.get("Action") or ""
        iv = r.get("Input Variables") or r.get("input_variables") or ""
        label = r.get("Label") or r.get("label") or ""
        codelist = r.get("Codelist") or r.get("codelist") or ""
        origin = r.get("Origin") or r.get("origin") or ""
        role = r.get("Role") or r.get("role") or ""
        dataset_tag = str(r.get("Dataset") or r.get("dataset") or "").strip().upper()
        _sas_r = str(r.get("Implemented SAS Code") or r.get("implemented_sas_code") or "").strip()
        if var in saved_by and _is_legacy_flag_block(saved_by[var], _sas_r, iv, dom.lower()):
            stale.add(var)            # saved engine output from the old first-IF parser — rebuild it
        if var in saved_by and var not in stale:              # user edit wins
            b = dict(saved_by[var])
        else:
            _sas = str(r.get("Implemented SAS Code") or r.get("implemented_sas_code") or "").strip()
            b = _struct_from_spec(var, dom, act, iv, studyid=sid, sas=_sas)
        b["variable"] = var
        b["label"] = b.get("label") or str(label).strip()
        b["codelist"] = b.get("codelist") or str(codelist).strip()
        b["origin"] = b.get("origin") or str(origin).strip()
        b["role"] = b.get("role") or str(role).strip()
        b["input_variables"] = b.get("input_variables") or str(iv).strip()
        # Mapping confidence + method from the spec row (drives the Domain-Build "Conf." column).
        _conf = r.get("_confidence")
        if _conf is not None:
            try:
                fv = float(_conf)
                b["confidence"] = round(fv * 100) if fv <= 1 else round(fv)
            except (TypeError, ValueError):
                pass
        b["method"] = str(r.get("_method") or r.get("_spec_source") or b.get("method") or "").upper()
        b.setdefault("mtype", "unmapped")
        b.setdefault("args", {})
        if var not in saved_by or var in stale:   # deterministic signals for the auto-derivation pass
            b["_sas"] = str(r.get("Implemented SAS Code") or r.get("implemented_sas_code") or "").strip()
            b["_action"] = str(act).upper()
        if dataset_tag == "QNAM":
            b["supp"] = True
        blocks.append(b)
    # rebuild the xx-merge `_temp`s the Mapping Rules state (pivot / keyed merge / sole input);
    # the rest stay flagged for Domain Build / AI instead of a confident copy of the wrong column.
    _reproduce_merge_temps(study, dom, blocks, rows, locked=set(saved_by) - stale)
    # a spec SAS that reads only other variables of this domain is built from that SAS
    _apply_sas_expressions(dom, blocks, rows, locked=set(saved_by) - stale)
    # auto-pick the derivations SDTM Designer configures deterministically (DM reference
    # dates → date_extreme, --DTC → iso_date), skipping any variable the user has saved.
    _apply_auto_derivations(study, dom, blocks, locked=set(saved_by) - stale)
    return {"domain": dom, "blocks": blocks, "settings": saved.get("settings", {}),
            "prep_steps": saved.get("prep_steps", []), "studyid": sid}


# ── Saved Domain Studio edits (S3) ───────────────────────────────────────────
def _save_key(study, domain):
    return f"sdtm_build/{study}/{str(domain).upper()}.json"


def _load_saved(study, domain):
    from .build_engine import _s3
    from .config import settings
    try:
        obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key=_save_key(study, domain))
        return json.loads(obj["Body"].read())
    except Exception:
        return {}


def _current_main(study) -> int:
    """Current phase (main) version for the study — from study_overrides.versioning. Defaults to 1.
    Read directly (no mapping_run import) to phase-tag build saves without a circular dependency."""
    try:
        from .db import get_db
        rows = get_db().execute("SELECT data FROM study_overrides WHERE study_id = %s", (study,))
        if rows:
            raw = rows[0].get("data")
            d = json.loads(raw) if isinstance(raw, str) else (raw or {})
            v = d.get("versioning") or {}
            return int(v.get("current_main") or ((v.get("phases") or [{"main": 1}])[-1].get("main") or 1))
    except Exception:  # noqa: BLE001
        pass
    return 1


def save_prep_steps(study, domain, prep_steps, meta=None):
    """Set just the Prep pipeline for a domain (keeps any saved blocks/settings). Used by the Pre-Build
    'Generate prep' action so Auto-Build (which loads saved prep_steps) runs the stack/merge.
    `meta` records how an automatic pass designed it; saving steps without it clears stale meta."""
    from .build_engine import _s3
    from .config import settings as cfg
    from datetime import datetime, timezone
    dom = str(domain).upper()
    # Consolidate runs of trivial per-variable UPPER() `derive` steps into one `uppercase` batch op
    # before persisting (single choke point for every saved pipeline; idempotent; build output
    # unchanged). Never let a consolidation slip break the save.
    try:
        from .prep_engine import consolidate_prep_steps
        prep_steps = consolidate_prep_steps(prep_steps or [])
    except Exception:  # noqa: BLE001
        prep_steps = prep_steps or []
    doc = _load_saved(study, dom) or {}
    doc["domain"] = dom
    doc["prep_steps"] = prep_steps or []
    if meta:
        doc["prep_meta"] = meta
    else:
        doc.pop("prep_meta", None)
    doc.setdefault("blocks", doc.get("blocks", []))
    doc.setdefault("settings", doc.get("settings", {}))
    doc["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _s3().put_object(Bucket=cfg.S3_BUCKET, Key=_save_key(study, dom), Body=json.dumps(doc, default=str).encode())
    return doc.get("prep_steps")


def save_blocks(study, domain, blocks, settings_doc=None, prep_steps=None, reviewed=None):
    from .build_engine import _s3
    from .config import settings as cfg
    from datetime import datetime, timezone
    dom = str(domain).upper()
    doc = {"domain": dom, "blocks": blocks, "settings": settings_doc or {},
           "prep_steps": prep_steps or [], "reviewed": reviewed or [],
           "main_version": _current_main(study),   # phase-tag so build history aligns to v1/v2
           "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    body = json.dumps(doc, default=str).encode()
    _s3().put_object(Bucket=cfg.S3_BUCKET, Key=_save_key(study, domain), Body=body)
    # History: snapshot this save as a version, keep the last 25
    try:
        pfx = f"sdtm_build/{study}/{dom}/versions/"
        items = _s3().list_objects_v2(Bucket=cfg.S3_BUCKET, Prefix=pfx).get("Contents", [])
        seqs = [int(it["Key"].split("/")[-1][:-5]) for it in items if it["Key"].endswith(".json") and it["Key"].split("/")[-1][:-5].isdigit()]
        nxt = (max(seqs) + 1) if seqs else 1
        _s3().put_object(Bucket=cfg.S3_BUCKET, Key=f"{pfx}{nxt:06d}.json", Body=body)
        for k in sorted(it["Key"] for it in items if it["Key"].endswith(".json"))[:-24]:
            _s3().delete_object(Bucket=cfg.S3_BUCKET, Key=k)
    except Exception:  # noqa: BLE001 — versioning must never break a save
        pass
    return {"ok": True}


def _ds_base(name) -> str:
    """Dataset basename, lower (drops a file extension like .sas7bdat/.xpt)."""
    return re.sub(r"\.[^./\\]+$", "", str(name or "")).strip().lower()


def _remap_build_refs(obj, ds_remap, referenced):
    """Recursively rewrite raw dataset-name references inside a saved prep/blocks doc using ds_remap
    (source dataset basename lower -> target dataset basename, or None when the target lacks it).
    Only keys that actually NAME a dataset are touched (dataset/left/right/file + the `datasets` list)
    plus `raw.<ds>.<col>` strings; prep-output names and every other field are left verbatim. Unmapped
    names (None) are kept as-is — the build skips a missing source resiliently. `referenced` collects
    every SOURCE raw dataset (one present in ds_remap) the doc touched, for the copy/skip decision."""
    _KEYS = ("dataset", "left", "right", "file")

    def _one(name):
        b = _ds_base(name)
        if b in ds_remap:
            referenced.add(b)
            tgt = ds_remap[b]
            return tgt if tgt else name
        return name

    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            if k in _KEYS and isinstance(v, str):
                new[k] = _one(v)
            elif k == "datasets" and isinstance(v, list):
                new[k] = [_one(x) if isinstance(x, str) else _remap_build_refs(x, ds_remap, referenced)
                          for x in v]
            else:
                new[k] = _remap_build_refs(v, ds_remap, referenced)
        return new
    if isinstance(obj, list):
        return [_remap_build_refs(x, ds_remap, referenced) for x in obj]
    if isinstance(obj, str) and obj.lower().startswith("raw."):
        p = obj.split(".")
        if len(p) >= 3 and _ds_base(p[1]) in ds_remap:
            referenced.add(_ds_base(p[1]))
            tgt = ds_remap[_ds_base(p[1])]
            if tgt:
                p[1] = tgt
                return ".".join(p)
    return obj


def copy_build_config(source_study, target_study, ds_remap, domains):
    """Copy the SAVED build config (prep pipeline + Domain Studio blocks + settings) for each domain
    from one study to another so the sister study BUILDS the same way, re-pointing raw dataset
    references via ds_remap. A domain is SKIPPED when it references raw sources but the target has
    NONE of them. Returns {"copied": [...], "skipped": [{domain, reason}]}."""
    copied, skipped = [], []
    for dom in sorted({str(d).upper() for d in (domains or [])}):
        doc = _load_saved(source_study, dom) or {}
        prep_steps = doc.get("prep_steps") or []
        blocks = doc.get("blocks") or []
        if not prep_steps and not blocks:
            continue                                   # nothing saved to copy for this domain
        referenced: set = set()
        new_prep = _remap_build_refs(prep_steps, ds_remap, referenced)
        new_blocks = _remap_build_refs(blocks, ds_remap, referenced)
        # If the saved config names raw sources but the target resolves none of them, skip the domain.
        resolvable = {b for b in referenced if ds_remap.get(b)}
        if referenced and not resolvable:
            skipped.append({"domain": dom, "reason": "target lacks every raw source this build reads"})
            continue
        save_blocks(target_study, dom, new_blocks,
                    settings_doc=doc.get("settings") or {},
                    prep_steps=new_prep, reviewed=doc.get("reviewed") or [])
        if doc.get("prep_meta"):                        # preserve how the prep was designed
            save_prep_steps(target_study, dom, new_prep, meta=doc.get("prep_meta"))
        copied.append(dom)
    return {"copied": copied, "skipped": skipped}


def _iv_from_block(b) -> str:
    """Effective raw Input-Variables string implied by a Domain Studio block's STRUCTURED
    source (mtype/dataset/column or recipe args.sources), so a source assigned in the builder
    syncs back to the spec even when the block's own input_variables text wasn't updated."""
    toks = []
    if str(b.get("mtype") or "").lower() == "assign" and b.get("dataset") and b.get("column"):
        toks.append(f"raw.{str(b['dataset']).lower()}.{str(b['column']).upper()}")
    for sitem in ((b.get("args") or {}).get("sources") or []):
        if isinstance(sitem, dict):
            ds = sitem.get("dataset") or sitem.get("file")
            col = sitem.get("column") or sitem.get("date_col") or sitem.get("col")
            if ds and col:
                toks.append(f"raw.{str(ds).lower()}.{str(col).upper()}")
        elif isinstance(sitem, str) and sitem.strip():
            t = sitem.strip()
            toks.append(t if t.lower().startswith("raw.") else f"raw.{t}")
    seen, out = set(), []
    for t in toks:
        if t.lower() not in seen:
            seen.add(t.lower()); out.append(t)
    return ", ".join(out)


def sync_blocks_to_spec(study, domain, blocks, user_email=None) -> int:
    """Route Domain Studio edits BACK into the Mapping Spec (mapping_run). For each edited block,
    update its variable's spec row — Input Variables, Label, Codelist, Origin, Role, Mapping Action —
    so the two never drift and exports reflect the build. Saves a new mapping_run version only when
    something actually changed. Returns the number of spec rows updated."""
    from .db import get_db

    db = get_db()
    rows = db.execute(
        "SELECT data FROM mapping_run WHERE study_id = %s ORDER BY version DESC LIMIT 1", (study,))
    if not rows:
        return 0
    data = rows[0].get("data")
    if isinstance(data, str):
        data = json.loads(data)
    specs = (data or {}).get("specs") or {}
    dom = str(domain).upper()
    spec_rows = specs.get(dom) or specs.get(str(domain))
    if not spec_rows:
        return 0
    by_var = {str(r.get("Variable") or r.get("variable") or "").upper(): r for r in spec_rows}
    # block field → spec column (Input Variables handled explicitly below, from block structure)
    field_map = {"label": "Label", "codelist": "Codelist",
                 "origin": "Origin", "role": "Role", "mapping_action": "Mapping Action"}
    changed = 0
    all_added: set = set()          # raw tokens assigned across all edited rows (surgical coverage)
    for b in blocks or []:
        row = by_var.get(str(b.get("variable") or "").upper())
        if row is None:
            continue
        touched = False
        added_toks: set = set()          # raw.file.col tokens newly assigned by this edit
        # Input Variables: prefer the block's own text, else derive from its structured source
        # (mtype/dataset/column or recipe args) so a source assigned in the builder — e.g. INVNAM
        # copied into ETHNIC — syncs back even when the text field wasn't touched (FB-0082).
        newiv = str(b.get("input_variables") or "").strip() or _iv_from_block(b)
        oldiv = str(row.get("Input Variables") or "").strip()
        if newiv and newiv != oldiv:
            old_t = {t.strip().lower() for t in re.split(r"[,\n]", oldiv) if t.strip().lower().startswith("raw.")}
            new_t = {t.strip().lower() for t in re.split(r"[,\n]", newiv) if t.strip().lower().startswith("raw.")}
            added_toks |= (new_t - old_t)
            row["Input Variables"] = newiv
            touched = True
        for bf, col in field_map.items():
            if bf not in b:
                continue
            newv, oldv = str(b.get(bf) or "").strip(), str(row.get(col) or "").strip()
            if newv != oldv and (newv or oldv):     # only real edits
                row[col] = newv
                touched = True
        if touched:
            if str(row.get("Input Variables", "")).strip():   # a source is now assigned
                row["_mapped"] = True
                if str(row.get("Mapping Action", "")).strip().upper() in ("", "DROP"):
                    row["Mapping Action"] = "ASSIGN"
            row["_method"] = "APPROVED"
            row["_from_domain_studio"] = True
            changed += 1
            all_added |= added_toks
    if not changed:
        return 0
    # SURGICAL coverage update: flip ONLY the raw columns whose source the edit just assigned to MAPPED,
    # then recompute the summary %. This keeps everything else (and the version diff) clean — no spurious
    # status flips from re-deriving the whole table.
    try:
        from . import mapping_engine_spec as _M
        ms = data.get("metadata_status") or []
        flipped = 0
        for tok in all_added:
            p = tok.split(".")
            if len(p) < 3:
                continue
            tfile, tcol = p[1], p[2]
            for m in ms:
                if m.get("status") != "UNMAPPED" or str(m.get("column_name", "")).lower() != tcol:
                    continue
                mf = str(m.get("file_name", "")).lower().rsplit(".", 1)[0]
                if mf == tfile or mf.startswith(tfile) or tfile.startswith(mf):
                    m["status"], m["mapping_method"] = "MAPPED", "APPROVED"
                    flipped += 1
        if flipped:
            data["summary"] = _M._summarize(ms)
            data["toc"] = _M._build_toc(specs)
    except Exception as exc:  # noqa: BLE001 — coverage refresh must never lose the spec sync
        import logging
        logging.getLogger(__name__).warning("coverage update after spec sync failed: %s", exc)

    ex = db.execute("SELECT MAX(version) AS v FROM mapping_run WHERE study_id = %s", (study,))
    version = ((ex[0]["v"] or 0) + 1) if ex else 1
    db.execute("INSERT INTO mapping_run (study_id, data, version, created_by) VALUES (%s, %s, %s, %s)",
               (study, json.dumps(data, default=str), version, user_email))
    try:
        db.execute("INSERT INTO audit_log (user_email, action, resource, study_id) VALUES (%s, %s, %s, %s)",
                   (user_email, "domain_studio_spec_sync", f"{dom}:{changed}", study))
    except Exception:  # noqa: BLE001
        pass
    return changed


def list_versions(study, domain):
    """Saved-version history for a domain (newest first)."""
    from .build_engine import _s3
    from .config import settings as cfg
    dom = str(domain).upper()
    out = []
    try:
        items = _s3().list_objects_v2(Bucket=cfg.S3_BUCKET, Prefix=f"sdtm_build/{study}/{dom}/versions/").get("Contents", [])
        for it in sorted((i for i in items if i["Key"].endswith(".json")), key=lambda i: i["Key"], reverse=True):
            ver = it["Key"].split("/")[-1][:-5]
            try:
                d = json.loads(_s3().get_object(Bucket=cfg.S3_BUCKET, Key=it["Key"])["Body"].read())
            except Exception:
                d = {}
            out.append({"version": ver, "saved_at": d.get("saved_at", ""),
                        "main_version": d.get("main_version", 1),   # phase (v1/v2…) this save belongs to
                        "n_vars": len(d.get("blocks", [])), "n_reviewed": len(d.get("reviewed", []))})
    except Exception:  # noqa: BLE001
        pass
    return out


def get_version(study, domain, version):
    """Load a specific saved version's doc (blocks + settings + prep_steps + reviewed)."""
    from .build_engine import _s3
    from .config import settings as cfg
    dom = str(domain).upper()
    try:
        obj = _s3().get_object(Bucket=cfg.S3_BUCKET, Key=f"sdtm_build/{study}/{dom}/versions/{version}.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return {"error": "version not found"}


def compare_to_spec(study, domain, blocks=None, settings_doc=None):
    """Compare the BUILT domain to its mapping spec: which spec variables are populated, which
    are missing/empty, and any built columns not in the spec. Findings shape mirrors verify."""
    dom = str(domain).upper()
    if blocks is None:
        blocks = compose_blocks(study, dom)["blocks"]
    out = _run_domain(study, dom, blocks, settings=settings_doc, row_limit=200)
    df = out.get("df")
    spec_vars = [str(r.get("Variable") or r.get("variable") or "").upper()
                 for r in _spec_rows(study, dom)]
    spec_vars = [v for v in spec_vars if v]
    built = {str(c).upper() for c in (df.columns if df is not None else [])}
    findings = []

    def add(level, variable, message):
        findings.append({"level": level, "variable": variable, "message": message})

    for v in spec_vars:
        if v not in built:
            b = next((x for x in blocks if str(x.get("variable", "")).upper() == v), None)
            if b and b.get("mtype") == "drop":
                continue  # intentionally dropped — not a mismatch
            add("error", v, f"{v} is in the spec but not built")
        elif df is not None and len(df):
            ser = df[[c for c in df.columns if str(c).upper() == v][0]]
            nblank = int(ser.isna().sum() + (ser.astype(str).str.strip() == "").sum())
            if nblank == len(df):
                add("warn", v, f"{v} is built but empty on all rows")
            elif nblank:
                add("warn", v, f"{v} empty on {nblank} of {len(df)} rows")
            else:
                add("pass", v, f"{v} built and populated")
    for c in sorted(built - set(spec_vars)):
        add("warn", c, f"{c} is built but not in the mapping spec")
    n_err = sum(1 for f in findings if f["level"] == "error")
    n_warn = sum(1 for f in findings if f["level"] == "warn")
    n_pass = sum(1 for f in findings if f["level"] == "pass")
    return {"findings": findings, "errors": n_err, "warnings": n_warn, "passed": n_pass,
            "nrows": int(len(df)) if df is not None else 0,
            "spec_vars": len(spec_vars), "built_cols": len(built)}


def lineage(study, domain, blocks=None):
    """Transformation lineage for the domain: per SDTM variable, its source(s) + method — for
    the raw→SDTM graph. [{variable, method, sources:[{dataset,column}], recipe, label}]."""
    dom = str(domain).upper()
    if blocks is None:
        blocks = compose_blocks(study, dom)["blocks"]
    out = []
    for b in blocks:
        v = str(b.get("variable", "")).upper()
        mt = b.get("mtype")
        rc = b.get("recipe") or ""
        a = b.get("args") or {}
        srcs = []
        if mt == "assign" and b.get("dataset") and b.get("column"):
            srcs = [{"dataset": b["dataset"], "column": b["column"]}]
        elif rc == "date_extreme":
            srcs = [{"dataset": s.get("dataset"), "column": s.get("date_col")} for s in (a.get("sources") or [])]
        elif rc in ("iso_date",):
            srcs = [{"dataset": a.get("dataset") or dom.lower(), "column": a.get("date_col") or a.get("y_col")}]
        elif rc == "concat":
            srcs = [{"dataset": a.get("dataset") or dom.lower(), "column": c} for c in (a.get("columns") or [])]
        elif rc == "keyed_lookup":
            srcs = [{"dataset": a.get("base_dataset") or dom.lower(), "column": a.get("base_col")},
                    {"dataset": a.get("dataset"), "column": a.get("column")}]
        elif rc == "seq_pivot":
            srcs = [{"dataset": a.get("dataset"), "column": a.get("column")},
                    {"dataset": a.get("dataset"), "column": a.get("seq_col")}]
        elif rc == "sas_expr":
            srcs = [{"dataset": f"sdtm.{dom.lower()}", "column": n} for n in (a.get("refs") or [])]
        # cross-domain sdtm.* refs from the input variables
        for m in _SDTM_REF_RE.findall(str(b.get("input_variables") or "").lower()):
            srcs.append({"dataset": f"sdtm.{m[0]}", "column": m[1].upper()})
        out.append({"variable": v, "label": b.get("label", ""), "mtype": mt, "recipe": rc,
                    "value": b.get("value", "") if mt == "constant" else "",
                    "supp": bool(b.get("supp")), "sources": [s for s in srcs if s.get("dataset")]})
    return {"domain": dom, "nodes": out}


def _prep_frames(study, prep_steps, persist=False):
    """Run the Prep pipeline and return {name: DataFrame} outputs to preload into the build. Only a
    build saves them: the live preview used to persist on every open and edit, overwriting the
    study's prep outputs (and deleting those of a step that failed at that moment)."""
    if not prep_steps:
        return {}
    from .prep_engine import run_prep
    try:
        return run_prep(study, prep_steps, persist=persist).get("frames") or {}
    except Exception:
        return {}


# ── Public entry points ──────────────────────────────────────────────────────
def run_domain(study, domain, blocks=None, settings_doc=None, prep_steps=None, row_limit=200):
    """Compose (if blocks not supplied) + run the Prep pipeline + execute a domain on top.
    Returns JSON-safe result (drops the DataFrames)."""
    if blocks is None:
        blocks = compose_blocks(study, domain)["blocks"]
    preloaded = _prep_frames(study, prep_steps)
    out = _run_domain(study, domain, blocks, preloaded=preloaded, settings=settings_doc, row_limit=row_limit,
                      prep_steps=prep_steps)
    out.pop("df", None)
    out.pop("supp_df", None)
    return out


def build_and_persist(study, domain, blocks=None, settings_doc=None, prep_steps=None):
    """Run Prep + execute a domain and persist parquet + sdtm_output (+ SUPP--)."""
    if blocks is None:
        blocks = compose_blocks(study, domain)["blocks"]
    preloaded = _prep_frames(study, prep_steps, persist=True)
    out = _run_domain(study, domain, blocks, preloaded=preloaded, settings=settings_doc, row_limit=200,
                      prep_steps=prep_steps)
    df = out.pop("df", None)
    supp_df = out.pop("supp_df", None)
    if df is not None and len(df):
        out["file_key"] = _persist(study, str(domain).upper(), df)
        if supp_df is not None and len(supp_df):
            _persist(study, f"SUPP{str(domain).upper()}", supp_df)
    # auto-generate + save the build programs (Python/SAS) — Domain Studio "Build & save"
    try:
        from .program_gen import save_programs
        save_programs(study, str(domain).upper(), blocks, settings_doc, by="domain_studio")
    except Exception:  # noqa: BLE001
        pass
    return out


# ── Conformance verification (SDTM Designer main.py:6600) ────────────────────
_ISO8601_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}(:\d{2}(:\d{2})?)?)?)?)?$")
_DOMAIN_PROFILES = {
    "AE": {"class": "EVENTS", "structure": "One record per adverse event per subject",
           "required": ["STUDYID", "DOMAIN", "USUBJID", "AESEQ", "AETERM"],
           "expected": ["AEDECOD", "AESTDTC", "AESEV", "AESER", "AEREL"],
           "keys": ["STUDYID", "USUBJID", "AESEQ"]},
    "DM": {"class": "SPECIAL PURPOSE", "structure": "One record per subject",
           "required": ["STUDYID", "DOMAIN", "USUBJID", "SUBJID", "RFSTDTC", "SEX", "ARMCD", "ARM", "COUNTRY"],
           "expected": ["AGE", "AGEU", "RACE", "ETHNIC"],
           "keys": ["STUDYID", "USUBJID"]},
    "LB": {"class": "FINDINGS", "structure": "One record per lab test per visit per subject",
           "required": ["STUDYID", "DOMAIN", "USUBJID", "LBSEQ", "LBTESTCD", "LBTEST"],
           "expected": ["LBORRES", "LBORRESU", "LBSTRESC", "LBSTRESN", "LBSTRESU", "LBDTC", "VISITNUM"],
           "keys": ["STUDYID", "USUBJID", "LBTESTCD", "LBDTC"]},
    "VS": {"class": "FINDINGS", "structure": "One record per vital sign per visit per subject",
           "required": ["STUDYID", "DOMAIN", "USUBJID", "VSSEQ", "VSTESTCD", "VSTEST"],
           "expected": ["VSORRES", "VSORRESU", "VSSTRESN", "VSDTC", "VISITNUM"],
           "keys": ["STUDYID", "USUBJID", "VSTESTCD", "VSDTC"]},
    "CM": {"class": "INTERVENTIONS", "structure": "One record per medication per subject",
           "required": ["STUDYID", "DOMAIN", "USUBJID", "CMSEQ", "CMTRT"],
           "expected": ["CMDECOD", "CMSTDTC", "CMINDC", "CMDOSE"],
           "keys": ["STUDYID", "USUBJID", "CMSEQ"]},
    "EX": {"class": "INTERVENTIONS", "structure": "One record per exposure interval per subject",
           "required": ["STUDYID", "DOMAIN", "USUBJID", "EXSEQ", "EXTRT"],
           "expected": ["EXDOSE", "EXDOSU", "EXSTDTC", "EXENDTC"],
           "keys": ["STUDYID", "USUBJID", "EXSEQ"]},
}
_DEFAULT_PROFILE = {"class": "", "structure": "", "required": ["STUDYID", "DOMAIN", "USUBJID"],
                    "expected": [], "keys": ["STUDYID", "USUBJID"]}


def _verify_domain(domain, df, blocks):
    """CDISC-style conformance checks for one domain against the BUILT dataset.
    Returns findings [{rule, level: error|warn|pass, variable, message, n, sample}]."""
    dom = domain.upper()
    prof = _DOMAIN_PROFILES.get(dom, _DEFAULT_PROFILE)
    cols = {str(c).upper(): c for c in df.columns}
    built = set(cols)
    F = []

    def add(rule, level, var, msg, n=0, sample=None):
        F.append({"rule": rule, "level": level, "variable": var, "message": msg,
                  "n": int(n), "sample": [str(x) for x in (sample or [])][:5]})

    if "DOMAIN" in cols:
        vals = df[cols["DOMAIN"]].dropna().astype(str).unique().tolist()
        if vals == [dom]:
            add("DOMAIN", "pass", "DOMAIN", f"DOMAIN = '{dom}' on all rows")
        else:
            add("DOMAIN", "error", "DOMAIN", f"DOMAIN should be '{dom}', found {vals[:3]}", len(df), vals)
    else:
        add("DOMAIN", "error", "DOMAIN", "DOMAIN variable is missing")

    for v in prof["required"]:
        if v not in built:
            add("REQUIRED", "error", v, f"Required variable {v} is missing from the dataset")
        else:
            ser = df[cols[v]]
            nblank = int(ser.isna().sum() + (ser.astype(str).str.strip() == "").sum())
            if nblank:
                add("REQUIRED", "warn", v, f"{v} is required (Core=Req) but empty on {nblank} rows", nblank)
            else:
                add("REQUIRED", "pass", v, f"{v} present and populated")

    for v in prof.get("expected", []):
        if v not in built:
            add("EXPECTED", "warn", v, f"Expected variable {v} is not present")

    seqv = dom + "SEQ"
    if seqv in cols and "USUBJID" in cols:
        sub = df[[cols["USUBJID"], cols[seqv]]]
        nb = int(sub[cols[seqv]].isna().sum())
        dup = int(sub.dropna().duplicated().sum())
        if nb or dup:
            add("SEQ", "error", seqv, f"{seqv}: {nb} null, {dup} duplicate (USUBJID,{seqv}) pairs", nb + dup)
        else:
            add("SEQ", "pass", seqv, f"{seqv} is unique per subject and populated")

    for var, orig in cols.items():
        if var.endswith("DTC"):
            ser = df[orig].dropna().astype(str).str.strip()
            ser = ser[ser != ""]
            if len(ser) == 0:
                continue
            bad = ser[~ser.str.match(_ISO8601_RE)]
            if len(bad):
                add("DTC_ISO", "error", var, f"{var}: {len(bad)} value(s) are not ISO 8601", len(bad), bad.unique().tolist())
            else:
                add("DTC_ISO", "pass", var, f"{var}: all values are ISO 8601")

    for var, orig in cols.items():
        if var.endswith("DY") and len(var) > 2 and var != "STUDYID":
            ser = pd.to_numeric(df[orig], errors="coerce").dropna()
            if len(ser) == 0:
                continue
            zero = int((ser == 0).sum())
            if zero:
                add("DY_NOZERO", "warn", var, f"{var}: {zero} row(s) are day 0 — SDTM study day has no day 0", zero)
            else:
                add("DY_NOZERO", "pass", var, f"{var}: no day 0")

    cmaps = _codelists()
    for b in blocks:
        v = str(b.get("variable", "")).upper()
        cl = str(b.get("codelist") or "").strip().upper()
        if v in cols and cl and cl in cmaps:
            valid = set(cmaps[cl].values())
            ser = df[cols[v]].dropna().astype(str).str.strip()
            ser = ser[ser != ""]
            if len(ser) == 0:
                continue
            bad = ser[~ser.isin(valid)]
            if len(bad):
                add("CT", "error", v, f"{v}: {bad.nunique()} value(s) not in codelist {cl}", len(bad), bad.unique().tolist())
            else:
                add("CT", "pass", v, f"{v}: all values valid for codelist {cl}")

    keys = [cols[k] for k in prof.get("keys", []) if k in cols]
    if len(keys) >= 2:
        dup = int(df[keys].duplicated().sum())
        if dup:
            add("KEY_UNIQUE", "error", " + ".join(prof["keys"]), f"{dup} duplicate key rows (keys should be unique)", dup)
        else:
            add("KEY_UNIQUE", "pass", " + ".join(prof["keys"]), "natural keys uniquely identify each row")
    return F


def verify_domain(study, domain, blocks=None, settings_doc=None):
    """Build a domain and return conformance findings + a score (0–100)."""
    if blocks is None:
        blocks = compose_blocks(study, domain)["blocks"]
    out = _run_domain(study, domain, blocks, settings=settings_doc, row_limit=1)
    df = out.get("df")
    if df is None:
        return {"findings": [], "score": 0, "nrows": 0,
                "profile": _DOMAIN_PROFILES.get(domain.upper(), _DEFAULT_PROFILE)}
    findings = _verify_domain(domain, df, blocks)
    scored = [f for f in findings if f["level"] in ("pass", "error")]
    passed = sum(1 for f in scored if f["level"] == "pass")
    score = round(100 * passed / len(scored)) if scored else 0
    n_err = sum(1 for f in findings if f["level"] == "error")
    n_warn = sum(1 for f in findings if f["level"] == "warn")
    return {"findings": findings, "score": score, "nrows": int(len(df)),
            "errors": n_err, "warnings": n_warn,
            "profile": _DOMAIN_PROFILES.get(domain.upper(), _DEFAULT_PROFILE)}
