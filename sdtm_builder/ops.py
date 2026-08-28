"""Deterministic operations. One named function per mapping decision.

Nothing here compiles or exec()s a string: a Block names an operation, the operation runs.
That is what makes a build reproducible and reviewable — you can read the function that
produced any column in the output.

Semantics are ported from SDTM Designer's code generator (`_code_from_struct` and friends
in sdtm_pred/backend/main.py) so a spec that built correctly there builds the same here.
"""
from __future__ import annotations

import pandas as pd

from .blocks import Block
from .util import as_float, as_int, blank_mask, empty_str_series, s, str_series, upper

# subject-key candidates, best first — used to align a column from another dataset
SUBJECT_KEYS = ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID", "SUBJECT_ID", "SUBJ_ID",
                "SCRNID", "SCREENINGNUMBER")

# EDC systems spell the study and subject keys many ways; these are the ones worth trying
# before declaring a study key absent.
STUDYID_COLUMNS = ("STUDYID", "STUDY_ID", "STUDYIDENTIFIER", "STUDY", "PROTOCOL", "PROTOCOLID")
SUBJID_COLUMNS = ("SUBJID", "SUBJECT_ID", "SUBJECTID", "X_SUBJID", "SUBJ_ID",
                  "SUBJECTNUMBER", "SUBJECT", "PATIENTID", "PATIENT_ID")

# tokens that mean "this date part is unknown"; a known year survives an unknown day
ISO_BAD_TOKENS = {"", "NAN", "NONE", "NAT", "UN", "UNK", "UNKN", "UNKNOWN", ".", "--", "NA", "NULL"}


# the SAS-style functions op_fn implements — the editor offers exactly this set
SAS_FUNCTIONS = {
    "substr", "scan", "strip", "trim", "left", "compress", "upcase", "lowcase", "propcase",
    "reverse", "length", "index", "tranwrd", "catx", "cats", "cat", "coalesce", "compbl",
    "zeropad", "put", "input",
}


class OpError(Exception):
    """A mapping could not be executed. Carries a message a clinical programmer can act on."""


# ── ISO 8601 ────────────────────────────────────────────────────────────────
def iso_parts(y=None, m=None, d=None, t=None) -> pd.Series:
    """PARTIAL-aware ISO 8601 date(time) from raw year/month/day(/time) components.

    Never fabricates precision: emits YYYY, YYYY-MM or YYYY-MM-DD depending on which parts
    are present, and appends THH:MM only when a full date and a real time both exist."""
    def norm(ser, width):
        if ser is None:
            return None
        t2 = ser.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)  # '4.0'->'4'
        t2 = t2.where(~(t2.isna() | t2.str.upper().isin(sorted(ISO_BAD_TOKENS))), "")
        return t2.mask(t2 != "", t2.str.zfill(width))

    Y, M, D = norm(y, 4), norm(m, 2), norm(d, 2)
    idx = next((p.index for p in (Y, M, D) if p is not None), None)
    if Y is None or idx is None:
        return pd.Series([""] * (len(idx) if idx is not None else 0), index=idx, dtype="string")
    res = Y.copy()
    if M is not None:
        res = res.mask((Y != "") & (M != ""), Y + "-" + M)
        if D is not None:
            res = res.mask((Y != "") & (M != "") & (D != ""), Y + "-" + M + "-" + D)
    if t is not None:
        T = t.astype("string").str.strip()
        T = T.where(~(T.isna() | T.str.upper().isin(sorted(ISO_BAD_TOKENS))), "")
        res = res.mask((res.str.len() >= 10) & (T != ""), res + "T" + T)
    return res.fillna("")


def iso_from_text(ser: pd.Series) -> pd.Series:
    """Free-text date column -> ISO date. Unparseable values become blank, never a guess."""
    parsed = pd.to_datetime(ser, errors="coerce", format="mixed")
    return parsed.dt.strftime("%Y-%m-%d").astype("string").fillna("")


# ── source resolution ───────────────────────────────────────────────────────
def _subject_key(a: pd.DataFrame, b: pd.DataFrame) -> str | None:
    for k in SUBJECT_KEYS:
        if k in a.columns and k in b.columns:
            return k
    return None


