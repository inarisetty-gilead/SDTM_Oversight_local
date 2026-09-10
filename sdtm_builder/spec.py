"""Read a Designer-format SDTM mapping spec workbook into a typed model.

Layout expected (SDTM Designer's core-spec export): one worksheet per domain, header on
row 1, one row per SDTM variable. Header names are matched case-insensitively through an
alias table, so a spec that says 'Source Variables' instead of 'Input Variables' still
loads. A sheet without a recognisable Variable column is skipped, not guessed at.

Supplemental qualifiers follow the Designer rule: a row whose `Dataset` cell is 'QNAM'
is NOT a parent-domain variable — it is transposed into SUPP<DOMAIN> at build time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .util import SDTM_DOMAINS, clean, norm_key, s, upper

# non-domain sheets in the Designer workbook
SKIP_SHEETS = {
    "history of revisions", "study", "standards", "toc", "<ds>", "codelist",
    "codelists", "computation", "valuelist", "value level", "maintenance",
    "readme", "instructions", "cover", "define", "documents", "dictionaries",
    "methods", "comments", "supplemental qualifiers",
}

# header alias -> canonical field name. Keys are norm_key()'d.
HEADER_ALIASES = {
    "variable": "variable", "variable_name": "variable", "sdtm_variable": "variable",
    "name": "variable",
    "label": "label", "variable_label": "label", "sdtm_label": "label",
    "mapping_action": "action", "action": "action",
    "input_variables": "input_variables", "input_variable": "input_variables",
    "source_variables": "input_variables", "source_variable": "input_variables",
    "mapping_rule": "mapping_rule", "derivation": "mapping_rule",
    "mapping_notes": "mapping_rule",
    "implemented_sas_code": "sas_code", "sas_code": "sas_code", "code": "sas_code",
    "codelist": "codelist", "controlled_terminology": "codelist", "ct": "codelist",
    "role": "role",
    "origin": "origin", "origin_type": "origin",
    "dataset": "dataset",
    "type": "type", "data_type": "type",
    "length": "length",
    "significant_digits": "sig_digits",
    "order": "order", "display_order": "order", "run_order": "run_order",
    "variable_order": "order", "seq": "order",
    "core": "core", "mandatory": "core",
    "gilead_mapping_usage": "mapping_usage", "mapping_usage": "mapping_usage",
    "key": "key", "key_variables": "key",
    "comment": "comment", "comments": "comment",
    "description": "comment", "notes": "comment",
    "derivation_rule": "mapping_rule", "derivation_notes": "mapping_rule",
    "mapping": "mapping_rule", "algorithm": "mapping_rule",
    "source": "input_variables", "source_column": "input_variables",
    "source_dataset": "source_dataset", "raw_dataset": "source_dataset",
    "cdisc_notes": "comment", "controlled_terms": "codelist",
    "controlled_terms_or_format": "codelist", "format": "codelist",
}

# how far down a sheet to look for the header row before giving up
HEADER_SCAN_ROWS = 12

SPEC_FIELDS = (
    "variable", "label", "action", "input_variables", "mapping_rule", "sas_code",
    "codelist", "role", "origin", "dataset", "type", "length", "sig_digits",
    "order", "run_order", "core", "mapping_usage", "key", "comment",
)


@dataclass
class SpecRow:
    """One SDTM variable as the mapping spec describes it."""
    domain: str
    variable: str
    label: str = ""
    action: str = ""
    input_variables: str = ""
    mapping_rule: str = ""
    sas_code: str = ""
    codelist: str = ""
    role: str = ""
    origin: str = ""
    dataset: str = ""
    type: str = ""
    length: str = ""
    sig_digits: str = ""
    order: str = ""
    run_order: str = ""
    core: str = ""
    mapping_usage: str = ""
    key: str = ""
    comment: str = ""
    sheet: str = ""
    row_number: int = 0

    @property
    def is_supp(self) -> bool:
        """Designer's SUPP rule: Dataset == 'QNAM' means supplemental qualifier."""
        return upper(self.dataset) == "QNAM"


