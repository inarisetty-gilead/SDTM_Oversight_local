"""Repair passes over a domain's blocks, run before execution.

These encode SDTM rules that hold regardless of how the spec is worded — a --DTC is an ISO
8601 date, a --DY is a study day, a --SEQ numbers records. They are ported from SDTM
Designer (`_dtc_iso_pass`, `_study_day_pass`, `_lobxfl_pass`, `_guarantee_keys`,
`_pair_test_vars`) and are idempotent: running them twice changes nothing.
"""
from __future__ import annotations

from .blocks import Block
from .translate import raw_refs, study_day_args
from .util import upper

DATE_SUFFIXES = ("DAT_RAW", "DAT", "DTE", "DATE", "DT")
YEAR_SUFFIXES = ("_YYYY", "_YY")
TIME_SUFFIXES = ("TIM", "TIME", "RTIM")


def _cmap(store, dataset: str) -> dict[str, str]:
    """{UPPER column -> actual column} for one raw dataset, or {} if it is absent."""
    key = store.resolve(dataset) if dataset else None
    return {upper(c): c for c in store.columns(key)} if key else {}


def dtc_iso_pass(blocks: list[Block], store, base_ds: str, skip: set[str] | None = None) -> None:
    """A --DTC must be a partial-aware ISO 8601 date(time), built from the raw year/month/day
    component columns plus a time column when the study collected them — not a bare copy of a
    raw date, which drops the time, mis-formats non-ISO dates and loses partial dates.

    Source resolution runs in three layers: the SDTM stem (AESTDTC -> AESTDAT), the column the
    spec already matched, then any raw.<ds>.<col> token listed in Input Variables."""
    skip = {upper(x) for x in (skip or set())}
    for b in blocks:
        v = b.variable
        if not (v.endswith("DTC") and len(v) > 3):
            continue
        if b.recipe and b.recipe != "iso_date":          # explicit pipeline/cond/extreme wins
            continue
        if b.mtype not in ("assign", "derived"):
            continue
        if b.recipe == "iso_date" and (b.args or {}).get("y_col"):
            continue                                     # already enriched
        if upper(b.args.get("dataset") or b.dataset) in skip:
            continue

        stem = v[:-3]
        ds = b.dataset or base_ds
        resolved = store.resolve(ds) or store.resolve(base_ds) or base_ds
        cmap = _cmap(store, resolved)

        # layer 1 — stem-based guess
        dcol = next((cmap[c] for c in (stem + "DAT_RAW", stem + "DAT", stem + "DT") if c in cmap), "")
        # layer 2 — the column the spec already matched, if it looks like a date
        if not dcol and b.mtype == "assign":
            bc = upper(b.column)
            if bc.endswith(DATE_SUFFIXES) and bc in cmap:
                dcol = cmap[bc]
        # layer 3 — any date-looking column named in Input Variables, in any listed dataset
        if not dcol:
            for ref_ds, ref_col in raw_refs(b.input_variables):
                if not ref_col.endswith(DATE_SUFFIXES):
                    continue
                actual = store.resolve(ref_ds)
                if not actual:
                    continue
                rc = _cmap(store, actual)
                if ref_col in rc:
                    dcol, resolved, cmap = rc[ref_col], actual, rc
                    break

        # layer 4 — the spec points straight at a year COMPONENT column (BRTHDAT_YYYY).
        # There is no whole-date column to parse; the parts are the source.
        ycol_direct = ""
        if not dcol:
            for ref_ds, ref_col in raw_refs(b.input_variables):
                if not ref_col.endswith(YEAR_SUFFIXES):
                    continue
                actual = store.resolve(ref_ds)
                rc = _cmap(store, actual) if actual else {}
                if ref_col in rc:
                    ycol_direct, resolved, cmap = rc[ref_col], actual, rc
                    break
            if not ycol_direct:
                for cand in (upper(b.column), stem + "DAT_YYYY", stem + "DAT_YY", stem + "_YYYY"):
                    if cand and cand.endswith(YEAR_SUFFIXES) and cand in cmap:
                        ycol_direct = cmap[cand]
                        break
        if not dcol and not ycol_direct:
            continue

        if ycol_direct:
            bu = upper(ycol_direct).rsplit("_", 1)[0]          # BRTHDAT_YYYY -> BRTHDAT
        else:
            base_name = dcol[:-4] if upper(dcol).endswith("_RAW") else dcol
            bu = upper(base_name)

        tcol = next((cmap[c] for c in (stem + sfx for sfx in TIME_SUFFIXES) if c in cmap), "")
        if not tcol:
            for ref_ds, ref_col in raw_refs(b.input_variables):
                if not ref_col.endswith(TIME_SUFFIXES):
                    continue
                rc = _cmap(store, store.resolve(ref_ds) or resolved)
                if ref_col in rc:
                    tcol = rc[ref_col]
                    break

        args = {"dataset": resolved, "date_col": dcol, "time_col": tcol}
        ycol = ycol_direct or cmap.get(bu + "_YYYY") or cmap.get(bu + "_YY")
        if ycol:                                         # components keep partial dates intact
            args.update({"y_col": ycol, "m_col": cmap.get(bu + "_MM", ""),
                         "d_col": cmap.get(bu + "_DD", "")})
        b.mtype, b.recipe, b.args = "derived", "iso_date", args


