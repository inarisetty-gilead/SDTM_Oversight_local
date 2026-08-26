"""Compare an independently built SDTM dataset against a vendor's delivered one.

This is the oversight deliverable. The build is only the means: what matters is a defensible
statement of where the vendor's data differs from what the mapping spec says it should be.

Records are matched on natural keys rather than on row position, because two correct
implementations can order records differently. Where keys repeat, occurrences within the key
are matched in a deterministic order and the ambiguity is reported rather than hidden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .rawio import READERS, read_dataset
from .util import norm_key, s, upper

# variables whose value is expected to differ between two correct implementations
NON_COMPARABLE_DEFAULT = {"DOMAIN"}

# candidate natural keys, in the order they are tried
TOPIC_SUFFIXES = ("TESTCD", "TERM", "TRT", "DECOD", "SPID", "REFID", "PARMCD", "SCAT", "CAT")
TIMING_SUFFIXES = ("DTC", "STDTC")


@dataclass
class VariableDiff:
    variable: str
    compared: int = 0
    differing: int = 0
    only_built_nonblank: int = 0
    only_vendor_nonblank: int = 0
    examples: list[dict] = field(default_factory=list)

    @property
    def agreement(self) -> float:
        return 100.0 if self.compared == 0 else 100.0 * (self.compared - self.differing) / self.compared


@dataclass
class DomainComparison:
    domain: str
    keys: list[str] = field(default_factory=list)
    rows_built: int = 0
    rows_vendor: int = 0
    matched: int = 0
    only_built: int = 0
    only_vendor: int = 0
    vars_only_built: list[str] = field(default_factory=list)
    vars_only_vendor: list[str] = field(default_factory=list)
    vars_compared: list[str] = field(default_factory=list)
    not_built: list[str] = field(default_factory=list)
    diffs: list[VariableDiff] = field(default_factory=list)
    only_built_rows: pd.DataFrame | None = None
    only_vendor_rows: pd.DataFrame | None = None
    notes: list[str] = field(default_factory=list)
    key_note: str = ""
    error: str = ""

    @property
    def total_differences(self) -> int:
        return sum(d.differing for d in self.diffs)

    @property
    def clean(self) -> bool:
        return (not self.error and self.only_built == 0 and self.only_vendor == 0
                and self.total_differences == 0 and not self.vars_only_vendor
                and not self.vars_only_built)


# ── vendor delivery discovery ───────────────────────────────────────────────
def discover_vendor(folder: str | Path) -> dict[str, Path]:
    """{DOMAIN: file} for a folder of delivered SDTM datasets."""
    p = Path(folder)
    if not p.is_dir():
        raise NotADirectoryError(f"vendor SDTM folder not found: {p}")
    out: dict[str, Path] = {}
    for f in sorted(p.rglob("*")):
        if not f.is_file() or f.name.startswith((".", "~$")):
            continue
        if f.suffix.lower() not in READERS or f.suffix.lower() in (".xlsx", ".xls", ".xlsm"):
            if f.suffix.lower() not in (".sas7bdat", ".xpt", ".csv", ".parquet"):
                continue
        dom = upper(norm_key(f.stem))
        out.setdefault(dom, f)
    return out


# ── key selection ───────────────────────────────────────────────────────────
def natural_keys(domain: str, built: pd.DataFrame, vendor: pd.DataFrame,
                 explicit: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Pick the variables that identify a record in both datasets."""
    notes: list[str] = []
    shared = [c for c in built.columns if c in vendor.columns]
    if explicit:
        keys = [upper(k) for k in explicit if upper(k) in shared]
        missing = [upper(k) for k in explicit if upper(k) not in shared]
        if missing:
            notes.append(f"requested key(s) not present in both datasets: {', '.join(missing)}")
        if keys:
            return keys, notes

    dom = upper(domain)
    if dom.startswith("SUPP"):
        keys = [c for c in ("USUBJID", "QNAM", "IDVAR", "IDVARVAL") if c in shared]
        return keys or shared[:1], notes
    if dom in ("DM",):
        return (["USUBJID"] if "USUBJID" in shared else shared[:1]), notes

    keys = ["USUBJID"] if "USUBJID" in shared else []
    if not keys:
        notes.append("no USUBJID in both datasets — records matched on the remaining keys only")

    content = [dom + sfx for sfx in TOPIC_SUFFIXES if dom + sfx in shared]
    timing = [dom + sfx for sfx in TIMING_SUFFIXES if dom + sfx in shared]
    if "VISITNUM" in shared:
        timing.append("VISITNUM")
    candidate = keys + content[:2] + timing[:1]

    if candidate and _unique(built, candidate) and _unique(vendor, candidate):
        return candidate, notes

    seq = dom + "SEQ"
    if seq in shared and keys:
        with_seq = keys + [seq]
        if _unique(built, with_seq) and _unique(vendor, with_seq):
            notes.append(
                f"records matched on {', '.join(with_seq)}. --SEQ is assigned by each "
                "implementation, so a difference in record ORDER will show up as value "
                "differences rather than as added/removed records."
            )
            return with_seq, notes

    if candidate:
        notes.append(
            f"{', '.join(candidate)} does not uniquely identify a record in "
            f"{'the built' if not _unique(built, candidate) else 'the vendor'} dataset. "
            "Repeated keys are matched in sorted order; review the duplicates before "
            "relying on the value differences."
        )
        return candidate, notes
    return (shared[:1], notes + ["no usable natural key — comparison is unreliable"])


