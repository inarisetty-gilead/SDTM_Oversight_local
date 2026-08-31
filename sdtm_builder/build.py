"""Build SDTM domains from a mapping spec and a folder of raw datasets.

The engine is deliberately boring: pick the domain's base dataset, run each spec row's
Block through its named operation, finish the dataset (sort / dedup / --SEQ), split out
supplemental qualifiers. Every variable's outcome is recorded, so the manifest can say not
just what was built but what was not, and why.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from . import automap, ops, passes, prep
from .blocks import Block
from .ops import OpError
from .rawio import RawStore
from .spec import Spec
from .prep import PrepStep
from . import translate as translate_mod
from .translate import translate_domain
from .util import norm_key, s, upper

# domains that must exist before others can derive from them (reference dates, USUBJID)
PRIORITY_DOMAINS = ("DM", "EX", "EC", "SV", "TA", "TV", "TS")

SUPP_COLUMNS = ["STUDYID", "RDOMAIN", "USUBJID", "IDVAR", "IDVARVAL",
                "QNAM", "QLABEL", "QVAL", "QORIG", "QEVAL"]

REF_DATE_VARS = ("RFSTDTC", "RFXSTDTC", "RFICDTC", "RFENDTC", "RFXENDTC", "RFPENDTC")


@dataclass
class BuildContext:
    """Everything one domain's operations may read. Deliberately explicit — an operation
    cannot reach outside this object, so a build has no hidden inputs."""
    domain: str
    store: RawStore
    base: pd.DataFrame
    frame: pd.DataFrame
    built: dict[str, pd.DataFrame]
    codelists: dict[str, dict[str, str]]
    studyid: str = ""
    registers: dict[int, pd.Series] = field(default_factory=dict)

    @property
    def index(self):
        return self.base.index

    def raw(self, name: str) -> pd.DataFrame:
        return self.store.get(name)

    def reference_dates(self, var: str):
        """A DM reference date (RFSTDTC etc.) as a Series indexed by USUBJID, or None."""
        dm = self.built.get("DM")
        if dm is None or upper(var) not in dm.columns or "USUBJID" not in dm.columns:
            return None
        src = dm.dropna(subset=["USUBJID"]).drop_duplicates("USUBJID")
        return pd.Series(src[upper(var)].values, index=src["USUBJID"].values)


@dataclass
class DomainResult:
    domain: str
    dataset: pd.DataFrame | None = None
    supp: pd.DataFrame | None = None
    blocks: list[Block] = field(default_factory=list)
    base_dataset: str = ""
    prep_step: PrepStep | None = None
    prep_reports: list[dict] = field(default_factory=list)
    prep_outputs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.dataset is not None and not self.error

    @property
    def counts(self) -> dict[str, int]:
        c = Counter(b.status for b in self.blocks)
        return {"built": c["built"], "dropped": c["dropped"],
                "not_built": c["not_built"], "error": c["error"], "empty": c["empty"],
                "edited": sum(1 for b in self.blocks if b.edited),
                "name_matched": sum(1 for b in self.blocks
                                    if b.method_source == "name_match" and b.status == "built"),
                "rows": 0 if self.dataset is None else len(self.dataset)}


# ── base-dataset selection ──────────────────────────────────────────────────
def choose_base(domain: str, blocks: list[Block], store: RawStore,
                override: str = "") -> tuple[str, list[str]]:
    """Which raw dataset supplies this domain's RECORDS.

    A dataset named after the domain wins. Otherwise the dataset the spec's ASSIGN rows
    reference most often supplies the records, and the rest are per-subject lookups. The
    choice is always reported so it can be overridden with --base."""
    notes: list[str] = []
    if override:
        key = store.resolve(override)
        if not key:
            raise ValueError(f"--base dataset '{override}' not found for domain {domain}")
        return key, [f"records taken from '{key}'"]

    own_all = store.resolve_all(domain)
    if len(own_all) > 1:
        notes.append(f"{domain} is collected across {len(own_all)} forms "
                     f"({', '.join(own_all)}) — only the first is used unless they are stacked")
    if own_all:
        return own_all[0], notes

    tally = Counter()
    for b in blocks:
        if b.mtype == "assign" and b.dataset and store.resolve(b.dataset):
            tally[store.resolve(b.dataset)] += 1
    if not tally:
        raise ValueError(
            f"no raw dataset found for {domain}: neither a dataset named '{domain.lower()}' "
            "nor any dataset referenced by the spec's Input Variables is present"
        )
    ranked = tally.most_common()
    base = ranked[0][0]
    notes.append(f"no raw dataset named '{domain.lower()}' — records taken from '{base}' "
                 f"({ranked[0][1]} mapped variables)")
    if len(ranked) > 1 and ranked[1][1] >= ranked[0][1]:
        others = ", ".join(f"{k} ({n})" for k, n in ranked[1:3])
        notes.append(
            f"'{base}' is not a clear record source — {others} contribute as many variables. "
            f"If {domain} records come from several forms, set --base {domain}=<dataset> or "
            "pre-stack them."
        )
    return base, notes


# ── single-domain build ─────────────────────────────────────────────────────
def build_domain(spec: Spec, store: RawStore, domain: str,
                 built: dict[str, pd.DataFrame] | None = None,
                 studyid: str = "", base_override: str = "",
                 sort_by: list[str] | None = None,
                 include_unbuilt: bool = True,
                 prep_mode: str = "auto",
                 prep_override: dict | None = None,
                 prep_steps: list[dict] | None = None,
                 edits: dict[str, dict] | None = None,
                 dedup: dict | None = None,
                 custom_fns: dict | None = None,
                 template_overrides: dict | None = None,
                 name_match_threshold: int = automap.DEFAULT_THRESHOLD) -> DomainResult:
    dom = upper(domain)
    rows = spec.rows(dom)
    result = DomainResult(domain=dom)
    if not rows:
        result.error = f"domain {dom} is not in the mapping spec"
        return result

    blocks = translate_domain(rows, studyid=studyid)
    result.blocks = blocks

    # ── data preparation: reshape the raw forms into this domain's record grain ──
    # A forced --base always wins: it is an explicit statement about where records come from.
    # A hand-built pipeline is authoritative: its outputs become datasets the mappings can
    # read by name, and nothing is auto-retargeted behind the user's back.
    prep_final = ""
    if prep_mode == "custom" and prep_steps:
        try:
            outputs, reports = prep.run_pipeline(prep_steps, store, dom)
            for name, frame in outputs.items():
                store.put(name, frame)
            result.prep_reports = reports
            result.prep_outputs = sorted(outputs)
            names = [r["name"] for r in reports if r.get("ok")]
            if names:
                result.warnings.append(
                    f"data preparation produced {len(names)} dataset(s): {', '.join(names)}")
            if not base_override and names:
                base_override = names[-1]      # the last step is the domain's records by default
            prep_final = store.resolve(base_override) or (names[-1] if names else "")
        except prep.PrepError as exc:
            result.error = f"data preparation failed: {exc}"
            return result

    step = None
    if prep_mode == "auto" and not base_override:
        try:
            if prep_override:
                step = PrepStep(op=prep_override["op"], name=prep_override.get("name") or dom.lower(),
                                params=prep_override.get("params", {}),
                                note=prep_override.get("note", "configured for this domain"))
            else:
                step = prep.detect(blocks, store, dom)
        except (KeyError, ValueError) as exc:
            result.warnings.append(f"data preparation could not be set up: {exc}")
            step = None

    if step is not None:
        try:
            prepared = prep.apply_step(step, store)
            store.put(step.name, prepared)
            prep.retarget(step, blocks, store, list(prepared.columns))
            result.prep_step = step
            result.warnings.append(step.note)
            if prepared.empty:
                result.warnings.append(
                    f"the prepared dataset '{step.name}' has no records — check the step's inputs")
        except (KeyError, ValueError) as exc:
            result.warnings.append(
                f"data preparation ({step.op}) failed and was skipped: {exc}")
            step = None
            result.prep_step = None

    try:
        base_name, notes = choose_base(dom, blocks, store,
                                       base_override or (step.name if step else ""))
    except ValueError as exc:
        result.error = str(exc)
        return result
    result.base_dataset = base_name
    if not step:
        result.warnings.extend(notes)

    try:
        base = store.get(base_name)
    except Exception as exc:                                   # noqa: BLE001
        result.error = f"could not read raw dataset '{base_name}': {exc}"
        return result
    if base.empty:
        result.warnings.append(f"base dataset '{base_name}' has no records")

    # A source the spec names but the study did not collect is reported, not executed.
    # the key derivations know their own conventions — never unmap them to a missing column
    for _b in blocks:
        if _b.variable in ("STUDYID", "USUBJID") and _b.mtype == "assign":
            key = store.resolve(_b.dataset)
            if not key or upper(_b.column) not in {upper(c) for c in store.columns(key)}:
                _b.mtype, _b.recipe, _b.dataset, _b.column = "derived", _b.variable.lower(), "", ""

    # The spec's SAS code is the most specific statement of intent it makes — honour it before
    # anything else looks at the block.
    compiled, refused = translate_mod.apply_sas_code(blocks, store, base_name)
    if compiled:
        result.warnings.append(f"{compiled} variable(s) are mapped from the spec's SAS code")
    if refused:
        result.warnings.append(
            f"{refused} variable(s) have SAS code this engine cannot interpret deterministically "
            "— map them by hand in the domain view rather than trusting a guess")

    missing = automap.unmap_missing_sources(blocks, store)
    if missing:
        result.warnings.append(
            f"{missing} variable(s) name a raw source that is not in this raw data — they are "
            "reported as not built rather than failing at execution time")

    # Layer 1: among the sources the spec lists, prefer the one that exists and fits the name.
    repointed = automap.refine_listed_sources(blocks, store)
    if repointed:
        result.warnings.append(
            f"{repointed} variable(s) were repointed to a better-matching source the spec also lists")

    passes.run_all(blocks, store, base_name, dom)

    # The reader's own pipeline overrides the spec's sources: wherever the final prepared
    # dataset carries a mapping's column, the mapping reads from it. Done after the automatic
    # passes (so nothing repoints it back) and before hand edits (so those still win).
    if prep_final:
        moved = prep.retarget_to_output(blocks, store, prep_final)
        if moved:
            result.warnings.append(
                f"{moved} variable(s) now read from the prepared dataset '{prep_final}' — "
                "its columns match their sources. A variable it does not carry keeps its "
                "raw source.")

    # Standard derivations from the company SAS templates fill what the spec leaves without
    # a workable mapping — before name matching, because a documented template rule outranks
    # a fuzzy guess. Each application is labelled and editable like any other mapping.
    from . import templates as templates_mod
    applied = templates_mod.apply_templates(blocks, store, dom, base_name,
                                            overrides=template_overrides)
    if applied:
        result.warnings.append(
            f"{len(applied)} variable(s) use standard template derivations: "
            + "; ".join(applied))

    # The user's own function library — deliberate rules, so they outrank the built-in
    # templates and the name-match guesses that follow.
    custom_applied = templates_mod.apply_custom_fns(blocks, custom_fns, dom)
    if custom_applied:
        result.warnings.append(
            f"{len(custom_applied)} variable(s) filled by your custom functions: "
            + "; ".join(custom_applied))

    # Layer 2: variables the spec leaves unmapped get a name-matched source. This is a GUESS
    # and is labelled as one everywhere it appears — see automap.name_match_unmapped.
    guessed = automap.name_match_unmapped(blocks, store, base_name,
                                          threshold=name_match_threshold)
    if guessed:
        result.warnings.append(
            f"{guessed} variable(s) have no source in the spec and were matched to a raw column "
            f"by name similarity (at or above {name_match_threshold}%). Check them: agreement "
            "with the vendor on a guessed mapping is not evidence that the spec was followed.")

    # User edits are applied LAST so they win over the automatic repair passes — otherwise a
    # deliberate --DTC mapping would be silently overwritten by the ISO pass.
    _apply_edits(blocks, edits, dom, result)

    # A hand edit may point a variable at the user's function library by name (recipe
    # 'custom_fn'). Resolve the name to the function's CURRENT steps here, so editing the
    # function later changes every variable that uses it on the next build.
    for b in blocks:
        if b.recipe == "custom_fn":
            name = s((b.args or {}).get("name"))
            fn = (custom_fns or {}).get(name)
            if fn is None:
                result.warnings.append(
                    f"{b.variable}: '{name or '?'}' is not in your function library")
                continue
            b.args = {**(b.args or {}), "steps": [dict(st) for st in (fn.get("steps") or [])]}
            b.method_source = "custom"
            b.reason = (f"your function '{name}'"
                        + (f": {fn['description']}" if fn.get("description") else ""))

    ctx = BuildContext(domain=dom, store=store, base=base,
                       frame=pd.DataFrame(index=base.index),
                       built=dict(built or {}), codelists=spec.codelists, studyid=studyid)

    # --SEQ is numbered after sorting; --LOBXFL depends on columns derived later in spec order
    seq_blocks = [b for b in blocks if b.mtype == "sequence"]
    late_blocks = [b for b in blocks if b.recipe in ("lobxfl", "age")]
    main_blocks = [b for b in blocks if b not in seq_blocks and b not in late_blocks]

    for b in main_blocks + late_blocks:
        _run_block(ctx, b)

    ctx.frame = _finalize(ctx.frame, sort_by, dedup)
    ctx.base = ctx.base.reindex(ctx.frame.index)               # keep sources row-aligned
    for b in seq_blocks:
        _run_block(ctx, b)

    full = ctx.frame
    result.supp = _supp_from_built(dom, full, blocks)

    # A submission-shaped SDTM dataset carries EVERY variable the spec defines for the domain,
    # in spec order — a variable that could not be populated is an empty column, not an absent
    # one. Only an explicit spec DROP removes a variable from the structure.
    keep = [b.variable for b in blocks
            if not b.supp and (b.status in ("built", "empty")
                               or (include_unbuilt and b.status in ("not_built", "error")))]
    for v in keep:                                              # unbuilt -> explicit empty column
        if v not in full.columns:
            full[v] = pd.Series(pd.NA, index=full.index, dtype="string")
    seen, order = set(), []
    for v in keep:
        if v not in seen:
            seen.add(v)
            order.append(v)
    result.dataset = full[order].reset_index(drop=True) if order else full.reset_index(drop=True)
    return result


def _run_block(ctx: BuildContext, b: Block) -> None:
    """Execute one block, recording its outcome on the block itself."""
    if b.mtype == "drop":
        b.status = "dropped"
        b.reason = b.reason or "excluded by the mapping spec"
        return
    if b.mtype == "unmapped":
        b.status = "not_built"
        b.method = "—"
        if not b.reason:                       # never leave a gap without a stated cause
            b.reason = ("the spec states no source and no rule this engine can act on"
                        if not b.input_variables else
                        f"none of the sources the spec lists resolved: {b.input_variables[:70]}")
        return
    try:
        ser = ops.execute(ctx, b)
    except OpError as exc:
        b.status, b.error = "not_built", str(exc)
        b.method = b.describe_source()
        return
    except (KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
        b.status, b.error = "error", f"{type(exc).__name__}: {exc}"
        b.method = b.describe_source()
        return
    if not isinstance(ser, pd.Series):
        ser = pd.Series(ser, index=ctx.index)
    ctx.frame[b.variable] = ser.reindex(ctx.frame.index) if len(ctx.frame.columns) else ser.values
    b.status = "built"
    b.method = b.describe_source()

    # A mapping that ran without error but populated NOTHING is the quietest kind of wrong:
    # the variable reads as built, the column is empty, and nobody looks again. Say so.
    if b.mtype != "drop" and len(ctx.frame.index):
        col = ctx.frame[b.variable]
        populated = int(col.astype("string").str.strip().ne("").fillna(False).sum())
        if populated == 0:
            b.status = "empty"
            b.reason = (b.reason or "") + (" · " if b.reason else "") + (
                "this mapping ran but produced no values — check the source has data for "
                "these records")


def _apply_edits(blocks: list[Block], edits: dict[str, dict] | None,
                 domain: str, result: "DomainResult") -> None:
    """Overlay the user's per-variable mapping edits onto the spec-derived blocks."""
    if not edits:
        return
    by_var = {b.variable: b for b in blocks}
    for var, edit in edits.items():
        v = upper(var)
        b = by_var.get(v)
        if b is None:
            result.warnings.append(f"edit for {v} ignored — it is not a variable in this domain's spec")
            continue
        b.spec_method = b.describe_source()
        b.mtype = s(edit.get("mtype")) or b.mtype
        for key in ("dataset", "column", "value", "recipe", "codelist"):
            if key in edit:
                b.__dict__[key] = s(edit.get(key))
        if "args" in edit:
            b.args = edit.get("args") or {}
        if b.mtype != "derived":
            # keep manual CT mappings — they apply to plain assigns too (op_assign
            # reads args.ct_overrides when normalising to the codelist)
            b.recipe, b.args = "", {k: v for k, v in (b.args or {}).items()
                                    if k == "ct_overrides"}
        b.edited = True
        b.method_source = "edit"
        b.confidence = 100
        b.edit_note = s(edit.get("note")) or f"edited by hand (spec said: {b.spec_method})"
    n = sum(1 for b in blocks if b.edited)
    if n:
        result.warnings.append(
            f"{n} variable(s) in {domain} are mapped by hand, not by the spec — "
            "the comparison for this domain is no longer fully independent of your own work.")