@dataclass
class Spec:
    path: str
    domains: dict[str, list[SpecRow]] = field(default_factory=dict)
    codelists: dict[str, dict[str, str]] = field(default_factory=dict)
    # per-codelist detail for the CT inspector: {NAME: {"label", "extensible",
    # "terms": [{"value", "decode"}]}} — same sheet, kept whole instead of flattened
    codelist_meta: dict[str, dict] = field(default_factory=dict)
    skipped_sheets: list[tuple[str, str]] = field(default_factory=list)
    # the TOC sheet: {DOMAIN: {active, label, class, structure, order}}. The spec's own
    # statement of which domains are in this study — Active = Y.
    toc: dict[str, dict] = field(default_factory=dict)

    def is_active(self, domain: str) -> bool:
        """Active per the TOC. A domain the TOC does not mention stays active — only an
        explicit N deactivates, so a spec without a TOC behaves as before."""
        entry = self.toc.get(upper(domain))
        return True if entry is None else bool(entry.get("active", True))

    @property
    def active_domains(self) -> list[str]:
        return [d for d in self.domain_names if self.is_active(d)]

    @property
    def inactive_domains(self) -> list[str]:
        return [d for d in self.domain_names if not self.is_active(d)]

    def rows(self, domain: str) -> list[SpecRow]:
        return self.domains.get(upper(domain), [])

    @property
    def domain_names(self) -> list[str]:
        return sorted(self.domains)


def _header_map(columns) -> dict[str, str]:
    """{canonical field -> actual column label} for one worksheet."""
    out: dict[str, str] = {}
    for col in columns:
        canon = HEADER_ALIASES.get(norm_key(col))
        if canon and canon not in out:
            out[canon] = col
    return out


def _find_header_row(probe: pd.DataFrame) -> int | None:
    """Which row holds the column headings.

    Spec workbooks very often open a sheet with a title banner — "DM: Subject demographics
    and characteristics" — and put the real headings on the row below. Assuming row 1 makes
    the whole workbook look empty, so find the row that actually names a Variable column."""
    for idx in range(min(HEADER_SCAN_ROWS, len(probe))):
        cells = [norm_key(v) for v in probe.iloc[idx].tolist()]
        if "variable" not in cells:
            continue
        recognised = sum(1 for c in cells if c in HEADER_ALIASES)
        if recognised >= 2:                     # a lone word could be data; two is a header
            return idx
    return None


def _read_sheet(xl: pd.ExcelFile, sheet: str) -> tuple[pd.DataFrame | None, int]:
    """Parse one sheet with its header row located rather than assumed."""
    probe = xl.parse(sheet_name=sheet, header=None, dtype=object, nrows=HEADER_SCAN_ROWS)
    row = _find_header_row(probe)
    if row is None:
        return None, 0
    df = xl.parse(sheet_name=sheet, header=row, dtype=object)
    return df, row


def _domain_from_sheet(sheet: str) -> str:
    """'AE', 'AE_Onc', 'AE (Adverse Events)' -> 'AE'."""
    base = re.split(r"[_\s(\-]", str(sheet).strip())[0]
    return base.upper()


