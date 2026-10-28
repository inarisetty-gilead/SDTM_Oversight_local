# Vendored from sdtm-studio sdtm_api/src/build_engine.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""SDTM build engine — turns the mapping spec + raw source data into built SDTM domains.

10x over SDTM Designer's engine on the things that matter:
  • SANDBOXED — no `exec()` of arbitrary/user/LLM code. Every mapping compiles to a typed
    RECIPE executed by deterministic pandas primitives; custom logic goes through the
    AST-allow-listed evaluator in build_safe.py. Closes SDTM Designer's #1 security gap.
  • DEPENDENCY DAG — build order is computed from actual cross-domain references
    (topological sort), not a hardcoded domain list; DM builds before anything that reads it.
  • LINEAGE — every built column records its recipe + source tokens (regulator-grade audit).
  • CONFORMANCE SCORE — each domain is validated (structure, keys, SEQ uniqueness, DOMAIN)
    and gets a quality score, mirroring the synthetic-data fidelity scorecard.
  • Deterministic + versioned; writes parquet to S3 and a record to `sdtm_output`.

Source data = the study's raw datasets (synthetic `synth/{study}/…` or real). AI never sees
data rows; it may only *suggest* a recipe (structured), executed deterministically here.
"""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime

import numpy as np
import pandas as pd

from . import sas_expr, sas_functions, sas_runtime
from .build_safe import safe_eval, validate
from .config import settings
from .aws_clients import get_client
from .db import get_db

logger = logging.getLogger(__name__)

_TRIAL_DESIGN = {"TA", "TE", "TI", "TS", "TV", "TD", "SE", "SV"}
_SUPP_COLS = ["STUDYID", "RDOMAIN", "USUBJID", "IDVAR", "IDVARVAL", "QNAM", "QLABEL", "QVAL", "QORIG", "QEVAL"]


def _s3():
    return get_client("s3", region_name=settings.S3_REGION)


# ── Spec + source access ─────────────────────────────────────────────────────
def load_spec(study_id: str) -> dict:
    db = get_db()
    rows = db.execute("SELECT data FROM mapping_run WHERE study_id = %s ORDER BY version DESC LIMIT 1", (study_id,))
    if not rows:
        return {}
    data = rows[0].get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}
    return data or {}


# Domains that are Active by default even without a raw source: trial-design domains (built from the
# protocol / trial-design pipeline) and the structural domains SV / SE / IE. An explicit Active=N
# override in the TOC can still force any of them off.
_ALWAYS_ACTIVE = {"TA", "TD", "TE", "TI", "TM", "TS", "TV", "SV", "SE", "IE"}


def _has_raw_parent_source(rows: list) -> bool:
    """True if any PARENT (non-supplemental) variable in the domain maps from a raw.* source — the
    signal that genuine study data flows into the domain, vs. a shell populated purely from other
    SDTM domains (sdtm.*). Supplemental (QNAM) rows don't count as a genuine contributor."""
    for r in rows or []:
        if str(r.get("Dataset") or "").upper() == "QNAM" or r.get("_supplemental"):
            continue
        for tok in re.split(r"[,\n]", str(r.get("Input Variables") or "")):
            if tok.strip().lower().startswith("raw."):
                return True
    return False


def active_mapping_domains(study_id: str, spec: dict) -> set[str]:
    """Domains Auto-Build should build. Default rule: a domain is Active only if it has a genuine
    raw-source (parent) variable in the mapping — purely sdtm.*-derived shells with no real source
    (e.g. IS here) are inactive and skipped. An explicit Active value in the spec's TOC column is a
    user override and always wins (Y forces on, N forces off), so users can correct the Active
    column by hand and re-run Auto-Build."""
    specs = spec.get("specs") or {}
    by_domain = {str(row.get("Dataset") or "").strip().upper(): row for row in (spec.get("toc") or [])}
    manual = _active_overrides(study_id)   # persistent per-study Active overrides (TOC toggle)
    out = set()
    for dom in specs:
        d = dom.upper()
        override = str((manual.get(d) or {}).get("Active")
                       or (by_domain.get(d) or {}).get("Active") or "").strip().upper()
        if override in ("Y", "N"):
            if override == "Y":
                out.add(d)
            continue
        if d in _ALWAYS_ACTIVE or _has_raw_parent_source(specs.get(dom) or []):
            out.add(d)
    return out


def _active_overrides(study_id: str) -> dict:
    """Per-study manual Active / Active SUPPxx overrides, saved in study_overrides by the TOC toggle.
    Returns {DOMAIN: {"Active": "Y"|"N", "Active SUPPxx": "Y"|"N"}}. Best-effort — never raises."""
    try:
        rows = get_db().execute("SELECT data FROM study_overrides WHERE study_id=%s", (study_id,))
        if rows:
            raw = rows[0].get("data")
            d = json.loads(raw) if isinstance(raw, str) else (raw or {})
            return {str(k).upper(): v for k, v in (d.get("active_overrides") or {}).items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


_SOURCE_CACHE: dict = {}
# Prepared frames (transpose / stack / merge outputs) for the domain currently being built by Auto-Build.
# Set per-domain in build_all so a spec token pointing at a prep output resolves to the prepped frame
# instead of a raw file — this is how a domain's saved Prep pipeline takes effect in Auto-Build.
_ACTIVE_PREP: dict = {}


def _load_source(study_id: str, file: str) -> pd.DataFrame | None:
    """Load a raw source dataset (synthetic or real) by file/dataset name — or a Prep pipeline output
    if one with that name is active for the current domain build."""
    f = str(file).lower()
    if f in _ACTIVE_PREP and _ACTIVE_PREP[f] is not None:
        df = _ACTIVE_PREP[f].copy()
        df.columns = [str(c).upper() for c in df.columns]
        return df
    key = (study_id, f)
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    df = None
    for s3key in (f"synth/{study_id}/{file.lower()}.parquet",
                  f"rawdata/{study_id}/{file.lower()}.parquet",
                  f"prep/{study_id}/{file.lower()}.parquet"):   # DERIVED prep outputs live here now
        try:
            obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key=s3key)
            df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
            break
        except Exception:
            continue
    if df is not None:
        df.columns = [str(c).upper() for c in df.columns]
    _SOURCE_CACHE[key] = df
    return df


def _load_built(study_id: str, domain: str) -> pd.DataFrame | None:
    """Read a previously-built SDTM domain (for cross-domain refs like DM.RFSTDTC)."""
    db = get_db()
    try:
        rows = db.execute("SELECT file_key FROM sdtm_output WHERE study_id=%s AND domain=%s ORDER BY created_at DESC LIMIT 1",
                          (study_id, domain.upper()))
    except Exception:
        return None
    if not rows or not rows[0].get("file_key"):
        return None
    try:
        obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key=rows[0]["file_key"])
        return pd.read_parquet(io.BytesIO(obj["Body"].read()))
    except Exception:
        return None


def _tok(token: str):
    """Parse an Input Variables token: 'raw.ae.aestdat' → ('raw','ae','AESTDAT')."""
    parts = str(token).strip().split(".")
    if len(parts) >= 3:
        return parts[0].lower(), parts[1].lower(), ".".join(parts[2:]).upper()
    return None, None, None


def _tokens(row: dict) -> list:
    raw = row.get("Input Variables") or row.get("input_variables") or ""
    return [_tok(t) for t in re.split(r"[,\s]+", str(raw)) if t.strip()]


# SAS functions / keywords / format words that are NOT source column names — dropped when reading the
# source column(s) out of a row's Implemented SAS Code.
_SAS_NONCOL = {
    "if", "then", "else", "do", "end", "eq", "ne", "gt", "lt", "ge", "le", "and", "or", "not", "in",
    "strip", "upcase", "lowcase", "trim", "left", "compress", "scan", "substr", "input", "put", "cats",
    "catx", "catt", "cat", "coalesce", "coalescec", "index", "find", "tranwrd", "propcase", "missing",
    "sum", "min", "max", "mean", "abs", "round", "int", "when", "case", "best", "best12", "comma",
    "yymmdd10", "yymmdd8", "e8601da", "e8601dt", "date9", "mmddyy10", "time5", "datepart", "timepart",
}


def _sas_src_cols(sas: str, var: str) -> list:
    """Ordered source COLUMN names referenced by a row's Implemented SAS Code — the authoritative signal
    for which raw column feeds a copy (e.g. `strip(upcase(datapagename))` → ['datapagename'];
    `if (cmdose eq .) then x=cmdose_raw` → ['cmdose','cmdose_raw']). Merge-program temporaries written
    `_country` are de-underscored to `country` (the Gilead convention is `_X` built from a RAW column
    named like X). The target variable itself and SAS functions/keywords/formats are excluded."""
    v = str(var or "").lower()
    out: list = []
    for tok in re.findall(r"[a-z_][a-z0-9_]*", str(sas or "").lower()):
        c = tok[1:] if tok.startswith("_") else tok      # de-underscore merge temps
        if not c or c in _SAS_NONCOL or c == v or re.fullmatch(r"\d+", c):
            continue
        if c not in out:
            out.append(c)
    return out


# ── xx-merge `_temp` variables (the legacy two-program pattern) ──────────────
# Gilead's legacy SAS split every domain into xx-merge.sas, which built leading-underscore
# temporaries (_invnam, _atc1pt1, _cmtrt, …) from raw — by the merge / scan / pivot / lookup the
# spec row's free-text Mapping Rule describes — and xx.sas, whose one-line assign from that temp is
# the spec's Implemented SAS Code (`strip(_invnam)`). A consolidated one-program build has to
# reproduce the temp's derivation as well. These helpers detect the temps and recognise the
# derivations the Mapping Rule states in the standard IG wording; anything else must be FLAGGED for
# review, never collapsed to a copy of whichever Input Variable happens to be listed first.
_SAS_AUTOVARS = {"_n_", "_error_", "_null_", "_all_", "_numeric_", "_character_", "_temporary_",
                 "_infile_", "_iorc_", "_last_", "_data_", "_file_", "_cmd_", "_msg_"}
_TEMP_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])_[A-Za-z][A-Za-z0-9_]*")
# Mapping-Rule wording meaning the temp is NOT a plain copy of its single input column. ("your
# cm-merge program" is the IG boilerplate naming where the temp lives — not a merge.)
_TEMP_LOGIC_RE = re.compile(
    r"\bmerge(?![\s-]*program)|\btranspos|\bpivot|\bif\b|\bthen\b|\bwhere\b|\bselect|\bconcatenat|\|\||\bhighest\b"
    r"|\blowest\b|\bearliest\b|\blatest\b|\bfirst\b|\blast\b|\bcount\b|\bsum\b|\bretain\b|\blag\b"
    r"|\bsplit|txtsplit|\bscan\s*\(|\bsubstr|\bderive|refer to \w+ mapping rule", re.I)


def merge_temps(sas: str) -> list:
    """Leading-underscore merge temporaries an Implemented SAS Code reads (`strip(upcase(_aeacn))`
    → ['_aeacn']), lower-cased, in order — SAS automatic names (_n_, _null_, …) and anything inside
    a quoted literal excluded."""
    s = re.sub(r"'[^']*'|\"[^\"]*\"", "", str(sas or ""))
    out = []
    for t in _TEMP_TOKEN_RE.findall(s):
        t = t.lower()
        if t not in _SAS_AUTOVARS and t not in out:
            out.append(t)
    return out


def temp_stem(temp: str) -> str:
    """The positional family a temp belongs to: `_pscrid3` → `_pscrid`, `_atc1pt2` → `_atc1pt`;
    '' when it has no trailing index. Several temps sharing a stem are one vertical→wide pivot."""
    m = re.fullmatch(r"(_[a-z][a-z0-9_]*?[a-z_])\d+", str(temp or "").lower())
    return m.group(1) if m else ""


def temp_is_plain_copy(rule: str, raws: list, n_sdtm_refs: int, positional: bool) -> bool:
    """True when a `_temp` can only be its one Input Variable: exactly one raw input, no SDTM-domain
    input, no positional siblings, and a Mapping Rule that states no derivation logic (merge, if/then,
    transpose, where, highest/first/last, split, 'refer to X mapping rule', …). Case/CT wording
    ('Original value should be put in Upper Case') is not logic — the emitters already strip/CT."""
    if len(raws) != 1 or n_sdtm_refs or positional:
        return False
    return not _TEMP_LOGIC_RE.search(str(rule or ""))


_WHERE_SEQ_RE = re.compile(r"\bwhere\s+([a-z_][a-z0-9_]*)\s*(?:=|eq)\s*(\d+)\b", re.I)


def parse_seq_pivot(rule: str, raws: list, base: str) -> dict | None:
    """A vertical→wide pivot the Mapping Rule states as `<value> (where <seq> = N)` — WHODrug ATC
    positional temps (`_atc1pt2` = ATC level-1 text where atcseq = 2). The value column is the one
    non-sequence Input Variable column; it is read from the base dataset when the base carries it,
    else from the first dataset listing both. `raws` = [(dataset_lower, COLUMN_UPPER)]."""
    m = _WHERE_SEQ_RE.search(str(rule or ""))
    if not m:
        return None
    seq_col, seq = m.group(1).upper(), int(m.group(2))
    by_ds: dict = {}
    for d, c in raws:
        by_ds.setdefault(d, set()).add(c)
    cands = [d for d, cs in by_ds.items() if seq_col in cs and len(cs - {seq_col}) == 1]
    if not cands:
        return None
    ds = base if base in cands else cands[0]
    col = next(iter(by_ds[ds] - {seq_col}))
    if any(c != col for d, cs in by_ds.items() for c in cs - {seq_col}):
        return None            # the Input Variables name more than one value column
    return {"dataset": ds, "column": col, "seq_col": seq_col, "seq": seq,
            "also_in": sorted(d for d in by_ds if d != ds)}


_MERGE_BY_RE = re.compile(
    r"merge\s+raw\.([a-z0-9_]+)\b[^.]*?\bby\s+(?:an?\s+)?(?:derived\s+)?([a-z_][a-z0-9_]*)", re.I)
_MERGE_ON_RE = re.compile(
    r"merge\s+(?:the\s+)?raw\.([a-z0-9_]+)\s+with\s+raw\.([a-z0-9_]+)\b[^.]*?\bon\s+"
    r"raw\.\1\.([a-z0-9_]+)\s+and\s+raw\.\2\.([a-z0-9_]+)", re.I)
_ASSIGN_FROM_RE = re.compile(r"assign\s+from\s+raw\.([a-z0-9_]+)\.([a-z0-9_]+)", re.I)
_SCAN_KEY_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*)\s*=\s*scan\s*\(\s*([a-z_][a-z0-9_]*)\s*,\s*(-?\d+)\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
    re.I)


