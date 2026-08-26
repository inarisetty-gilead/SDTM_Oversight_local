"""Deterministic source resolution beyond the literal spec text.

Two layers, both ported from SDTM Designer (`_best_assign_source`, `_automap_block`,
`_name_score`, `AUTOMAP_SYN`) and both pure name matching — no model, no network:

  1. **Best listed source.** When the spec names several raw columns for one variable, take
     the one that EXISTS and whose name best matches the SDTM variable, not merely the first
     one written down. `AETOXGR` should read `AETOXGR_STD`, not the `AESEV_STD` beside it.

  2. **Name-match fallback.** For a variable the spec leaves unmapped, look for a raw column
     of a similar name in the domain's own records (and in DM). This is how Designer reaches
     high coverage on specs that do not spell every source out.

Layer 2 is a GUESS, and the tool says so. A name-matched variable is recorded with
`method_source="name_match"` and a confidence, counted separately from spec-derived
mappings everywhere it is reported, so agreement with a vendor on a guessed mapping is
never mistaken for agreement on a specified one.
"""
from __future__ import annotations

import difflib

from .blocks import Block
from .translate import raw_refs, sdtm_refs
from .util import upper

# Designer's threshold is 45. This tool defaults higher: for oversight a wrong mapping is
# worse than an absent one, because it produces a confident value to compare against.
DEFAULT_THRESHOLD = 70
DESIGNER_THRESHOLD = 45

# SDTM variable -> raw column names that mean the same thing when the names diverge.
# Mostly MedDRA coding columns as EDC systems name them.
SYNONYMS = {
    "USUBJID": ["X_SUBJID", "SUBJECTID"],
    "AESEV": ["AETOXGR", "AESEVN"],
    "AEDECOD": ["MDRPT"], "AELLT": ["MDRLLT"], "AELLTCD": ["MDRLLTC"],
    "AEPTCD": ["MDRPTC"], "AEHLT": ["MDRHLT"], "AEHLTCD": ["MDRHLTC"],
    "AEHLGT": ["MDRHLGT"], "AEHLGTCD": ["MDRHLGTC"], "AEBODSYS": ["MDRSOC"],
    "AEBDSYCD": ["MDRSOCC"], "AESOC": ["MDRSOC"], "AESOCCD": ["MDRSOCC"],
    "AEREFID": ["RECORDID"], "AESPID": ["RECORDPOSITION"],
    "CMDECOD": ["WHODRUG", "WHODD"], "CMINDC": ["CMIND"],
}


def name_score(var: str, col: str) -> int:
    """0-100 similarity between an SDTM variable name and a raw column name."""
    v, c = upper(var), upper(col)
    if not (v and c):
        return 0
    if v == c:
        return 99
    if v in c or c in v:
        return 80
    return int(difflib.SequenceMatcher(None, v, c).ratio() * 100)


def best_listed_source(var: str, input_variables: str, store) -> tuple[str, str] | None:
    """Among the raw sources the spec lists, the one that exists and best fits the variable."""
    v = upper(var)
    tokens = raw_refs(input_variables)
    if not tokens:
        return None

    def exists(ds, col):
        key = store.resolve(ds)
        return bool(key) and col in {upper(c) for c in store.columns(key)}

    present = [(d, c) for d, c in tokens if exists(d, c)]
    candidates = present or tokens

    def score(dc):
        d, c = dc
        s = name_score(v, c)
        if c == v:
            s += 200                       # exact match
        elif c == v + "_STD":
            s += 120                       # the standardised form of the same field
        elif c.startswith(v):
            s += 80                        # same stem
        if (d, c) in present:
            s += 1000                      # a column that exists always beats one that does not
        return s

    return max(candidates, key=score)


