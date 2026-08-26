"""Spec row -> Block. Deterministic, and honest about what it cannot resolve.

Ported from SDTM Designer's `_struct_from_spec` / `_struct_from_resolved`
(sdtm_pred/backend/main.py) with two deliberate changes for an oversight tool:

  * Designer turns a same-domain `sdtm.<dom>.<col>` reference into generated Python that
    it exec()s. Here it becomes the first-class `copy_var` recipe, and a cross-domain
    reference becomes `sdtm_ref` — both executed by named functions.
  * Designer hardcodes STUDYID to one sponsor study. Here STUDYID resolves from the raw
    data, or from --studyid, and is otherwise reported rather than invented.
"""
from __future__ import annotations

import re

from .blocks import Block
from .spec import SpecRow
from .util import as_int, s, upper

REF_DATE_VARS = {"RFSTDTC", "RFXSTDTC", "RFICDTC", "RFENDTC", "RFXENDTC", "RFPENDTC"}

SAS_LITERAL_RE = re.compile(r'^\s*"([^"]*)"\s*$|^\s*\'([^\']*)\'\s*$')
SAS_CALL_RE = re.compile(r"^([A-Za-z_]+)\s*\((.*)\)\s*$", re.S)
UNARY_SAS_FUNCS = {"strip", "upcase", "lowcase", "propcase", "compbl", "left", "trim", "reverse"}

RAW_TOKEN_RE = re.compile(r"raw\.\s*([A-Za-z0-9_]+)\s*\.\s*([A-Za-z0-9_.]+)", re.I)
SDTM_TOKEN_RE = re.compile(r"sdtm\.\s*([A-Za-z0-9_]+)\s*\.\s*([A-Za-z0-9_]+)", re.I)


def split_tokens(iv: str) -> list[str]:
    """Input Variables cells mix commas, semicolons and newlines."""
    return [t.strip() for t in re.split(r"[,\n;]+", s(iv)) if t.strip()]


def raw_refs(iv: str) -> list[tuple[str, str]]:
    """Every raw.<dataset>.<column> token, lowercased dataset / UPPER column."""
    return [(m.group(1).lower(), m.group(2).upper()) for m in RAW_TOKEN_RE.finditer(s(iv))]


def sdtm_refs(iv: str) -> list[tuple[str, str]]:
    return [(m.group(1).upper(), m.group(2).upper()) for m in SDTM_TOKEN_RE.finditer(s(iv))]


def sas_unary_chain(sas: str) -> list[str] | None:
    """'strip(upcase(tmethod))' -> ['strip', 'upcase'] (outermost first), else None."""
    txt = s(sas).replace("\n", " ").replace("\r", " ")
    funcs: list[str] = []
    while True:
        m = SAS_CALL_RE.match(txt)
        if not m:
            break
        fn = m.group(1).lower()
        if fn not in UNARY_SAS_FUNCS:
            return None
        funcs.append(fn)
        txt = m.group(2).strip()
    if funcs and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", txt):
        return funcs
    return None


def study_day_args(var: str, iv: str) -> dict:
    """--DY: event --DTC and the DM reference date, from the spec Input Variables.
    'sdtm.ae.aestdtc, sdtm.dm.rfstdtc' -> dtc_var=AESTDTC, ref_var=RFSTDTC."""
    v = upper(var)
    dtc = ref = ""
    for dom, col in sdtm_refs(iv):
        if col in REF_DATE_VARS or dom == "DM":
            ref = ref or col
        elif col.endswith("DTC"):
            dtc = dtc or col
    if not dtc and v.endswith("DY") and len(v) > 2:
        dtc = v[:-2] + "DTC"                        # AESTDY -> AESTDTC
    return {"dtc_var": dtc, "ref_var": ref or "RFSTDTC"}


