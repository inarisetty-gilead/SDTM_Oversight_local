"""Standard derivations lifted from the company SAS templates (SDTM-Designer:
dm-merge_normalized_template.sas and siblings).

These fill variables the spec leaves WITHOUT a workable mapping — never ones the spec or the
reader has already mapped — and only when the inputs they need actually exist in this study.
Every application is labelled (`method_source = "template"`) with the template it came from,
so the reader sees exactly what was assumed and can replace it like any other mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .blocks import Block
from .util import upper

# raw column spellings the templates read
REPORTED_AGE = ("AGE_REP", "AGE", "AGEYR", "AGE_YEARS")
AGE_UNIT = ("AGEU", "_AGEU", "AGE_UNIT")
DEATH_DATE = ("DEATHDAT_RAW", "DEATHDAT", "DTHDAT_RAW", "DTHDAT", "DTHDTC", "DEATHDT")
EXPOSURE_START = ("EXSTDAT", "EXSTDTC", "DOSSTDAT", "DOSESTDAT", "DOSEDT", "EXDAT", "DOSDAT")
EXPOSURE_END = ("EXENDAT", "EXENDTC", "DOSENDAT", "DOSEENDAT")
VISIT_DATE = ("VISDAT", "VISITDAT", "SVSTDAT", "VISDTC", "SVDAT")
CONSENT_DATE = ("ICDAT", "ICDTC", "CONSDAT", "CONSENTDT", "RFICDAT", "MAINCONSDAT", "CONSDTC")
DISPOSITION_DATE = ("DSSTDAT", "DSSTDTC", "DSDAT", "COMPDAT", "EOSDAT")


def _date_sources(store, *column_banks) -> list[dict]:
    """Every (dataset, date column) in the study carrying one of these columns — the
    sources a per-subject earliest/latest date pools over."""
    out, seen = [], set()
    for bank in column_banks:
        for name in sorted(store.refs):
            cols = {upper(c): c for c in store.columns(name)}
            for want in bank:
                if want in cols and (name, want) not in seen:
                    seen.add((name, want))
                    out.append({"dataset": name, "date_col": cols[want]})
    return out


def _named(sources: list[dict]) -> str:
    return ", ".join(f"{x['dataset']}.{x['date_col']}" for x in sources)


def _find_source(store, column_names) -> tuple[str, str] | None:
    """The first (dataset, column) in the study carrying one of these columns."""
    for name in sorted(store.refs):
        cols = {upper(c): c for c in store.columns(name)}
        for want in column_names:
            if want in cols:
                return name, cols[want]
    return None


@dataclass
class Template:
    variable: str
    domains: tuple[str, ...]
    source: str                       # which SAS template it comes from
    describe: str
    apply: Callable                   # (block, blocks_by_var, store, base_ds) -> str | None


def _apply_age(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    # The one template that also UPGRADES a spec mapping: a spec that assigns AGE from a
    # reported-age column has named the template's first branch and silently dropped its
    # second — every uncollected age would stay blank. Keep the spec's column as primary
    # and add the derivation underneath it, exactly as the SAS template does.
    spec_assigned = (b.mtype == "assign" and upper(b.column) in REPORTED_AGE)
    if not (b.mtype == "unmapped" or spec_assigned):
        return None
    has_birth = "BRTHDTC" in by_var and by_var["BRTHDTC"].mtype not in ("drop", "unmapped")
    has_ref = "RFSTDTC" in by_var and by_var["RFSTDTC"].mtype not in ("drop", "unmapped")
    base_cols = {upper(c) for c in store.columns(store.resolve(base_ds) or "")}
    reported = (upper(b.column) if spec_assigned
                else next((c for c in REPORTED_AGE if c in base_cols), ""))
    reported_ds = b.dataset if spec_assigned else ""
    if not (reported or (has_birth and has_ref)):
        return None
    b.mtype, b.recipe = "derived", "age"
    b.dataset, b.column = "", ""
    b.args = {"age_col": reported, "age_dataset": reported_ds,
              "birth_var": "BRTHDTC", "ref_var": "RFSTDTC"}
    if reported and has_birth and has_ref:
        return (f"the reported age ({reported}), with whole years BRTHDTC→RFSTDTC filling "
                "the records where none was collected")
    if reported:
        return f"the reported age ({reported})"
    return "whole years from BRTHDTC to RFSTDTC, anniversary rule"


def _apply_ageu(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    if b.mtype != "unmapped":
        return None
    age = by_var.get("AGE")
    if age is None or age.mtype in ("drop", "unmapped"):
        return None
    base_cols = {upper(c): c for c in store.columns(store.resolve(base_ds) or "")}
    unit = next((base_cols[c] for c in AGE_UNIT if c in base_cols), "")
    if unit:
        b.mtype, b.dataset, b.column = "assign", store.resolve(base_ds) or base_ds, unit
        return f"the collected unit ({unit})"
    b.mtype, b.value = "constant", "YEARS"
    return "the constant YEARS — ages here are derived in whole years"


def _apply_dthdtc(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    if b.mtype != "unmapped":
        return None
    found = _find_source(store, DEATH_DATE)
    if not found:
        return None
    ds, col = found
    b.mtype, b.dataset, b.column = "assign", ds, col
    return f"the death date collected in {ds}.{col}"


def _apply_dthfl(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    if b.mtype != "unmapped":
        return None
    found = _find_source(store, DEATH_DATE)
    if not found:
        return None
    ds, col = found
    b.mtype, b.recipe = "derived", "cond"
    b.args = {"rules": [{"src": {"dataset": ds, "column": col}, "op": "notmissing",
                         "value": "", "then": {"kind": "text", "text": "Y"}}],
              "else": {"kind": "missing"}}
    return f"'Y' when a death date exists in {ds}.{col}, blank otherwise"


def _extreme(b: Block, sources: list[dict], func: str) -> None:
    b.mtype, b.recipe = "derived", "date_extreme"
    b.dataset, b.column = "", ""
    b.args = {"func": func, "sources": sources}


def _apply_rfstdtc(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    if b.mtype != "unmapped":
        return None
    src = _date_sources(store, EXPOSURE_START)
    if not src:
        return None
    _extreme(b, src, "min")
    return f"the first study treatment exposure ({_named(src)})"


def _apply_rfxstdtc(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    return _apply_rfstdtc(b, by_var, store, base_ds)


def _apply_rfendtc(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    if b.mtype != "unmapped":
        return None
    src = _date_sources(store, VISIT_DATE) or _date_sources(store, EXPOSURE_END, EXPOSURE_START)
    if not src:
        return None
    _extreme(b, src, "max")
    return f"the last visit/contact date ({_named(src)})"


def _apply_rfxendtc(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    if b.mtype != "unmapped":
        return None
    src = _date_sources(store, EXPOSURE_END) or _date_sources(store, EXPOSURE_START)
    if not src:
        return None
    _extreme(b, src, "max")
    return f"the last study treatment exposure ({_named(src)})"


def _apply_rficdtc(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    if b.mtype != "unmapped":
        return None
    src = _date_sources(store, CONSENT_DATE)
    if not src:
        return None
    _extreme(b, src, "min")
    return f"the earliest informed-consent date ({_named(src)})"


def _apply_rfpendtc(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    if b.mtype != "unmapped":
        return None
    src = _date_sources(store, VISIT_DATE, EXPOSURE_END, DISPOSITION_DATE)
    if not src:
        return None
    _extreme(b, src, "max")
    return f"the last date of participation ({_named(src)})"


def _make_blfl(dom: str):
    """--BLFL: 'Y' on the last non-missing result on or before RFSTDTC, per subject and
    test — the SDTMIG baseline convention, run by the same engine as --LOBXFL."""
    def apply(b: Block, by_var: dict, store, base_ds: str) -> str | None:
        if b.mtype != "unmapped":
            return None
        tc, dtc = f"{dom}TESTCD", f"{dom}DTC"
        for need in (tc, dtc):
            v = by_var.get(need)
            if v is None or v.mtype in ("drop", "unmapped"):
                return None
        b.mtype, b.recipe = "derived", "lobxfl"
        b.args = {"testcd_var": tc, "dtc_var": dtc, "result_var": f"{dom}ORRES",
                  "ref_var": "RFSTDTC"}
        return (f"'Y' on the last non-missing {dom}ORRES on or before RFSTDTC, "
                f"per subject and {tc}")
    return apply


REGISTRY: list[Template] = [
    Template("AGE", ("DM",), "dm-merge template",
             "reported age, else whole years from birth to the reference start", _apply_age),
    Template("AGEU", ("DM",), "dm-merge template",
             "the collected age unit, else YEARS", _apply_ageu),
    Template("DTHDTC", ("DM",), "dm-merge template",
             "the collected death date", _apply_dthdtc),
    Template("DTHFL", ("DM",), "dm-merge template",
             "Y when a death date exists", _apply_dthfl),
    Template("RFSTDTC", ("DM",), "dm-merge template",
             "first study treatment exposure date", _apply_rfstdtc),
    Template("RFENDTC", ("DM",), "dm-merge template",
             "last visit or contact date", _apply_rfendtc),
    Template("RFXSTDTC", ("DM",), "dm-merge template",
             "first study treatment exposure date", _apply_rfxstdtc),
    Template("RFXENDTC", ("DM",), "dm-merge template",
             "last study treatment exposure date", _apply_rfxendtc),
    Template("RFICDTC", ("DM",), "dm-merge template",
             "earliest informed consent date", _apply_rficdtc),
    Template("RFPENDTC", ("DM",), "dm-merge template",
             "last date of participation", _apply_rfpendtc),
    Template("VSBLFL", ("VS",), "SDTMIG baseline convention",
             "Y on the last result on or before RFSTDTC", _make_blfl("VS")),
    Template("EGBLFL", ("EG",), "SDTMIG baseline convention",
             "Y on the last result on or before RFSTDTC", _make_blfl("EG")),
    Template("LBBLFL", ("LB",), "SDTMIG baseline convention",
             "Y on the last result on or before RFSTDTC", _make_blfl("LB")),
]


def apply_templates(blocks: list[Block], store, domain: str, base_ds: str,
                    overrides: dict | None = None) -> list[str]:
    """Fill unmapped variables from the template registry. Returns what was applied.

    `overrides` comes from the function library: per variable, {"enabled": False} keeps a
    template out of the build entirely, and {"edit": {...}} adjusts the derivation the
    template produced — the user saw it in the Functions section and changed it."""
    dom = upper(domain)
    overrides = {upper(k): v for k, v in (overrides or {}).items()}
    by_var = {b.variable: b for b in blocks}
    applied = []
    for t in REGISTRY:
        ov = overrides.get(t.variable) or {}
        if dom not in t.domains or ov.get("enabled") is False:
            continue
        b = by_var.get(t.variable)
        # each template guards its own applicability; the loop only enforces that a hand
        # edit is never touched
        if b is None or b.edited:
            continue
        note = t.apply(b, by_var, store, base_ds)
        if note is None:
            continue
        edit = ov.get("edit") or {}
        if edit:
            for k in ("dataset", "column", "value"):
                if edit.get(k) is not None:
                    setattr(b, k, edit[k])
            if edit.get("args"):
                b.args = {**(b.args or {}), **edit["args"]}
            note += " — adjusted by you in the function library"
        b.method_source = "template"
        b.confidence = 95
        b.reason = f"standard derivation from the {t.source}: {note}"
        applied.append(f"{t.variable} — {note}")
    return applied


def apply_custom_fns(blocks: list[Block], custom_fns: dict | None, domain: str) -> list[str]:
    """Apply the user's own function library. A custom function outranks a built-in template
    (the user wrote it deliberately) but never a spec mapping unless it says override — and a
    hand edit is never touched. Applications are labelled `custom` so they read as the
    user's rule, not the spec's."""
    dom = upper(domain)
    by_var = {b.variable: b for b in blocks}
    applied = []
    for fn in (custom_fns or {}).values():
        if not fn.get("enabled", True):
            continue
        doms = [upper(d) for d in (fn.get("domains") or [])]
        if doms and dom not in doms:
            continue
        b = by_var.get(upper(fn.get("variable")))
        if b is None or b.edited:
            continue
        spec_mapped = b.mtype != "unmapped" and b.method_source not in ("template", "custom")
        if spec_mapped and not fn.get("override"):
            continue
        b.mtype, b.recipe = "derived", "pipeline"
        b.dataset, b.column, b.value = "", "", ""
        b.args = {"steps": [dict(st) for st in (fn.get("steps") or [])]}
        b.method_source = "custom"
        b.confidence = 90
        b.reason = (f"your function '{fn.get('name')}'"
                    + (f": {fn['description']}" if fn.get("description") else ""))
        applied.append(f"{b.variable} — {fn.get('name')}")
    return applied