def study_day_pass(blocks: list[Block]) -> None:
    """--DY is always a study day, never a copy of the --DTC. When the --DTC it reads is itself
    dropped or unmapped in this domain, the --DY is dropped too rather than resurrected."""
    by_var = {b.variable: b for b in blocks}
    for b in blocks:
        v = b.variable
        if not (v.endswith("DY") and len(v) > 2 and v != "STUDYID"):
            continue
        if b.recipe and b.recipe != "study_day":
            continue
        args = study_day_args(v, b.input_variables)
        dep = by_var.get(upper(args.get("dtc_var")))
        if dep is not None and dep.mtype in ("drop", "unmapped", ""):
            b.mtype, b.recipe, b.args, b.dataset, b.column = "drop", "", {}, "", ""
            b.reason = f"study day reads {dep.variable}, which is dropped or unmapped"
            continue
        if b.mtype == "drop":
            continue                                     # an explicit spec drop stays dropped
        b.mtype, b.recipe, b.args = "derived", "study_day", args


def lobxfl_pass(blocks: list[Block]) -> None:
    """--LOBXFL is derived, not collected: wire it to the recipe when the domain has the
    --TESTCD and --DTC it needs, otherwise leave it alone."""
    present = {b.variable for b in blocks}
    for b in blocks:
        v = b.variable
        if not (v.endswith("LOBXFL") and len(v) > 6):
            continue
        if b.recipe and b.recipe != "lobxfl":
            continue
        dom = v[:-6]                                     # VSLOBXFL -> VS
        tc, dtc = dom + "TESTCD", dom + "DTC"
        if tc not in present or dtc not in present:
            continue                                     # not a findings layout
        res = next((c for c in (dom + "ORRES", dom + "STRESC", dom + "STRESN") if c in present),
                   dom + "ORRES")
        ex = b.args or {}
        b.mtype, b.recipe = "derived", "lobxfl"
        b.args = {
            "testcd_var": ex.get("testcd_var", tc),
            "dtc_var": ex.get("dtc_var", dtc),
            "result_var": ex.get("result_var", res),
            "ref_var": ex.get("ref_var", "RFXSTDTC"),
            "group_vars": ex.get("group_vars", ["USUBJID", tc]),
        }


def guarantee_keys(blocks: list[Block], store, base_ds: str, domain: str) -> None:
    """DOMAIN / USUBJID are never dropped — a spec that drops them leaves the whole built
    dataset unusable (and subject-less), which is a spec defect, not an instruction."""
    base_cols = _cmap(store, base_ds)
    for b in blocks:
        if b.variable == "DOMAIN" and b.mtype in ("drop", "unmapped"):
            b.mtype, b.value, b.recipe, b.args = "constant", upper(domain), "", {}
            b.reason = "domain code is mandatory — restored"
        elif b.variable == "USUBJID" and b.mtype in ("drop", "unmapped"):
            for key in ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID"):
                if key in base_cols:
                    b.mtype, b.dataset, b.column = "assign", base_ds, base_cols[key]
                    b.recipe, b.args = "", {}
                    b.reason = f"subject key is mandatory — carried from {base_cols[key]}"
                    break