def parse_keyed_lookup(rule: str, raws: list, base: str, colmap: dict) -> dict | None:
    """A lookup merge the Mapping Rule states in the IG wording, verified against the study's real
    columns (`colmap` = {dataset_lower: [COLUMNS]}):

      • "Merge raw.inv … by a derived INVID value that comes from X_SUBJID
         (invid = scan(x_subjid, -2, '-');) … Assign from RAW.INV.INVNAME value."   (DM.INVNAM)
      • "Merge the raw.protdev with raw.inv dataset on raw.protdev.pd_invid and raw.inv.invid to
         get … raw.inv.invname"                                                    (DV.PDINVNAM)

    plus "For each subject select the DM record with the highest screening number" → the base is
    de-duplicated per subject keeping the highest SCRNID. Returns the keyed_lookup args, or None
    unless every named dataset and column really exists."""
    txt = " ".join(str(rule or "").split())
    cols = {str(d).lower(): {str(c).upper() for c in cs} for d, cs in (colmap or {}).items()}
    base_cols = cols.get(base, set())
    scan = None
    m = _MERGE_ON_RE.search(txt)
    if m and m.group(1).lower() == base:
        lk, base_col, key_col = m.group(2).lower(), m.group(3).upper(), m.group(4).upper()
    else:
        m = _MERGE_BY_RE.search(txt)
        if not m:
            return None
        lk, key_col = m.group(1).lower(), m.group(2).upper()
        sm = next((s for s in _SCAN_KEY_RE.finditer(txt) if s.group(1).upper() == key_col), None)
        if sm:
            base_col, scan = sm.group(2).upper(), {"n": int(sm.group(3)), "sep": sm.group(4)}
        else:
            base_col = key_col                      # merged on the same-named column
    af = _ASSIGN_FROM_RE.search(txt)
    if af and af.group(1).lower() == lk:
        val_col = af.group(2).upper()
    else:                                           # the one Input Variable from the lookup dataset
        vals = [c for d, c in raws if d == lk and c != key_col]
        if len(vals) != 1:
            return None
        val_col = vals[0]
    lk_cols = cols.get(lk, set())
    if lk == base or not (base_col in base_cols and key_col in lk_cols and val_col in lk_cols):
        return None
    out = {"dataset": lk, "column": val_col, "key_col": key_col,
           "base_dataset": base, "base_col": base_col}
    if scan:
        out["scan"] = scan
    if re.search(r"highest\s+screening\s+number", txt, re.I) and "SCRNID" in base_cols:
        out["dedup"] = {"order_col": "SCRNID", "keep": "max"}
    return out


# ── Recipe compilation (spec row → typed recipe) ─────────────────────────────
def compile_recipe(row: dict, domain: str) -> dict | None:
    """Compile one spec row into a typed, sandboxed recipe. None = drop/no-op."""
    var = str(row.get("Variable") or row.get("variable") or "").strip().upper()
    if not var:
        return None
    action = str(row.get("Mapping Action") or row.get("mapping_action") or "").strip().upper()
    if action == "DROP":
        return None
    toks = _tokens(row)
    raw_toks = [t for t in toks if t[0] == "raw" and t[2]]
    supp = str(row.get("Dataset") or "").strip().upper() == "QNAM"
    base = {"var": var, "label": row.get("Label", ""), "supp": supp,
            "idvar": row.get("IDVAR", ""), "qeval": row.get("QEVAL", ""),
            "sources": [f"{t[1]}.{t[2]}" for t in raw_toks], "method": row.get("_method", "")}
    rule = str(row.get("Mapping Rule") or "").strip()
    expr = str(row.get("_expr") or "").strip()  # user/AI-authored safe expression (Domain Studio)
    recode = row.get("_recode") if isinstance(row.get("_recode"), dict) else None

    # ── User overrides from Domain Studio (change the derivation / do your own) ──
    op_o = str(row.get("_op") or "").strip().lower()
    if op_o:
        if op_o in ("const", "constant"):
            return {**base, "op": "const", "value": row.get("_const", "")}
        if op_o == "seq":
            return {**base, "op": "seq"}
        if op_o in ("copy", "iso_date", "study_day"):
            src_str = str(row.get("_src") or "").strip()
            if src_str:
                _, f, c = _tok("raw." + src_str)
                src = ("raw", f, c)
                base["sources"] = [f"{f}.{c}"]
            else:
                src = raw_toks[0] if raw_toks else None
            if op_o == "copy" and src:
                return {**base, "op": "copy", "src": src, "extra": [], "recode": recode}
            if op_o == "iso_date" and base["sources"]:
                return {**base, "op": "iso_date"}
            if op_o == "study_day" and base["sources"]:
                return {**base, "op": "study_day", "ref": "RFSTDTC"}

    if expr:
        return {**base, "op": "expr", "expr": expr}
    if var.endswith("SEQ"):
        return {**base, "op": "seq"}
    if var == "DOMAIN":
        return {**base, "op": "const", "value": domain}
    if var == "STUDYID":
        return {**base, "op": "studyid"}
    if var.endswith("DTC") and raw_toks:
        return {**base, "op": "iso_date"}
    if var.endswith("DY") and (var[:-2] + "DTC") and raw_toks:
        return {**base, "op": "study_day", "ref": "RFSTDTC"}
    # A spec SAS reading only other variables of this domain (DM.SUBJID = scan(usubjid,-2,'-')||…)
    # is the value; the raw Input Variables are provenance. Used when no raw column is SAS-named.
    _own = {t[2] for t in toks if t[0] == "sdtm" and t[1] == domain.lower()}
    _sas_all = str(row.get("Implemented SAS Code") or row.get("implemented_sas_code") or "")
    _sx = sas_expr.own_domain_expr(_sas_all, _own, var) if _own and _sas_all.strip() else None
    _sx_rec = ({**base, "op": "sas_expr", "sas": sas_expr.to_sas(_sx[0]), "refs": _sx[1]} if _sx else None)
    # the spec's IF / ELSE chain over one raw dataset's listed columns — every branch kept
    if raw_toks and _sas_all.strip() and not _sx:
        try:
            _t, _ch = sas_expr.parse_if_chain(_sas_all)
        except sas_expr.SasExprError:
            _ch = None
        _f = raw_toks[0][1]
        # only the domain's own form: another form's rows are not this domain's rows here
        if _ch and any(c["if"] for c in _ch) and _f == domain.lower() and \
                set(sas_expr.chain_names(_ch)) <= {t[2].upper() for t in raw_toks if t[1] == _f}:
            return {**base, "op": "sas_if", "dataset": _f, "chains": sas_expr.chains_to_json(_ch)}
    if raw_toks:
        # Pick the VALUE source, not a merge key. A spec's Input Variables often lists a join key
        # first and the real value column(s) after (e.g. COUNTRY: raw.dm.x_subjid, raw.inv.country,
        # raw.ixrsrand.country, raw.dm.country — x_subjid is the key, the value lives in the *.country
        # columns). When any token's COLUMN NAME equals the SDTM variable, those name-matching tokens
        # are the value sources: copy the first and keep the rest as a coalesce chain (across datasets,
        # resolved by subject in the executor), ignoring the non-matching key tokens. Otherwise keep the
        # historical behaviour (first token wins, same-dataset co-sources coalesce).
        # Pick the VALUE source, not a merge key. The spec's Input Variables often lists a join key
        # first and the real value column(s) after (COUNTRY: raw.dm.x_subjid, …, raw.inv.country, …).
        # Priority, grounded in the authoritative Implemented SAS Code:
        #   1. SAS-NAMED — tokens whose column is named in the Implemented SAS Code (e.g. CMCAT's
        #      `strip(upcase(datapagename))` → cm.datapagename, NOT the mislabeled anticnr.CMCAT column).
        #      COUNTRY's `strip(_country)` → de-underscored `country` → the *.country tokens.
        #   2. NAME-MATCH — else tokens whose column == the SDTM variable.
        #   3. LEGACY — else the first token, co-sourced with same-dataset siblings only (historical).
        # The chosen `extra` tokens are coalesced (first non-blank) by the executor; a cross-dataset
        # value source resolves onto the spine by subject, so COUNTRY lands on ixrsrand.country.
        sas = str(row.get("Implemented SAS Code") or row.get("implemented_sas_code") or "")
        cand = _sas_src_cols(sas, var)
        sas_toks = [t for c in cand for t in raw_toks if t[2].lower() == c]   # SAS-named value sources
        name_match = [t for t in raw_toks if t[2].upper() == var.upper()]     # columns named like the var
        value_toks = sas_toks or name_match
        if value_toks:
            src = value_toks[0]
            value_extra = [t for t in value_toks[1:]]
            # Add same-dataset fallbacks, but ONLY variants of THIS variable (col starts with the var
            # name: LBFAST_NSV, AERELDRV, CMDOSE_RAW) — never a different, shorter variable that merely
            # shares the dataset (MIORRES must not fill the unit MIORRESU).
            same_ds = [t for t in raw_toks if t != src and t not in value_extra
                       and t[1] == src[1] and t[2].upper().startswith(var.upper())]
            extra = value_extra + same_ds
        else:
            # The SAS reads an xx-merge `_temp` none of the Input Variables is named for: its value
            # comes from the merge/pivot/lookup in the Mapping Rule, not from whichever token is
            # listed first (DM.INVNAM = strip(_invnam) is NOT raw.dm.x_subjid). Copy only when the
            # temp can be nothing but its one input; otherwise leave it unmapped and say why —
            # Domain Build reproduces the derivations it recognises and routes the rest to review.
            if _sx_rec:
                return _sx_rec
            temps = merge_temps(sas)
            if temps and not temp_is_plain_copy(
                    rule, [(t[1], t[2]) for t in raw_toks], sum(1 for t in toks if t[0] == "sdtm"),
                    positional=any(temp_stem(t) for t in temps)):
                return {**base, "op": "unmapped", "merge_temps": temps}
            # Legacy: no SAS-named or name-matching value source — first token, same-dataset co-sources.
            src = raw_toks[0]
            extra = [t for t in raw_toks[1:] if t[1] == src[1]]
        return {**base, "op": "copy", "src": src, "extra": extra, "recode": recode}
    return _sx_rec or {**base, "op": "unmapped"}


# ── Per-row build-logic explanation (drives the Pre-Build Review) ─────────────
_FINDINGS_CLASS = {"LB", "EG", "PC", "VS", "TR", "RS", "DA", "FA", "RP", "MB", "MS", "IS", "MI", "OE",
                   "CV", "PP", "MK", "MO", "NV", "RE", "UR", "GF", "SS", "CP", "BS"}


def _explain_op(op, var, raw_toks, sdtm_toks, needs_transpose, rec=None):
    """Translate the resolved build op into (plain-English 'what Auto-Build does', status).
    status: ok=handled directly · auto=post-build derivation · transpose/merge=Auto-Build can't (needs
    a Prep step) · coding=dictionary coding unavailable · empty=no source, builds blank · drop."""
    v = var.upper()
    if op == "const":
        return ("Assigns a fixed value (e.g. DOMAIN = the domain code).", "ok")
    if op == "studyid":
        return ("Assigns the study identifier (STUDYID).", "ok")
    if op == "seq":
        return ("Generates the sequence number (--SEQ), numbered per subject after sorting.", "ok")
    if op == "copy":
        s = (rec or {}).get("src") or (raw_toks[0] if raw_toks else ("", "?", "?"))
        extra = (rec or {}).get("extra") or []
        coalesce = (f" (coalesced with {', '.join(f'{e[1]}.{e[2]}' for e in extra)})" if extra else "")
        return (f"Copies raw {s[1]}.{s[2]} straight into {v} (1:1){coalesce}.", "ok")
    if op == "iso_date":
        s = (rec or {}).get("src") or (raw_toks[0] if raw_toks else ("", "?", "?"))
        return (f"Formats raw {s[1]}.{s[2]} as an ISO-8601 date/time.", "ok")
    if op == "study_day":
        return ("Derives the study day (--DY) from the matching --DTC and DM.RFSTDTC.", "auto")
    if op == "expr":
        return ("Runs a custom expression authored in Domain Build.", "ok")
    if op == "sas_if":
        return (f"Derives {v} with the spec's IF / ELSE logic over raw {(rec or {}).get('dataset', '')}.", "ok")
    if op == "sas_expr":
        return (f"Derives {v} = {(rec or {}).get('sas', '')} from this domain's "
                f"{', '.join((rec or {}).get('refs') or [])} (the spec SAS).", "ok")
    # op == 'unmapped' — work out WHY, and whether a post-build/Prep step covers it.
    if (rec or {}).get("merge_temps"):
        t = ", ".join(rec["merge_temps"])
        return (f"Implemented SAS reads the xx-merge temporary {t}; its merge/pivot/lookup (Mapping "
                f"Rule) is reproduced in Domain Build where recognised — Auto-Build leaves it blank "
                f"rather than copying an unrelated Input Variable.", "merge")
    if v.endswith(("STRESC", "STRESN")):
        return ("Auto-derived after build: standardized result copied from the matching --ORRES.", "auto")
    if v.endswith(("STDY", "ENDY")) or v.endswith("DY"):
        return ("Auto-derived after build: study day from the matching --DTC and DM.RFSTDTC.", "auto")
    if v == "EPOCH":
        return ("Auto-derived after build: epoch from the SE element intervals (needs SE built).", "auto")
    if needs_transpose and v.endswith(("TESTCD", "TEST", "ORRES", "ORRESU")):
        return ("Findings result column — Auto-Build does NOT transpose. Needs a wide→tall PREP "
                "(transpose) step to fill it.", "transpose")
    if sdtm_toks:
        s = sdtm_toks[0]
        return (f"Should derive from another SDTM domain (sdtm.{s[1]}.{s[2]}). Auto-Build's basic engine "
                f"builds only from RAW sources — this needs a MERGE / cross-domain step (Domain Build).", "merge")
    if v.endswith(("DECOD", "BODSYS", "PTCD", "LLT", "LLTCD", "HLT", "HLTCD", "HLGT", "HLGTCD")):
        return ("Dictionary-coded term (MedDRA / WHODrug) — no coding dictionary is wired in, so it "
                "builds blank.", "coding")
    return ("No raw source or rule — Auto-Build builds this column empty. Add a source/rule, route it, "
            "or drop it.", "empty")


def _stack_skip_var(v: str) -> bool:
    """Identifiers / timing / derived vars don't indicate a stack even when fed by many files
    (e.g. DM.RFSTDTC is a min across event dates — not a reason to stack those datasets)."""
    u = str(v).upper()
    return (u in ("STUDYID", "DOMAIN", "USUBJID", "SUBJID", "SPDEVID", "RDOMAIN")
            or u.endswith(("SEQ", "DTC", "DTM", "DY", "STDY", "ENDY", "DUR")))