def source_series(ctx, dataset: str, column: str) -> pd.Series:
    """A raw column aligned to the domain frame.

    Same dataset as the domain base (or row-identical): a straight column copy, all rows
    kept. A different dataset: one value per subject, looked up on the subject key — the
    row counts differ, so a positional copy would silently misalign the data."""
    col = upper(column)
    frame = ctx.raw(dataset)
    if col not in frame.columns:
        raise OpError(f"column {col} is not in raw dataset '{dataset}'")
    base = ctx.base
    if frame is base or (len(frame) == len(base) and frame.index.equals(base.index)):
        return frame[col]
    key = _subject_key(frame, base)
    if key is None:
        raise OpError(
            f"'{dataset}' has no subject key in common with this domain's base dataset, "
            f"so {col} cannot be aligned to its records"
        )
    src = frame.dropna(subset=[key]).drop_duplicates(key)
    lookup = pd.Series(src[col].values, index=src[key].values)   # works even when col == key
    return base[key].map(lookup)


def _series_for(ctx, spec: dict, selfvar: str = "", cast: str = "string") -> pd.Series:
    """One transform input: the running value ('self'), an earlier pipeline step, an
    already-built SDTM variable, a literal, or a raw column."""
    spec = spec or {}
    kind = str(spec.get("kind") or "").lower()
    idx = ctx.index

    def out(ser):
        return str_series(ser) if cast == "string" else ser

    if kind == "self" and selfvar:
        return out(ctx.frame.get(selfvar, empty_str_series(idx)))
    if kind == "step":
        n = as_int(spec.get("step"), 0)
        return out(ctx.registers.get(n, empty_str_series(idx)))
    if kind == "var" or (not kind and spec.get("var")):
        name = upper(spec.get("var"))
        if name in ctx.frame.columns:
            return out(ctx.frame[name])
        raise OpError(f"SDTM variable {name} is not built yet — it must appear earlier in the spec")
    if kind == "text" or (not kind and spec.get("text") is not None and not spec.get("column")):
        return pd.Series(s(spec.get("text")), index=idx, dtype="string")
    ds, col = s(spec.get("dataset")), s(spec.get("column"))
    if ds and col:
        return out(source_series(ctx, ds, col))
    return empty_str_series(idx)


def apply_codelist(ctx, ser: pd.Series, codelist: str, overrides: dict | None = None) -> pd.Series:
    """Normalise raw entries to CT submission values ('male'/'M'/'Male' -> 'M').
    A value with no CT match is passed through unchanged and flagged later by validation —
    never dropped, never invented."""
    cmap = dict(ctx.codelists.get(upper(codelist), {}))
    for k, v in (overrides or {}).items():
        if s(k):
            cmap[upper(k)] = v
    if not cmap:
        return ser
    text = str_series(ser).str.upper()
    return text.map(cmap).fillna(str_series(ser))


# ── operations ──────────────────────────────────────────────────────────────
def op_constant(ctx, b: Block) -> pd.Series:
    val = b.value if b.mtype == "constant" else b.args.get("value", "")
    return pd.Series(s(val), index=ctx.index, dtype="string")


def op_assign(ctx, b: Block) -> pd.Series:
    if not (b.dataset and b.column):
        raise OpError("no source dataset/column resolved for this variable")
    val = source_series(ctx, b.dataset, b.column)
    if b.codelist:
        return apply_codelist(ctx, val, b.codelist, (b.args or {}).get("ct_overrides"))
    # SAS STRIP on character sources; numeric copied as-is
    if pd.api.types.is_numeric_dtype(val) and not pd.api.types.is_bool_dtype(val):
        return val
    return str_series(val)


def op_sequence(ctx, b: Block) -> pd.Series:
    """--SEQ: 1..n within the grouping variable (USUBJID by default), in final row order."""
    grp = upper((b.args or {}).get("group") or "USUBJID")
    if grp in ctx.frame.columns:
        return (ctx.frame.groupby(grp, dropna=False).cumcount() + 1).astype("Int64")
    return pd.Series(range(1, len(ctx.index) + 1), index=ctx.index, dtype="Int64")