def _base(row: SpecRow, order: int) -> Block:
    return Block(
        variable=upper(row.variable), domain=upper(row.domain),
        label=row.label, action=upper(row.action), input_variables=row.input_variables,
        mapping_rule=row.mapping_rule, sas_code=row.sas_code, codelist=upper(row.codelist),
        role=row.role, origin=row.origin, type=row.type, length=row.length,
        order=as_int(row.order, order), sheet_row=row.row_number,
        supp=row.is_supp, qlabel=(row.label if row.is_supp else ""),
        qorig=(row.origin if row.is_supp else ""),
    )


def translate_row(row: SpecRow, order: int = 0, studyid: str = "") -> Block:
    """One spec row -> one executable Block."""
    b = _base(row, order)
    v, act, iv, sas = b.variable, b.action, s(b.input_variables), s(b.sas_code)
    dom = b.domain
    raws = raw_refs(iv)
    sdtms = sdtm_refs(iv)

    # ── invariants that never depend on the spec text ───────────────────────
    if v == "DOMAIN":
        b.mtype, b.value = "constant", dom
        return b

    if act == "DROP":                                # an explicit spec DROP always wins
        b.mtype, b.reason = "drop", "spec Mapping Action = DROP"
        return b

    if v == "STUDYID":
        if raws:
            b.mtype, b.dataset, b.column = "assign", raws[0][0], raws[0][1]
        elif studyid:
            b.mtype, b.value = "constant", studyid
        else:
            b.mtype, b.recipe = "derived", "studyid"
        return b

    # --SEQ and --DY derive without a source column; recognise them before the
    # "ASSIGN with no input -> drop" rule so they are not thrown away.
    if v.endswith("SEQ") and v != "IDVARVAL":
        b.mtype = "sequence"
        b.args = {"group": "USUBJID"}
        return b

    if v.endswith("DY") and len(v) > 2:
        b.mtype, b.recipe = "derived", "study_day"
        b.args = study_day_args(v, iv)
        return b

    if v == "USUBJID":
        if raws:
            b.mtype, b.dataset, b.column = "assign", raws[0][0], raws[0][1]
        else:
            b.mtype, b.recipe = "derived", "usubjid"
        return b

    # ── the spec's own instructions ─────────────────────────────────────────
    # ASSIGN whose Implemented SAS Code is a bare quoted literal is a constant, even when a
    # (usually spurious) raw input sits alongside it — e.g. TUTESTCD = "LESIDENT".
    lit = SAS_LITERAL_RE.match(sas) if sas else None
    if act == "ASSIGN" and lit:
        b.mtype = "constant"
        b.value = lit.group(1) if lit.group(1) is not None else lit.group(2)
        return b

    if act == "ASSIGN" and not iv:
        # An ASSIGN with no source is an UNFINISHED spec row, not an instruction to drop the
        # variable. Leaving it unmapped keeps it eligible for name matching and hand mapping;
        # dropping it here would hide it from both.
        b.mtype = "unmapped"
        b.reason = "the spec says ASSIGN but names no Input Variable"
        return b

    if raws:
        ds, col = raws[0]
        b.mtype, b.dataset, b.column = "assign", ds, col
        # honour a transform in the Implemented SAS Code (strip(upcase(x)) must upper-case).
        # Only upgrade when the chain adds something beyond strip — a plain assign strips.
        chain = sas_unary_chain(sas)
        if chain and (set(chain) - {"strip"}):
            steps = [{"op": "assign", "dataset": ds, "column": col}]
            for fn in reversed(chain):               # innermost transform applies first
                steps.append({"op": "fn", "args": {"fn": fn, "sources": [{"kind": "self"}]}})
            b.mtype, b.recipe, b.args = "derived", "pipeline", {"steps": steps}
        return b

    if sdtms:
        same = [c for d, c in sdtms if d == dom]
        if same:
            b.mtype, b.recipe = "derived", "copy_var"
            b.args = {"source_var": same[0]}
            return b
        d, c = sdtms[0]
        b.mtype, b.recipe = "derived", "sdtm_ref"
        b.args = {"source_domain": d, "source_var": c}
        return b

    # Nothing deterministic to execute. Say so — do not guess, do not emit an empty column
    # that would read as "the vendor added data we did not".
    b.mtype = "unmapped"
    if s(b.mapping_rule):
        b.reason = "narrative mapping rule with no machine-readable Input Variables"
    elif act:
        b.reason = f"Mapping Action '{act}' with no resolvable source"
    else:
        b.reason = "spec row has no Input Variables and no Mapping Action"
    return b