def load_spec(path: str | Path) -> Spec:
    """Read the whole workbook. Raises FileNotFoundError / ValueError on a bad file;
    individual unreadable sheets are recorded in `skipped_sheets`, not fatal."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"mapping spec not found: {p}")
    try:
        xl = pd.ExcelFile(p)
    except Exception as exc:                                  # noqa: BLE001 - surfaced to CLI
        raise ValueError(f"could not open mapping spec {p}: {exc}") from exc

    spec = Spec(path=str(p))
    empty_layouts: list[str] = []
    for sheet in xl.sheet_names:
        low = str(sheet).strip().lower()
        if low in SKIP_SHEETS or low.endswith("-data"):
            if low in ("codelist", "codelists"):
                spec.codelists, spec.codelist_meta = _read_codelist_sheet(xl, sheet)
            if low == "toc":
                spec.toc = _read_toc(xl, sheet)
                spec.skipped_sheets.append(
                    (sheet, f"table of contents — {sum(1 for t in spec.toc.values() if t['active'])} "
                            f"of {len(spec.toc)} datasets active"))
                continue
            spec.skipped_sheets.append((sheet, "non-domain sheet"))
            continue
        try:
            df, header_row = _read_sheet(xl, sheet)
        except Exception as exc:                              # noqa: BLE001
            spec.skipped_sheets.append((sheet, f"unreadable: {exc}"))
            continue
        if df is None:
            spec.skipped_sheets.append((sheet, "no 'Variable' column in the first "
                                               f"{HEADER_SCAN_ROWS} rows"))
            continue
        hdr = _header_map(df.columns)

        domain = _domain_from_sheet(sheet)
        rows: list[SpecRow] = []
        for i, (_, r) in enumerate(df.iterrows(), start=header_row + 2):
            var = upper(r.get(hdr["variable"]))
            if not var or var == "VARIABLE":
                continue
            vals = {f: s(r.get(hdr[f])) for f in SPEC_FIELDS if f in hdr}
            vals["variable"] = var
            rows.append(SpecRow(domain=domain, sheet=str(sheet), row_number=i, **vals))
        if rows:
            spec.domains.setdefault(domain, []).extend(rows)
        else:
            empty_layouts.append(sheet)
            spec.skipped_sheets.append((sheet, "has the layout but no variable rows"))

    if not spec.domains:
        if empty_layouts:
            raise ValueError(
                f"{p.name} is a blank specification template: {len(empty_layouts)} domain "
                f"sheet(s) ({', '.join(empty_layouts[:6])}"
                f"{' …' if len(empty_layouts) > 6 else ''}) have the column headings but no "
                "variable rows. Fill it in, or point at a completed mapping spec."
            )
        raise ValueError(
            f"no domain sheets found in {p.name}. Expected one worksheet per SDTM domain with "
            "a 'Variable' column. The header may sit below a title row — that is handled — but "
            "the sheet must have a column named Variable."
        )
    return spec


TOC_ACTIVE = ("active", "active_flag", "in_study", "instudy", "included")


def _read_toc(xl: pd.ExcelFile, sheet: str) -> dict[str, dict]:
    """The TOC sheet: which datasets this study actually uses (Active = Y), plus their label,
    class, structure and ordering. `-DATA` companion rows are the raw-data views of the same
    domain and are folded into it, never listed separately."""
    try:
        probe = xl.parse(sheet_name=sheet, header=None, dtype=object, nrows=HEADER_SCAN_ROWS)
    except Exception:                                            # noqa: BLE001
        return {}
    header_row = None
    for i in range(min(HEADER_SCAN_ROWS, len(probe))):
        cells = [norm_key(v) for v in probe.iloc[i].tolist()]
        if "dataset" in cells and any(a in cells for a in TOC_ACTIVE):
            header_row = i
            break
    if header_row is None:
        return {}
    df = xl.parse(sheet_name=sheet, header=header_row, dtype=object)
    hdr = {norm_key(c): c for c in df.columns}
    ds_col = hdr.get("dataset") or hdr.get("domain")
    act_col = next((hdr[a] for a in TOC_ACTIVE if a in hdr), None)
    if not (ds_col and act_col):
        return {}
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        name = upper(r.get(ds_col))
        if not name:
            continue
        if name.endswith("-DATA"):
            continue                     # the raw-data companion of a domain, not a domain
        out[name] = {
            "active": upper(r.get(act_col)) == "Y",
            "label": s(r.get(hdr["label"])) if "label" in hdr else "",
            "class": s(r.get(hdr["class"])) if "class" in hdr else "",
            "structure": s(r.get(hdr["structure"])) if "structure" in hdr else "",
            "order": s(r.get(hdr["display_order"])) if "display_order" in hdr else "",
        }
    return out


def _read_codelist_sheet(xl: pd.ExcelFile, sheet: str) -> tuple[dict[str, dict[str, str]], dict[str, dict]]:
    """Optional 'Codelist' sheet -> ({CODELIST_NAME: {term_upper: submission_value}},
    {CODELIST_NAME: {"label", "extensible", "terms": [{"value", "decode"}]}}).

    Every spelling on the row (submission value, decode, synonyms) maps TO the submission
    value, so messy raw entries ('male', 'M', 'Male') normalise to the CT term. The meta
    dict keeps the sheet's own detail for the CT inspector — the terms in order, and
    whether the sheet marks the codelist extensible."""
    try:
        df = xl.parse(sheet_name=sheet, dtype=object)
    except Exception:                                          # noqa: BLE001
        return {}, {}
    hdr = {norm_key(c): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in hdr:
                return hdr[n]
        return None

    c_name = pick("codelist", "codelist_name", "id", "oid", "name")
    c_sub = pick("submission_value", "submissionvalue", "term", "value", "coded_value",
                 "codelist_value")            # Designer-style sheets: 'Codelist Value'
    c_dec = pick("decode", "decoded_value", "preferred_term", "translated_value",
                 "codelist_value_decode")     # …and 'Codelist Value Decode'
    c_syn = pick("synonyms", "synonym", "aliases")
    c_ext = pick("codelist_extensible", "extensible")
    c_lbl = pick("codelist_label", "label")
    if not (c_name and c_sub):
        return {}, {}

    out: dict[str, dict[str, str]] = {}
    meta: dict[str, dict] = {}
    for _, r in df.iterrows():
        name = upper(r.get(c_name))
        sub = s(r.get(c_sub))
        if not name or not sub:
            continue
        bucket = out.setdefault(name, {})
        m = meta.setdefault(name, {"label": "", "extensible": False, "terms": []})
        if c_lbl and not m["label"]:
            m["label"] = s(r.get(c_lbl))
        if c_ext and upper(r.get(c_ext)) in ("Y", "YES", "TRUE"):
            m["extensible"] = True
        decode = s(r.get(c_dec)) if c_dec else ""
        m["terms"].append({"value": sub, "decode": decode})
        spellings = [sub, decode]
        if c_syn:
            spellings += [t.strip() for t in re.split(r"[;,|]", s(r.get(c_syn))) if t.strip()]
        for sp in spellings:
            if sp:
                bucket.setdefault(sp.strip().upper(), sub)
    return out, meta


# ── how much of the spec is actually actionable ─────────────────────────────
def analyse(spec: "Spec") -> dict:
    """Report what the mapping spec states, before any raw data is involved.

    This answers the question people actually ask when coverage looks low — "why aren't all
    my variables built?" — because the usual answer is that the spec does not name a source
    for them. Counting it here separates a spec limitation from a tool limitation."""
    from .translate import translate_row

    per_domain, totals = [], {
        "variables": 0, "with_source": 0, "derived": 0, "constant": 0, "sequence": 0,
        "dropped_by_spec": 0, "assign_without_source": 0, "blank_rows": 0, "other_unmapped": 0,
    }
    for dom in spec.domain_names:
        rows = spec.rows(dom)
        d = {"domain": dom, "variables": len(rows), "with_source": 0, "derived": 0,
             "constant": 0, "sequence": 0, "dropped_by_spec": 0,
             "assign_without_source": 0, "blank_rows": 0, "other_unmapped": 0}
        for i, r in enumerate(rows):
            b = translate_row(r, i)
            if b.mtype == "assign":
                d["with_source"] += 1
            elif b.mtype == "derived":
                d["derived"] += 1
            elif b.mtype == "constant":
                d["constant"] += 1
            elif b.mtype == "sequence":
                d["sequence"] += 1
            elif b.mtype == "drop":
                d["dropped_by_spec"] += 1
            elif "names no Input Variable" in b.reason:
                d["assign_without_source"] += 1
            elif "no Input Variables and no Mapping Action" in b.reason:
                d["blank_rows"] += 1
            else:
                d["other_unmapped"] += 1
        d["actionable"] = d["with_source"] + d["derived"] + d["constant"] + d["sequence"]
        d["needs_a_source"] = d["assign_without_source"] + d["blank_rows"] + d["other_unmapped"]
        per_domain.append(d)
        for k in totals:
            totals[k] += d.get(k, 0)
    totals["actionable"] = sum(d["actionable"] for d in per_domain)
    totals["needs_a_source"] = sum(d["needs_a_source"] for d in per_domain)
    totals["pct_actionable"] = (round(100 * totals["actionable"] / totals["variables"])
                                if totals["variables"] else 0)
    return {"totals": totals, "domains": per_domain}


# ── does the spec's own text say what it means? ─────────────────────────────
_DOMAIN_PREFIXES = {d[:2] for d in SDTM_DOMAINS}
# not subject-level — USUBJID is not expected here, unlike every other domain
_NO_USUBJID_DOMAINS = {"TA", "TE", "TI", "TV", "TS"}


def _reworded(found: str, dom: str) -> str:
    """found's own domain prefix corrected to dom, same leading underscore and case —
    '_trcat' against dom TU becomes '_tucat', 'TRCAT' becomes 'TUCAT'."""
    lead, core = ("_", found[1:]) if found.startswith("_") else ("", found)
    prefix = dom if core[:2].isupper() else dom.lower()
    return lead + prefix + core[2:]


def _find_ref_row(ref: "Spec", dom: str, var: str) -> "SpecRow | None":
    for r in ref.rows(dom):
        if upper(r.variable) == var:
            return r
    return None


# structured columns worth comparing against a standard — free-text columns (Mapping Rule,
# Implemented SAS Code) are not, since a study legitimately writes its own version of those
_COMPARE_FIELDS = (("codelist", "Codelist"), ("type", "Type"), ("role", "Role"),
                   ("origin", "Origin"), ("action", "Action"))


def check_consistency(spec: "Spec", standards: "Spec | None" = None,
                       ta: "Spec | None" = None) -> dict:
    """Catch what a build never can, because the build for TU and the build for TR each run
    correctly against their OWN domain — mistakes like these live only in the spec's own
    text and metadata, never in a built dataset. Checks only the domains the TOC marks
    Active = Y (a spec without a TOC treats every domain as active, as elsewhere in the app):

    - a row's Mapping Rule / Implemented SAS Code names a variable that belongs to a
      DIFFERENT domain (e.g. TU's TUCAT row mentions '_trcat' — TR's variable, not TU's)
    - the same variable defined more than once in one domain's own rows
    - a row names a Codelist the spec's own Codelist sheet never defines
    - the same variable name declared with a different Type in two different domains
    - a variable name longer than the 8-character SDTM limit
    - an active subject-level domain with no STUDYID, DOMAIN or USUBJID row

    When a company standards spec and/or TA spec (same workbook shape, loaded the same way)
    are supplied, a variable's Codelist/Type/Role/Origin/Action is also compared against
    whichever of the two names that same domain.variable — the TA spec first (it is the more
    specific of the two), the standards spec otherwise.
    """
    issues: list[dict] = []
    domains = spec.active_domains
    var_types: dict[str, list[tuple[str, int, str]]] = {}   # VARIABLE -> [(domain, row, type)]

    for dom in domains:
        dom_u = upper(dom)
        seen: dict[str, list[SpecRow]] = {}
        present: set[str] = set()
        for r in spec.rows(dom):
            var = upper(r.variable)
            present.add(var)
            if not r.is_supp:
                seen.setdefault(var, []).append(r)

            if len(var) > 8:
                issues.append({
                    "domain": dom, "variable": var, "row": r.row_number, "label": r.label,
                    "kind": "long_variable_name", "field": "", "found": var, "expected": "",
                    "other_domain": "",
                    "message": f"{var} is {len(var)} characters — SDTM variable "
                               f"(and QNAM) names cannot be longer than 8.",
                    "snippet": "",
                })

            if r.codelist and upper(r.codelist) not in spec.codelists:
                issues.append({
                    "domain": dom, "variable": var, "row": r.row_number, "label": r.label,
                    "kind": "missing_codelist", "field": "codelist", "found": r.codelist,
                    "expected": "", "other_domain": "",
                    "message": f"{var} names codelist {r.codelist!r}, which the spec's own "
                               f"Codelist sheet does not define.",
                    "snippet": "",
                })

            if s(r.type):
                var_types.setdefault(var, []).append((dom, r.row_number, r.type.strip()))

            ref_row, ref_source = None, ""
            if ta is not None:
                ref_row = _find_ref_row(ta, dom, var)
                ref_source = "TA spec" if ref_row is not None else ""
            if ref_row is None and standards is not None:
                ref_row = _find_ref_row(standards, dom, var)
                ref_source = "standards spec" if ref_row is not None else ""
            if ref_row is not None:
                diffs = [f"{label} is {s(getattr(r, fld))!r} here vs "
                         f"{s(getattr(ref_row, fld))!r} in the {ref_source}"
                         for fld, label in _COMPARE_FIELDS
                         if s(getattr(r, fld)) and s(getattr(ref_row, fld))
                         and s(getattr(r, fld)).upper() != s(getattr(ref_row, fld)).upper()]
                if diffs:
                    issues.append({
                        "domain": dom, "variable": var, "row": r.row_number, "label": r.label,
                        "kind": "standards_mismatch", "field": ref_source,
                        "found": "", "expected": "", "other_domain": "",
                        "message": f"{var} differs from the {ref_source}: " + "; ".join(diffs),
                        "snippet": "",
                    })

            if not var.startswith(dom_u):
                continue
            suffix = var[len(dom_u):]
            if len(suffix) < 2:
                continue           # too short (e.g. a domain code alone) — not specific enough
            pattern = re.compile(
                rf"(?<![A-Za-z0-9])(_?[A-Za-z]{{2}}{re.escape(suffix)})(?![A-Za-z0-9])",
                re.IGNORECASE)
            flagged_prefixes: set[str] = set()
            for field_name in ("mapping_rule", "sas_code"):
                text = getattr(r, field_name) or ""
                if not text.strip():
                    continue
                for m in pattern.finditer(text):
                    found = m.group(1)
                    prefix = found.lstrip("_")[:2].upper()
                    if prefix == dom_u or prefix not in _DOMAIN_PREFIXES or prefix in flagged_prefixes:
                        continue
                    flagged_prefixes.add(prefix)
                    lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
                    issues.append({
                        "domain": dom, "variable": var, "row": r.row_number, "label": r.label,
                        "kind": "cross_domain_reference", "field": field_name,
                        "found": found, "expected": _reworded(found, dom_u),
                        "other_domain": prefix,
                        "message": (f"{field_name.replace('_', ' ')} references {found!r} — "
                                    f"{prefix} is a different domain, and this row is "
                                    f"{dom_u}.{var}. Likely copied from {prefix} without renaming."),
                        "snippet": text[lo:hi].strip(),
                    })
        for var, rs in seen.items():
            if len(rs) > 1:
                rows_str = ", ".join(str(x.row_number) for x in rs if x.row_number)
                issues.append({
                    "domain": dom, "variable": var, "row": rs[0].row_number, "label": rs[0].label,
                    "kind": "duplicate_variable", "field": "", "found": "", "expected": "",
                    "other_domain": "",
                    "message": f"{var} is defined {len(rs)} times in {dom}"
                               + (f" (rows {rows_str})" if rows_str else ""),
                    "snippet": "",
                })

        missing = [v for v in ("STUDYID", "DOMAIN") if v not in present]
        if dom_u not in _NO_USUBJID_DOMAINS and "USUBJID" not in present:
            missing.append("USUBJID")
        if missing:
            issues.append({
                "domain": dom, "variable": ", ".join(missing), "row": 0, "label": "",
                "kind": "missing_identifier", "field": "", "found": "", "expected": "",
                "other_domain": "",
                "message": f"{dom} has no row for {', '.join(missing)} — every subject-level "
                           f"domain needs these.",
                "snippet": "",
            })

    for var, entries in var_types.items():
        types = {t.upper() for _, _, t in entries}
        doms = {d for d, _, _ in entries}
        if len(types) > 1 and len(doms) > 1:
            detail = "; ".join(f"{d}.{var} row {row}: {t}" for d, row, t in entries)
            issues.append({
                "domain": "/".join(sorted(doms)), "variable": var, "row": entries[0][1],
                "label": "", "kind": "type_mismatch", "field": "type", "found": "", "expected": "",
                "other_domain": "",
                "message": f"{var} is declared with different Types across domains: {detail}",
                "snippet": "",
            })

    return {
        "totals": {
            "domains_checked": len(domains),
            "cross_domain_references": sum(1 for i in issues if i["kind"] == "cross_domain_reference"),
            "duplicate_variables": sum(1 for i in issues if i["kind"] == "duplicate_variable"),
            "missing_codelists": sum(1 for i in issues if i["kind"] == "missing_codelist"),
            "type_mismatches": sum(1 for i in issues if i["kind"] == "type_mismatch"),
            "long_variable_names": sum(1 for i in issues if i["kind"] == "long_variable_name"),
            "missing_identifiers": sum(1 for i in issues if i["kind"] == "missing_identifier"),
            "standards_mismatches": sum(1 for i in issues if i["kind"] == "standards_mismatch"),
            "issues": len(issues),
        },
        "issues": sorted(issues, key=lambda i: (i["domain"], i["row"] or 0, i["variable"])),
    }