def refine_listed_sources(blocks: list[Block], store) -> int:
    """Layer 1 over every assign block. Returns how many sources were repointed."""
    changed = 0
    for b in blocks:
        if b.mtype != "assign" or not b.input_variables or not b.column:
            continue
        best = best_listed_source(b.variable, b.input_variables, store)
        if not best:
            continue
        bd, bc = best[0], upper(best[1])
        cur = upper(b.column)
        if bc == cur:
            continue
        key = store.resolve(bd)
        if not key or bc not in {upper(c) for c in store.columns(key)}:
            continue

        def fits(col, v=b.variable):
            return col == v or col == v + "_STD" or col.startswith(v)

        # only switch on a clear improvement, so a deliberate well-named source is never lost
        if fits(bc) and not fits(cur):
            b.dataset, b.column = bd, best[1]
            b.reason = (b.reason or
                        f"the spec lists several sources; {bd}.{best[1]} matches {b.variable}")
            changed += 1
    return changed


def unmap_missing_sources(blocks: list[Block], store) -> int:
    """A spec that names `raw.ae.AETERM` when the study collected `event_term` has named a
    column that does not exist. Mark those blocks unmapped rather than letting them fail at
    execution time: unmapped is recoverable (name matching, hand mapping) and gives the
    reader a far better reason than a column-not-found error."""
    def column_missing(ds: str, col: str) -> bool:
        key = store.resolve(ds)
        return not key or upper(col) not in {upper(c) for c in store.columns(key)}

    missed = 0
    for b in blocks:
        # a derived recipe carries its sources inside args; check those too, so a pipeline
        # over an absent column is reported rather than raising at execution time
        if b.mtype == "derived" and b.args:
            bad = ""
            for step in (b.args.get("steps") or []):
                for src in ((step.get("args") or {}).get("sources") or []):
                    if src.get("dataset") and src.get("column") \
                            and column_missing(src["dataset"], src["column"]):
                        bad = f"{src['dataset']}.{src['column']}"
                        break
                if not bad and step.get("dataset") and step.get("column") \
                        and column_missing(step["dataset"], step["column"]):
                    bad = f"{step['dataset']}.{step['column']}"
                if bad:
                    break
            if bad:
                b.mtype, b.recipe, b.args, b.dataset, b.column = "unmapped", "", {}, "", ""
                b.reason = f"the spec's derivation reads {bad}, which is not in the raw data"
                missed += 1
            continue
        if b.mtype != "assign" or not b.dataset or not b.column:
            continue
        key = store.resolve(b.dataset)
        if key and upper(b.column) in {upper(c) for c in store.columns(key)}:
            continue
        # capture the names BEFORE blanking them, or the message loses what it is about
        where, named_ds = f"{b.dataset}.{b.column}", b.dataset
        b.mtype, b.dataset, b.column = "unmapped", "", ""
        b.reason = (f"the spec names {where}, which is not in the raw data" if key else
                    f"the spec names dataset '{named_ds}', which this study did not collect")
        missed += 1
    return missed


def name_match_unmapped(blocks: list[Block], store, base_dataset: str,
                        threshold: int = DEFAULT_THRESHOLD,
                        extra_datasets: tuple[str, ...] = ("dm",)) -> int:
    """Layer 2. Give an unmapped variable the best similarly-named raw column from the
    domain's own records, then DM. Returns how many were matched."""
    if threshold <= 0:
        return 0
    searchable = [d for d in (base_dataset, *extra_datasets) if d and store.resolve(d)]
    if not searchable:
        return 0
    seen_keys = {store.resolve(d) for d in searchable}
    # STUDYID and USUBJID have dedicated derivations that know the SDTM conventions. Letting
    # a fuzzy name match claim them produces a confident, wrong subject key — the worst
    # possible error, because every downstream record then fails to match.
    reserved = {"STUDYID", "USUBJID", "DOMAIN"}
    matched = 0
    for b in blocks:
        if b.mtype != "unmapped" or b.variable in reserved:
            continue
        best, best_score = None, 0
        for ds in searchable:
            key = store.resolve(ds)
            for col in store.columns(key):
                sc = name_score(b.variable, col)
                if sc > best_score:
                    best, best_score = (key, col), sc
        for syn in SYNONYMS.get(b.variable, []):
            key = store.resolve(base_dataset)
            if key and syn in {upper(c) for c in store.columns(key)} and best_score < 90:
                best, best_score = (key, syn), 90
        if best and best_score >= threshold:
            b.mtype, b.dataset, b.column = "assign", best[0], best[1]
            b.method_source = "name_match"
            b.confidence = best_score
            b.reason = (f"the spec does not name a source; matched to {best[0]}.{best[1]} "
                        f"by name ({best_score}% similar) — verify before relying on it")
            matched += 1
    return matched