def _unique(df: pd.DataFrame, keys: list[str]) -> bool:
    cols = [k for k in keys if k in df.columns]
    return bool(cols) and not df.duplicated(subset=cols).any()


# ── value normalisation ─────────────────────────────────────────────────────
def _norm_col(ser: pd.Series, ignore_case: bool, numeric_tol: float) -> pd.Series:
    """Comparable text form of a column. Numbers are normalised so 1, 1.0 and '1.00'
    agree; everything else is stripped text with missings collapsed to ''."""
    num = pd.to_numeric(ser, errors="coerce")
    txt = ser.astype("string").str.strip().fillna("")
    txt = txt.where(~txt.str.lower().isin(["nan", "none", "nat", "<na>", "null"]), "")
    if num.notna().any():
        decimals = max(0, min(10, int(round(-1 * _log10(numeric_tol))))) if numeric_tol > 0 else 6
        as_num = num.round(decimals).map(
            lambda x: "" if pd.isna(x) else (f"{x:.{decimals}f}".rstrip("0").rstrip(".") or "0")
        ).astype("string")
        txt = txt.where(num.isna(), as_num)
    return txt.str.upper() if ignore_case else txt


def _log10(x: float) -> float:
    import math
    return math.log10(x) if x > 0 else -6.0


# ── the comparison ──────────────────────────────────────────────────────────
def compare_domain(domain: str, built: pd.DataFrame, vendor: pd.DataFrame,
                   keys: list[str] | None = None, not_built: list[str] | None = None,
                   ignore_case: bool = False, numeric_tol: float = 1e-9,
                   ignore_vars: set[str] | None = None,
                   max_examples: int = 5) -> DomainComparison:
    cmp = DomainComparison(domain=upper(domain))
    cmp.rows_built, cmp.rows_vendor = len(built), len(vendor)
    cmp.not_built = sorted(not_built or [])
    ignore = {upper(v) for v in (ignore_vars or set())} | NON_COMPARABLE_DEFAULT

    built = built.copy()
    vendor = vendor.copy()
    built.columns = [upper(c) for c in built.columns]
    vendor.columns = [upper(c) for c in vendor.columns]

    key_list, notes = natural_keys(domain, built, vendor, keys)
    cmp.keys, cmp.notes = key_list, notes
    if not key_list:
        cmp.error = "no variables in common — the datasets cannot be matched"
        return cmp

    verb = "identifies" if len(key_list) == 1 else "identify"
    cmp.key_note = (
        f"{', '.join(key_list)} {verb} the record and {'is' if len(key_list) == 1 else 'are'} "
        "therefore not value-compared: a difference there shows up as an unmatched record instead."
    )
    cmp.vars_only_built = sorted(set(built.columns) - set(vendor.columns))
    cmp.vars_only_vendor = sorted(set(vendor.columns) - set(built.columns))
    shared = [c for c in built.columns if c in vendor.columns]
    cmp.vars_compared = [c for c in shared if c not in key_list and c not in ignore]

    # deterministic occurrence counter so repeated keys still line up 1:1
    b_key = _key_frame(built, key_list, ignore_case, numeric_tol)
    v_key = _key_frame(vendor, key_list, ignore_case, numeric_tol)
    built["__k"] = b_key
    vendor["__k"] = v_key
    built["__n"] = built.groupby("__k").cumcount()
    vendor["__n"] = vendor.groupby("__k").cumcount()

    merged = built.merge(vendor, on=["__k", "__n"], how="outer",
                         suffixes=("__b", "__v"), indicator=True)
    both = merged[merged["_merge"] == "both"]
    cmp.matched = len(both)
    only_b = merged[merged["_merge"] == "left_only"]
    only_v = merged[merged["_merge"] == "right_only"]
    cmp.only_built, cmp.only_vendor = len(only_b), len(only_v)

    key_cols_b = [f"{k}__b" if f"{k}__b" in merged.columns else k for k in key_list]
    key_cols_v = [f"{k}__v" if f"{k}__v" in merged.columns else k for k in key_list]
    if cmp.only_built:
        cmp.only_built_rows = only_b[key_cols_b].rename(
            columns=dict(zip(key_cols_b, key_list))).reset_index(drop=True)
    if cmp.only_vendor:
        cmp.only_vendor_rows = only_v[key_cols_v].rename(
            columns=dict(zip(key_cols_v, key_list))).reset_index(drop=True)

    for var in cmp.vars_compared:
        cb, cv = f"{var}__b", f"{var}__v"
        if cb not in both.columns or cv not in both.columns:
            continue
        nb = _norm_col(both[cb], ignore_case, numeric_tol)
        nv = _norm_col(both[cv], ignore_case, numeric_tol)
        differs = nb != nv
        d = VariableDiff(variable=var, compared=int(len(both)), differing=int(differs.sum()))
        d.only_built_nonblank = int(((nb != "") & (nv == "")).sum())
        d.only_vendor_nonblank = int(((nv != "") & (nb == "")).sum())
        if d.differing:
            sample = both[differs].head(max_examples)
            for _, r in sample.iterrows():
                ex = {k: s(r.get(f"{k}__b", r.get(k))) for k in key_list}
                ex["built"] = s(r[cb])
                ex["vendor"] = s(r[cv])
                d.examples.append(ex)
        cmp.diffs.append(d)

    cmp.diffs.sort(key=lambda d: (-d.differing, d.variable))
    return cmp