def _stack_plan(domain: str, rows: list) -> tuple:
    """Files that hold the SAME records for the domain — i.e. more than one raw file supplies the same
    CONTENT variable (a result/topic like --ORRES/--TERM), the signal they should be stacked into one
    base. Returns (stack_files, var_src) where var_src[VAR] = {file: source_col} over all raw sources.
    Empty stack_files = no stack needed (single source, or only identifiers/dates span files)."""
    var_files, var_src = {}, {}
    for r in rows or []:
        if str(r.get("Mapping Action") or "").upper() == "DROP":
            continue
        if str(r.get("Dataset") or "").upper() == "QNAM":
            continue
        var = str(r.get("Variable") or r.get("variable") or "").strip().upper()
        if not var:
            continue
        for kind, f, col in _tokens(r):
            if kind == "raw" and f and col:
                var_src.setdefault(var, {})[f.lower()] = col.upper()
                if not _stack_skip_var(var):
                    var_files.setdefault(var, set()).add(f.lower())
    stack_files = sorted({f for fs in var_files.values() if len(fs) > 1 for f in fs})
    return (stack_files, var_src) if len(stack_files) >= 2 else ([], var_src)


def derive_prep_steps(domain: str, rows: list) -> list:
    """Derive the pre-process pipeline a domain needs — STACK the raw files that carry the same records,
    MERGE the other SDTM domains its variables reference, TRANSPOSE a wide Findings source — straight
    from the mapping-spec source tokens (mirrors what a standard build program does before deriving
    variables). Returns [{op, detail, datasets?/with?}], ordered stack → merge → transpose."""
    sdtm_refs = {}
    for r in rows or []:
        if str(r.get("Mapping Action") or "").upper() == "DROP":
            continue
        var = str(r.get("Variable") or r.get("variable") or "").strip().upper()
        if not var:
            continue
        for kind, f, col in _tokens(r):
            if kind == "sdtm" and f and f.upper() != domain.upper():
                sdtm_refs.setdefault(f.upper(), set()).add(var)
    steps = []
    stack_files, _ = _stack_plan(domain, rows)
    # 'runner' says who executes the step: 'prep' → it becomes a Prep Data step Auto-Build runs;
    # 'build' → the build already does it automatically (cross-domain derivation), no prep needed.
    # STACK — more than one raw file supplies the same CONTENT variable → they hold the same records
    # and must be stacked into one base before mapping. (Prep Data)
    if len(stack_files) > 1:
        steps.append({"op": "stack", "runner": "prep", "datasets": stack_files,
                      "detail": f"Stack {', '.join(stack_files)} into one {domain} base "
                                f"(the same variables are collected across these files)."})
    # TRANSPOSE — a Findings domain whose result columns have no direct raw source (wide raw → tall). (Prep Data)
    result_rows = [r for r in rows
                   if str(r.get("Variable") or "").upper().endswith(("ORRES", "TESTCD"))
                   and str(r.get("Mapping Action") or "").upper() != "DROP"]
    if (domain.upper() in _FINDINGS_CLASS and _has_raw_parent_source(rows) and result_rows
            and not any(_has_raw_parent_source([r]) for r in result_rows)):
        steps.append({"op": "transpose", "runner": "prep",
                      "detail": f"Transpose the wide {domain} raw into one row per test "
                                f"(populates --TESTCD / --ORRES / --ORRESU)."})
    # MERGE — cross-domain references (RFSTDTC from DM, VISIT from SV, EPOCH from SE, …). The build
    # already fills these via its derivations, so they are NOT written to Prep Data. (build-handled)
    for dom, vs in sorted(sdtm_refs.items()):
        vv = ", ".join(sorted(vs))
        steps.append({"op": "merge", "runner": "build", "with": dom,
                      "detail": f"Merge {dom} on USUBJID — supplies {vv[:70]}{'…' if len(vv) > 70 else ''}."})
    return steps


def generate_prep_pipeline(domain: str, rows: list) -> tuple:
    """Turn the derived STACK plan into EXECUTABLE prep-engine steps: rename each source file's columns
    to the SDTM variable names, then stack the files into one <domain>_base dataset. Returns
    (steps, base_name, remap_vars) — remap_vars are the variables now sourced from the stacked base.
    Only the STACK is generated (deterministic + the thing Auto-Build can't do itself); cross-domain
    MERGE is already handled by the build's derivations, and TRANSPOSE needs a separate mapping."""
    dl = str(domain).lower()
    stack_files, var_src = _stack_plan(domain, rows)
    if not stack_files:
        return [], None, {}   # no genuine multi-file stack
    base_name = f"{dl}_base"
    steps, renamed = [], []
    for f in stack_files:
        # one target per source column — a rename cannot copy (TEST-001 RP: ESTDLDAT_RAW listed for both
        # RPORRES and RPTESTCD, so only the second survived and RPTESTCD held dates)
        renames, used = [], set()
        for v in sorted(var_src):
            c = var_src[v].get(f)
            if c and str(c).upper() not in used:
                used.add(str(c).upper())
                renames.append({"from": c, "to": v})
        nm = f"{f}__r"
        steps.append({"op": "rename", "name": nm, "params": {"dataset": f, "renames": renames}})
        renamed.append(nm)
    steps.append({"op": "stack", "name": base_name, "params": {"datasets": renamed}})
    # Only variables actually sourced from the stacked files get remapped to the base.
    remap_vars = sorted({v for v in var_src if any(f in stack_files for f in var_src[v])})
    return steps, base_name, remap_vars


# Friendly --TEST labels for common WIDE findings result columns (fallback is a prettified name).
_FINDINGS_TEST_LABELS = {
    "OVRLRESP": "Overall Response", "TRGRESP": "Target Lesion Response",
    "NTRGRESP": "Non-target Lesion Response", "NEWLES": "New Lesion Response",
    "BESTRESP": "Best Overall Response",
}


# Map a few known raw result-column names to their CDISC --TESTCD submission value.
_TESTCD_NORM = {"NEWLSNYN": "NEWLES", "NEWLESYN": "NEWLES", "NEWLSN": "NEWLES"}


_FINDINGS_CACHE: dict = {}


def _ta_findings_map(study_id: str, domain: str) -> dict:
    """The TA reference spec's 'Findings' sheet, resolved to {RAW_COL_UPPER: (TESTCD, TEST)} for a
    domain. This sheet is the VALUE-LEVEL authority: for each findings --ORRES target it names the raw
    source column and the --TESTCD submission value (e.g. tlsn_rec1.ldiam_raw → LDIAM 'Longest
    Diameter'). The build uses it so --TESTCD comes from the standard, not a guess off the column name.
    Returns {} when the TA spec has no Findings rows for the domain."""
    dom = str(domain).upper()
    # Resolve the study's TA spec workbook (same resolver family as Run Mapping / the Reference viewer).
    try:
        row = get_db().execute("SELECT therapeutic_area FROM studies WHERE study_id=%s", (study_id,))
        ta = (row[0].get("therapeutic_area") if row else "") or ""
    except Exception:  # noqa: BLE001
        ta = ""
    from .reference_spec import _TA_KEYS, _LEGACY_TA
    slug = re.sub(r"[^a-z0-9]+", "", ta.lower())
    key = _TA_KEYS.get(slug) or _TA_KEYS.get(ta.lower())
    if key is None:
        key = f"specs/ta_spec_{slug}.xlsx" if slug else _LEGACY_TA
    ck = (key, dom)
    if ck in _FINDINGS_CACHE:
        return _FINDINGS_CACHE[ck]
    out: dict = {}
    try:
        from .mapping_engine_spec import _download_spec
        path = _download_spec(key) or _download_spec(_LEGACY_TA)
        sheets = pd.read_excel(path, sheet_name=None)
        fsheet = next((df for nm, df in sheets.items() if str(nm).strip().lower() == "findings"), None)
        if fsheet is not None:
            f = fsheet.copy()
            f.columns = [str(c).strip() for c in f.columns]
            f = f[f["SDTM Dataset"].astype(str).str.strip().str.upper() == dom]
            # --TEST labels per TESTCD (rows whose target is the --TEST variable; label in the SAS/rule col).
            tests = {}
            for _, r in f[f["SDTM Target Variable"].astype(str).str.upper() == dom + "TEST"].iterrows():
                tc = str(r.get("SDTM Reference Variable Value") or "").strip().upper()
                lbl = str(r.get("SDTM Target Variable Implemented SAS Code")
                          or r.get("SDTM Target Variable Mapping Rule") or "").strip().strip('"').strip()
                if tc and lbl:
                    tests[tc] = lbl
            # Map each raw source column (of the --ORRES target) to its TESTCD + TEST.
            for _, r in f[f["SDTM Target Variable"].astype(str).str.upper() == dom + "ORRES"].iterrows():
                tc = str(r.get("SDTM Reference Variable Value") or "").strip().upper()
                for tok in re.split(r"[,\n]", str(r.get("Raw Variables") or "")):
                    _, _, col = _tok(tok.strip()) if tok.strip().lower().startswith("raw.") else (None, None, None)
                    if col and tc:
                        out[col.upper()] = (tc, tests.get(tc, tc.title()))
    except Exception as exc:  # noqa: BLE001 — never let a spec read break the build
        logger.warning("TA Findings sheet read failed for %s: %s", dom, exc)
    _FINDINGS_CACHE[ck] = out
    return out


def _testcd_from(col: str) -> str:
    """A --TESTCD from a raw column name: letters/digits only, letter-first, ≤8 (CDISC --TESTCD rule).
    Drops a trailing _STD/_RAW/_UN marker and maps a few known wide-result names to their CDISC code."""
    c = re.sub(r"[^A-Za-z0-9]", "", re.sub(r"_(STD|RAW|UN)$", "", str(col), flags=re.I)).upper()
    if c in _TESTCD_NORM:
        return _TESTCD_NORM[c]
    if c and not c[0].isalpha():
        c = "X" + c
    return c[:8]


def findings_transpose_step(study_id: str, domain: str, rows: list) -> dict | None:
    """The DECLARATIVE wide→tall `transpose_findings` prep step a Findings domain needs when the spec
    left its --TESTCD without a raw source, or None when no transpose applies.

    When the IG-CORE spec builds --TESTCD/--TEST from a merge-program placeholder (no raw source) but
    --ORRES is co-sourced from several WIDE raw result columns in one file (e.g. RS from
    ors_rec.ovrlresp / trgresp / ntrgresp), SDTM needs one ROW PER TEST. This returns the prep-engine
    step that pivots those result columns into {--TESTCD, --TEST, --ORRES} rows — carrying the
    subject/visit/date keys, deriving a per-row ISO --DTC from the assessment date, and (for RS)
    stamping --CAT='RECIST 1.1'. --TESTCD/--TEST come from the TA spec's Findings sheet
    (_ta_findings_map); the column name is the fallback (_testcd_from).

    This is the ONE place the step is built: _auto_findings_base runs it on-the-fly, and the prep
    router APPENDS it to the SAVED pipeline so Domain Build shows it and Domain Studio reshapes
    identically to Auto-Build. Returns None when not applicable (not findings, --TESTCD already
    sourced, no --ORRES raw columns, or <2 result columns) so already-tall domains are untouched."""
    dom = str(domain).upper()
    if dom not in _FINDINGS_CLASS:
        return None
    pfx = dom  # SDTM variable prefix == 2-letter domain code

    def _rowvar(r):
        return str(r.get("Variable") or r.get("variable") or "").upper()

    # --TESTCD must have NO real raw source — otherwise the raw is already tall and no transpose is needed.
    # (A spec that mis-points --TESTCD at wide result columns — e.g. TEST-001 VS — is reshaped by the
    # domain's saved transpose pipeline or the TA Findings map, not fabricated here: without the TA map we
    # can't tell a real wide-measurement matrix from a --TESTCD/--ORRES both mis-pointed at date/metadata
    # columns, which would mint bogus test codes.)
    for r in rows:
        if _rowvar(r) == pfx + "TESTCD" and any(k == "raw" for (k, f, c) in _tokens(r) if k):
            return None

    # Collect the --ORRES raw result columns, grouped by source file.
    from collections import OrderedDict
    by_file: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        if _rowvar(r) != pfx + "ORRES":
            continue
        for (k, f, c) in _tokens(r):
            if k == "raw" and f and c:
                by_file.setdefault(f, [])
                if c not in by_file[f]:
                    by_file[f].append(c)
    if not by_file:
        return None
    # The file that supplies the most result columns; need ≥2 to be a wide form worth transposing.
    file = max(by_file, key=lambda f: len(by_file[f]))
    cols = by_file[file]
    src = _load_source(study_id, file)
    if src is None or src.empty:
        return None
    srcup = {str(c).upper(): c for c in src.columns}
    # Already tall: if the chosen file carries its own per-row test-code column (e.g. LBTESTCD), the raw
    # is one row per test already and a multi-column --ORRES is a coalesce across source files, NOT a wide
    # matrix — transposing it would fabricate bogus test codes from metadata columns. Leave it tall.
    if any(("TESTCD" in u or "TSTCD" in u) for u in srcup):
        return None
    cols = [c for c in cols if c in srcup]
    if len(cols) < 2:
        return None

    # Carry subject / site / visit / date keys so cross-file merges, USUBJID and dates still resolve.
    _KEYS = {"SUBJID", "X_SUBJID", "USUBJID", "SUBJECTID", "INVID", "SITEID", "STUDYID",
             "VISIT", "VISITNUM", "FOLDERNAME", "FOLDER", "INSTANCENAME", "PROJECT"}
    id_vars = [srcup[u] for u in srcup
               if u in _KEYS or u.endswith(("DAT", "DATE", "DTC", "_DT", "DAT_RAW"))]
    # The TA spec's Findings sheet is the authority for the --TESTCD/--TEST of each raw result column.
    # Use it when present; otherwise fall back to deriving the code from the column name.
    fmap = _ta_findings_map(study_id, dom)
    is_rs = dom == "RS"
    measures = []
    for c in cols:
        spec_tc = fmap.get(str(c).upper())
        if spec_tc:
            testcd, test = spec_tc
        else:
            testcd = _testcd_from(c)
            test = _FINDINGS_TEST_LABELS.get(testcd) or str(c).replace("_", " ").title()
        m = {"value_col": srcup[c], "testcd": testcd, "test": test}
        if is_rs:   # RS response assessments are RECIST 1.1 — category the spec leaves to a merge program
            m["cat"] = "RECIST 1.1"
        measures.append(m)
    # Wide form only: the result columns must resolve to ≥2 DISTINCT test codes. If they collapse to a
    # single code (e.g. --ORRES coalesced from alternative source columns of ONE measurement), the raw is
    # already tall / a coalesce — not a wide matrix — so leave it to the normal assign/coalesce path.
    if len({m["testcd"] for m in measures if m.get("testcd")}) < 2:
        return None
    params = {"dataset": file, "id_vars": id_vars,
              "testcd_col": pfx + "TESTCD", "test_col": pfx + "TEST",
              "orres_col": pfx + "ORRES", "orresu_col": pfx + "ORRESU",
              "measures": measures}
    if is_rs:
        params["cat_col"] = pfx + "CAT"
    # Per-row ISO --DTC when the assessment date lives in THIS (transposed) file — so each test row
    # keeps its own date instead of collapsing to one date per subject via a cross-file merge.
    dcol = next((srcup[u] for u in srcup
                 if u in {pfx + "DAT", pfx + "DAT_RAW", "VISITDAT"}
                 or u.endswith(("DAT", "DAT_RAW"))), None)
    if dcol and (pfx + "DTC") not in srcup:
        params["dtc_col"] = pfx + "DTC"
        params["dtc_source_col"] = dcol
    return {"op": "transpose_findings", "name": f"{dom.lower()}_base", "params": params}


