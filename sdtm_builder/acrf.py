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
# '=' may be glued (EGPOS=SUPINE); IN / WHEN need their own spaces. Flattened aCRF text
# also glues WORDS ('andEGTESTCD', 'EGTRCVYNin') — a lowercase letter on either side
# still bounds the variable token.
# the value is matched with a lookahead so one assignment's value can never swallow the
# next assignment ('EGORRESU= ms EGTPT =ENDOF...' is two annotations, not one)
ASSIGN_RE = re.compile(r"(?<![A-Z0-9_])([A-Z]{2}[A-Z0-9_]{1,6})(?:\s*=\s*|\s+(?:IN|WHEN)\s+)(?=[\"']?([A-Za-z0-9][A-Za-z0-9_ /.-]{0,40}))")
BARE_RE = re.compile(r"(?<![A-Z0-9_])([A-Z][A-Z0-9_]{2,7})(?=[a-z]|\b)")
NOTE_RE = re.compile(r"NOT\s+(SUBMITTED|ENTERED|MAPPED|COLLECTED)", re.IGNORECASE)


def _page_lines(page) -> list[tuple[float, float, str]]:
    """The page's text as positioned lines [(y, x, text)], top first — the raw material
    for finding the CRF question an annotation sits next to, and the form name."""
    spans: list[tuple[float, float, str]] = []

    def visit(text, cm, tm, font_dict, font_size):
        t = (text or "").strip()
        if t:
            spans.append((float(tm[5]), float(tm[4]), t))

    try:
        page.extract_text(visitor_text=visit)
    except Exception:
        return []
    lines: dict[float, list[tuple[float, str]]] = {}
    for y, x, t in spans:
        key = round(y / 3) * 3           # spans within ~3pt share a line
        lines.setdefault(key, []).append((x, t))
    out = []
    for y in sorted(lines, reverse=True):
        parts = sorted(lines[y])
        out.append((y, parts[0][0], " ".join(t for _x, t in parts).strip()))
    return out


VAR_TOKEN_RE = re.compile(r"(?<![A-Z0-9_])(?:SUPP)?[A-Z]{2}[A-Z0-9_]{2,8}(?=[a-z]|\b)")
FORM_TITLE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}\s*\(.{3,60}\)")


def _annotation_detector(prefixes: set[str]):
    """True for text that is itself annotation content — dotted names, NOT SUBMITTED
    notes, or any token that starts with a domain code the specs define. Real CRF
    wording (questions, form titles) is what remains."""
    def looks(text: str) -> bool:
        if QUALIFIED_RE.search(text) or NOTE_RE.search(text):
            return True
        return any(t.startswith("SUPP") or (len(t) >= 4 and t[:2] in prefixes)
                   for t in VAR_TOKEN_RE.findall(text))
    return looks


def _image_pages(pdf_path: str | Path) -> int:
    """How many pages draw images — a scanned CRF body means the questions are pixels,
    not text, and the question column cannot be filled from this PDF."""
    from pypdf import PdfReader
    n = 0
    try:
        for page in PdfReader(str(pdf_path)).pages:
            res = page.get("/Resources") or {}
            xo = res.get("/XObject")
            if xo and len(xo.get_object() or {}):
                n += 1
    except Exception:
        return 0
    return n


def _question_for(lines: list[tuple[float, float, str]], y: float, own: str,
                  is_annotation) -> str:
    """The CRF question nearest an annotation at height y: same line first, then the
    closest line above, skipping other annotations and the annotation's own text."""
    def usable(t: str) -> bool:
        return bool(t) and t != own and own not in t and not is_annotation(t) \
            and not t.strip().isdigit()
    # a question BELONGS to its annotation only nearby — text half a page away is some
    # other field's wording, and a wrong question is worse than none
    same = [t for ly, _x, t in lines if abs(ly - y) <= 6 and usable(t)]
    if same:
        return same[0][:140]
    above = [(ly, t) for ly, _x, t in lines if 6 < ly - y <= 40 and usable(t)]
    if above:
        return min(above, key=lambda p: p[0] - y)[1][:140]
    below = [(ly, t) for ly, _x, t in lines if 6 < y - ly <= 20 and usable(t)]
    if below:
        return max(below, key=lambda p: p[0])[1][:140]
    return ""


def _form_name(lines: list[tuple[float, float, str]], is_annotation) -> str:
    """The form's name. aCRF pages usually title themselves 'EG (ECG Test Results)' —
    that pattern wins wherever it sits; otherwise the topmost non-annotation line."""
    for _y, _x, t in lines:
        m = FORM_TITLE_RE.search(t)
        if m:
            return m.group(0)[:80]
    for _y, _x, t in lines:                          # lines come top first
        if len(t) > 2 and not t.strip().isdigit() and not is_annotation(t):
            return t[:80]
    return ""