def _finalize(df: pd.DataFrame, sort_by: list[str] | None,
              dedup: dict | None = None) -> pd.DataFrame:
    """Dataset-level finishing applied after the columns are built and before --SEQ:
    sort, then optionally keep only the first or last record per group."""
    if sort_by:
        cols = [c for c in (upper(x) for x in sort_by) if c in df.columns]
        if cols:
            df = df.sort_values(by=cols, kind="stable", na_position="last")
    dd = dedup or {}
    keys = [k for k in (upper(x) for x in (dd.get("keys") or [])) if k in df.columns]
    if dd.get("enabled") and keys:
        df = df.drop_duplicates(subset=keys, keep=(dd.get("keep") or "first"))
    return df


def _supp_from_built(domain: str, df: pd.DataFrame, blocks: list[Block]) -> pd.DataFrame | None:
    """Transpose the parent's QNAM columns into the standard SUPP-- structure: one row per
    (record, QNAM) where QVAL is present, linked back through IDVAR/IDVARVAL."""
    qnam = [b for b in blocks if b.supp and b.variable in df.columns
            and b.status in ("built", "empty")]
    if not qnam:
        return None
    dom = upper(domain)
    if "USUBJID" not in df.columns:
        return pd.DataFrame(columns=SUPP_COLUMNS)
    seq_col = f"{dom}SEQ" if f"{dom}SEQ" in df.columns else ""
    idvar = seq_col

    def blank(v) -> bool:
        return s(v) == ""

    out = []
    for _, row in df.iterrows():
        subj = row["USUBJID"]
        if blank(subj):
            continue
        sid = "" if "STUDYID" not in df.columns or blank(row["STUDYID"]) else s(row["STUDYID"])
        idval = "" if not seq_col or blank(row[seq_col]) else s(row[seq_col]).split(".")[0]
        for b in qnam:
            val = row[b.variable]
            if blank(val):                       # SUPP carries only present qualifier values
                continue
            out.append({"STUDYID": sid, "RDOMAIN": dom, "USUBJID": s(subj),
                        "IDVAR": idvar, "IDVARVAL": idval,
                        "QNAM": b.variable, "QLABEL": b.qlabel or b.label or "",
                        "QVAL": val, "QORIG": b.qorig or b.origin or "CRF", "QEVAL": ""})
    supp = pd.DataFrame(out, columns=SUPP_COLUMNS)
    if len(supp):
        supp = supp.sort_values(["USUBJID", "QNAM", "IDVARVAL"], kind="stable").reset_index(drop=True)
    return supp