_Q = r"""['"]"""


def _rule_text(r) -> str:
    return " ".join(str(r.get(k) or "") for k in ("Mapping Rule", "mapping_rule", "Implemented SAS Code",
                                                   "implemented_sas_code"))


def rule_transpose_steps(study_id: str, domain: str, rows: list, columns: dict | None = None) -> list | None:
    """The transpose a spec's --TESTCD Mapping Rule spells out, as exact prep steps — or None.

    Gilead specs describe a wide form to transpose in prose: "Transpose the raw dataset … if lmpdat_raw
    ne ' ' then _rptestcd = "LMPSTDTC"; if pregdat_raw ne ' ' then _rptestcd = "PCONFDTC"; …", with
    the names in the --TEST rule ('If rptestcd is "LMPSTDTC" then _rptest = "Last Menstrual Period
    Start Date"') and the value in the --ORRES rule (strip(col), or %m_sdtm_isodate(srcdt=col) for a
    date). Read deterministically, each source column becomes one test: the dataset holding it is
    found among this study's forms, each dataset is transposed, and they are stacked into <dom>_base.
    A test whose column the study does not have is left out; fewer than two tests means no transpose."""
    dom = str(domain).upper()
    p = dom.lower()
    var = lambda r: str(r.get("Variable") or r.get("variable") or "").upper()  # noqa: E731
    row = {var(r): r for r in rows}
    tc = row.get(f"{dom}TESTCD")
    if not tc or "transpose" not in _rule_text(tc).lower():
        return None
    text = _rule_text(tc)
    # "… else _pctestcd = pctestcd": the raw is already one record per test, with extra tests added — a
    # transpose of only those would drop every other record, so it is not this pattern
    if re.search(rf"else\s+_?{p}testcd\s*=\s*[a-z_]", text, re.I):
        return None
    neq = rf"(?:ne|\^=|~=|!=|<>)\s*(?:{_Q}\s*{_Q}|\.)"          # ne ' ' (character) or ne . (numeric)
    extra = r"(?:\s+(?:or|and)\s+[^;]*?)?"                   # "… or daretyn_std = 'N'" — the test's column comes first
    pairs = re.findall(rf"if\s+([a-z_][a-z0-9_]*)\s+{neq}{extra}\s*then\s+_?{p}testcd\s*=\s*{_Q}\s*([A-Za-z0-9_]+)\s*{_Q}",
                       text, re.I)
    pairs += [(c, code) for c, code in re.findall(
        rf"if\s+not\s+missing\(\s*([a-z_][a-z0-9_]*)\s*\)\s*then\s+_?{p}testcd\s*=\s*{_Q}\s*([A-Za-z0-9_]+)\s*{_Q}",
        text, re.I)]
    if len(pairs) < 2:
        return None
    is_code = rf"if\s+_?{p}testcd\s+(?:is|=|eq)\s*{_Q}\s*([A-Za-z0-9_]+)\s*{_Q}\s*then\s+_?{p}"
    names = {c.upper(): n.strip().strip("'\"").strip()
             for c, n in re.findall(is_code + rf"test\s*=\s*{_Q}(.+?){_Q}\s*;", _rule_text(row.get(f"{dom}TEST", {})), re.I)}
    orres = {c.upper(): e for c, e in re.findall(is_code + r"orres\s*=\s*([^;]+);",
                                                  _rule_text(row.get(f"{dom}ORRES", {})), re.I)}
    cols = columns if columns is not None else (_meta_columns(study_id) or {})
    cols = {str(f).lower(): {str(c).upper(): c for c in cs} for f, cs in cols.items()}
    spec_files = [f for r in (tc, row.get(f"{dom}ORRES", {})) for (k, f, _c) in _tokens(r) if k == "raw" and f]
    spec_files += [f for r in rows for (k, f, _c) in _tokens(r) if k == "raw" and f]
    order = list(dict.fromkeys([*spec_files, *cols]))
    by_file: dict = {}
    iso_codes = []
    for col, code in pairs:
        code = code.upper()
        expr = orres.get(code, "")
        m = re.search(r"srcdt\s*=\s*([a-z_][a-z0-9_]*)", expr, re.I) or re.search(r"\(\s*([a-z_][a-z0-9_]*)\s*\)", expr, re.I)
        src_col = (m.group(1) if m else col).upper()
        # the column as named, else without its _RAW suffix (DANUMDP_RAW is DANUMDP in some studies)
        cands = list(dict.fromkeys(x for n in (src_col, col.upper()) for x in (n, re.sub(r"_RAW$", "", n))))
        f, c = next(((f, cols[f][n]) for n in cands for f in order if n in cols.get(f, {})), (None, None))
        if f is None:
            continue
        by_file.setdefault(f, []).append({"value_col": c, "testcd": code, "test": names.get(code) or code})
        if "isodate" in expr.lower():
            iso_codes.append(code)
    if sum(len(v) for v in by_file.values()) < 2:
        return None
    # each test's unit column: the --ORRESU sources (or the form) by the value column's stem —
    # HEIGHT_RAW ↔ HEIGHT_UN, SYSBP_RAW ↔ SYSBPU, EGVR_RAW ↔ EGVRU
    unit_src = {c.upper() for (k, _f, c) in _tokens(row.get(f"{dom}ORRESU", {})) if k == "raw" and c}
    dtc_src = [(f, c.upper()) for (k, f, c) in _tokens(row.get(f"{dom}DTC", {})) if k == "raw" and f and c]
    steps, outs, dtc_time = [], [], []
    not_done = re.findall(rf"if\s+([a-z_][a-z0-9_]*)\s*=\s*{_Q}([^'\"]*){_Q}\s*then\s+_?{p}testcd\s*=\s*{_Q}\s*{dom}ALL\s*{_Q}",
                          text, re.I)
    stat_value = (re.findall(rf"then\s+_?{p}stat\s*=\s*{_Q}([^'\"]+){_Q}", _rule_text(row.get(f"{dom}STAT", {})), re.I)
                  or ["NOT DONE"])[0].strip().upper()
    for f, ms in by_file.items():
        fc = cols[f]
        for m in ms:
            stem = re.sub(r"_RAW$", "", str(m["value_col"]).upper())
            units = [stem + sfx for sfx in ("U", "_UN", "_UNIT", "U_STD", "_UN_STD")]
            u = next((x for x in units if x in fc and x in unit_src), None) or next((x for x in units if x in fc), None)
            if u:
                m["unit_col"] = fc[u]
        used = {str(m["value_col"]).upper() for m in ms}
        params = {"dataset": f"raw.{f}", "id_vars": [c for uc, c in fc.items() if uc not in used],   # every other column rides along
                  "testcd_col": f"{dom}TESTCD", "test_col": f"{dom}TEST", "orres_col": f"{dom}ORRES",
                  "orresu_col": f"{dom}ORRESU", "measures": ms}
        # the --DTC rule's date (a date column, never its time) — on this form, or merged in from the form
        # that holds it by subject AND visit (TEST-001 VS: visdt.visitdat; vsperf has only VSTIM)
        is_date = lambda c: bool(re.search(r"DAT|DTC|DATE", c)) and not re.search(r"TIM", c)  # noqa: E731
        d = next((fc[c] for ff, c in dtc_src if ff == f and c in fc and is_date(c)), None)
        other = next(((ff, c) for ff, c in dtc_src if ff != f and is_date(c) and c in cols.get(ff, {})), None)
        if d is None and other:
            ff, c = other
            keys = [k for k in ("X_SUBJID", "FOLDER", "FOLDERNAME", "INSTANCENAME") if k in fc and k in cols[ff]]
            if "X_SUBJID" in keys and len(keys) > 1:
                dn = f"_{dom}DAT"
                steps.append({"op": "rename", "name": f"{ff}__d", "params": {"dataset": f"raw.{ff}", "renames": [{"from": cols[ff][c], "to": dn}]}})
                steps.append({"op": "merge", "name": f"{f}__dt", "params": {"inputs": [
                    {"dataset": f"raw.{f}"}, {"dataset": f"{ff}__d", "columns": [*[cols[ff][k] for k in keys], dn]}],
                    "on": [fc[k] for k in keys], "how": "left"}})
                params["dataset"] = f"{f}__dt"
                params["id_vars"] = [*params["id_vars"], dn]
                d = dn
        if d and f"{dom}DTC" not in fc:
            params.update(dtc_col=f"{dom}DTC", dtc_source_col=d)
            tim = next((fc[c] for ff, c in dtc_src if ff == f and c in fc and re.search(r"TIM", c)), None)
            if tim:
                dtc_time.append(tim)
        # the spec's not-done record: "if vsperf_std = 'N' then _vstestcd = 'VSALL'" — a visit whose tests
        # were not done gives ONE <dom>ALL record (TEST / STAT / REASND / date from the spec) instead of tests
        nd = [(fc.get(c.upper()) or fc.get(re.sub(r"_STD$", "", c.upper())), v) for c, v in not_done]
        nd = [(c, v) for c, v in nd if c]
        if nd:
            def _eq(c, v):
                v = v.strip().upper()
                alts = {"N": ["N", "NO"], "Y": ["Y", "YES"]}.get(v, [v])
                return f"upcase(strip({c})) in ({', '.join(repr(a) for a in alts)})"
            cond = " or ".join(_eq(c, v) for c, v in nd)
            src_ds = params["dataset"]
            steps.append({"op": "filter", "name": f"{f}__done", "params": {"dataset": src_ds, "expression": f"not ({cond})"}})
            params["dataset"] = f"{f}__done"
            code = f"{dom}ALL"
            chain = [("TESTCD", f"'{code}'"), ("TEST", repr(names.get(code) or code)), ("STAT", f"'{stat_value}'")]
            reas = next((fc[c] for (k, ff, c) in [(k, ff, c.upper()) for (k, ff, c) in _tokens(row.get(f"{dom}REASND", {}))]
                         if k == "raw" and ff == f and c in fc), None)
            if reas:
                chain.append(("REASND", f"upcase(strip({reas}))"))
            if d:
                iso = f"put(input(strip({d}), ?? anydtdte32.), e8601da10.)"
                chain.append(("DTC", f"ifc(missing({iso}), strip({d}), {iso})"))
            prev = f"{f}__ndr"
            steps.append({"op": "filter", "name": prev, "params": {"dataset": src_ds, "expression": cond}})
            for k, (var, expr) in enumerate(chain):
                nm = f"{f}__nd" if k == len(chain) - 1 else f"{f}__nd{k}"
                steps.append({"op": "compute", "name": nm, "params": {"dataset": prev, "target": f"{dom}{var}", "expression": expr}})
                prev = nm
            outs.append(f"{f}__nd")
        name = f"{f}__t"
        steps.append({"op": "transpose_findings", "name": name, "params": params})
        outs.append(name)
    last = outs[0]
    if len(outs) > 1:
        steps.append({"op": "stack", "name": f"{p}_stack", "params": {"datasets": outs}})
        last = f"{p}_stack"
    if dtc_time:     # the --DTC rule's time, on the transposed form: a full ISO date gets THH:MM
        t, dtc = dtc_time[0], f"{dom}DTC"
        steps.append({"op": "compute", "name": f"{p}_dtctm", "params": {"dataset": last, "target": dtc, "expression":
                      f"ifc(length(strip({dtc})) = 10 and not missing({t}), catx('T', {dtc}, {t}), {dtc})"}})
        last = f"{p}_dtctm"
    # --STRESC / --STRESU: each test's standardised column, named in the spec row's inputs and carried
    # onto its record (HEIGHT_RAW → HEIGHT_STD, SYSBP_RAW → SYSBP; units HEIGHT_STD_UN, SYSBPU)
    carried = {u for f in by_file for u in cols[f]}
    stem = {m["testcd"]: re.sub(r"_RAW$", "", str(m["value_col"]).upper()) for ms in by_file.values() for m in ms}
    for target, prefer in ((f"{dom}STRESC", ("", "_STD", "_STD_RAW")), (f"{dom}STRESU", ("_STD_UN", "U", "_UN", "_STD_UNIT"))):
        srcs = [c.upper() for (k, _f, c) in _tokens(row.get(target, {})) if k == "raw" and c and c.upper() in carried]
        pick = {}
        for code, st in stem.items():
            c = next((st + sfx for sfx in prefer if st + sfx in srcs), None)
            if c:
                pick[code] = c
        if pick:
            expr = "''"
            for code, c in reversed(list(pick.items())):
                expr = f"ifc({dom}TESTCD = '{code}', cats({c}), {expr})"
            steps.append({"op": "compute", "name": f"{p}_{target[len(dom):].lower()}",
                          "params": {"dataset": last, "target": target, "expression": expr}})
            last = f"{p}_{target[len(dom):].lower()}"
    if iso_codes:   # the --ORRES rule's %m_sdtm_isodate: a full date → ISO 8601, a partial one kept as is
        o = f"{dom}ORRES"
        iso = f"put(input(strip({o}), ?? anydtdte32.), e8601da10.)"
        codes = ", ".join(f"'{c}'" for c in iso_codes)
        steps.append({"op": "compute", "name": f"{p}_iso", "params": {"dataset": last, "target": o, "expression":
                      f"ifc({dom}TESTCD in ({codes}) and length(strip({o})) >= 8 and not missing({iso}), {iso}, {o})"}})
        last = f"{p}_iso"
    steps[-1]["name"] = f"{p}_base"     # the last step writes the domain's base
    return steps


