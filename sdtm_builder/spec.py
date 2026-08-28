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

from .util import clean, norm_key, s, upper

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
                spec.codelists = _read_codelist_sheet(xl, sheet)
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


def _read_codelist_sheet(xl: pd.ExcelFile, sheet: str) -> dict[str, dict[str, str]]:
    """Optional 'Codelist' sheet -> {CODELIST_NAME: {term_upper: submission_value}}.

    Every spelling on the row (submission value, decode, synonyms) maps TO the submission
    value, so messy raw entries ('male', 'M', 'Male') normalise to the CT term."""
    try:
        df = xl.parse(sheet_name=sheet, dtype=object)
    except Exception:                                          # noqa: BLE001
        return {}
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
    if not (c_name and c_sub):
        return {}

    out: dict[str, dict[str, str]] = {}
    for _, r in df.iterrows():
        name = upper(r.get(c_name))
        sub = s(r.get(c_sub))
        if not name or not sub:
            continue
        bucket = out.setdefault(name, {})
        spellings = [sub]
        if c_dec:
            spellings.append(s(r.get(c_dec)))
        if c_syn:
            spellings += [t.strip() for t in re.split(r"[;,|]", s(r.get(c_syn))) if t.strip()]
        for sp in spellings:
            if sp:
                bucket.setdefault(sp.strip().upper(), sub)
    return out


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