def _annotation_texts(pdf_path: str | Path):
    """(page number, source, text, y position or None, page lines) for every annotation
    box and every page's flattened text."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise AcrfError(f"could not open the aCRF PDF: {exc}") from exc
    for pnum, page in enumerate(reader.pages, start=1):
        lines = _page_lines(page)
        try:
            for ref in (page.get("/Annots") or []):
                obj = ref.get_object()
                content = obj.get("/Contents")
                if s(content):
                    rect = obj.get("/Rect")
                    y = None
                    try:
                        y = (float(rect[1]) + float(rect[3])) / 2
                    except Exception:
                        pass
                    yield pnum, "annotation", str(content), y, lines
        except Exception:            # a malformed annotation must not sink the page
            pass
        text = " ".join(t for _y, _x, t in lines)
        if text.strip():
            yield pnum, "page", text, None, lines


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
    is_annotation = _annotation_detector(known_domains)

    def trim_value(val: str) -> str:
        """An assignment's value ends where the NEXT annotation token begins —
        'BEFOREINFUSION EGREPNUM' is a value and then a different annotation.
        Case matters: uppercasing would hide where a glued word ends."""
        for m in VAR_TOKEN_RE.finditer(val):
            t = m.group(0)
            if m.start() > 0 and (t.startswith("SUPP")
                                  or (len(t) >= 4 and t[:2] in known_domains)):
                return val[:m.start()].strip()
        return val.strip()

    def add(page: int, kind: str, domain: str, variable: str, value: str, snippet: str,
            question: str = "", form: str = ""):
        key = (page, domain, variable, value)
        if key in seen:
            return
        seen.add(key)
        rows.append({"page": page, "kind": kind, "domain": domain, "variable": variable,
                     "value": value, "text": snippet.strip()[:160],
                     "question": question, "form": form})

    for pnum, source, text, y, lines in _annotation_texts(pdf_path):
        pages = max(pages, pnum)
        form = _form_name(lines, is_annotation)
        question = (_question_for(lines, y, text, is_annotation)
                    if (source == "annotation" and y is not None) else "")

        def find_question(m) -> str:
            """Flattened text: the question is the text just before the variable on its line."""
            if question:
                return question
            before = text[max(0, m.start() - 90):m.start()].strip()
            before = re.sub(r"[A-Z]{2}\.[A-Z0-9_]{1,8}\s*$", "", before).strip()
            return before[-90:] if before and not is_annotation(before) else ""
        for m in NOTE_RE.finditer(text):
            add(pnum, "note", "", "", m.group(0).upper(),
                text[max(0, m.start() - 40):m.end() + 20], find_question(m), form)
        consumed: set[str] = set()
        for m in QUALIFIED_RE.finditer(text):
            dom, var = upper(m.group(1)), upper(m.group(2))
            consumed.update((var, dom))         # neither half may resurface as a bare token
            add(pnum, "qualified", dom, var, "",
                text[max(0, m.start() - 30):m.end() + 30], find_question(m), form)
        for m in ASSIGN_RE.finditer(text):
            var, val = upper(m.group(1)), m.group(2).strip()
            if var in NOISE:
                continue
            # the same variable can be assigned twice on a page (EGTESTCD = QTCFAG and
            # EGTESTCD = EGALL are two annotations) — every assignment's VALUE tokens are
            # values, never annotations of their own
            if var[:2] in known_domains or var[:4] == "SUPP":
                consumed.add(var)
                val = trim_value(val)
                consumed.update(t.upper() for t in VAR_TOKEN_RE.findall(val))
                add(pnum, "assignment", "", var, val,
                    text[max(0, m.start() - 20):m.end() + 10], find_question(m), form)
        for m in BARE_RE.finditer(text):
            var = upper(m.group(1))
            if var in NOISE or var in consumed or var in AUTOMATIC:
                continue
            if var[:2] not in known_domains:
                continue
            if source == "page" and len(var) < 4:   # flattened text: short tokens are labels
                continue
            add(pnum, "bare", "", var, "", text[max(0, m.start() - 25):m.end() + 25],
                find_question(m), form)
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
                vars_[upper(r.variable)] = {"label": r.label, "origin": r.origin,
                                            "input": r.input_variables}
        if vars_:
            out[upper(dom)] = vars_
    if not out:
        raise AcrfError(f"{p.name} has no sheets with a Variable column — is it a spec?")
    return out


def load_ecrf(path: str | Path) -> dict:
    """{RAW VARIABLE: {"form", "label"}} from an eCRF spec — one worksheet per form,
    with the collected variable names and their question text (the Label column).
    This is where CRF questions live when the aCRF PDF draws its form as graphics."""
    import pandas as pd
    p = Path(path)
    if not p.exists():
        raise AcrfError(f"{p} does not exist")
    try:
        xl = pd.ExcelFile(p)
    except Exception as exc:
        raise AcrfError(f"could not read {p.name} as a workbook: {exc}") from exc
    out: dict[str, dict] = {}
    for sheet in xl.sheet_names:
        try:
            probe = xl.parse(sheet, header=None, nrows=12)
        except Exception:
            continue
        header_row = None
        for i in range(len(probe)):
            cells = [str(c).strip().lower() for c in probe.iloc[i].tolist()]
            if any("variable" in c for c in cells) and any(c.startswith("label") or "question" in c for c in cells):
                header_row = i
                break
        if header_row is None:
            continue
        df = xl.parse(sheet, header=header_row)
        cols = {str(c).strip().lower(): c for c in df.columns}
        var_col = next((cols[c] for c in cols if "variable" in c), None)
        q_col = next((cols[c] for c in cols if c.startswith("label") or "question" in c), None)
        if not (var_col and q_col):
            continue
        for _i, row in df.iterrows():
            var, q = s(row.get(var_col)), s(row.get(q_col))
            if var and q and upper(var) not in out:
                out[upper(var)] = {"form": str(sheet), "label": q}
    if not out:
        raise AcrfError(f"{p.name} has no sheets with Variable Name and Label columns — "
                        "is it an eCRF spec?")
    return out


TOKEN_SPLIT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _fill_questions_from_ecrf(rows: list[dict], standards: dict, ta: dict,
                              ecrf: dict) -> int:
    """Where the PDF gave no question, take it from the eCRF spec: the annotation's
    variable is looked up directly, then via the standards' Input Variables (the raw
    names the SDTM variable is mapped from). Returns how many were filled."""
    filled = 0
    for r in rows:
        if r.get("question") or not r.get("variable"):
            continue
        var = upper(r["variable"])
        candidates = [var]
        for spec in (standards, ta):
            for dom, vars_ in (spec or {}).items():
                meta = vars_.get(var)
                if meta and meta.get("input"):
                    candidates += [upper(t) for t in TOKEN_SPLIT_RE.findall(meta["input"])]
        hit = next((ecrf[c] for c in candidates if c in ecrf), None)
        if hit:
            r["question"] = hit["label"][:140]
            if not r.get("form"):
                r["form"] = hit["form"][:80]
            filled += 1
    return filled


def _domains_for(var: str, *specs: dict) -> list[str]:
    doms = []
    for spec in specs:
        for dom, vars_ in (spec or {}).items():
            if var in vars_ and dom not in doms:
                doms.append(dom)
    return sorted(doms)


def check(pdf_path: str | Path, standards_path: str | Path,
          ta_path: str | Path | None = None,
          ecrf_path: str | Path | None = None) -> dict:
    """The whole aCRF check: extraction, verdict per annotation, and the reverse look —
    what the standards say is collected on the CRF but was never annotated. An eCRF spec,
    when given, supplies the question text the PDF itself cannot."""
    standards = load_standard(standards_path)
    ta = load_standard(ta_path) if s(ta_path) else {}
    ecrf = load_ecrf(ecrf_path) if s(ecrf_path) else {}
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
        elif var.startswith("SUPP") or f"SUPP{var[:2]}" in a.get("text", "").upper():
            # 'EGTRCVYN in SUPPEG' — a supplemental qualifier, annotated the usual way
            supp_dom = var[4:] if var.startswith("SUPP") else f"SUPP{var[:2]}"
            row["verdict"] = "supp"
            row["advice"] = (f"Annotated as a supplemental qualifier ({supp_dom or 'SUPP--'}) "
                             "— confirm the QNAM/QLABEL are defined in the standards' "
                             "SUPP conventions.")
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
    notes = []
    pdf_had_questions = any(r.get("question") for r in rows)
    filled_from_ecrf = _fill_questions_from_ecrf(rows, standards, ta, ecrf) if ecrf else 0
    if filled_from_ecrf:
        notes.append(f"{filled_from_ecrf} question(s) came from the eCRF spec — matched by "
                     "the annotation's variable and the standards' input variables.")
    if rows and not pdf_had_questions and not filled_from_ecrf:
        imgs = _image_pages(pdf_path)
        why = (f"{imgs} page(s) draw the CRF as an image" if imgs
               else "this PDF's text carries only the annotations — the form itself is "
                    "drawn as graphics")
        notes.append(
            f"No CRF question wording could be read: {why}. "
            + ("The eCRF spec gave no matches either — check its Variable Name column "
               "against the standards' Input Variables." if ecrf else
               "Point the check at your eCRF spec (optional field) and the questions "
               "will be filled from its Label column instead."))
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
            "origins_recorded": origins_recorded, "notes": notes}