def _auto_findings_base(study_id: str, domain: str, rows: list) -> dict:
    """Run the deterministic Findings wide→tall transpose ON-THE-FLY and return its `<dom>_base` frame,
    or {} when no transpose applies. Fallback for Auto-Build when the SAVED pipeline has no transpose
    step of its own (see findings_transpose_step for the reshape rules)."""
    steps = rule_transpose_steps(study_id, domain, rows)        # the spec's own transpose rule first
    if not steps:
        step = findings_transpose_step(study_id, domain, rows)
        steps = [step] if step else None
    if not steps:
        return {}
    dom = str(domain).upper()
    try:
        from .prep_engine import run_prep
        frames = run_prep(study_id, steps, persist=False).get("frames") or {}
    except Exception as exc:  # noqa: BLE001 — a transpose failure must never break the build
        logger.warning("findings transpose for %s failed: %s", dom, exc)
        return {}
    base = frames.get(f"{dom.lower()}_base")
    if base is None or base.empty:
        return {}
    return {f"{dom.lower()}_base": base}


def explain_build_logic(study_id: str, spec: dict) -> list:
    """For each ACTIVE domain, list every spec row with what Auto-Build actually does to it. Powers the
    Pre-Build Review so the user can see, row by row, where the spec→build is handled vs. where it needs
    a Prep step (transpose / merge). Read-only; builds nothing."""
    specs = spec.get("specs") or {}
    active = active_mapping_domains(study_id, spec)
    return [explain_domain(dom, specs.get(dom) or specs.get(dom.upper()) or []) for dom in sorted(active)]


def explain_domain(dom: str, rows: list) -> dict:
    """One domain of explain_build_logic: {domain, needs_transpose, prep_steps, rows, counts}."""
    # A genuine transpose case = a Findings domain that HAS raw data for this domain, but whose
    # result columns aren't directly sourced (wide raw needs reshaping). If the domain has NO raw
    # data at all, the result columns are simply un-sourced ("empty"), not a transpose problem.
    result_rows = [r for r in rows
                   if str(r.get("Variable") or "").upper().endswith(("ORRES", "TESTCD"))
                   and str(r.get("Mapping Action") or "").upper() != "DROP"]
    has_any_raw = _has_raw_parent_source(rows)
    needs_transpose = (dom.upper() in _FINDINGS_CLASS and has_any_raw and result_rows
                       and not any(_has_raw_parent_source([r]) for r in result_rows))
    row_out, counts = [], {}
    for r in rows:
        var = str(r.get("Variable") or r.get("variable") or "").strip().upper()
        if not var:
            continue
        action = str(r.get("Mapping Action") or r.get("mapping_action") or "").strip().upper()
        toks = _tokens(r)
        raw_toks = [t for t in toks if t[0] == "raw" and t[2]]
        sdtm_toks = [t for t in toks if t[0] == "sdtm" and t[2]]
        src = ", ".join(f"{t[1]}.{t[2]}" for t in raw_toks) or ", ".join(f"sdtm.{t[1]}.{t[2]}" for t in sdtm_toks)
        if action == "DROP":
            does, status = "Dropped from the domain — not built.", "drop"
            op = "drop"
        else:
            rec = compile_recipe(r, dom)
            op = rec["op"] if rec else "unmapped"
            does, status = _explain_op(op, var, raw_toks, sdtm_toks, needs_transpose, rec)
        counts[status] = counts.get(status, 0) + 1
        row_out.append({"variable": var, "action": action, "source": src, "op": op,
                        "does": does, "status": status})
    return {"domain": dom, "needs_transpose": bool(needs_transpose),
            "prep_steps": derive_prep_steps(dom, rows),
            "rows": row_out, "counts": counts}


# ── DAG ordering ─────────────────────────────────────────────────────────────
def build_order(specs: dict) -> list[str]:
    """Topological order from cross-domain references (sdtm.<dom>.* tokens). DM leads."""
    domains = [d for d in specs if d.upper() not in _TRIAL_DESIGN]
    deps = {d: set() for d in domains}
    for d in domains:
        for row in specs[d]:
            for kind, dom, _ in _tokens(row):
                if kind == "sdtm" and dom and dom.upper() in {x.upper() for x in domains} and dom.upper() != d.upper():
                    deps[d].add(dom.upper())
    order, seen = [], set()

    def visit(d, stack):
        if d in seen or d in stack:
            return
        stack.add(d)
        for dep in sorted(deps.get(d, ())):
            match = next((x for x in domains if x.upper() == dep), None)
            if match:
                visit(match, stack)
        stack.discard(d)
        seen.add(d)
        order.append(d)

    # DM first (many domains read its reference dates), then the rest
    for d in sorted(domains, key=lambda x: (x.upper() != "DM", x.upper())):
        visit(d, set())
    return order


# ── Source columns + Controlled Terminology (for Domain Studio editing) ──────
def _meta_columns(study_id: str) -> dict:
    """{file: [COL, …]} from the study's parsed metadata (no parquet read)."""
    db = get_db()
    rows = db.execute("SELECT data FROM study_metadata WHERE study_id=%s ORDER BY version DESC LIMIT 1", (study_id,))
    if not rows:
        return {}
    data = rows[0].get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}
    out: dict = {}
    for c in (data or {}).get("columns", []):
        fn = str(c.get("file_name", "")).strip().lower()
        cn = str(c.get("column_name", "")).strip().upper()
        if fn and cn:
            out.setdefault(fn, []).append(cn)
    return out


def domain_sources(study_id: str, domain: str, rows: list) -> dict:
    """Source datasets + columns available for a domain's derivations (for the picker).
    Files referenced by the spec come first; then all other raw files in the study."""
    cols = _meta_columns(study_id)
    ref = []
    for row in rows:
        for kind, f, _ in _tokens(row):
            if kind == "raw" and f and f not in ref:
                ref.append(f)
    ordered = ref + [f for f in cols if f not in ref]
    return {"datasets": {f: cols.get(f, []) for f in ordered}}


_CT_CACHE: dict = {}


def _core_codelists() -> dict:
    """{CODELIST_NAME: [{value, decode}]} from core_spec.xlsx's Codelist sheet (cached).
    This is the controlled terminology already shipped in the core spec."""
    if _CT_CACHE.get("map") is not None:
        return _CT_CACHE["map"]
    m: dict = {}
    try:
        obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key="specs/core_spec.xlsx")
        cl = pd.read_excel(io.BytesIO(obj["Body"].read()), sheet_name="Codelist")
        cl.columns = [str(c).strip() for c in cl.columns]
        for name, grp in cl.groupby("Codelist Name"):
            terms = []
            for _, r in grp.iterrows():
                v = str(r.get("Codelist Value", "") or "").strip()
                d = str(r.get("Codelist Value Decode", "") or "").strip()
                if v:
                    terms.append({"value": v, "decode": d})
            if terms:
                m[str(name).strip().upper()] = terms
    except Exception as exc:  # noqa: BLE001
        logger.warning("core_spec Codelist load failed: %s", exc)
    _CT_CACHE["map"] = m
    return m


def clear_spec_caches() -> None:
    """Drop the build caches derived from the reference-spec workbooks — the TA 'Findings' value-level
    map (keyed by workbook S3 key, so a re-upload to the SAME key would otherwise stay stale) and the
    core-spec codelists. Called by reference_spec.invalidate_spec_caches on every spec upload/rollback
    so a newly uploaded spec takes effect without a restart."""
    _FINDINGS_CACHE.clear()
    _CT_CACHE.clear()


def _ct_terms(codelist: str) -> list:
    """CT terms (submission values) for a codelist name, from the core spec."""
    if not codelist:
        return []
    t = _core_codelists().get(str(codelist).strip().upper(), [])
    return [x["value"] for x in t]


def _auto_ct_map(source_values: list, terms: list) -> dict:
    """Suggest a source-value → CT-value recode. Exact value/decode match first (so
    'Male'→M, not F), then a first-word fallback (so 'Black'→'BLACK OR AFRICAN AMERICAN')."""
    def n(x):
        return re.sub(r"[^a-z0-9]", "", str(x).lower())
    out = {}
    for sv in source_values:
        nv = n(sv)
        if not nv:
            continue
        hit = next((t["value"] for t in terms if nv in (n(t["value"]), n(t["decode"]))), None)
        if not hit:
            for t in terms:
                words = str(t["decode"]).lower().split()
                if words and n(words[0]) == nv:
                    hit = t["value"]
                    break
        if hit:
            out[sv] = hit
    return out


def variable_ct(study_id: str, domain: str, var: str, rows: list) -> dict:
    """Controlled-terminology view for a variable: codelist name, source distinct values,
    the core-spec CT terms (value + decode), a suggested auto-map, and the current recode."""
    var = var.upper()
    row = next((r for r in rows if str(r.get("Variable") or r.get("variable") or "").upper() == var), None)
    if not row:
        return {"error": f"{var} not in spec"}
    codelist = str(row.get("Codelist") or row.get("codelist") or "").strip()
    rec = compile_recipe(row, domain)
    source_col, source_values = None, []
    if rec and rec.get("op") == "copy":
        f, c = rec["src"][1], rec["src"][2]
        df = _load_source(study_id, f)
        if df is not None and c in df.columns:
            source_col = f"{f}.{c}"
            source_values = sorted({str(x).strip() for x in df[c].dropna() if str(x).strip()})[:80]
    term_objs = _core_codelists().get(codelist.upper(), [])
    current = row.get("_recode") if isinstance(row.get("_recode"), dict) else {}
    suggested = _auto_ct_map(source_values, term_objs) if (source_values and term_objs) else {}
    return {"variable": var, "codelist": codelist, "source": source_col,
            "source_values": source_values,
            "terms": [t["value"] for t in term_objs],
            "term_decodes": term_objs,
            "suggested": suggested, "recode": current}


# ── Helpers ──────────────────────────────────────────────────────────────────
def _subj_col(df):
    for c in df.columns:
        if c.upper() in ("USUBJID", "SUBJID", "SUBJECT", "SUBJECTID"):
            return c
    return None


def _iso(series):
    """Normalise an arbitrary RAW date column to ISO 8601.

    Deliberately permissive: raw source dates arrive in whatever format the
    study collected them in, and converting them is the whole point. Contrast
    _sdtm_dtc below, which parses values that are already ISO.
    """
    dt = pd.to_datetime(series, errors="coerce")
    return dt.dt.strftime("%Y-%m-%d").where(dt.notna(), "")


def _sdtm_dtc(series):
    """Parse a built SDTM --DTC to a date. Strict, and that is the point.

    SDTM dates are ISO 8601 by construction, and PARTIAL ones are legitimate:
    a known month with an unknown day is '2026-01', because '2026-01-01' would
    assert a day nobody collected.

    Parsing those without an explicit format goes wrong two ways:

      1. '2026-01' parses as 2026-01-01, so a partial date yields a confident
         wrong study day — '2026-02' became day 32 relative to 2026-01-01.
      2. pandas 2.x infers ONE format from the first non-null value and coerces
         everything that does not match to NaT. So a correctly collected
         '2026-01-15' came out missing whenever a partial date appeared earlier
         in the column, which made one row's result depend on the other rows
         and on their order.

    Truncating to 10 characters drops any time part; `format` then accepts only
    a complete date, so a partial one becomes NaT. That is the correct answer:
    an unknown day supports no day arithmetic.
    """
    return pd.to_datetime(series.astype("string").str[:10],
                          format="%Y-%m-%d", errors="coerce")


# ── Domain builder ───────────────────────────────────────────────────────────
def _fill_standard_derivations(out, domain: str, study_id: str, subj):
    """Deterministically populate the standard SDTM derived variables so the mapping spec doesn't have
    to spell them out and the user never authors them — only reviews. Fills ONLY columns already in
    `out` that are entirely empty (never clobbers real data). Returns [(var, human method)]."""
    filled = []

    def empty(c):
        if c not in out.columns:
            return False
        s = out[c]
        return bool(s.isna().all() or (s.astype("string").fillna("").str.strip() == "").all())

    cols = list(out.columns)

    # 1) --STRESC (char) / --STRESN (numeric) standardized results from --ORRES.
    for c in cols:
        if c.endswith("STRESC"):
            base = c[:-6] + "ORRES"
            if base in out.columns and empty(c) and not empty(base):
                out[c] = out[base].astype("string")
                filled.append((c, f"Standardized result — copy of {base}"))
        elif c.endswith("STRESN"):
            base = c[:-6] + "ORRES"
            if base in out.columns and empty(c) and not empty(base):
                out[c] = pd.to_numeric(out[base], errors="coerce")
                filled.append((c, f"Numeric standardized result from {base}"))

    # Subject-keyed derivations must use the BUILT output's subject variable (USUBJID), not the raw
    # spine's subject column — otherwise the merge to DM/SE finds nothing.
    osubj = "USUBJID" if "USUBJID" in out.columns else _subj_col(out)
    if osubj is None or osubj not in out.columns:
        return filled
    subj = osubj

    # 2) --DY / --STDY / --ENDY = study day of the matching --DTC relative to DM.RFSTDTC.
    ref_dm = _load_built(study_id, "DM")
    refmap = None
    if ref_dm is not None:
        rs = "USUBJID" if "USUBJID" in ref_dm.columns else _subj_col(ref_dm)
        rf = next((x for x in ref_dm.columns if x.upper() == "RFSTDTC"), None)
        if rs and rf:
            refmap = ref_dm.drop_duplicates(subset=[rs]).set_index(ref_dm[rs].astype(str))[rf]
    if refmap is not None:
        rd = _sdtm_dtc(out[subj].astype(str).map(refmap))
        for c in cols:
            dtc = c[:-4] + "STDTC" if c.endswith("STDY") else c[:-4] + "ENDTC" if c.endswith("ENDY") else (c[:-2] + "DTC" if c.endswith("DY") else None)
            if dtc and dtc in out.columns and empty(c) and not empty(dtc):
                ed = _sdtm_dtc(out[dtc])
                dy = (ed - rd).dt.days
                dy = dy.where(dy < 0, dy + 1)   # SDTM: no day 0, on/after ref shifts +1
                out[c] = dy.astype("Int64")
                filled.append((c, f"Study day from {dtc} & DM.RFSTDTC"))

    # 3) EPOCH = the SE element interval that contains the record's reference date.
    if "EPOCH" in out.columns and empty("EPOCH"):
        se = _load_built(study_id, "SE")
        refc = (next((x for x in out.columns if x.upper() == "RFSTDTC"), None)
                or next((c for c in out.columns if c.endswith("STDTC")), None)
                or next((c for c in out.columns if c.endswith("DTC")), None))
        if se is not None and refc:
            ss = _subj_col(se)
            secol = {x.upper(): x for x in se.columns}
            if ss and "SESTDTC" in secol and "EPOCH" in secol:
                rec = _sdtm_dtc(out[refc])
                by_subj = {k: v for k, v in se.groupby(se[ss].astype(str))}
                vals = []
                for i, sid_ in out[subj].astype(str).items():
                    d = rec.iloc[i] if i < len(rec) else pd.NaT
                    v, g = pd.NA, by_subj.get(sid_)
                    if g is not None and pd.notna(d):
                        st = _sdtm_dtc(g[secol["SESTDTC"]])
                        en = _sdtm_dtc(g[secol["SEENDTC"]]) if "SEENDTC" in secol else st
                        for j in range(len(g)):
                            a = st.iloc[j]
                            b = en.iloc[j] if (j < len(en) and pd.notna(en.iloc[j])) else a
                            if pd.notna(a) and a <= d and (pd.isna(b) or d <= b):
                                v = g.iloc[j][secol["EPOCH"]]
                                break
                    vals.append(v)
                if any(pd.notna(x) for x in vals):
                    out["EPOCH"] = vals
                    filled.append(("EPOCH", "SE element interval containing the record date"))

    # VISITNUM from the Trial Visits (TV) domain: the spec maps it from sdtm.sv.visitnum, but SV often
    # leaves it unsourced, so VISITNUM is blank across every findings/events domain. TV carries the
    # planned VISIT↔VISITNUM mapping, so look VISITNUM up from VISIT when it's otherwise empty.
    if "VISITNUM" in out.columns and empty("VISITNUM") and "VISIT" in out.columns and not empty("VISIT"):
        tv = _load_built(study_id, "TV")
        if tv is not None and {"VISIT", "VISITNUM"} <= {str(c).upper() for c in tv.columns}:
            tvc = {str(c).upper(): c for c in tv.columns}
            norm = lambda s: s.astype("string").str.strip().str.upper()
            vmap = (tv.dropna(subset=[tvc["VISIT"]])
                      .assign(_k=lambda d: norm(d[tvc["VISIT"]]))
                      .drop_duplicates("_k").set_index("_k")[tvc["VISITNUM"]])
            mapped = norm(out["VISIT"]).map(vmap)
            if mapped.notna().any():
                out["VISITNUM"] = mapped.astype("Int64")
                filled.append(("VISITNUM", "Planned visit number from TV (matched on VISIT)"))
    return filled