def _date_parts_for(ctx, dataset: str, date_col: str) -> dict:
    """Given one date column, find the sibling parts the form actually collected.

    Naming the whole date should be enough: if the form split it into
    AESTDAT_YYYY / _MM / _DD, those are what preserve a partial date, and asking the user to
    name each one is asking them to do the engine's job."""
    key = ctx.store.resolve(dataset)
    if not key:
        return {}
    cols = {upper(c): c for c in ctx.store.columns(key)}
    base = upper(date_col)
    # the reader may name the whole date, its raw form, or any one of its parts — all of them
    # point at the same underlying date, so reduce whichever was picked to the common stem
    for suffix in ("_RAW", "_YYYY", "_YY", "_MM", "_DD", "_STD"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    found = {}
    for suffix, arg in (("_YYYY", "y_col"), ("_YY", "y_col"), ("_MM", "m_col"), ("_DD", "d_col")):
        if arg not in found and base + suffix in cols:
            found[arg] = cols[base + suffix]
    if "y_col" not in found:
        return {}                       # no year part means there is nothing to assemble
    stem = base[:-3] if base.endswith("DAT") else base
    for suffix in ("TIM", "TIME", "RTIM"):
        if stem + suffix in cols:
            found["time_col"] = cols[stem + suffix]
            break
    return found


def op_iso_date(ctx, b: Block) -> pd.Series:
    a = b.args or {}
    ds = s(a.get("dataset")) or b.dataset
    ycol, mcol, dcol = upper(a.get("y_col")), upper(a.get("m_col")), upper(a.get("d_col"))
    date_col, time_col = upper(a.get("date_col")), upper(a.get("time_col"))

    # only a whole-date column was named — look for the parts the form collected beside it
    if date_col and not ycol:
        parts = _date_parts_for(ctx, ds, date_col)
        if parts:
            ycol = upper(parts.get("y_col"))
            mcol = upper(parts.get("m_col"))
            dcol = upper(parts.get("d_col"))
            time_col = time_col or upper(parts.get("time_col"))

    tser = source_series(ctx, ds, time_col) if time_col else None

    if ycol:                                    # component columns keep partial dates
        return iso_parts(
            y=source_series(ctx, ds, ycol),
            m=source_series(ctx, ds, mcol) if mcol else None,
            d=source_series(ctx, ds, dcol) if dcol else None,
            t=tser,
        )
    if not date_col:
        raise OpError("no raw date column resolved for this --DTC variable")
    dates = iso_from_text(source_series(ctx, ds, date_col))
    if tser is None:
        return dates
    t = str_series(tser)
    t = t.where(~(t.isna() | t.str.upper().isin(sorted(ISO_BAD_TOKENS))), "")
    full = dates.str.len() >= 10
    return dates.mask(full & (t != ""), dates + "T" + t)


def op_study_day(ctx, b: Block) -> pd.Series:
    """--DY = event date - reference date, +1 on or after the reference (no day 0).
    The reference (RFSTDTC by default) comes from the built DM domain, per subject."""
    a = b.args or {}
    dtc = upper(a.get("dtc_var"))
    ref_var = upper(a.get("ref_var")) or "RFSTDTC"
    if not dtc:
        raise OpError("could not determine the event --DTC variable for this study day")
    if dtc not in ctx.frame.columns:
        raise OpError(f"{dtc} is not built in this domain, so {b.variable} cannot be derived")

    ev = pd.to_datetime(str_series(ctx.frame[dtc]).str[:10], errors="coerce", format="mixed")
    ref = ctx.reference_dates(ref_var)
    if ref is None:
        if ref_var in ctx.frame.columns:
            ref_raw = ctx.frame[ref_var]
        else:
            raise OpError(
                f"reference date {ref_var} is unavailable — build DM before {b.domain} "
                f"so {b.variable} can be derived from it"
            )
    else:
        if "USUBJID" not in ctx.frame.columns:
            raise OpError(f"USUBJID is not built, so {ref_var} cannot be looked up per subject")
        ref_raw = ctx.frame["USUBJID"].map(ref)
    rf = pd.to_datetime(str_series(ref_raw).str[:10], errors="coerce", format="mixed")
    diff = (ev - rf).dt.days
    return diff.where(diff < 0, diff + 1).astype("Int64")


def op_lobxfl(ctx, b: Block) -> pd.Series:
    """--LOBXFL: 'Y' on the last non-missing observation on or before first exposure."""
    a = b.args or {}
    tc, dtc = upper(a.get("testcd_var")), upper(a.get("dtc_var"))
    res_var = upper(a.get("result_var"))
    ref_var = upper(a.get("ref_var")) or "RFXSTDTC"
    if not (tc and dtc):
        raise OpError("--LOBXFL needs both a --TESTCD and a --DTC variable")
    f = ctx.frame
    missing = [c for c in (tc, dtc, "USUBJID") if c not in f.columns]
    if missing:
        raise OpError(f"--LOBXFL needs {', '.join(missing)}, which {b.domain} does not have")

    ref = ctx.reference_dates(ref_var)
    if ref is None:
        raise OpError(f"first-exposure date {ref_var} is unavailable — build DM first")
    rfx = pd.to_datetime(str_series(f["USUBJID"].map(ref)).str[:10], errors="coerce", format="mixed")
    obs = pd.to_datetime(str_series(f[dtc]).str[:10], errors="coerce", format="mixed")
    res = str_series(f[res_var]) if res_var in f.columns else pd.Series("x", index=f.index, dtype="string")

    group = [c for c in (upper(g) for g in (a.get("group_vars") or ["USUBJID", tc])) if c in f.columns]
    if not group:
        raise OpError("--LOBXFL has no grouping variables available")

    out = pd.Series("", index=f.index, dtype="string")
    eligible = obs.notna() & rfx.notna() & (obs <= rfx) & ~blank_mask(res)
    work = f[group].copy()
    work["__obs"] = obs
    picked = work[eligible]
    if len(picked):
        last = picked.sort_values("__obs", kind="stable").groupby(group, dropna=False).tail(1)
        out.loc[last.index] = "Y"
    return out


def op_concat(ctx, b: Block) -> pd.Series:
    a = b.args or {}
    ds = s(a.get("dataset")) or b.dataset
    cols = [upper(c) for c in (a.get("columns") or []) if s(c)]
    sep = a.get("sep", " ")
    if not cols:
        raise OpError("no columns given to concatenate")
    parts = [str_series(source_series(ctx, ds, c)).fillna("") for c in cols]
    joined = parts[0]
    for p in parts[1:]:
        joined = joined + sep + p
    return joined


def op_date_extreme(ctx, b: Block) -> pd.Series:
    """Earliest / latest date per subject, pooled across any number of raw datasets.

    Every dataset must be joined to this domain on a key they BOTH carry. Picking a key per
    dataset independently is what silently produces an empty column: the base keyed on
    USUBJID and a source keyed on SUBJID look fine on their own and match nothing."""
    a = b.args or {}
    func = str(a.get("func", "min")).lower()
    if func not in ("min", "max"):
        raise OpError(f"take the earliest (min) or the latest (max), not '{func}'")
    srcs = [x for x in (a.get("sources") or []) if s(x.get("dataset")) and s(x.get("date_col"))]
    if not srcs:
        raise OpError("no dataset and date-column pairs were given")

    wanted = [upper(g) for g in (a.get("group_by") or []) if s(g)]
    base_keys = [k for k in (wanted or SUBJECT_KEYS) if k in ctx.base.columns]
    if not base_keys:
        # the built frame may carry a key the raw base does not, e.g. a composed USUBJID
        for k in (wanted or SUBJECT_KEYS):
            if k in ctx.frame.columns:
                base_keys = [k]
                break
    if not base_keys:
        raise OpError("this domain's records carry no subject key to group the dates by")

    # Each dataset is reduced to one date per subject and aligned to THIS domain's records on
    # whichever key the two share. Aggregating on one key and joining on another is what
    # produced an empty column: both steps look right, and nothing matches.
    aligned, used, skipped = [], [], []
    for src in srcs:
        name = src["dataset"]
        try:
            frame = ctx.raw(name)
        except KeyError:
            skipped.append(f"{name} (not in the raw data)")
            continue
        col = _find_column(frame, (upper(src["date_col"]),))
        if not col:
            skipped.append(f"{name}.{src['date_col']} (column not there)")
            continue
        key = next((k for k in base_keys if k in frame.columns), None)
        if key is None:
            key = next((k for k in SUBJECT_KEYS
                        if k in frame.columns and k in ctx.base.columns), None)
        if key is None:
            skipped.append(f"{name} (no subject key shared with {ctx.domain})")
            continue

        dates = pd.to_datetime(frame[col], errors="coerce", format="mixed")
        if dates.notna().sum() == 0:
            skipped.append(f"{name}.{col} (no readable dates)")
            continue
        per_subject = (pd.DataFrame({"__s": frame[key].astype("string"), "__d": dates})
                       .dropna(subset=["__s", "__d"]).groupby("__s")["__d"].agg(func))
        lhs = (ctx.base[key] if key in ctx.base.columns else ctx.frame[key]).astype("string")
        aligned.append(lhs.map(per_subject))
        used.append(f"{name}.{col}")

    if not aligned:
        raise OpError("none of the given datasets could be used: " + "; ".join(skipped))

    pooled = pd.concat(aligned, axis=1)
    out = pooled.max(axis=1) if func == "max" else pooled.min(axis=1)
    if out.notna().sum() == 0:
        raise OpError(
            f"dates were read from {', '.join(used[:3])} but none matched a record in "
            f"{ctx.domain}. Check that these datasets identify subjects the same way "
            f"({ctx.domain} uses {', '.join(base_keys[:2])}).")
    if skipped:
        b.reason = (b.reason or "") + (" · skipped " + "; ".join(skipped[:3]))
    return out.dt.strftime("%Y-%m-%d").astype("string").fillna("")


def op_copy_var(ctx, b: Block) -> pd.Series:
    src = upper((b.args or {}).get("source_var"))
    if src not in ctx.frame.columns:
        raise OpError(f"{src} is not built yet — it must appear earlier in the spec than {b.variable}")
    val = ctx.frame[src]
    return apply_codelist(ctx, val, b.codelist) if b.codelist else val


def op_sdtm_ref(ctx, b: Block) -> pd.Series:
    """Pull a variable from another already-built SDTM domain, matched on USUBJID."""
    a = b.args or {}
    dom, var = upper(a.get("source_domain")), upper(a.get("source_var"))
    other = ctx.built.get(dom)
    if other is None:
        raise OpError(f"{dom} has not been built yet — build it before {b.domain}")
    if var not in other.columns:
        raise OpError(f"{var} is not present in the built {dom} dataset")
    if "USUBJID" not in other.columns or "USUBJID" not in ctx.frame.columns:
        raise OpError(f"USUBJID is needed on both {dom} and {b.domain} to carry {var} across")
    src = other.dropna(subset=["USUBJID"]).drop_duplicates("USUBJID")
    return ctx.frame["USUBJID"].map(pd.Series(src[var].values, index=src["USUBJID"].values))


def _find_column(frame: pd.DataFrame, candidates) -> str:
    cols = {upper(c): c for c in frame.columns}
    for cand in candidates:
        if cand in cols:
            return cols[cand]
    return ""


def op_studyid(ctx, b: Block) -> pd.Series:
    """STUDYID from --studyid, else whichever spelling of a study key the raw data carries."""
    if ctx.studyid:
        return pd.Series(ctx.studyid, index=ctx.index, dtype="string")
    col = _find_column(ctx.base, STUDYID_COLUMNS)
    if col:
        return str_series(ctx.base[col])
    for name in ctx.store.refs:                      # any dataset that has one
        frame_cols = {upper(c): c for c in ctx.store.columns(name)}
        for cand in STUDYID_COLUMNS:
            if cand in frame_cols:
                return source_series(ctx, name, frame_cols[cand])
    raise OpError("no study identifier in the raw data — set STUDYID explicitly")


def op_usubjid(ctx, b: Block) -> pd.Series:
    """USUBJID: carried from the raw data when it is there, otherwise composed from the study
    and subject identifiers using the SDTM convention STUDYID-SUBJID.

    Composing it is recorded as a convention, not as something the spec said, because the
    vendor may compose it differently — and if they do, every record will fail to match, which
    is exactly the finding you want surfaced rather than hidden."""
    col = _find_column(ctx.base, ("USUBJID",))
    if col:
        return str_series(ctx.base[col])

    subj_col = _find_column(ctx.base, SUBJID_COLUMNS)
    if subj_col:
        subj = str_series(ctx.base[subj_col])
        study = (ctx.frame["STUDYID"] if "STUDYID" in ctx.frame.columns
                 else pd.Series(ctx.studyid, index=ctx.index, dtype="string"))
        study = str_series(study)
        if not blank_mask(study).all():
            b.method_source = "convention"
            b.confidence = 80
            b.reason = (f"composed as STUDYID-{subj_col} (SDTM convention) — the raw data has no "
                        "USUBJID. Confirm the vendor composes it the same way.")
            return (study.fillna("") + "-" + subj.fillna("")).astype("string")

    dm = ctx.built.get("DM")
    key = _find_column(ctx.base, SUBJID_COLUMNS[1:])
    if dm is not None and key and "USUBJID" in dm.columns:
        dm_key = _find_column(dm, SUBJID_COLUMNS)
        if dm_key:
            src = dm.dropna(subset=[dm_key]).drop_duplicates(dm_key)
            b.method_source = "convention"
            b.reason = f"carried from the built DM on {dm_key}"
            return str_series(
                ctx.base[key].map(pd.Series(src["USUBJID"].values, index=src[dm_key].values)))

    raise OpError(
        "USUBJID is not in this domain's raw data, and no study/subject identifier pair was "
        "found to compose it from — add a raw.<dataset>.USUBJID Input Variable to the spec")


def _impute_partial_date(series) -> pd.Series:
    """A partial ISO date completed for arithmetic: a bare year becomes 01 January, a
    year-month becomes the 1st. Deliberate and explicit — AGE is whole years anyway, so
    imputing the earliest day of the known period is the standard convention, and relying on
    whatever a parser happens to assume is how a library upgrade changes ages silently."""
    text = series.astype("string").str.strip()
    m = text.str.extract(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")
    completed = (m[0] + "-" + m[1].fillna("1").str.zfill(2)
                 + "-" + m[2].fillna("1").str.zfill(2))
    return pd.to_datetime(completed, errors="coerce", format="%Y-%m-%d")


def op_age(ctx, b: Block) -> pd.Series:
    """AGE as the company DM template derives it: the reported age wherever the study
    collected one, and whole years from birth to the reference date for the records where it
    did not — anniversary rule (SAS YRDIF 'AGE'), never a fraction. A plain copy of the
    reported column would leave every uncollected age blank; deriving everything would ignore
    what was actually reported. The template does both, so this does both."""
    a = b.args or {}

    reported = None
    age_col = upper(a.get("age_col"))
    if age_col:
        ds = s(a.get("age_dataset"))
        try:
            src = (source_series(ctx, ds, age_col) if ds
                   else ctx.base[_find_column(ctx.base, (age_col,))])
            reported = pd.to_numeric(src, errors="coerce")
        except (OpError, KeyError):
            reported = None

    derived = None
    birth_var = upper(a.get("birth_var")) or "BRTHDTC"
    ref_var = upper(a.get("ref_var")) or "RFSTDTC"
    if birth_var in ctx.frame.columns and ref_var in ctx.frame.columns:
        birth = _impute_partial_date(ctx.frame[birth_var])
        ref = _impute_partial_date(ctx.frame[ref_var])
        years = ref.dt.year - birth.dt.year
        before = ((ref.dt.month < birth.dt.month)
                  | ((ref.dt.month == birth.dt.month) & (ref.dt.day < birth.dt.day)))
        derived = (years - before.astype("int")).astype("Int64")

    if reported is not None and derived is not None:
        return reported.astype("Int64").combine_first(derived)
    if reported is not None:
        return reported.astype("Int64")
    if derived is not None:
        return derived
    raise OpError(
        f"AGE needs a reported age column, or {birth_var} and {ref_var} built in this domain")


# ── SAS-style functions (recipe='fn') ───────────────────────────────────────
def op_fn(ctx, b: Block) -> pd.Series:
    a = b.args or {}
    fn = str(a.get("fn", "")).lower()
    v = b.variable
    srcs = a.get("sources") or []
    if not fn:
        raise OpError("no function named")
    if not srcs:
        if fn in ("catx", "cats", "cat", "coalesce"):
            raise OpError(f"{fn} needs at least one input")
        srcs = [{}]
    parts = [_series_for(ctx, x, v, "string") for x in srcs]
    raw = [_series_for(ctx, x, v, "raw") for x in srcs]
    return apply_sas_function(fn, parts, a, raw=raw)


def apply_sas_function(fn: str, parts: list, args: dict, raw: list | None = None) -> pd.Series:
    """The one SAS function engine — variable recipes and the prep Compute step both call
    this, so SUBSTR in one place can never behave differently from SUBSTR in another.
    `parts` are string-cast inputs in order; `raw` keeps the uncast originals for PUT."""
    a = args or {}
    if not parts:
        raise OpError(f"{fn or 'the function'} needs at least one input")

    def one(cast="string"):
        return parts[0] if cast == "string" else (raw or parts)[0]

    def many():
        return parts

    if fn == "catx":
        parts = [p.str.strip().fillna("") for p in many()]
        sep = str(a.get("sep", ""))
        stacked = pd.concat(parts, axis=1)
        return stacked.apply(lambda r: sep.join([str(x) for x in r if str(x) != ""]),
                             axis=1).astype("string")
    if fn in ("cats", "cat"):
        parts = many()
        parts = [p.str.strip() for p in parts] if fn == "cats" else parts
        joined = parts[0].fillna("")
        for p in parts[1:]:
            joined = joined + p.fillna("")
        return joined
    if fn == "coalesce":
        return pd.concat(many(), axis=1).bfill(axis=1).iloc[:, 0]
    if fn == "compbl":
        return one().str.replace(r"\s+", " ", regex=True).str.strip()
    if fn == "zeropad":
        return one().str.strip().str.zfill(as_int(a.get("width"), 0))
    if fn == "put":
        raw = one(cast="raw")
        return raw.map(lambda x: "" if pd.isna(x)
                       else (str(int(x)) if isinstance(x, float) and float(x).is_integer()
                             else str(x))).astype("string")
    if fn == "input":
        return pd.to_numeric(one(), errors="coerce")
    if fn == "substr":
        start = max(as_int(a.get("start"), 1) - 1, 0)          # SAS is 1-based
        length = s(a.get("len"))
        if length in ("", "0"):
            return one().str.slice(start)
        return one().str.slice(start, start + as_int(length))
    if fn == "scan":
        delim = a.get("delim") or " "
        word = as_int(a.get("word"), 1)
        return one().str.split(delim).str[word - 1 if word > 0 else word]
    if fn == "strip":
        return one().str.strip()
    if fn == "trim":
        return one().str.rstrip()
    if fn == "left":
        return one().str.lstrip()
    if fn == "upcase":
        return one().str.upper()
    if fn == "lowcase":
        return one().str.lower()
    if fn == "propcase":
        return one().str.title()
    if fn == "reverse":
        return one().str.slice(step=-1)
    if fn == "length":
        return one().str.len().astype("Int64")
    if fn == "index":
        return (one().str.find(s(a.get("find"))) + 1).astype("Int64")
    if fn == "tranwrd":
        return one().str.replace(s(a.get("find")), s(a.get("replace")), regex=False)
    if fn == "compress":
        chars = s(a.get("chars"))
        if not chars:
            return one().str.replace(r"\s+", "", regex=True)
        res = one()
        for ch in chars:
            res = res.str.replace(ch, "", regex=False)
        return res
    raise OpError(f"unsupported SAS function '{fn}'")


# ── conditional logic (recipe='cond') ───────────────────────────────────────
def _cond_mask(ctx, rule: dict, selfvar: str) -> pd.Series:
    op = str((rule or {}).get("op", "eq")).lower()
    text = _series_for(ctx, (rule or {}).get("src"), selfvar, "string")
    num = pd.to_numeric(_series_for(ctx, (rule or {}).get("src"), selfvar, "raw"), errors="coerce")
    val = (rule or {}).get("value", "")
    blank = blank_mask(text)

    if op == "missing":
        return blank
    if op == "notmissing":
        return ~blank
    if op in ("in", "notin"):
        items = [x.strip() for x in str(val).split(",") if x.strip()]
        hit = text.isin(items)
        return ~hit if op == "notin" else hit
    if op == "contains":
        return text.str.contains(str(val), na=False, regex=False)
    if op == "starts":
        return text.fillna("").str.startswith(str(val))
    if op == "ends":
        return text.fillna("").str.endswith(str(val))
    if op in ("gt", "lt", "ge", "le"):
        f = as_float(val)
        return {"gt": num > f, "lt": num < f, "ge": num >= f, "le": num <= f}[op]
    if op == "between":
        return num.between(as_float(val), as_float((rule or {}).get("value2")))
    if op == "ne":
        return text != str(val)
    return text == str(val)


def _result_series(ctx, res: dict, selfvar: str) -> pd.Series:
    if (res or {}).get("kind") == "missing":
        return empty_str_series(ctx.index)
    return _series_for(ctx, res, selfvar, "string")


def op_cond(ctx, b: Block) -> pd.Series:
    """if / else-if / else. First matching rule wins, as in a SAS IF-THEN-ELSE chain.
    A rule may carry extra conditions in rule['and'] — all must hold, as in
    IF not missing(RGMDTN) AND RGSCAT = '…' THEN …"""
    a = b.args or {}
    rules = a.get("rules") or []
    out = _result_series(ctx, a.get("else") or {"kind": "missing"}, b.variable)
    for rule in reversed(rules):                      # reverse so the FIRST rule wins
        mask = _cond_mask(ctx, rule, b.variable).fillna(False)
        for extra in (rule or {}).get("and") or []:   # compound IF … AND … AND …
            mask &= _cond_mask(ctx, extra, b.variable).fillna(False)
        out = out.mask(mask, _result_series(ctx, rule.get("then"), b.variable))
    return out


# ── pipeline (recipe='pipeline') ────────────────────────────────────────────
def op_pipeline(ctx, b: Block) -> pd.Series:
    """Chain operations on one variable: copy -> transform -> conditional. Each step can
    read the running value ('self') or any earlier step's output ('step')."""
    steps = (b.args or {}).get("steps") or []
    if not steps:
        raise OpError("pipeline has no steps")
    saved_regs = dict(ctx.registers)
    ctx.registers.clear()
    running = None
    try:
        for i, st in enumerate(steps, start=1):
            op = str(st.get("op", "fn")).lower()
            sub = Block(variable=b.variable, domain=b.domain, codelist=b.codelist)
            if op == "assign":
                sub.mtype, sub.dataset, sub.column = "assign", s(st.get("dataset")), s(st.get("column"))
            elif op == "constant":
                sub.mtype, sub.value = "constant", st.get("value", "")
            else:
                sub.mtype, sub.recipe, sub.args = "derived", op, (st.get("args") or {})
            if running is not None:                   # expose the running value as 'self'
                ctx.frame[b.variable] = running
            running = execute(ctx, sub)
            ctx.registers[i] = running
        return running
    finally:
        ctx.registers.clear()
        ctx.registers.update(saved_regs)


def op_ct(ctx, b: Block) -> pd.Series:
    """Controlled terminology, sdtm.oak style (assign_ct / hardcode_ct): the input — a
    raw column, fixed text, or the running value — is normalised to the codelist's
    submission values. Unmatched values pass through unchanged for validation to flag,
    never dropped, never invented."""
    a = b.args or {}
    src = (a.get("sources") or [a])[0]
    ser = _series_for(ctx, src, b.variable, "string")
    codelist = s(a.get("codelist")) or b.codelist
    if not codelist:
        raise OpError("name the codelist to apply")
    if upper(codelist) not in ctx.codelists:
        raise OpError(f"codelist '{codelist}' is not in the spec's Codelist sheet")
    return apply_codelist(ctx, ser, codelist, a.get("ct_overrides"))


RECIPE_OPS = {
    "iso_date": op_iso_date,
    "ct": op_ct,
    "study_day": op_study_day,
    "lobxfl": op_lobxfl,
    "concat": op_concat,
    "date_extreme": op_date_extreme,
    "constant": op_constant,
    "copy_var": op_copy_var,
    "sdtm_ref": op_sdtm_ref,
    "studyid": op_studyid,
    "age": op_age,
    "usubjid": op_usubjid,
    "fn": op_fn,
    "cond": op_cond,
    "pipeline": op_pipeline,
}


def execute(ctx, b: Block) -> pd.Series:
    """Run one block and return its column. Raises OpError with an actionable message."""
    if b.mtype == "constant":
        return op_constant(ctx, b)
    if b.mtype == "assign":
        return op_assign(ctx, b)
    if b.mtype == "sequence":
        return op_sequence(ctx, b)
    if b.mtype == "derived":
        fn = RECIPE_OPS.get(b.recipe)
        if fn is None:
            raise OpError(f"no deterministic rule for derivation '{b.recipe or 'unspecified'}'")
        return fn(ctx, b)
    raise OpError(f"nothing to execute for mapping type '{b.mtype}'")