def pair_test_vars(blocks: list[Block]) -> None:
    """--TESTCD and --TEST are the code and the name of the same finding. If one has a raw
    source and the other does not, give it the same source; the codelist then maps name to
    code. Prevents a populated --TEST alongside an empty --TESTCD."""
    tc = next((b for b in blocks if b.variable.endswith("TESTCD")), None)
    te = next((b for b in blocks if b.variable.endswith("TEST")
               and not b.variable.endswith("TESTCD")), None)
    if not (tc and te):
        return

    def mapped(b):
        return b.mtype in ("assign", "derived", "constant")

    def src(b):
        return (b.dataset, b.column) if b.mtype == "assign" and b.dataset and b.column else None

    if not mapped(tc) and src(te):
        tc.mtype, (tc.dataset, tc.column) = "assign", src(te)
        tc.recipe, tc.args = "", {}
        tc.reason = f"paired with {te.variable} — same source; codelist maps name to code"
    elif not mapped(te) and src(tc):
        te.mtype, (te.dataset, te.column) = "assign", src(tc)
        te.recipe, te.args = "", {}
        te.reason = f"paired with {tc.variable} — same source"


def run_all(blocks: list[Block], store, base_ds: str, domain: str,
            skip: set[str] | None = None) -> None:
    """Every pass, in the order the engine depends on."""
    pair_test_vars(blocks)
    guarantee_keys(blocks, store, base_ds, domain)
    # a multi-source date is an extreme across forms — decide that before the ISO pass turns
    # it into a plain single-column date
    reference_date_pass(blocks, store)
    dtc_iso_pass(blocks, store, base_ds, skip)
    study_day_pass(blocks)
    lobxfl_pass(blocks)


def _many_records_per_subject(store, dataset: str) -> bool:
    """Does this raw form hold more than one record per subject? If so, a date taken from it
    has to be aggregated rather than copied."""
    key = store.resolve(dataset)
    if not key:
        return False
    try:
        frame = store.get(key)
    except (KeyError, OSError, ValueError):
        return False
    for col in ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID", "SUBJECT_ID"):
        if col in frame.columns:
            subjects = frame[col].nunique(dropna=True)
            return bool(subjects and len(frame) > subjects)
    return False


def reference_date_pass(blocks: list[Block], store) -> int:
    """A subject-level REFERENCE date whose spec lists several raw date columns is an
    earliest-or-latest across those forms, not a copy of whichever was written first.

    That is how RFSTDTC, RFENDTC and the other reference dates are defined — the first dose
    across every exposure form, the last contact across every visit form. Taking the first
    listed source silently produces a date that is right for one form and wrong overall.
    """
    from . import automap

    wired = 0
    for b in blocks:
        v = b.variable
        if b.edited or b.mtype in ("drop", "constant", "sequence"):
            continue
        if b.recipe and b.recipe not in ("iso_date", "date_extreme"):
            continue
        # ONLY the subject-level reference dates. An ordinary --DTC is the date OF a record:
        # DS.DSSTDTC draws on the consent, enrolment and completion forms, but those forms ARE
        # its records, so collapsing them to one date per subject would destroy the domain.
        if not (v in automap.REF_START_VARS or v in automap.REF_END_VARS):
            continue

        sources = automap.date_sources_from_spec(b, store)
        if not sources:
            continue

        # Several date sources is plainly an extreme across forms. But ONE source is too, when
        # that form holds many records per subject and the spec asks for the most recent or the
        # earliest — "keep the most recent scheduled visit date" is a max over the visit form,
        # not a copy of whichever visit row happens to come first.
        text = f"{b.mapping_rule} {b.sas_code}".lower()
        says_extreme = any(w in text for w in automap.LATEST_WORDS + automap.EARLIEST_WORDS)
        if len(sources) < 2:
            if not (says_extreme and _many_records_per_subject(store, sources[0]["dataset"])):
                continue                   # an ordinary single-record --DTC, left alone

        b.mtype, b.recipe = "derived", "date_extreme"
        b.args = {"func": automap.extreme_for(b), "group_by": ["USUBJID"], "sources": sources}
        which = "latest" if b.args["func"] == "max" else "earliest"
        where = ", ".join(f"{s['dataset']}.{s['date_col']}" for s in sources[:4])
        b.reason = (f"the spec lists {len(sources)} date sources — taking the {which} per "
                    f"subject across {where}{' …' if len(sources) > 4 else ''}"
                    if len(sources) > 1 else
                    f"the spec asks for the {which} date, and {sources[0]['dataset']} holds "
                    f"several records per subject — taking the {which} of {where}")
        wired += 1
    return wired