def _assembled_base(domain: str):
    """If this domain's Prep produced an assembled <dom>_base (present in _ACTIVE_PREP) whose
    UPPERCASE columns are SDTM variable names — the ds-merge / stack / transpose output — return
    (frame, {UPPER columns}). The build then reads the domain's values straight from it, so the
    mapping spec never has to point at raw.<dom>_base (non-destructive Prep integration)."""
    dl = str(domain).lower()
    for name in (f"{dl}_base", f"{dl}base"):
        f = _ACTIVE_PREP.get(name)
        if f is not None:      # an empty base is an answer too: the domain has no records
            return f, {str(c).upper() for c in f.columns}
    return None, set()


def _bp_result_series(res: dict, out, src_series):
    """A build_exec `cond` result/source dict → a Series aligned to `out` (for Auto-Build's
    deterministic-blueprint override). Handles kind text / missing / var (a built column) / column."""
    kind = str((res or {}).get("kind") or "").lower()
    na = pd.Series(pd.NA, index=out.index, dtype="string")
    if kind == "missing" or not res:
        return na
    if kind == "text":
        return pd.Series(str(res.get("text", "")), index=out.index, dtype="string")
    if kind == "var":
        col = next((c for c in out.columns if str(c).upper() == str(res.get("var", "")).upper()), None)
        return out[col].astype("string") if col else na
    if kind == "column":
        s = src_series(f"{res.get('dataset', '')}.{res.get('column', '')}")
        if s is None:
            return na
        s = pd.Series(getattr(s, "values", s))
        s.index = out.index[: len(s)] if len(s) == len(out) else out.index
        return s.astype("string")
    return na


def _bp_cmp(s, op: str, val):
    """Row mask for a canonical `cond` operator on a string Series (mirrors build_exec._cond_expr)."""
    su = s.astype("string")
    blank = su.isna() | (su.str.strip() == "")
    if op == "missing":
        return blank
    if op == "notmissing":
        return ~blank
    U, V = su.str.strip().str.upper(), str(val).strip().upper()
    if op == "eq":
        return U == V
    if op == "ne":
        return U != V
    if op == "in":
        return U.isin([x.strip().upper() for x in str(val).split(",") if x.strip()])
    if op == "notin":
        return ~U.isin([x.strip().upper() for x in str(val).split(",") if x.strip()])
    if op == "contains":
        return su.str.contains(str(val), case=False, na=False)
    if op == "starts":
        return su.fillna("").str.upper().str.startswith(V)
    if op == "ends":
        return su.fillna("").str.upper().str.endswith(V)
    n = pd.to_numeric(su, errors="coerce")
    try:
        fv = float(val)
    except (TypeError, ValueError):
        fv = float("nan")
    return {"gt": n > fv, "lt": n < fv, "ge": n >= fv, "le": n <= fv}.get(op, U == V)


def _apply_blueprint_overrides(study_id: str, domain: str, out, src_series) -> list:
    """Auto-Build honours the SAME deterministic Build-Blueprint edits Domain Build does: replay the
    saved blocks flagged `_bp_apply` (conditional flags, constants, copied/coalesced sources) onto the
    built frame, so a blueprint edit like DTHFL='Y' when DTHDTC is not missing holds in BOTH paths.
    Only marked variables are touched — every other variable is left to its own recipe."""
    try:
        from .build_exec import _load_saved
        blocks = (_load_saved(study_id, domain) or {}).get("blocks") or []
    except Exception:  # noqa: BLE001
        return []
    applied = []
    carry = ("retain", "lag")
    # RETAIN / LAG read the finished values of other variables, so they replay last
    for b in sorted(blocks, key=lambda b: b.get("recipe") in carry):
        if not b.get("_bp_apply"):
            continue
        v = str(b.get("variable", "")).upper()
        if not v:
            continue
        a = b.get("args") or {}
        try:
            if b.get("mtype") == "constant":
                out[v] = str(b.get("value", ""))
            elif b.get("recipe") == "cond":
                res = _bp_result_series(a.get("else") or {"kind": "missing"}, out, src_series)
                for r in reversed(a.get("rules") or []):          # first listed rule wins (build_exec parity)
                    mask = _bp_cmp(_bp_result_series(r.get("src") or {}, out, src_series),
                                   r.get("op", "eq"), r.get("value", "")).fillna(False)
                    res = res.mask(mask, _bp_result_series(r.get("then") or {}, out, src_series))
                out[v] = res.values
            elif b.get("mtype") == "assign" and b.get("dataset") and b.get("column"):
                s = src_series(f"{b['dataset']}.{b['column']}")
                if s is None:
                    continue
                out[v] = pd.Series(getattr(s, "values", s)).astype("string").values
            elif b.get("recipe") in carry:
                from .build_exec import _carry_args
                src_, by_, ord_ = _carry_args(a)
                col = next((c for c in out.columns if str(c).upper() == src_["var"]), None)
                if col is None:
                    continue
                if b["recipe"] == "retain":
                    mode = "first" if str(a.get("mode") or "").lower() == "first" else "locf"
                    res = sas_runtime._sx_retain(out, out[col], by_, ord_, mode)
                else:
                    res = sas_runtime._sx_lag(out, out[col], by_, ord_, max(int(float(a.get("n") or 1)), 1),
                                              str(a.get("fn") or "lag").lower() == "dif")
                out[v] = res.values
            elif b.get("recipe") == "fn":
                # the recipe is a SAS expression (sas_functions) — evaluated, never exec'd
                tree, n = sas_functions.fn_tree(a)
                srcs = list(a.get("sources") or [])
                if n and not srcs:
                    continue
                cols = {f"__S{i}": _bp_result_series(sr, out, src_series) for i, sr in enumerate(srcs)}
                out[v] = sas_expr.evaluate(tree, cols, out.index).values
            else:
                continue
            applied.append(v)
        except Exception as exc:  # noqa: BLE001 — an override must never break the build
            logger.warning("blueprint override failed for %s.%s: %s", domain, v, exc)
    return applied


def _apply_knowledge_derivations(study_id: str, domain: str, out, src_series, spec_vars: set, skip: set) -> list:
    """Apply the CDISC-IG + company STANDARD derivations the app KNOWS (sdtm_knowledge) — so a rule
    like DTHFL='Y' when the subject died happens with ZERO user authoring, for every variable that
    has a standard derivation. Only touches variables the spec already includes; a human blueprint
    edit (`skip`) always wins. These are, by definition, a build that differs from the spec's own
    stated mapping — the caller flags them `changed_from_spec`."""
    from .sdtm_knowledge import domain_derivations, compile_cond_args
    applied = []
    for var, d in domain_derivations(domain).items():
        if var in skip or var not in out.columns:
            continue
        args, _ = compile_cond_args(d, spec_vars)
        if not args["rules"]:
            continue
        try:
            res = _bp_result_series(args.get("else") or {"kind": "missing"}, out, src_series)
            for r in reversed(args["rules"]):
                mask = _bp_cmp(_bp_result_series(r.get("src") or {}, out, src_series),
                               r.get("op", "eq"), r.get("value", "")).fillna(False)
                res = res.mask(mask, _bp_result_series(r.get("then") or {}, out, src_series))
            out[var] = res.values
            applied.append(var)
        except Exception as exc:  # noqa: BLE001 — a standard derivation must never break the build
            logger.warning("knowledge derivation failed for %s.%s: %s", domain, var, exc)
    return applied


def _sas_split_args(s: str) -> list:
    """Split a function's argument list on top-level commas (respecting nested parens/quotes)."""
    args, depth, buf, q = [], 0, "", ""
    for ch in s:
        if q:
            buf += ch
            if ch == q:
                q = ""
        elif ch in "'\"":
            q = ch; buf += ch
        elif ch == "(":
            depth += 1; buf += ch
        elif ch == ")":
            depth -= 1; buf += ch
        elif ch == "," and depth == 0:
            args.append(buf.strip()); buf = ""
        else:
            buf += ch
    if buf.strip():
        args.append(buf.strip())
    return args


def _eval_sas_expr(expr: str, out):
    """Evaluate a small SAS-style construction expression against the built frame `out` → a Series,
    or None if it isn't something we can execute (a prose standard, an unknown function, a missing
    column). Handles the common Gilead patterns: scan/strip/upcase/lowcase/compress/put/coalesce/
    substr, string literals, and bare column references (case-insensitive)."""
    e = str(expr or "").strip()
    if not e:
        return None
    m = re.fullmatch(r"'([^']*)'|\"([^\"]*)\"", e)          # string literal → constant
    if m:
        return pd.Series(m.group(1) if m.group(1) is not None else m.group(2), index=out.index, dtype="string")
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)", e, re.S)   # function call
    if m:
        fn, args = m.group(1).lower(), _sas_split_args(m.group(2))
        if not args:
            return None
        base = _eval_sas_expr(args[0], out)
        if fn in ("strip", "trim", "left") and base is not None:
            return base.astype("string").str.strip()
        if fn == "upcase" and base is not None:
            return base.astype("string").str.upper()
        if fn == "lowcase" and base is not None:
            return base.astype("string").str.lower()
        if fn == "compress" and base is not None:
            if len(args) > 1:
                chars = args[1].strip().strip("'\"")
                s = base.astype("string")
                for c in chars:
                    s = s.str.replace(c, "", regex=False)
                return s
            return base.astype("string").str.replace(r"\s+", "", regex=True)
        if fn in ("put", "input") and base is not None:
            return base.astype("string").str.strip()
        if fn == "scan" and base is not None and len(args) >= 2:
            try:
                n = int(float(args[1]))
            except (TypeError, ValueError):
                return None
            delim = args[2].strip().strip("'\"") if len(args) > 2 else " "
            idx = n - 1 if n > 0 else n                    # SAS scan is 1-based; negative = from the end
            parts = base.astype("string").str.split(re.escape(delim))
            return parts.str[idx]
        if fn in ("coalesce", "coalescec"):
            cols = [_eval_sas_expr(a, out) for a in args]
            cols = [c for c in cols if c is not None]
            if cols:
                return pd.concat(cols, axis=1).bfill(axis=1).iloc[:, 0]
            return None
        if fn == "substr" and base is not None and len(args) >= 2:
            try:
                start = int(float(args[1])) - 1
                ln = int(float(args[2])) if len(args) > 2 else None
            except (TypeError, ValueError):
                return None
            s = base.astype("string")
            return s.str.slice(start, start + ln if ln is not None else None)
        return None
    col = next((c for c in out.columns if str(c).upper() == e.upper()), None)   # bare column ref
    if col is not None:
        return out[col].astype("string")
    return None


def _apply_ct_standard(out, rows) -> list:
    """Enforce a CDISC general rule the standard owns: a coded variable's value must be its CT
    submission value. Map each variable that has a codelist to the CT value by matching the built
    value against the codelist's submission values OR decodes (case-insensitive) — so a spec-bound
    SEX='Female' becomes 'F'. Non-destructive: a value not in the codelist is left as-is (to flag)."""
    cmaps = _core_codelists()
    recoded = []
    for row in rows:
        var = str(row.get("Variable") or row.get("variable") or "").upper()
        cl = str(row.get("Codelist") or row.get("codelist") or "").strip().upper()
        if not var or var not in out.columns or not cl or cl not in cmaps:
            continue
        norm = {}
        for t in cmaps[cl]:
            v = str(t.get("value", "") or "").strip()
            if not v or v.lower() == "nan":
                continue
            norm[v.upper()] = v
            d = str(t.get("decode", "") or "").strip()
            if d and d.lower() != "nan":
                norm.setdefault(d.upper(), v)
        s = out[var].astype("string")
        mapped = s.str.strip().str.upper().map(norm)
        if mapped.notna().any():
            out[var] = mapped.where(mapped.notna(), s)
            recoded.append(var)
    return recoded


def _apply_construction_standards(study_id: str, domain: str, out) -> list:
    """Fill any variable the STUDY spec left EMPTY using the Gilead IG-CORE Construction Standard's
    executable expression (e.g. SITEID/INVID = scan(USUBJID,-2,'-')). Non-destructive: only touches a
    column that is entirely blank, and only when the standard actually evaluates. This is what makes
    the Construction Standards drive the build, not just document it."""
    try:
        from .sdtm_knowledge import construction_map
        cmap = construction_map(domain)
    except Exception:  # noqa: BLE001
        return []
    filled = []
    for var, expr in cmap.items():
        if var not in out.columns:
            continue
        col = out[var]
        blank = bool(col.isna().all() or (col.astype("string").fillna("").str.strip() == "").all())
        if not blank:
            continue
        try:
            s = _eval_sas_expr(expr, out)
        except Exception:  # noqa: BLE001
            s = None
        if s is not None and (s.astype("string").fillna("").str.strip() != "").any():
            out[var] = s.values
            filled.append(var)
    return filled