# ── study-level build ───────────────────────────────────────────────────────
def order_domains(domains: list[str]) -> list[str]:
    """DM and the other reference domains first, then everything else alphabetically."""
    want = [d for d in PRIORITY_DOMAINS if d in domains]
    return want + sorted(d for d in domains if d not in want)


def build_study(spec: Spec, store: RawStore, domains: list[str] | None = None,
                studyid: str = "", base_overrides: dict[str, str] | None = None,
                sort_overrides: dict[str, list[str]] | None = None,
                include_unbuilt: bool = True,
                prep_modes: dict[str, str] | None = None,
                prep_overrides: dict[str, dict] | None = None,
                prep_pipelines: dict[str, list] | None = None,
                edits: dict[str, dict] | None = None,
                dedups: dict[str, dict] | None = None,
                custom_fns: dict | None = None,
                template_overrides: dict | None = None,
                name_match_threshold: int = automap.DEFAULT_THRESHOLD,
                progress=None) -> dict[str, DomainResult]:
    """Build every requested domain, in dependency order. A domain that fails is recorded
    and the run continues — one bad spec sheet must not hide the rest of the delivery."""
    # No explicit list means "the study's domains" — and the spec's TOC is the authority on
    # that. A domain the TOC marks Active = N is deliberately out of this study; building it
    # anyway would report spec gaps for work nobody was meant to do.
    if domains is None and spec.toc:
        targets = [upper(d) for d in spec.active_domains]
    else:
        targets = [upper(d) for d in (domains or spec.domain_names)]
    targets = [d for d in order_domains(targets)]
    base_overrides = {upper(k): v for k, v in (base_overrides or {}).items()}
    sort_overrides = {upper(k): v for k, v in (sort_overrides or {}).items()}
    prep_modes = {upper(k): v for k, v in (prep_modes or {}).items()}
    prep_overrides = {upper(k): v for k, v in (prep_overrides or {}).items()}
    prep_pipelines = {upper(k): v for k, v in (prep_pipelines or {}).items()}
    edits = {upper(k): v for k, v in (edits or {}).items()}
    dedups = {upper(k): v for k, v in (dedups or {}).items()}

    built: dict[str, pd.DataFrame] = {}
    results: dict[str, DomainResult] = {}
    for dom in targets:
        if progress:
            progress(dom)
        res = build_domain(spec, store, dom, built=built, studyid=studyid,
                           base_override=base_overrides.get(dom, ""),
                           sort_by=sort_overrides.get(dom),
                           include_unbuilt=include_unbuilt,
                           prep_mode=prep_modes.get(dom, "auto"),
                           prep_override=prep_overrides.get(dom),
                           prep_steps=prep_pipelines.get(dom),
                           edits=edits.get(dom), dedup=dedups.get(dom),
                           custom_fns=custom_fns, template_overrides=template_overrides,
                           name_match_threshold=name_match_threshold)
        results[dom] = res
        if res.ok:
            built[dom] = res.dataset
            if res.supp is not None and len(res.supp):
                built[f"SUPP{dom}"] = res.supp
    return results
