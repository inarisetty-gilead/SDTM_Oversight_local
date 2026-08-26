"""Generate synthetic raw datasets from the mapping spec.

The spec already describes the raw data it expects: every `raw.<dataset>.<column>` token in
Input Variables names a column some EDC extract is supposed to supply. That is enough to
build a raw folder with the right shape, so a spec can be exercised end to end before any
real extract exists.

The data is invented. It is realistic in *shape* — subjects, visits, dates that follow
enrolment, units that match their measurement, controlled terms where the spec names a
codelist — and meaningless in *content*. A marker file is written beside it, and the
application refuses to treat a comparison against a build from synthetic data as evidence.

Generation is deterministic: the same spec and seed produce byte-identical files.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .spec import Spec
from .translate import raw_refs
from .util import s, upper

MARKER = ".synthetic.json"

# one row per subject, per subject-visit, or several rows per subject
GRAIN_SUBJECT, GRAIN_VISIT, GRAIN_EVENT = "subject", "visit", "event"

# domains whose records are events rather than scheduled assessments
EVENT_DOMAINS = {"AE", "CM", "MH", "DS", "DV", "CE", "HO", "PR", "SU", "EX", "EC", "AG"}
SUBJECT_DOMAINS = {"DM", "SC", "SE"}

# physiological ranges keyed by what the measurement is called
MEASURES = {
    "SYSBP": ("mmHg", 122, 14, 80, 200), "DIABP": ("mmHg", 76, 9, 40, 120),
    "PULSE": ("beats/min", 72, 10, 40, 140), "HR": ("beats/min", 72, 10, 40, 140),
    "TEMP": ("C", 36.7, 0.4, 34.0, 40.0), "RESP": ("breaths/min", 16, 3, 8, 40),
    "HEIGHT": ("cm", 170, 10, 140, 200), "WEIGHT": ("kg", 74, 14, 40, 140),
    "BMI": ("kg/m2", 26, 4, 15, 45), "SPO2": ("%", 97, 2, 85, 100),
    "HGB": ("g/dL", 13.5, 1.6, 7, 18), "WBC": ("10^9/L", 6.8, 2.0, 1, 20),
    "PLAT": ("10^9/L", 250, 70, 20, 600), "NEUT": ("10^9/L", 4.0, 1.5, 0.5, 12),
    "ALT": ("U/L", 26, 12, 5, 200), "AST": ("U/L", 24, 10, 5, 200),
    "BILI": ("umol/L", 10, 4, 2, 60), "CREAT": ("umol/L", 78, 18, 40, 300),
    "GLUC": ("mmol/L", 5.3, 1.1, 2.5, 20), "ALB": ("g/L", 42, 4, 20, 55),
    "QT": ("msec", 400, 25, 300, 520), "QTCF": ("msec", 410, 22, 300, 520),
    "PR": ("msec", 158, 22, 90, 260), "QRS": ("msec", 92, 12, 60, 180),
    "RR": ("msec", 850, 120, 400, 1500), "EGVR": ("beats/min", 72, 10, 40, 140),
}

AE_TERMS = ["HEADACHE", "NAUSEA", "FATIGUE", "DIARRHOEA", "VOMITING", "PYREXIA", "RASH",
            "ANAEMIA", "COUGH", "ARTHRALGIA", "DIZZINESS", "CONSTIPATION", "NEUTROPENIA"]
CM_TERMS = ["PARACETAMOL", "OMEPRAZOLE", "ONDANSETRON", "DEXAMETHASONE", "IBUPROFEN",
            "METFORMIN", "AMLODIPINE", "LEVOTHYROXINE", "ATORVASTATIN"]
MH_TERMS = ["HYPERTENSION", "TYPE 2 DIABETES MELLITUS", "ASTHMA", "OSTEOARTHRITIS",
            "HYPERCHOLESTEROLAEMIA", "GASTROOESOPHAGEAL REFLUX DISEASE"]
RACES = ["WHITE", "ASIAN", "BLACK OR AFRICAN AMERICAN", "AMERICAN INDIAN OR ALASKA NATIVE",
         "NOT REPORTED"]
ETHNICS = ["NOT HISPANIC OR LATINO", "HISPANIC OR LATINO", "NOT REPORTED"]


def _u(*parts) -> float:
    """A stable number in [0,1) for these parts — the whole generator's source of randomness,
    so output depends only on the inputs and never on call order."""
    h = hashlib.blake2b("\x1f".join(str(p) for p in parts).encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big") / 2 ** 64


def _pick(seq, *parts):
    return seq[int(_u(*parts) * len(seq)) % len(seq)]


def _norm(mean, sd, *parts):
    """A bounded, symmetric deviate — enough spread to look real, no library needed."""
    return mean + sd * (sum(_u(*parts, i) for i in range(6)) - 3.0) / 1.2


@dataclass
class SynthOptions:
    subjects: int = 40
    visits: int = 5
    seed: str = "sdtm-oversight"
    studyid: str = ""
    start: date = date(2024, 1, 8)
    visit_gap_days: int = 28
    events_per_subject: int = 3
    fmt: str = "csv"


@dataclass
class Subject:
    idx: int
    subjid: str
    usubjid: str
    site: str
    sex: str
    race: str
    ethnic: str
    birth: date
    enrolled: date
    visits: list = field(default_factory=list)


def build_roster(opts: SynthOptions, studyid: str) -> list[Subject]:
    subs = []
    for i in range(1, opts.subjects + 1):
        site = f"{100 + (i - 1) % 5:03d}"
        subjid = f"{site}-{i:04d}"
        enrolled = opts.start + timedelta(days=int(_u(opts.seed, i, "enrol") * 120))
        byear = 1945 + int(_u(opts.seed, i, "byear") * 50)
        subs.append(Subject(
            idx=i, subjid=subjid, usubjid=f"{studyid}-{subjid}", site=site,
            sex=_pick(["M", "F"], opts.seed, i, "sex"),
            race=_pick(RACES, opts.seed, i, "race"),
            ethnic=_pick(ETHNICS, opts.seed, i, "eth"),
            birth=date(byear, 1 + int(_u(opts.seed, i, "bm") * 12),
                       1 + int(_u(opts.seed, i, "bd") * 28)),
            enrolled=enrolled,
            visits=[enrolled + timedelta(days=v * opts.visit_gap_days) for v in range(opts.visits)],
        ))
    return subs


# ── the schema the spec implies ─────────────────────────────────────────────
def raw_schema(spec: Spec) -> dict[str, dict[str, dict]]:
    """{dataset: {column: {domains, variables, labels, codelists, roles}}} from Input Variables."""
    out: dict[str, dict[str, dict]] = {}
    for dom in spec.domain_names:
        for r in spec.rows(dom):
            for ds, col in raw_refs(r.input_variables):
                meta = out.setdefault(ds, {}).setdefault(col, {
                    "domains": set(), "variables": set(), "labels": set(),
                    "codelists": set(), "roles": set(), "types": set()})
                meta["domains"].add(dom)
                meta["variables"].add(upper(r.variable))
                if s(r.label):
                    meta["labels"].add(s(r.label))
                if s(r.codelist):
                    meta["codelists"].add(upper(r.codelist))
                if s(r.role):
                    meta["roles"].add(s(r.role))
                if s(r.type):
                    meta["types"].add(s(r.type).lower())
    return out


def _grain(dataset: str, columns: dict) -> str:
    doms = {d for m in columns.values() for d in m["domains"]}
    cols = {upper(c) for c in columns}
    if doms & SUBJECT_DOMAINS and not (doms - SUBJECT_DOMAINS):
        return GRAIN_SUBJECT
    if any(c.startswith("VISIT") or c in ("FOLDERNAME", "INSTANCENAME") for c in cols):
        return GRAIN_VISIT
    if doms & EVENT_DOMAINS:
        return GRAIN_EVENT
    return GRAIN_VISIT


def _measure_for(col: str, meta: dict):
    cu = upper(col)
    for key, m in MEASURES.items():
        if cu == key or cu.startswith(key) or key in cu:
            return key, m
    for var in meta["variables"]:
        for key, m in MEASURES.items():
            if var.startswith(key) or key in var:
                return key, m
    blob = " ".join(meta["labels"]).lower()
    for key, m in MEASURES.items():
        if key.lower() in blob:
            return key, m
    return None, None


def _value(col: str, meta: dict, sub: Subject, visit: int, when: date,
           row: int, opts: SynthOptions, studyid: str, cache: dict, codelists: dict):
    cu = upper(col)
    seed = opts.seed
    key = (sub.subjid, cu, visit, row)

    # structural identifiers — these must be consistent for anything downstream to link
    if cu in ("STUDYID", "PROJECT", "STUDY", "STUDY_ID"):
        return studyid
    if cu == "DOMAIN":
        return ""
    if cu in ("SUBJID", "SUBJECTNUMBER", "SCRNID", "SUBJECT_ID", "SUBJ_ID"):
        return sub.subjid
    if cu in ("USUBJID", "X_SUBJID", "SUBJECT", "SUBJECTID"):
        return sub.usubjid
    if cu in ("SITEID", "SITENUMBER", "STUDYSITEID", "STUDYENVSITENUMBER", "SITE_NUMBER"):
        return sub.site
    if cu == "INVID":
        return f"INV{sub.site}"
    if cu in ("RECORDID", "INSTANCEID", "DATAPAGEID"):
        return 10000 + sub.idx * 100 + row
    if cu in ("RECORDPOSITION", "INSTANCEREPEATNUMBER", "PAGEREPEATNUMBER", "FOLDERSEQ"):
        return row + 1
    if cu in ("FOLDERNAME", "INSTANCENAME", "VISIT"):
        return "SCREENING" if visit == 0 else f"CYCLE {visit} DAY 1"
    if cu in ("VISITNUM", "FOLDERSEQNUM"):
        return visit + 1
    if cu == "DATAPAGENAME":
        return "eCRF"

    # a codelist named by the spec is the best possible source of a valid value
    for cl in meta["codelists"]:
        terms = codelists.get(cl)
        if terms:
            return _pick(sorted(set(terms.values())), seed, *key)

    # demographics
    if cu.startswith("SEX") or "sex" in " ".join(meta["labels"]).lower():
        return sub.sex
    if "RACE" in cu:
        return sub.race
    if "ETHNIC" in cu:
        return sub.ethnic
    if cu.startswith("BRTH") or "BIRTH" in cu:
        return sub.birth.isoformat()
    if cu in ("AGE", "AGE_REP", "AGEYR"):
        return sub.enrolled.year - sub.birth.year
    if cu == "AGEU":
        return "YEARS"

    # split date parts mirror the record date, so a partial date rebuilds correctly
    if cu.endswith("_YYYY"):
        return when.year
    if cu.endswith("_MM"):
        return f"{when.month:02d}"
    if cu.endswith("_DD"):
        return f"{when.day:02d}"

    # measurement satellites follow whatever the base measurement produced
    for suf in ("_STD_UN", "_STD", "_UNIT", "_UN", "_U", "U", "_RAW"):
        if cu.endswith(suf) and len(cu) > len(suf):
            base = cu[: -len(suf)]
            if base in cache:
                if suf in ("_UN", "_UNIT", "_U", "U", "_STD_UN"):
                    return cache.get(base + "|unit", "")
                return cache[base]

    # dates and times
    label_blob = " ".join(meta["labels"]).lower()
    if cu.endswith(("DAT", "DATE", "DTC", "DTM", "DT")) or "date" in label_blob:
        offset = int(_u(seed, *key, "d") * 5)
        return (when + timedelta(days=offset)).isoformat()
    if cu.endswith(("TIM", "TIME")) or "time" in label_blob:
        return f"{6 + int(_u(seed, *key, 'h') * 14):02d}:{int(_u(seed, *key, 'm') * 60):02d}"

    # a real measurement
    name, m = _measure_for(col, meta)
    if m:
        unit, mean, sd, lo, hi = m
        drift = (visit / max(opts.visits - 1, 1)) * sd * 0.4 * (1 if _u(seed, sub.subjid, name) > .5 else -1)
        val = _norm(mean + drift, sd * 0.5, seed, sub.subjid, name, visit, row)
        val = round(min(max(val, lo), hi), 1 if sd < 5 else 0)
        cache[cu] = val
        cache[cu + "|unit"] = unit
        return val

    # verbatim terms
    if "AETERM" in meta["variables"] or cu.startswith("AETERM"):
        return cache.setdefault("|ae", _pick(AE_TERMS, seed, *key))
    if meta["variables"] & {"AEDECOD", "AELLT", "AEBODSYS", "AESOC"}:
        return cache.get("|ae", _pick(AE_TERMS, seed, *key))
    if meta["variables"] & {"CMTRT", "CMDECOD"} or cu.startswith("CMTRT"):
        return _pick(CM_TERMS, seed, *key)
    if meta["variables"] & {"MHTERM", "MHDECOD"} or cu.startswith("MHTERM"):
        return _pick(MH_TERMS, seed, *key)

    # yes/no flags
    if cu.endswith(("YN", "FL", "_STD")) or "y/n" in label_blob or "performed" in label_blob:
        return _pick(["Y", "N", "N"], seed, *key)

    # sequence-ish
    if cu.endswith(("SEQ", "NUM", "NO", "CNT", "COUNT")):
        return row + 1

    if "num" in meta["types"] or "integer" in meta["types"] or "float" in meta["types"]:
        return round(_norm(50, 20, seed, *key), 1)

    # nothing specific is known — a short, obviously synthetic token
    return f"{cu[:8]}-{sub.idx:03d}"


def generate(spec: Spec, out_dir: str | Path, opts: SynthOptions | None = None) -> dict:
    """Write a synthetic raw folder for this spec. Returns a summary."""
    opts = opts or SynthOptions()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    studyid = s(opts.studyid) or "SYNTH-001"
    schema = raw_schema(spec)
    if not schema:
        raise ValueError(
            "this spec names no raw.<dataset>.<column> sources, so there is no raw schema to "
            "generate from")
    subs = build_roster(opts, studyid)

    written, total_rows = [], 0
    for dataset in sorted(schema):
        columns = schema[dataset]
        grain = _grain(dataset, columns)
        # base measurements are produced before their unit/std/raw satellites
        ordered = sorted(columns, key=lambda c: (
            any(upper(c).endswith(x) for x in ("_STD_UN", "_STD", "_UNIT", "_UN", "_U", "U", "_RAW")),
            upper(c)))

        records = []
        for sub in subs:
            if grain == GRAIN_SUBJECT:
                slots = [(0, sub.visits[0], 0)]
            elif grain == GRAIN_VISIT:
                slots = [(v, sub.visits[v], 0) for v in range(opts.visits)]
            else:
                n = 1 + int(_u(opts.seed, sub.subjid, dataset) * opts.events_per_subject)
                slots = [(min(r, opts.visits - 1),
                          sub.enrolled + timedelta(days=7 + r * 21), r) for r in range(n)]
            for visit, when, row in slots:
                cache: dict = {}
                records.append({c: _value(c, columns[c], sub, visit, when, row, opts,
                                          studyid, cache, spec.codelists) for c in ordered})

        frame = pd.DataFrame(records, columns=ordered)
        path = out / f"{dataset}.{opts.fmt}"
        if opts.fmt == "csv":
            frame.to_csv(path, index=False)
        elif opts.fmt == "parquet":
            frame.to_parquet(path, index=False)
        else:
            raise ValueError(f"unsupported synthetic output format: {opts.fmt}")
        written.append({"dataset": dataset, "rows": len(frame), "columns": len(ordered),
                        "grain": grain, "file": path.name})
        total_rows += len(frame)

    marker = {
        "synthetic": True,
        "generated_from": spec.path,
        "studyid": studyid,
        "subjects": opts.subjects,
        "visits": opts.visits,
        "seed": opts.seed,
        "datasets": written,
        "warning": "Invented data. Any comparison against a vendor delivery built from this "
                   "folder tests the mapping logic only — it says nothing about the vendor.",
    }
    (out / MARKER).write_text(json.dumps(marker, indent=2))
    (out / "READ_ME_SYNTHETIC.txt").write_text(
        "SYNTHETIC DATA — NOT REAL STUDY DATA\n"
        "====================================\n\n"
        f"Generated from: {spec.path}\n"
        f"Subjects: {opts.subjects}   Visits: {opts.visits}   Seed: {opts.seed}\n\n"
        "Every value here is invented. The shape is realistic — subjects, visits, dates that\n"
        "follow enrolment, units matching their measurement — so a mapping spec can be\n"
        "exercised end to end before a real extract exists.\n\n"
        "Use it to check that the spec builds. Do NOT use it to judge a vendor: a comparison\n"
        "against a delivery is only meaningful when the build reads the same raw data the\n"
        "vendor read.\n")
    return {"out_dir": str(out), "studyid": studyid, "datasets": written,
            "rows": total_rows, "subjects": opts.subjects}


def read_marker(folder: str | Path) -> dict | None:
    """The synthetic marker for a raw folder, or None when the data is real."""
    p = Path(folder) / MARKER
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"synthetic": True}
