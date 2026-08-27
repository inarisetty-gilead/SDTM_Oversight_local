"""Annotated-CRF checking.

Pull the SDTM annotations out of an aCRF PDF — both live PDF annotation objects (the
usual FreeText boxes) and text that was flattened onto the page — and hold every one of
them against the standards mapping and the TA spec: which annotations are off-standard,
what to do about each, and which collected variables the standards expected on the CRF
that never appear in it. Fully local and deterministic, like everything else here.
"""
from __future__ import annotations

import re
from difflib import get_close_matches
from pathlib import Path

from .spec import load_spec
from .util import s, upper


class AcrfError(Exception):
    """The check could not run. The message says which input and why."""


# uppercase words that appear on CRF pages and would otherwise look like SDTM variables
NOISE = frozenset({
    "CRF", "PAGE", "VISIT", "DATE", "TIME", "YES", "NO", "N/A", "NA", "SDTM", "SUPP",
    "NOT", "SUBMITTED", "ENTERED", "MAPPED", "DONE", "NONE", "OTHER", "UNKNOWN", "IF",
    "THE", "AND", "FOR", "SEE", "PDF", "FORM", "SITE", "SUBJECT", "STUDY", "SCREEN",
    "INV", "ONLY", "ANY", "ALL", "PER", "WHEN", "WHERE", "IN", "ON", "TO", "OF",
})
# variables every domain carries that are assigned, not annotated
AUTOMATIC = frozenset({"STUDYID", "DOMAIN", "USUBJID"})

QUALIFIED_RE = re.compile(r"\b(SUPP[A-Z]{2}|[A-Z]{2})\.([A-Z][A-Z0-9_]{1,7})\b")
ASSIGN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9_]{1,6})\s*(?:=|IN|WHEN)\s+[\"']?([A-Za-z0-9][A-Za-z0-9_ /.-]{0,40})")
BARE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,7})\b")
NOTE_RE = re.compile(r"NOT\s+(SUBMITTED|ENTERED|MAPPED|COLLECTED)", re.IGNORECASE)


