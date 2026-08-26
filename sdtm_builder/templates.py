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
    has_birth = "BRTHDTC" in by_var and by_var["BRTHDTC"].mtype not in ("drop", "unmapped")
    has_ref = "RFSTDTC" in by_var and by_var["RFSTDTC"].mtype not in ("drop", "unmapped")
    base_cols = {upper(c) for c in store.columns(store.resolve(base_ds) or "")}
    reported = next((c for c in REPORTED_AGE if c in base_cols), "")
    if not (reported or (has_birth and has_ref)):
        return None
    b.mtype, b.recipe = "derived", "age"
    b.args = {"age_col": reported, "birth_var": "BRTHDTC", "ref_var": "RFSTDTC"}
    return ("the reported age" + (f" ({reported})" if reported else "")
            if reported else "whole years from BRTHDTC to RFSTDTC, anniversary rule")


def _apply_ageu(b: Block, by_var: dict, store, base_ds: str) -> str | None:
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
    found = _find_source(store, DEATH_DATE)
    if not found:
        return None
    ds, col = found
    b.mtype, b.dataset, b.column = "assign", ds, col
    return f"the death date collected in {ds}.{col}"


def _apply_dthfl(b: Block, by_var: dict, store, base_ds: str) -> str | None:
    found = _find_source(store, DEATH_DATE)
    if not found:
        return None
    ds, col = found
    b.mtype, b.recipe = "derived", "cond"
    b.args = {"rules": [{"src": {"dataset": ds, "column": col}, "op": "notmissing",
                         "value": "", "then": {"kind": "text", "text": "Y"}}],
              "else": {"kind": "missing"}}
    return f"'Y' when a death date exists in {ds}.{col}, blank otherwise"


REGISTRY: list[Template] = [
    Template("AGE", ("DM",), "dm-merge template",
             "reported age, else whole years from birth to the reference start", _apply_age),
    Template("AGEU", ("DM",), "dm-merge template",
             "the collected age unit, else YEARS", _apply_ageu),
    Template("DTHDTC", ("DM",), "dm-merge template",
             "the collected death date", _apply_dthdtc),
    Template("DTHFL", ("DM",), "dm-merge template",
             "Y when a death date exists", _apply_dthfl),
]


def apply_templates(blocks: list[Block], store, domain: str, base_ds: str) -> list[str]:
    """Fill unmapped variables from the template registry. Returns what was applied."""
    dom = upper(domain)
    by_var = {b.variable: b for b in blocks}
    applied = []
    for t in REGISTRY:
        if dom not in t.domains:
            continue
        b = by_var.get(t.variable)
        # only a variable the spec leaves without a workable mapping — the spec and the
        # reader always outrank a template
        if b is None or b.edited or b.mtype not in ("unmapped",):
            continue
        note = t.apply(b, by_var, store, base_ds)
        if note is None:
            continue
        b.method_source = "template"
        b.confidence = 95
        b.reason = f"standard derivation from the {t.source}: {note}"
        applied.append(f"{t.variable} — {note}")
    return applied
