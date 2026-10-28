# Vendored from sdtm-studio sdtm_api/src/biomedical_concepts.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""Biomedical Concepts (BC) — the CDISC-standard link from a clinical concept (e.g. "Systolic Blood
Pressure") to its COMPLETE SDTM representation: domain, --TESTCD, --TEST, category, the standard
result unit, and datatype. This is the DETERMINISTIC backbone for findings construction — instead of
an LLM guessing that a raw column maps to VSTESTCD=SYSBP / VSTEST="Systolic Blood Pressure" /
VSORRESU=mmHg, the build looks it up here, so --TESTCD / --TEST / --ORRESU are right by construction.

Seeded with the common submission-grade concepts (VS / EG / LB / DA / PC …); a curated overlay in S3
(reference/biomedical_concepts.json) lets the GCDS team add/edit concepts (esp. study-/TA-specific
analytes). Resolvers match a raw column, a test code, or a test NAME to a concept.
"""
from __future__ import annotations

import json
import logging
import re


from .config import settings
from .aws_clients import get_client

logger = logging.getLogger(__name__)

_STORE_KEY = "reference/biomedical_concepts.json"


def _s3():
    return get_client("s3", region_name=settings.S3_REGION)


def _bc(domain, testcd, test, unit="", dtype="Num", cat="", codelist="", synonyms=()):
    return {"id": f"{domain}.{testcd}", "domain": domain.upper(), "testcd": testcd.upper(),
            "test": test, "cat": cat, "orresu": unit, "result_type": dtype, "codelist": codelist,
            "synonyms": [s.lower() for s in (list(synonyms) + [test, testcd])]}


# ── Seed library — common submission-grade concepts. Units follow CDISC UNIT CT. ──────────────
_SEED = [
    # Vital Signs
    _bc("VS", "SYSBP", "Systolic Blood Pressure", "mmHg", cat="", synonyms=["systolic bp", "sbp"]),
    _bc("VS", "DIABP", "Diastolic Blood Pressure", "mmHg", synonyms=["diastolic bp", "dbp"]),
    _bc("VS", "PULSE", "Pulse Rate", "beats/min", synonyms=["pulse", "pulse rate"]),
    _bc("VS", "HR", "Heart Rate", "beats/min", synonyms=["heart rate"]),
    _bc("VS", "RESP", "Respiratory Rate", "breaths/min", synonyms=["respiration", "resp rate"]),
    _bc("VS", "TEMP", "Temperature", "C", synonyms=["temperature", "body temperature"]),
    _bc("VS", "HEIGHT", "Height", "cm", synonyms=["height", "body height"]),
    _bc("VS", "WEIGHT", "Weight", "kg", synonyms=["weight", "body weight"]),
    _bc("VS", "BMI", "Body Mass Index", "kg/m2", synonyms=["bmi", "body mass index"]),
    _bc("VS", "WSTCIR", "Waist Circumference", "cm", synonyms=["waist circumference", "waist"]),
    _bc("VS", "HIPCIR", "Hip Circumference", "cm", synonyms=["hip circumference", "hip"]),
    _bc("VS", "OXYSAT", "Oxygen Saturation", "%", synonyms=["oxygen saturation", "spo2", "o2 sat"]),
    # ECG
    _bc("EG", "QT", "QT Interval", "msec", synonyms=["qt interval", "qt"]),
    _bc("EG", "QTCF", "QTcF Interval, Fridericia's Correction Formula", "msec", synonyms=["qtcf"]),
    _bc("EG", "QTCB", "QTcB Interval, Bazett's Correction Formula", "msec", synonyms=["qtcb"]),
    _bc("EG", "PR", "PR Interval", "msec", synonyms=["pr interval", "pr"]),
    _bc("EG", "RR", "RR Interval", "msec", synonyms=["rr interval", "rr"]),
    _bc("EG", "QRS", "QRS Duration", "msec", synonyms=["qrs duration", "qrs"]),
    _bc("EG", "EGHR", "Heart Rate", "beats/min", synonyms=["ecg heart rate", "eghr"]),
    _bc("EG", "EGINTP", "ECG Interpretation", dtype="Char", synonyms=["ecg interpretation", "interpretation"]),
    # Labs — hematology
    _bc("LB", "HGB", "Hemoglobin", "g/dL", synonyms=["hemoglobin", "haemoglobin", "hgb"]),
    _bc("LB", "HCT", "Hematocrit", "%", synonyms=["hematocrit", "haematocrit", "hct"]),
    _bc("LB", "RBC", "Erythrocytes", "10^12/L", synonyms=["rbc", "red blood cells", "erythrocytes"]),
    _bc("LB", "WBC", "Leukocytes", "10^9/L", synonyms=["wbc", "white blood cells", "leukocytes"]),
    _bc("LB", "PLAT", "Platelets", "10^9/L", synonyms=["platelets", "plat", "plt"]),
    _bc("LB", "NEUT", "Neutrophils", "10^9/L", synonyms=["neutrophils", "neut", "anc"]),
    _bc("LB", "LYM", "Lymphocytes", "10^9/L", synonyms=["lymphocytes", "lym"]),
    # Labs — chemistry
    _bc("LB", "ALT", "Alanine Aminotransferase", "U/L", synonyms=["alt", "sgpt", "alanine aminotransferase"]),
    _bc("LB", "AST", "Aspartate Aminotransferase", "U/L", synonyms=["ast", "sgot", "aspartate aminotransferase"]),
    _bc("LB", "ALP", "Alkaline Phosphatase", "U/L", synonyms=["alp", "alkaline phosphatase"]),
    _bc("LB", "BILI", "Bilirubin", "umol/L", synonyms=["bilirubin", "total bilirubin", "bili"]),
    _bc("LB", "CREAT", "Creatinine", "umol/L", synonyms=["creatinine", "creat"]),
    _bc("LB", "BUN", "Blood Urea Nitrogen", "mmol/L", synonyms=["bun", "urea", "blood urea nitrogen"]),
    _bc("LB", "GLUC", "Glucose", "mmol/L", synonyms=["glucose", "gluc"]),
    _bc("LB", "ALB", "Albumin", "g/L", synonyms=["albumin", "alb"]),
    _bc("LB", "SODIUM", "Sodium", "mmol/L", synonyms=["sodium", "na"]),
    _bc("LB", "K", "Potassium", "mmol/L", synonyms=["potassium", "k"]),
    _bc("LB", "CL", "Chloride", "mmol/L", synonyms=["chloride", "cl"]),
    _bc("LB", "CALCIUM", "Calcium", "mmol/L", synonyms=["calcium", "ca"]),
    # Labs — coagulation
    _bc("LB", "PT", "Prothrombin Time", "sec", synonyms=["prothrombin time", "pt"]),
    _bc("LB", "APTT", "Activated Partial Thromboplastin Time", "sec", synonyms=["aptt", "ptt"]),
    _bc("LB", "INR", "International Normalized Ratio", "ratio", synonyms=["inr"]),
    # Labs — renal / other chemistry
    _bc("LB", "EGFR", "Glomerular Filtration Rate, Estimated", "mL/min/1.73m2", synonyms=["egfr", "estimated gfr"]),
    _bc("LB", "PHOS", "Phosphate", "mmol/L", synonyms=["phosphate", "phosphorus", "phos"]),
    _bc("LB", "MG", "Magnesium", "mmol/L", synonyms=["magnesium", "mg"]),
    _bc("LB", "URATE", "Urate", "umol/L", synonyms=["urate", "uric acid"]),
    _bc("LB", "CK", "Creatine Kinase", "U/L", synonyms=["creatine kinase", "ck", "cpk"]),
    _bc("LB", "LDH", "Lactate Dehydrogenase", "U/L", synonyms=["ldh", "lactate dehydrogenase"]),
    _bc("LB", "GGT", "Gamma Glutamyl Transferase", "U/L", synonyms=["ggt", "gamma glutamyl transferase"]),
    _bc("LB", "CHOL", "Cholesterol", "mmol/L", synonyms=["cholesterol", "total cholesterol"]),
    _bc("LB", "TRIG", "Triglycerides", "mmol/L", synonyms=["triglycerides", "trig"]),
    _bc("LB", "AMYLASE", "Amylase", "U/L", synonyms=["amylase"]),
    _bc("LB", "LIPASE", "Lipase", "U/L", synonyms=["lipase"]),
    # Labs — HIV immunology (TA-specific)
    _bc("LB", "CD4", "CD4", "cells/uL", synonyms=["cd4", "cd4 count", "cd4 absolute", "cd4 (absolute)", "cd4 abs"]),
    _bc("LB", "CD4PCT", "CD4 Percent", "%", synonyms=["cd4%", "cd4 percent", "cd4 (%)", "cd4 pct"]),
    _bc("LB", "CD8", "CD8", "cells/uL", synonyms=["cd8", "cd8 count", "cd8 (absolute)"]),
    _bc("LB", "CD8PCT", "CD8 Percent", "%", synonyms=["cd8%", "cd8 percent", "cd8 (%)"]),
    _bc("LB", "CD4CD8", "CD4/CD8 Ratio", "ratio", synonyms=["cd4/cd8", "cd4 cd8 ratio", "cd4/cd8 ratio"]),
    _bc("LB", "HIVRNA", "HIV-1 RNA", "copies/mL", synonyms=["hiv rna", "hiv-1 rna", "hiv-1 rna quantitative",
        "hiv rna quantitative", "viral load", "vl", "hiv1 rna"]),
    _bc("LB", "HIVRNAQL", "HIV-1 RNA, Qualitative", dtype="Char", synonyms=["hiv-1 rna qualitative", "hiv rna qualitative"]),
    # Labs — HIV / hepatitis / syphilis serology (qualitative, Char results — no unit)
    _bc("LB", "HIV12AB", "HIV-1/2 Antibody", dtype="Char", synonyms=["hiv-1/2 antibody", "hiv 1/2 antibody",
        "hiv type1/type2 (ab)", "hiv type1/type2 antibody", "hiv-1/2 antibody rapid", "hiv-1/2 antibody, rapid",
        "hiv-1/2 antibody differentiation used for confirmation", "hiv-1/2 antibody differentiation"]),
    _bc("LB", "HIV12AGAB", "HIV-1/2 Antigen/Antibody", dtype="Char", synonyms=["hiv-1/2 antigen/antibody",
        "hiv-1/2 antigen/antibody, rapid", "hiv-1/2 antigen/antibody rapid", "hiv-1/2 antibody/antigen",
        "hiv-1/2 antibody/antigen, laboratory", "hiv-1/2 antibody/antigen laboratory"]),
    _bc("LB", "HIV1AB", "HIV-1 Antibody", dtype="Char", synonyms=["hiv-1 antibody", "hiv-1 antibody, laboratory",
        "hiv-1 antibody laboratory", "hiv-1 antibody, rapid", "hiv-1 antibody rapid"]),
    _bc("LB", "HIV2AB", "HIV-2 Antibody", dtype="Char", synonyms=["hiv-2 antibody", "hiv-2 antibody, laboratory",
        "hiv-2 antibody laboratory"]),
    _bc("LB", "HIVP24", "HIV-1 p24 Antigen", dtype="Char", synonyms=["hiv p24 antigen", "hiv p24 antigen (ag)",
        "hiv-1 p24 antigen", "p24 antigen", "hiv-1 antigen, rapid", "hiv-1 antigen rapid"]),
    _bc("LB", "TREPAB", "Treponema pallidum Antibody", dtype="Char", synonyms=["treponema pallidum antibody",
        "treponema pallidum", "syphilis antibody", "tp antibody"]),
    _bc("LB", "HBSAG", "Hepatitis B Surface Antigen", dtype="Char", synonyms=["hepatitis b surface antigen", "hbsag"]),
    _bc("LB", "HBSAB", "Hepatitis B Surface Antibody", dtype="Char", synonyms=["hepatitis b surface antibody", "hbsab", "anti-hbs"]),
    _bc("LB", "HBCAB", "Hepatitis B Core Antibody", dtype="Char", synonyms=["hepatitis b core antibody", "hbcab", "anti-hbc"]),
    _bc("LB", "HCVAB", "Hepatitis C Antibody", dtype="Char", synonyms=["hepatitis c antibody", "hcv antibody", "anti-hcv"]),
    _bc("LB", "COOMBSDIR", "Direct Antiglobulin Test", dtype="Char", synonyms=["antiglobulin test, direct (coombs test)",
        "antiglobulin test direct (coombs test)", "direct antiglobulin test", "coombs test", "direct coombs", "dat"]),
    # Urinalysis (common qualitative dipstick)
    _bc("LB", "UPROT", "Protein, Urine", dtype="Char", synonyms=["protein urine", "urine protein"]),
    _bc("LB", "UGLUC", "Glucose, Urine", dtype="Char", synonyms=["glucose urine", "urine glucose"]),
    _bc("LB", "UKET", "Ketones, Urine", dtype="Char", synonyms=["ketones urine", "urine ketones"]),
    _bc("LB", "UBLOOD", "Blood, Urine", dtype="Char", synonyms=["blood urine", "urine blood"]),
    # Drug Accountability
    _bc("DA", "DISPAMT", "Dispensed Amount", synonyms=["dispensed amount", "dispensed", "dispamt", "numdp"]),
    _bc("DA", "RETAMT", "Returned Amount", synonyms=["returned amount", "returned", "retamt", "numrt"]),
    _bc("DA", "TAKENAMT", "Amount Taken", synonyms=["amount taken", "taken"]),
    # PK concentration
    _bc("PC", "CONC", "Concentration", "ug/mL", synonyms=["concentration", "conc", "plasma concentration"]),
]


_CACHE: dict = {}


def _load_store() -> dict:
    """Curated overlay {id: {fields}} from S3 (added/edited concepts). Empty if none."""
    try:
        obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key=_STORE_KEY)
        d = json.loads(obj["Body"].read().decode("utf-8", "replace"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _catalog() -> dict:
    """{id: concept} = seed overlaid with the curated store (curated wins)."""
    if _CACHE.get("cat"):
        return _CACHE["cat"]
    cat = {c["id"]: dict(c) for c in _SEED}
    for cid, ov in (_load_store() or {}).items():
        base = cat.get(cid) or {"id": cid, "domain": str(cid).split(".")[0].upper(),
                                "testcd": str(cid).split(".")[-1].upper(), "synonyms": []}
        base.update({k: v for k, v in (ov or {}).items() if v not in (None, "")})
        cat[cid] = base
    _CACHE["cat"] = cat
    return cat


def _invalidate():
    _CACHE.pop("cat", None)


def list_bcs(domain: str | None = None) -> list:
    out = [dict(c) for c in _catalog().values()]
    if domain:
        out = [c for c in out if c.get("domain") == str(domain).upper()]
    return sorted(out, key=lambda c: (c.get("domain", ""), c.get("testcd", "")))


def get_bc(bc_id: str) -> dict | None:
    return _catalog().get(str(bc_id))


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def resolve(domain: str, hint: str) -> dict | None:
    """Match a raw column / test code / test name to a Biomedical Concept for the domain. Exact TESTCD
    first, then exact synonym, then a normalized-substring synonym match. None if nothing sensible."""
    dom = str(domain).upper()
    h = str(hint or "").strip()
    if not h:
        return None
    cands = [c for c in _catalog().values() if c.get("domain") == dom]
    hu = h.upper()
    for c in cands:                                   # exact test code
        if c["testcd"] == hu:
            return c
    hn = _norm(h)
    for c in cands:                                   # exact synonym / name
        if hn and hn in {_norm(s) for s in c.get("synonyms", [])}:
            return c
    for c in cands:                                   # normalized substring either direction
        for s in c.get("synonyms", []):
            sn = _norm(s)
            if sn and hn and (sn in hn or hn in sn) and abs(len(sn) - len(hn)) <= 4:
                return c
    return None


def decode_test(domain: str, testcd: str) -> str:
    """The standard --TEST name for a --TESTCD (deterministic decode). '' if unknown."""
    c = get_bc(f"{str(domain).upper()}.{str(testcd).strip().upper()}")
    return c["test"] if c else ""


def standard_unit(domain: str, testcd: str) -> str:
    c = get_bc(f"{str(domain).upper()}.{str(testcd).strip().upper()}")
    return (c.get("orresu") or "") if c else ""


def save_bc(bc: dict, user_email: str = "") -> dict:
    """Add/edit a concept in the curated overlay (GCDS)."""
    from datetime import datetime, timezone
    dom = str(bc.get("domain", "")).strip().upper()
    tc = str(bc.get("testcd", "")).strip().upper()
    if not dom or not tc:
        raise ValueError("domain and testcd are required")
    rec = {"id": f"{dom}.{tc}", "domain": dom, "testcd": tc,
           "test": str(bc.get("test", "")).strip(), "cat": str(bc.get("cat", "")).strip(),
           "orresu": str(bc.get("orresu", "")).strip(),
           "result_type": str(bc.get("result_type", "Num")).strip() or "Num",
           "codelist": str(bc.get("codelist", "")).strip(),
           "synonyms": [str(s).lower() for s in (bc.get("synonyms") or []) if str(s).strip()],
           "updated_by": user_email, "updated_at": datetime.now(timezone.utc).isoformat()}
    store = _load_store()
    store[rec["id"]] = rec
    _s3().put_object(Bucket=settings.S3_BUCKET, Key=_STORE_KEY,
                     Body=json.dumps(store, default=str).encode("utf-8"), ContentType="application/json")
    _invalidate()
    return rec


def delete_bc(bc_id: str) -> bool:
    """Remove a CURATED concept (seed concepts can't be deleted, only overridden)."""
    store = _load_store()
    if str(bc_id) in store:
        del store[str(bc_id)]
        _s3().put_object(Bucket=settings.S3_BUCKET, Key=_STORE_KEY,
                         Body=json.dumps(store, default=str).encode("utf-8"), ContentType="application/json")
        _invalidate()
        return True
    return False


_ID_KEEP = {"STUDYID", "USUBJID", "SUBJID", "X_SUBJID", "SUBJECTID", "SITEID", "VISIT", "VISITNUM",
            "VISITDY", "SPDEVID", "DATAPAGENAME", "INSTANCENAME", "FOLDER", "FOLDERNAME", "RECORDID"}


def bc_findings_base(raw_df, domain: str, id_vars=None):
    """DETERMINISTIC findings base from a WIDE raw form: every column that matches a Biomedical Concept
    for the domain becomes a long --TESTCD/--TEST/--ORRES/--ORRESU block (e.g. DA columns DANUMDP2 /
    DANUMRT → DATESTCD=DISPAMT/RETAMT). Returns a long DataFrame, or None when no column matches a
    concept (not a wide findings form — a long/name-column source needs a different path). This is the
    BC-driven replacement for an AI-guessed transpose."""
    import pandas as pd
    dom = str(domain).upper()
    cols = list(raw_df.columns)
    idv = [c for c in cols if str(c).upper() in _ID_KEEP] if id_vars is None else \
          [c for c in id_vars if c in cols]
    idset = {str(c).upper() for c in idv}
    measures = [(c, resolve(dom, c)) for c in cols if str(c).upper() not in idset]
    measures = [(c, bc) for c, bc in measures if bc]
    if not measures:
        return None
    tc, tv, orr, ou = f"{dom}TESTCD", f"{dom}TEST", f"{dom}ORRES", f"{dom}ORRESU"
    frames = []
    for c, bc in measures:
        f = raw_df[idv].copy()
        f[tc] = bc["testcd"]
        f[tv] = bc["test"]
        f[orr] = raw_df[c].astype(object)
        f[ou] = bc.get("orresu", "")
        frames.append(f)
    out = pd.concat(frames, ignore_index=True)
    keep = out[orr].astype(str).str.strip().replace({"nan": "", "None": ""}) != ""
    return out[keep].reset_index(drop=True)


_BLANK_STR = {"", "nan", "none", "<na>", "nat", "null"}


def _blank_mask(series):
    """Robust 'is blank' — covers NaN, pandas <NA>/NaT, and the strings they stringify to."""
    return series.isna() | series.astype(str).str.strip().str.lower().isin(_BLANK_STR)


def apply_to_findings(df, domain: str):
    """DETERMINISTIC findings enrichment: fill --TESTCD from the --TEST name via BC (only when BC
    recognizes the name), fill --TEST from the concept's standard name for each --TESTCD, and --ORRESU
    from the standard unit where blank. Never invents/mis-codes an unknown test. Returns (df, n_fixed)."""
    dom = str(domain).upper()
    tc, tv, ou = f"{dom}TESTCD", f"{dom}TEST", f"{dom}ORRESU"
    if tc not in df.columns:
        return df, 0
    fixed = 0
    # Reverse decode: a findings source often has the test NAME but not the CDISC code (e.g. LB carries
    # LBTEST="CD4 (absolute)" but LBTESTCD blank). Resolve the name → concept and fill the code.
    if tv in df.columns:
        empty_code = _blank_mask(df[tc])
        tvs = df[tv].astype(str).str.strip()
        for name in tvs[empty_code].unique():
            n = str(name).strip()
            if not n or n.lower() in _BLANK_STR:
                continue
            bc = resolve(dom, n)
            if bc:
                m = empty_code & (tvs == n)
                df.loc[m, tc] = bc["testcd"]
                fixed += int(m.sum())
    codes = [str(x).strip().upper() for x in df[tc].dropna().unique() if str(x).strip() and str(x).strip().lower() not in _BLANK_STR]
    decode = {code: decode_test(dom, code) for code in codes}
    unit = {code: standard_unit(dom, code) for code in codes}
    tcu = df[tc].astype(str).str.strip().str.upper()
    if tv in df.columns:
        for code, name in decode.items():
            if not name:
                continue
            m = tcu == code
            need = m & (_blank_mask(df[tv]) | (df[tv].astype(str).str.strip().str.upper() != name.upper()))
            if need.any():
                df.loc[need, tv] = name
                fixed += int(need.sum())
    if ou in df.columns:
        for code, u in unit.items():
            if not u:
                continue
            need = (tcu == code) & _blank_mask(df[ou])
            if need.any():
                df.loc[need, ou] = u
                fixed += int(need.sum())
    return df, fixed