def _apply_study_sas(domain: str, rows: list, out, skip: set) -> list:
    """Deterministically honor a STUDY spec row's own `Implemented SAS Code`, evaluated with the SAME
    safe evaluator the Construction Standards use (`_eval_sas_expr`) and bound to THIS study's built
    frame `out`. The study's explicit per-variable derivation is the authority for that column when it
    is a simple expression (scan/substr/strip/upcase/compress/put/coalesce, string literal or a bare
    column reference), with or without a leading `VAR =` assignment; complex SAS the evaluator can't
    parse is left to the recipe / other tiers (returns None → no-op). Non-destructive to human edits
    (`skip`) and only applies when the expression evaluates to a value. Returns the vars it set."""
    applied = []
    for rw in rows:
        var = str(rw.get("Variable") or rw.get("variable") or "").upper()
        if not var or var in skip or var not in out.columns:
            continue
        sas = str(rw.get("Implemented SAS Code") or rw.get("implemented_sas_code") or "").strip()
        if not sas:
            continue
        expr = sas
        m = re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.+?);?\s*$", expr, re.S)  # drop "VAR =" + trailing ;
        if m:
            expr = m.group(1).strip()
        expr = expr.rstrip(";").strip()
        try:
            s = _eval_sas_expr(expr, out)
        except Exception:  # noqa: BLE001 — a study expression must never break the build
            s = None
        if s is not None and (s.astype("string").fillna("").str.strip() != "").any():
            out[var] = s.values
            applied.append(var)
    return applied


def build_domain(study_id: str, domain: str, rows: list, studyid: str = "",
                 standards_version: str | None = None) -> dict:
    """Build one SDTM domain from its spec rows + raw source. Returns df, supp, lineage,
    per-variable block results, and a conformance score. `standards_version` selects which
    Construction Standards snapshot the auto-derivations/fills resolve against (default = current
    released)."""
    from . import sdtm_knowledge as _K
    _sv_token = _K.set_active_version(standards_version)
    try:
        return _build_domain_impl(study_id, domain, rows, studyid)
    finally:
        _K.reset_active_version(_sv_token)


def _build_domain_impl(study_id: str, domain: str, rows: list, studyid: str = "") -> dict:
    recipes = [r for r in (compile_recipe(row, domain) for row in rows) if r]
    parent = [r for r in recipes if not r["supp"]]
    supp_recipes = [r for r in recipes if r["supp"]]

    # Spine = the raw file referenced by the most copy/date recipes.
    from collections import Counter
    files = Counter()
    for r in parent:
        if r["op"] in ("copy", "iso_date", "study_day"):
            for s in r["sources"]:
                files[s.split(".")[0]] += 1
    spine_file = files.most_common(1)[0][0] if files else None
    spine = _load_source(study_id, spine_file) if spine_file else None

    # Non-destructive Prep integration: if Prep assembled a <dom>_base, build FROM it (the base is
    # the domain's records) — the spec keeps its real raw sources and is never rewritten.
    base_frame, base_cols = _assembled_base(domain)
    if base_frame is not None and base_frame.empty:
        # the prep ran and assembled no records (TEST-001 DA: its result columns are empty) — the
        # domain is empty; falling back to a raw form would invent records with no test or result
        return {"domain": domain, "df": pd.DataFrame(), "supp_df": None, "lineage": {},
                "blocks": [{"variable": r["var"], "ok": True, "op": "empty base"} for r in parent],
                "score": 0, "rows": 0,
                "note": f"the prepared {domain.lower()}_base has no records, so {domain} has none"}
    if base_frame is not None:
        spine_file, spine = f"{domain.lower()}_base", base_frame

    lineage, blocks = {}, []
    if spine is None or spine.empty:
        note = (f"{domain} maps only derived / cross-domain variables — no raw column source in the spec "
                f"to build rows from. Assign a source dataset or derive it in Domain Build."
                if not spine_file else
                f"source dataset for {domain} not found (expected raw file '{spine_file}'). "
                f"Upload it or generate synthetic data.")
        return {"domain": domain, "df": pd.DataFrame(), "supp_df": None, "lineage": {},
                "blocks": [{"variable": r["var"], "ok": False, "error": "no source data"} for r in parent],
                "score": 0, "rows": 0, "note": note}

    spine = spine.copy()
    subj = _subj_col(spine)
    if domain.upper() == "DM" and subj:
        spine = spine.drop_duplicates(subset=[subj]).reset_index(drop=True)
    nrows = len(spine)
    cols_env = {c.upper(): spine[c] for c in spine.columns}
    out = pd.DataFrame(index=spine.index)

    def _src_series(tok_str):
        """Series for a 'file.COL' source token, cross-file merged onto the spine by subject."""
        _, f, col = _tok("raw." + tok_str)
        if f == spine_file or f is None:
            return cols_env.get(col)
        other = _load_source(study_id, f)
        if other is None or col not in [c.upper() for c in other.columns]:
            return None
        osubj = _subj_col(other)
        if not (osubj and subj):
            return None
        o = other.drop_duplicates(subset=[osubj])  # one row/subject for a clean per-subject lookup
        m = o.set_index(o[osubj].astype(str))[col]
        return spine[subj].astype(str).map(m).reset_index(drop=True)

    ref_dm = None
    if any(r["op"] == "study_day" for r in parent):
        ref_dm = _load_built(study_id, "DM")

    # spec-SAS expressions read other variables of this domain, so they run after them
    for r in sorted(parent, key=lambda r: r["op"] == "sas_expr"):
        var = r["var"]
        try:
            # Prep-base wins: if the assembled base supplies this SDTM variable, take it verbatim
            # (the ds-merge / stack already derived it per the standard) — no spec rewrite needed.
            bcol = (next((c for c in spine.columns if str(c).upper() == var.upper()), None)
                    if base_cols and var.upper() in base_cols and var.upper() not in ("STUDYID", "DOMAIN")
                    and not var.upper().endswith("SEQ") else None)
            if bcol is not None:
                out[var] = spine[bcol].values
                blocks.append({"variable": var, "ok": True, "op": "prep-base"})
                lineage[var] = {"op": "prep-base", "sources": [f"{spine_file}.{var}"], "method": "PREP"}
                continue
            if r["op"] == "const":
                out[var] = r["value"]
            elif r["op"] == "studyid":
                out[var] = cols_env.get("STUDYID", pd.Series([studyid or study_id[:12]] * nrows, index=spine.index))
            elif r["op"] == "copy":
                # Coalesce the curated source chain (primary + extras, chosen in compile_recipe) in
                # order, first non-blank per row. _src_series resolves each token onto the spine —
                # same-dataset row-aligned, cross-dataset mapped by subject — so a cross-dataset value
                # source (COUNTRY ← ixrsrand.country) lands correctly. A token that can't resolve (no
                # subject key, e.g. an investigator-keyed form) is skipped, not fatal.
                s = None
                for ext in [r["src"]] + list(r.get("extra") or []):
                    es = _src_series(f"{ext[1]}.{ext[2]}")
                    if es is None:
                        continue
                    es = es.astype("string")
                    s = es if s is None else s.mask(s.isna() | (s.str.strip() == ""), es)
                if s is None:
                    raise ValueError(f"source {r['src'][1]}.{r['src'][2]} not found")
                if isinstance(r.get("recode"), dict) and r["recode"]:
                    ct = {str(k).upper(): v for k, v in r["recode"].items()}
                    s = s.astype("string").str.strip().str.upper().map(ct).fillna(s)
                out[var] = s.values
            elif r["op"] == "iso_date":
                s = _src_series(f'{r["sources"][0].split(".")[0]}.{r["sources"][0].split(".")[1]}') if r["sources"] else None
                out[var] = _iso(s).values if s is not None else ""
            elif r["op"] == "study_day":
                ev = _src_series(f'{r["sources"][0].split(".")[0]}.{r["sources"][0].split(".")[1]}') if r["sources"] else None
                if ev is not None and ref_dm is not None and subj and _subj_col(ref_dm):
                    rs = _subj_col(ref_dm)
                    refmap = ref_dm.drop_duplicates(subset=[rs]).set_index(ref_dm[rs].astype(str)).get("RFSTDTC")
                    ref = spine[subj].astype(str).map(refmap) if refmap is not None else None
                    # `ev` is a raw source column (unknown format), `ref` is
                    # DM.RFSTDTC and therefore ISO. See _sdtm_dtc.
                    ed = pd.to_datetime(ev, errors="coerce")
                    rd = _sdtm_dtc(ref) if ref is not None else pd.Series(pd.NaT, index=ed.index)
                    dy = (ed - rd).dt.days
                    dy = dy.where(dy < 0, dy + 1)  # no day 0: on/after ref shifts +1
                    out[var] = dy.astype("Int64").values
                else:
                    out[var] = pd.NA
            elif r["op"] == "sas_if":
                _ch = sas_expr.chains_from_json(r["chains"])
                env = {n: _src_series(f"{r['dataset']}.{n}") for n in sas_expr.chain_names(_ch)}
                out[var] = sas_expr.evaluate_chains(_ch, {k: v for k, v in env.items() if v is not None},
                                                    out.index).values
            elif r["op"] == "sas_expr":
                out[var] = sas_expr.evaluate(sas_expr.parse(r["sas"]),
                                             {**cols_env, **{c: out[c] for c in out.columns}}, out.index).values
            elif r["op"] == "expr":
                ok, reason = validate(r["expr"])
                if not ok:
                    raise ValueError(reason)
                out[var] = safe_eval(r["expr"], cols_env, nrows).values
            elif r["op"] == "seq":
                out[var] = 0  # numbered after sort (below)
            else:
                out[var] = pd.NA
            blocks.append({"variable": var, "ok": True, "op": r["op"]})
            lineage[var] = {"op": r["op"], "sources": r["sources"], "method": r["method"]}
        except Exception as e:  # noqa: BLE001 — translate, mark block failed (deterministic, isolated)
            out[var] = pd.NA
            blocks.append({"variable": var, "ok": False, "op": r["op"], "error": str(e)})
            lineage[var] = {"op": r["op"], "sources": r["sources"], "error": str(e)}

    # Identifier safety net — USUBJID must ALWAYS populate. A findings/events spec often ASSIGNs
    # USUBJID from a merge-program variable (e.g. SAS strip(usubjid), no Input Variables), so the
    # recipe has no source and USUBJID comes out blank — which then starves --SEQ, --DY and EPOCH of
    # the subject key. When that happens, derive it from the spine's own subject id: the fully-qualified
    # X_SUBJID when present (that IS the USUBJID), else STUDYID-SUBJID. Only when empty; never clobbers.
    if "USUBJID" in out.columns and subj:
        _u = out["USUBJID"]
        if _u.isna().all() or (_u.astype("string").fillna("").str.strip() == "").all():
            _xs = cols_env.get("X_SUBJID")
            if _xs is not None and (_xs.astype("string").fillna("").str.strip() != "").any():
                out["USUBJID"] = _xs.astype("string").str.strip().values
                blocks.append({"variable": "USUBJID", "ok": True, "op": "auto-id"})
                lineage["USUBJID"] = {"op": "auto-id", "sources": [f"{spine_file}.X_SUBJID"], "method": "STANDARD"}
            else:
                _sc = cols_env.get("SUBJID")
                if _sc is None and subj:
                    _sc = cols_env.get(subj.upper())
                if _sc is not None and (_sc.astype("string").fillna("").str.strip() != "").any():
                    _sidv = (out["STUDYID"] if "STUDYID" in out.columns
                             and not out["STUDYID"].astype("string").fillna("").eq("").all()
                             else pd.Series([studyid or study_id[:12]] * nrows, index=spine.index))
                    out["USUBJID"] = (_sidv.astype("string").str.strip() + "-"
                                      + _sc.astype("string").str.strip()).values
                    blocks.append({"variable": "USUBJID", "ok": True, "op": "auto-id"})
                    lineage["USUBJID"] = {"op": "auto-id", "sources": [f"{spine_file}.SUBJID"],
                                          "method": "STANDARD"}

    # Auto-fill the STANDARD derived variables the spec need not spell out — so the build populates
    # them with zero user authoring (the "review-only" contract). Deterministic, safe, never
    # overwrites a real value; records each fill in `lineage` so the reviewer can see how it was built.
    try:
        for var, method in _fill_standard_derivations(out, domain, study_id, subj):
            blocks.append({"variable": var, "ok": True, "op": "auto-derived"})
            lineage[var] = {"op": "auto-derived", "sources": [], "method": method}
    except Exception as exc:  # noqa: BLE001 — a derivation bug must never break the build
        logger.warning("standard-derivation pass failed for %s: %s", domain, exc)

    # Knowledge-driven STANDARD derivations (CDISC IG + company) the app applies with zero user
    # authoring — e.g. DTHFL='Y' when the subject died. Runs before the human-edit pass so an explicit
    # blueprint edit still wins. Each is, by construction, a build that differs from the spec's stated
    # mapping → flagged `changed_from_spec` for the reviewer (the spec itself is never rewritten).
    _spec_vars = {str(rw.get("Variable") or rw.get("variable") or "").upper() for rw in rows}
    try:
        from .build_exec import _load_saved as _ls
        _edited = {str(b.get("variable", "")).upper()
                   for b in (_ls(study_id, domain) or {}).get("blocks", []) if b.get("_bp_apply")}
    except Exception:  # noqa: BLE001
        _edited = set()

    # Study-authored Implemented SAS Code — deterministic, study-specific. Evaluated with the same safe
    # evaluator the Construction Standards use, so Auto-Build honors the study's own per-variable rule
    # where it's a simple expression. It wins over the default recipe, loses to a human blueprint edit,
    # and is protected from the global standard passes below (added to `_edited`) so the study's rule is
    # what builds.
    for var in _apply_study_sas(domain, rows, out, _edited):
        blocks.append({"variable": var, "ok": True, "op": "study-sas"})
        lineage[var] = {"op": "study-sas", "sources": [], "method": "STUDY_SAS"}
        _edited.add(var)

    for var in _apply_knowledge_derivations(study_id, domain, out, _src_series, _spec_vars, _edited):
        blocks.append({"variable": var, "ok": True, "op": "knowledge"})
        lineage[var] = {"op": "knowledge", "sources": [], "method": "STANDARD", "changed_from_spec": True}

    # Construction Standards drive the build: fill any variable the STUDY spec left empty from the
    # Gilead IG-CORE standard (e.g. SITEID/INVID = scan(USUBJID,-2,'-')). Runs after the spec recipes
    # and standard/knowledge fills, before the human blueprint edit (which always wins).
    for var in _apply_construction_standards(study_id, domain, out):
        blocks.append({"variable": var, "ok": True, "op": "standard"})
        lineage[var] = {"op": "standard", "sources": [], "method": "GILEAD-IG-CORE"}

    # Deterministic Build-Blueprint edits (conditional flags / constants / copied sources) — replay
    # them so Auto-Build stays in sync with Domain Build. Runs after the standard fills so an explicit
    # human edit wins, and before finalize so the values sort/dedup with everything else.
    for var in _apply_blueprint_overrides(study_id, domain, out, _src_series):
        blocks.append({"variable": var, "ok": True, "op": "blueprint"})
        lineage[var] = {"op": "blueprint", "sources": [], "method": "BLUEPRINT", "changed_from_spec": True}

    # Standard CDISC rule (owned by the Construction Standard, applied to the study-bound value):
    # a coded variable must carry its CT submission value — e.g. SEX 'Female' → 'F'.
    for var in _apply_ct_standard(out, rows):
        lin = lineage.get(var) or {}
        lin["ct_standardized"] = True
        lineage[var] = lin

    # Finalize: order columns per spec; sort by the spec's Key Seq keys; --SEQ last.
    order = [r["var"] for r in parent]
    out = out.reindex(columns=[c for c in order if c in out.columns])
    subj_out = _subj_col(out)
    # Sort keys from the core spec's "Key Seq" column (ascending), else subject + --DTC.
    key_seq = {}
    for row in rows:
        v = str(row.get("Variable") or row.get("variable") or "").strip().upper()
        ks = row.get("Key Seq")
        try:
            if v and ks not in (None, "") and not (isinstance(ks, float) and pd.isna(ks)):
                key_seq[v] = float(ks)
        except (TypeError, ValueError):
            pass
    sort_cols = [v for v, _ in sorted(key_seq.items(), key=lambda kv: kv[1]) if v in out.columns]
    if not sort_cols and subj_out:
        sort_cols = [subj_out] + [c for c in out.columns if c.endswith("DTC")][:1]
    if sort_cols:
        out = out.sort_values(sort_cols, kind="stable").reset_index(drop=True)
    seq_var = next((r["var"] for r in parent if r["op"] == "seq"), None)
    if seq_var and subj_out:
        out[seq_var] = out.groupby(subj_out).cumcount().add(1).astype("Int64")

    # Biomedical Concepts — deterministic findings enrichment: fill --TEST from the concept's standard
    # name for each --TESTCD and --ORRESU from the standard unit (blanks only). Makes VSTEST/LBTEST/…
    # right by construction instead of relying on an AI-guessed transpose.
    if domain.upper() in _FINDINGS_CLASS:
        try:
            from .biomedical_concepts import apply_to_findings
            out, _bc_fixed = apply_to_findings(out, domain)
        except Exception as exc:  # noqa: BLE001 — BC enrichment must never break the build
            logger.warning("BC enrichment failed for %s: %s", domain, exc)

    # SUPPQUAL — transpose QNAM columns onto the standard SUPP structure.
    supp_df = _build_supp(domain, out, spine, supp_recipes, spine_file, study_id, studyid, subj_out)

    score, checks = _conformance(domain, out, seq_var, subj_out)
    row_by_var = {str(rw.get("Variable") or rw.get("variable") or "").upper(): rw for rw in rows}
    recipe_view = []
    for r in recipes:
        rw = row_by_var.get(r["var"], {})
        recipe_view.append({
            "var": r["var"], "op": r["op"], "label": r.get("label", ""),
            "sources": r["sources"], "supp": r["supp"], "expr": r.get("expr", ""),
            "codelist": str(rw.get("Codelist") or "").strip(),
            "key_seq": rw.get("Key Seq"),
            "rule": str(rw.get("Mapping Rule") or "").strip(),
            "origin": str(rw.get("Origin") or "").strip(),
            "core": str(rw.get("CDISC Core") or rw.get("Gilead Core") or "").strip(),
        })
    finalize = {"sort_keys": sort_cols, "seq_var": seq_var,
                "dedup": domain.upper() == "DM", "subject_key": subj_out}
    return {"domain": domain, "df": out, "supp_df": supp_df, "lineage": lineage, "blocks": blocks,
            "recipes": recipe_view, "spine": spine_file, "finalize": finalize,
            "rows": int(len(out)), "cols": int(out.shape[1]), "score": score, "checks": checks,
            "supp_rows": int(len(supp_df)) if supp_df is not None else 0}