def _key_frame(df: pd.DataFrame, keys: list[str], ignore_case: bool, tol: float) -> pd.Series:
    parts = [_norm_col(df[k], ignore_case, tol) if k in df.columns
             else pd.Series("", index=df.index, dtype="string") for k in keys]
    out = parts[0].fillna("")
    for p in parts[1:]:
        out = out + "\x1f" + p.fillna("")
    return out


def compare_study(results: dict, vendor_folder: str | Path, **kwargs) -> dict[str, DomainComparison]:
    """Compare every built domain (and SUPP) against the vendor delivery folder."""
    vendor_files = discover_vendor(vendor_folder)
    per_domain_keys = kwargs.pop("keys", {}) or {}
    out: dict[str, DomainComparison] = {}

    def _cmp(name: str, frame: pd.DataFrame, unbuilt: list[str]):
        path = vendor_files.get(name)
        if path is None:
            c = DomainComparison(domain=name, rows_built=len(frame))
            c.error = "not present in the vendor delivery"
            out[name] = c
            return
        try:
            vdf = read_dataset(path)
        except Exception as exc:                                # noqa: BLE001
            c = DomainComparison(domain=name, rows_built=len(frame))
            c.error = f"could not read vendor file {path.name}: {exc}"
            out[name] = c
            return
        out[name] = compare_domain(name, frame, vdf, keys=per_domain_keys.get(name),
                                   not_built=unbuilt, **kwargs)

    for dom, res in results.items():
        if not res.ok:
            continue
        unbuilt = [b.variable for b in res.blocks if b.status in ("not_built", "error")]
        _cmp(dom, res.dataset, unbuilt)
        if res.supp is not None and len(res.supp):
            _cmp(f"SUPP{dom}", res.supp, [])

    built_names = set(out)
    for dom, path in vendor_files.items():
        if dom in built_names:
            continue
        c = DomainComparison(domain=dom)
        try:
            c.rows_vendor = len(read_dataset(path))
        except Exception:                                       # noqa: BLE001
            pass
        c.error = "delivered by the vendor but not built here (not in the mapping spec, or not requested)"
        out[dom] = c
    return out