# ── suggesting a derivation's arguments from what the spec already says ──────
REF_START_VARS = {"RFSTDTC", "RFXSTDTC", "RFICDTC"}
REF_END_VARS = {"RFENDTC", "RFXENDTC", "RFPENDTC"}
DATE_ENDINGS = ("DAT", "DATE", "DTC", "DTM", "DT", "DAT_RAW", "DATE_RAW", "_RAW")

LATEST_WORDS = ("most recent", "latest", "last ", "maximum", "max(")
EARLIEST_WORDS = ("earliest", "first ", "minimum", "min(")


def looks_like_a_date(store, dataset: str, column: str) -> bool:
    """Is this raw column a date? Judged by its name and by whether the form split it."""
    col = upper(column)
    if col.endswith(DATE_ENDINGS) and not col.endswith(("TIM", "TIME")):
        base = col[:-4] if col.endswith("_RAW") else col
        key = store.resolve(dataset)
        if key:
            cols = {upper(c) for c in store.columns(key)}
            if col in cols or f"{base}_YYYY" in cols:
                return True
        return True
    return False


def date_sources_from_spec(block: Block, store) -> list[dict]:
    """The dataset/column pairs the spec already lists for this variable, in spec order,
    keeping only those that exist and look like dates."""
    out, seen = [], set()
    for ds, col in raw_refs(block.input_variables):
        key = store.resolve(ds)
        if not key:
            continue
        cols = {upper(c): c for c in store.columns(key)}
        if upper(col) not in cols or not looks_like_a_date(store, ds, col):
            continue
        pair = (key, cols[upper(col)])
        if pair not in seen:
            seen.add(pair)
            out.append({"dataset": pair[0], "date_col": pair[1]})
    return out


def extreme_for(block: Block) -> str:
    """Earliest or latest? Taken from the variable, then from what the spec's rule says."""
    v = block.variable
    text = f"{block.mapping_rule} {block.sas_code}".lower()
    if any(w in text for w in LATEST_WORDS):
        return "max"
    if any(w in text for w in EARLIEST_WORDS):
        return "min"
    if v in REF_END_VARS or v.endswith(("ENDTC", "ENDY")):
        return "max"
    return "min"


def suggest_args(block: Block, store, recipe: str) -> dict:
    """What the spec already tells us about this derivation's arguments.

    The spec lists the datasets and columns in Input Variables. Making someone retype them
    into a form is asking them to copy out what the tool can already read."""
    if recipe == "date_extreme":
        sources = date_sources_from_spec(block, store)
        args: dict = {"func": extreme_for(block), "group_by": ["USUBJID"]}
        if sources:
            args["sources"] = sources
        return args
    if recipe == "iso_date":
        refs = raw_refs(block.input_variables)
        for ds, col in refs:
            if looks_like_a_date(store, ds, col):
                key = store.resolve(ds)
                if key:
                    return {"dataset": key, "date_col": upper(col)}
        return {"dataset": store.resolve(block.dataset) or block.dataset,
                "date_col": upper(block.column)} if block.column else {}
    if recipe == "concat":
        refs = raw_refs(block.input_variables)
        if refs:
            key = store.resolve(refs[0][0])
            return {"dataset": key or refs[0][0],
                    "columns": [c for d, c in refs if (store.resolve(d) or d) == key], "sep": " "}
        return {}
    if recipe == "copy_var":
        same = [c for d, c in sdtm_refs(block.input_variables) if d == block.domain]
        return {"source_var": same[0]} if same else {}
    if recipe == "sdtm_ref":
        other = [(d, c) for d, c in sdtm_refs(block.input_variables) if d != block.domain]
        return {"source_domain": other[0][0], "source_var": other[0][1]} if other else {}
    if recipe == "study_day":
        from .translate import study_day_args
        return study_day_args(block.variable, block.input_variables)
    return {}