def translate_domain(rows: list[SpecRow], studyid: str = "") -> list[Block]:
    """All spec rows for one domain, in spec order."""
    return [translate_row(r, order=i, studyid=studyid) for i, r in enumerate(rows)]


def apply_sas_code(blocks: list[Block], store, base_dataset: str) -> tuple[int, int]:
    """Use the spec's `Implemented SAS Code` as the mapping when it is a supported expression.

    Much of a real spec's detail lives in that column rather than in Input Variables — a
    variable whose Input Variables says `sdtm.dm.usubjid` and whose SAS code says
    `scan(usubjid, -2, '-')` is a substring of USUBJID, not a copy of it. Reading only the
    former produces a confident wrong value, which is worse than producing nothing.

    Returns (compiled, refused): how many mappings the SAS code supplied, and how many were
    outside the supported grammar and therefore left for the reader to map by hand."""
    from . import sasexpr

    known_vars = {b.variable for b in blocks}
    # STUDYID/USUBJID/DOMAIN have dedicated derivations that know the SDTM conventions and
    # cope with a study that spells its keys differently. Their SAS code must not override them.
    reserved = {"STUDYID", "USUBJID", "DOMAIN"}
    compiled = refused = 0

    for b in blocks:
        sas = s(b.sas_code)
        if not sas or b.action == "DROP" or b.variable in reserved:
            continue
        if b.mtype in ("constant", "sequence", "drop"):
            continue
        if SAS_LITERAL_RE.match(sas):                 # a bare literal is already a constant
            continue

        # identifiers may name a sibling SDTM variable or a column in the raw data this row
        # references; anything else means we cannot honour the expression
        datasets = [ds for ds, _ in raw_refs(b.input_variables)] or [base_dataset]

        def resolve(name: str, _ds=datasets, _self=b):
            up = upper(name).split(".")[-1]
            if up in known_vars and up != _self.variable:
                return {"kind": "var", "var": up}
            for ds in _ds:
                key = store.resolve(ds)
                if key and up in {upper(c) for c in store.columns(key)}:
                    return {"dataset": key, "column": up}
            if up == _self.variable and _self.dataset and _self.column:
                return {"dataset": _self.dataset, "column": _self.column}
            raise sasexpr.Unresolved(name)

        try:
            got = sasexpr.to_block_args(sas, resolve)
        except (ValueError, TypeError, IndexError):
            got = None

        if got is None:
            # Only report a refusal when the SAS code was the ONLY thing that could have
            # mapped this variable — a plain raw assign stands on its own.
            if b.mtype in ("unmapped", "derived") and b.recipe in ("", "copy_var", "sdtm_ref"):
                b.mtype, b.recipe, b.args = "unmapped", "", {}
                b.reason = (f"the spec's SAS code is not a supported expression: {sas[:80]}"
                            + ("…" if len(sas) > 80 else ""))
                refused += 1
            continue

        if got["mtype"] == "constant":
            b.mtype, b.value, b.recipe, b.args = "constant", got["value"], "", {}
        elif got["mtype"] == "passthrough":
            continue                                  # a bare identifier: the existing assign is right
        else:
            b.mtype, b.recipe, b.args = "derived", got["recipe"], got["args"]
        b.reason = f"from the spec's SAS code: {sas[:70]}" + ("…" if len(sas) > 70 else "")
        compiled += 1

    return compiled, refused