def _annotation_texts(pdf_path: str | Path):
    """(page number, text) for every annotation box and every page's flattened text."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise AcrfError(f"could not open the aCRF PDF: {exc}") from exc
    for pnum, page in enumerate(reader.pages, start=1):
        try:
            for ref in (page.get("/Annots") or []):
                obj = ref.get_object()
                content = obj.get("/Contents")
                if s(content):
                    yield pnum, "annotation", str(content)
        except Exception:            # a malformed annotation must not sink the page
            pass
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            yield pnum, "page", text


def extract_annotations(pdf_path: str | Path, known_domains: set[str]) -> tuple[list[dict], int]:
    """Candidate SDTM annotations with page provenance.

    Annotation boxes are trusted as written. Flattened page text is noisy — CRF labels are
    uppercase too — so bare tokens from page text are kept only when they start with a
    domain code the specs actually define, which is the honest line between an annotation
    and a form label."""
    from pypdf import PdfReader                    # noqa: F401  (import error surfaces here)
    rows: list[dict] = []
    seen: set[tuple] = set()
    pages = 0

    def add(page: int, kind: str, domain: str, variable: str, value: str, snippet: str):
        key = (page, domain, variable, value)
        if key in seen:
            return
        seen.add(key)
        rows.append({"page": page, "kind": kind, "domain": domain, "variable": variable,
                     "value": value, "text": snippet.strip()[:160]})

    for pnum, source, text in _annotation_texts(pdf_path):
        pages = max(pages, pnum)
        for m in NOTE_RE.finditer(text):
            add(pnum, "note", "", "", m.group(0).upper(),
                text[max(0, m.start() - 40):m.end() + 20])
        consumed: set[str] = set()
        for m in QUALIFIED_RE.finditer(text):
            dom, var = upper(m.group(1)), upper(m.group(2))
            consumed.update((var, dom))         # neither half may resurface as a bare token
            add(pnum, "qualified", dom, var, "",
                text[max(0, m.start() - 30):m.end() + 30])
        for m in ASSIGN_RE.finditer(text):
            var, val = upper(m.group(1)), m.group(2).strip()
            if var in NOISE or var in consumed:
                continue
            if var[:2] in known_domains or var[:4] == "SUPP":
                consumed.add(var)
                add(pnum, "assignment", "", var, val,
                    text[max(0, m.start() - 20):m.end() + 10])
        for m in BARE_RE.finditer(text):
            var = upper(m.group(1))
            if var in NOISE or var in consumed or var in AUTOMATIC:
                continue
            if var[:2] not in known_domains:
                continue
            if source == "page" and len(var) < 4:   # flattened text: short tokens are labels
                continue
            add(pnum, "bare", "", var, "", text[max(0, m.start() - 25):m.end() + 25])
    return rows, pages


def load_standard(path: str | Path) -> dict:
    """{DOMAIN: {VARIABLE: {"label", "origin"}}} from a standards or TA workbook — the
    same reader as the mapping spec, so one format serves everywhere."""
    p = Path(path)
    if not p.exists():
        raise AcrfError(f"{p} does not exist")
    try:
        spec = load_spec(p)
    except Exception as exc:
        raise AcrfError(f"could not read {p.name} as a spec workbook: {exc}") from exc
    out: dict[str, dict] = {}
    for dom in spec.domain_names:
        vars_ = {}
        for r in spec.rows(dom):
            if r.variable:
                vars_[upper(r.variable)] = {"label": r.label, "origin": r.origin}
        if vars_:
            out[upper(dom)] = vars_
    if not out:
        raise AcrfError(f"{p.name} has no sheets with a Variable column — is it a spec?")
    return out


def _domains_for(var: str, *specs: dict) -> list[str]:
    doms = []
    for spec in specs:
        for dom, vars_ in (spec or {}).items():
            if var in vars_ and dom not in doms:
                doms.append(dom)
    return sorted(doms)


def check(pdf_path: str | Path, standards_path: str | Path,
          ta_path: str | Path | None = None) -> dict:
    """The whole aCRF check: extraction, verdict per annotation, and the reverse look —
    what the standards say is collected on the CRF but was never annotated."""
    standards = load_standard(standards_path)
    ta = load_standard(ta_path) if s(ta_path) else {}
    known_domains = set(standards) | set(ta) | {f"SUPP{d}" for d in set(standards) | set(ta)}
    known_prefixes = {d[:2] for d in set(standards) | set(ta)} | {"SU"}

    annotations, pages = extract_annotations(pdf_path, known_prefixes)

    all_std_vars = {v for vars_ in standards.values() for v in vars_}
    all_ta_vars = {v for vars_ in ta.values() for v in vars_}
    rows = []
    annotated_by_domain: dict[str, set] = {}

    for a in annotations:
        row = dict(a)
        var, dom = a["variable"], a["domain"]
        if a["kind"] == "note":
            row["verdict"] = "note"
            row["advice"] = "Marked as not submitted/collected — no dataset variable expected."
            rows.append(row)
            continue

        core_dom = dom[4:] if dom.startswith("SUPP") else dom
        in_std_doms = _domains_for(var, standards)
        in_ta_doms = _domains_for(var, ta)

        if dom and core_dom not in standards and core_dom not in ta:
            row["verdict"] = "unknown_domain"
            row["advice"] = (f"Domain {dom} is not in the standards mapping"
                             + (" or the TA spec" if ta else "")
                             + " — check the annotation, or add the domain to the standards.")
        elif dom and not dom.startswith("SUPP") and var in standards.get(dom, {}):
            row["verdict"] = "matched"
            row["advice"] = ""
            annotated_by_domain.setdefault(dom, set()).add(var)
        elif dom and not dom.startswith("SUPP") and var in ta.get(dom, {}):
            row["verdict"] = "ta_only"
            row["advice"] = ("In the TA spec but not the standards mapping — confirm the "
                            "TA addition is intended for this study, or align the standards.")
            annotated_by_domain.setdefault(dom, set()).add(var)
        elif dom.startswith("SUPP"):
            row["verdict"] = "supp"
            row["advice"] = (f"Annotated as a supplemental qualifier ({dom}) — confirm the "
                            "QNAM/QLABEL are defined in the standards' SUPP conventions.")
        elif not dom and in_std_doms:
            row["verdict"] = "matched"
            row["domain"] = "/".join(in_std_doms)
            row["advice"] = ""
            for d in in_std_doms:
                annotated_by_domain.setdefault(d, set()).add(var)
        elif not dom and in_ta_doms:
            row["verdict"] = "ta_only"
            row["domain"] = "/".join(in_ta_doms)
            row["advice"] = ("In the TA spec but not the standards mapping — confirm the "
                            "TA addition is intended for this study, or align the standards.")
            for d in in_ta_doms:
                annotated_by_domain.setdefault(d, set()).add(var)
        else:
            near_pool = (list(standards.get(core_dom, {})) if core_dom in standards
                         else sorted(all_std_vars | all_ta_vars))
            near = get_close_matches(var, near_pool, n=1, cutoff=0.75)
            row["verdict"] = "not_in_standard"
            if near:
                row["advice"] = (f"Not in the standards. Closest standard variable: "
                                 f"{near[0]} — fix the annotation if this is a typo.")
            else:
                supp_dom = core_dom or (var[:2] if var[:2] in standards else "--")
                row["advice"] = ("Not in the standards" + (" or the TA spec" if ta else "")
                                 + f". Correct the annotation, map it as SUPP{supp_dom} "
                                 "with a defined QNAM, or raise a standards change request.")
        rows.append(row)

    # the reverse look: CRF-origin variables the standards define for the annotated
    # domains that the aCRF never mentions
    missing = []
    origins_recorded = any(v.get("origin") for vars_ in standards.values() for v in vars_.values())
    for dom in sorted(annotated_by_domain):
        got = annotated_by_domain[dom]
        for var, meta in sorted(standards.get(dom, {}).items()):
            if var in got or var in AUTOMATIC or var.endswith("SEQ"):
                continue
            origin = s(meta.get("origin"))
            if origins_recorded and "CRF" not in origin.upper():
                continue                     # derived / assigned — not expected on the CRF
            missing.append({"domain": dom, "variable": var, "label": meta.get("label", ""),
                            "origin": origin,
                            "advice": ("The standards collect this on the CRF but it is "
                                       "never annotated — annotate it, or record why it "
                                       "is not collected in this study."
                                       if origins_recorded else
                                       "In the standards for this domain but never "
                                       "annotated — check whether it is collected "
                                       "(the standards workbook records no origins, so "
                                       "derived variables appear here too).")})
    counts = {"annotations": len([r for r in rows if r["verdict"] != "note"]),
              "matched": len([r for r in rows if r["verdict"] == "matched"]),
              "ta_only": len([r for r in rows if r["verdict"] == "ta_only"]),
              "supp": len([r for r in rows if r["verdict"] == "supp"]),
              "off_standard": len([r for r in rows if r["verdict"]
                                   in ("not_in_standard", "unknown_domain")]),
              "notes": len([r for r in rows if r["verdict"] == "note"]),
              "missing": len(missing)}
    return {"pages": pages, "rows": rows, "missing": missing, "counts": counts,
            "domains_annotated": sorted(annotated_by_domain),
            "origins_recorded": origins_recorded}