def _build_supp(domain, out, spine, supp_recipes, spine_file, study_id, studyid, subj_out):
    if not supp_recipes or not subj_out or "USUBJID" not in out.columns:
        return None
    seq_col = next((c for c in out.columns if c.endswith("SEQ")), None)
    recs = []
    for r in supp_recipes:
        qnam = r["var"]
        src = r["sources"][0] if r["sources"] else None
        if not src:
            continue
        f, col = src.split(".")[0], src.split(".")[1].upper()
        base = spine if f == spine_file else _load_source(study_id, f)
        if base is None or col not in [c.upper() for c in base.columns]:
            continue
        for i, row in out.iterrows():
            val = spine.iloc[i][col] if (f == spine_file and i < len(spine)) else None
            if val is None or str(val).strip() in ("", "nan", "None"):
                continue
            recs.append({"STUDYID": out.get("STUDYID", pd.Series([studyid])).iloc[i] if "STUDYID" in out else studyid,
                         "RDOMAIN": domain, "USUBJID": row["USUBJID"],
                         "IDVAR": seq_col or "", "IDVARVAL": str(row[seq_col]) if seq_col else "",
                         "QNAM": qnam, "QLABEL": r["label"], "QVAL": str(val),
                         "QORIG": "CRF", "QEVAL": ""})
    return pd.DataFrame(recs, columns=_SUPP_COLS) if recs else None


# ── Conformance / quality score ──────────────────────────────────────────────
def _conformance(domain, df, seq_var, subj_out):
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    add("DOMAIN value", "DOMAIN" in df.columns and (df["DOMAIN"] == domain).all() if "DOMAIN" in df.columns else False,
        f"DOMAIN = '{domain}'" if "DOMAIN" in df.columns else "DOMAIN column missing")
    for key in ("STUDYID", "USUBJID"):
        add(f"{key} present & non-null", key in df.columns and df[key].notna().all(),
            f"{key} " + ("populated" if key in df.columns else "missing"))
    if seq_var:
        uniq = df.groupby(subj_out)[seq_var].apply(lambda s: s.is_unique).all() if subj_out else True
        add(f"{seq_var} unique within subject", bool(uniq), "sequence numbers unique per subject")
    # populated columns (not all-null)
    if len(df):
        filled = float((df.notna().any() & (df.astype(str) != "").any()).mean()) if len(df.columns) else 1.0
        add("Columns populated", filled >= 0.6, f"{filled*100:.0f}% of columns have data")
    passed = sum(1 for c in checks if c["status"] == "pass")
    score = round(100 * passed / len(checks)) if checks else 0
    return score, checks


# ── Persist ──────────────────────────────────────────────────────────────────
def _persist(study_id, domain, df):
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)
    key = f"sdtm/output/{study_id}/{domain}.parquet"
    _s3().put_object(Bucket=settings.S3_BUCKET, Key=key, Body=buf.getvalue())
    preview = [{c: (None if pd.isna(v) else (int(v) if isinstance(v, float) and float(v).is_integer() else v))
                for c, v in r.items()} for _, r in df.head(100).iterrows()]
    db = get_db()
    try:
        db.execute("INSERT INTO sdtm_output (study_id, domain, row_count, file_key, format, build_status, data_preview, built_by) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                   (study_id, domain.upper(), int(len(df)), key, "parquet", "complete", json.dumps(preview), "build_engine"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("sdtm_output insert failed for %s: %s", domain, exc)
    return key


# ── Orchestration ────────────────────────────────────────────────────────────
def _studyid(study_id):
    db = get_db()
    r = db.execute("SELECT protocol_number, study_name FROM studies WHERE study_id=%s", (study_id,))
    return ((r[0].get("protocol_number") or r[0].get("study_name") or study_id) if r else study_id).replace(" ", "")[:20]


def _domain_prep_frames(study_id: str, domain: str) -> dict:
    """Run the domain's saved Prep pipeline (from Domain Build) and return its output frames keyed by
    lowercased name, so Auto-Build's _load_source can resolve them. Best-effort — no prep = {}."""
    try:
        from .build_exec import _load_saved
        from .prep_engine import run_prep
        steps = (_load_saved(study_id, domain) or {}).get("prep_steps") or []
        frames = run_prep(study_id, steps, persist=True).get("frames") or {} if steps else {}
        out = {str(k).lower(): v for k, v in frames.items() if v is not None}
        if out:
            logger.info("Auto-Build ran %d prep step(s) for %s → outputs: %s", len(steps), domain, list(out))
    except Exception as exc:  # noqa: BLE001 — a prep failure must not break the build
        logger.warning("prep pipeline for %s failed: %s", domain, exc)
        out, steps = {}, []
    # Deterministic findings wide→tall transpose when the spec left --TESTCD unsourced (one row per
    # test from wide result columns, e.g. RS). It is the CORRECT base for such a domain and WINS over a
    # naive stack base an earlier auto-prep may have saved under the same <dom>_base name (stacking
    # heterogeneous files can't produce --TESTCD rows). _auto_findings_base returns {} for already-tall
    # findings (--TESTCD sourced) and non-findings, so this never disturbs them. It runs ONLY as a
    # FALLBACK: when the SAVED pipeline already carries a transpose_findings step for <dom>_base (the
    # prep router now persists one), that saved step produced the base above and re-running here would
    # be redundant — so skip it. Studies whose saved prep predates the persisted transpose (stack-only)
    # still get the on-the-fly override, keeping Auto-Build output identical for them.
    base_name = f"{str(domain).lower()}_base"
    saved_transpose = any(str(s.get("op")) == "transpose_findings"
                          and str(s.get("name") or "").strip().lower() == base_name
                          for s in (steps or []))
    if not saved_transpose:
        try:
            spec = load_spec(study_id)
            specs = spec.get("specs") or {}
            rows = specs.get(str(domain).upper()) or specs.get(str(domain)) or []
            auto = _auto_findings_base(study_id, domain, rows)
            if auto:
                out.update(auto)   # transpose base overrides any stack base of the same name
                logger.info("Auto-Build findings transpose (fallback) for %s → %d test rows",
                            domain, len(next(iter(auto.values()))))
        except Exception as exc:  # noqa: BLE001
            logger.warning("findings transpose check for %s failed: %s", domain, exc)
    return out


def build_all(study_id: str, domains: list | None = None, persist: bool = True,
              standards_version: str | None = None) -> dict:
    """Auto-Build: DAG-order + build every domain from the spec + raw source. `standards_version`
    pins the whole run to one Construction Standards snapshot (default = current released)."""
    from . import sdtm_knowledge as _K
    _sv_token = _K.set_active_version(standards_version)
    try:
        return _build_all_impl(study_id, domains, persist, standards_version)
    finally:
        _K.reset_active_version(_sv_token)


def _build_all_impl(study_id: str, domains: list | None, persist: bool,
                    standards_version: str | None) -> dict:
    spec = load_spec(study_id)
    specs = spec.get("specs") or {}
    if not specs:
        return {"error": "No mapping spec found. Run Mapping first."}
    active_domains = active_mapping_domains(study_id, spec)
    _SOURCE_CACHE.clear()
    sid = _studyid(study_id)
    order = [domain for domain in build_order(specs) if domain.upper() in active_domains]
    if domains:
        want = {d.upper() for d in domains}
        order = [d for d in order if d.upper() in want]
    results, total_score = [], []
    for dom in order:
        # Run this domain's saved Prep pipeline (transpose / stack / merge) first, so a reshape set up
        # once in Domain Build takes effect in Auto-Build too. Its outputs resolve as sources below.
        _ACTIVE_PREP.clear()
        _ACTIVE_PREP.update(_domain_prep_frames(study_id, dom))
        res = build_domain(study_id, dom, specs[dom], sid, standards_version=standards_version)
        _ACTIVE_PREP.clear()
        if persist and res["rows"]:
            res["file_key"] = _persist(study_id, dom, res["df"])
            if res.get("supp_df") is not None and len(res["supp_df"]):
                _persist(study_id, f"SUPP{dom.upper()}", res["supp_df"])
        results.append({k: v for k, v in res.items() if k not in ("df", "supp_df")})
        if res["rows"]:
            total_score.append(res["score"])
            if persist:   # auto-generate + save the build programs (Python/SAS) for this domain
                try:
                    from .program_gen import generate_for_domain
                    generate_for_domain(study_id, dom)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("program gen failed for %s: %s", dom, exc)
    # Trial-design domains (TA/TE/TI/TV/TS) have no raw source, so the raw loop above skips them
    # (build_order filters _TRIAL_DESIGN). Build them from the trial_design store instead and mark
    # their spec rows, so they land in SDTM Output + the Mapping Spec TOC + export just like the rest.
    td_order = []
    if not domains or any(d.upper() in _TRIAL_DESIGN for d in domains):
        try:
            from .trial_design_build import sync_trial_design
            td_built = sync_trial_design(study_id, "build_engine", persist)
            for dom, info in td_built.items():
                if domains and dom.upper() not in {d.upper() for d in domains}:
                    continue
                td_order.append(dom)
                results.append({"domain": dom, "rows": info["rows"], "score": 100,
                                "status": "complete", "source": "trial_design"})
                total_score.append(100)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trial-design build skipped: %s", exc)
    order = order + td_order
    overall = round(sum(total_score) / len(total_score)) if total_score else 0
    manifest = {"study_id": study_id, "studyid": sid, "order": order,
                "domains": results, "overall_score": overall,
                "built": sum(1 for r in results if r["rows"]),
                "created_at": datetime.utcnow().isoformat()}
    if persist:
        _s3().put_object(Bucket=settings.S3_BUCKET, Key=f"sdtm/output/{study_id}/build_manifest.json",
                         Body=json.dumps(manifest).encode())
    return {"ok": True, **manifest}


def build_manifest(study_id: str) -> dict:
    try:
        obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key=f"sdtm/output/{study_id}/build_manifest.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return {"study_id": study_id, "domains": []}
