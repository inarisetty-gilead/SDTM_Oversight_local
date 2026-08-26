"""Build a self-contained test study: raw datasets, a Designer-format mapping spec, and a
'vendor' SDTM delivery that differs from the spec in known, checkable ways."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture")
RAW, VENDOR = OUT / "raw", OUT / "vendor"
for d in (RAW, VENDOR):
    d.mkdir(parents=True, exist_ok=True)

SUBJ = [f"101-{i:03d}" for i in range(1, 7)]
STUDY = "GS-TEST-001"

# ── raw datasets (as an EDC extract would look) ─────────────────────────────
dm = pd.DataFrame({
    "STUDYID": STUDY, "SUBJID": SUBJ,
    "USUBJID": [f"{STUDY}-{s}" for s in SUBJ],
    "SITEID": [s.split("-")[0] for s in SUBJ],
    "SEXCD": ["m", "F", "Male", "f", "M", "female"],
    "BRTHDAT_YYYY": ["1975", "1962", "1988", "1990", "1955", ""],
    "BRTHDAT_MM": ["03", "11", "07", "01", "09", ""],
    "BRTHDAT_DD": ["12", "", "30", "22", "05", ""],
    "RACECD": ["WHITE", "ASIAN", "BLACK OR AFRICAN AMERICAN", "WHITE", "ASIAN", "WHITE"],
    # collected, but the mapping spec never names it as an Input Variable
    "ETHNICCD": ["NOT HISPANIC OR LATINO", "HISPANIC OR LATINO", "NOT HISPANIC OR LATINO",
                 "NOT HISPANIC OR LATINO", "HISPANIC OR LATINO", "NOT REPORTED"],
    "ARMCD": ["TRT", "PBO", "TRT", "PBO", "TRT", "PBO"],
    "RFSTDAT": ["2024-01-15", "2024-01-22", "2024-02-01", "2024-02-12", "2024-03-04", "2024-03-11"],
})
dm.to_csv(RAW / "dm.csv", index=False)

ae_rows = [
    # subj, term, start, end, sev, ser, out
    (0, "headache",            "2024-01-20", "2024-01-25", "mild",     "N"),
    (0, "Nausea",              "2024-02-02", "",           "moderate", "N"),
    (1, "FATIGUE",             "2024-01-30", "2024-02-05", "mild",     "N"),
    (2, "vomiting",            "2024-02-10", "2024-02-11", "severe",   "Y"),
    (2, "Rash",                "2024-02-15", "",           "mild",     "N"),
    (3, "dizziness",           "2024-02-20", "2024-02-22", "moderate", "N"),
    (4, "Pyrexia",             "2024-03-10", "2024-03-12", "mild",     "N"),
    (5, "cough",               "2024-03-15", "",           "mild",     "N"),
]
ae = pd.DataFrame({
    "STUDYID": STUDY,
    "SUBJID": [SUBJ[r[0]] for r in ae_rows],
    "AETERM": [r[1] for r in ae_rows],
    "AESTDAT": [r[2] for r in ae_rows],
    "AEENDAT": [r[3] for r in ae_rows],
    "AESEVCD": [r[4] for r in ae_rows],
    "AESER": [r[5] for r in ae_rows],
    "AECOMMENT": ["", "took paracetamol", "", "hospitalised", "", "", "", "resolved"],
})
ae["USUBJID"] = ae["SUBJID"].map(dict(zip(dm["SUBJID"], dm["USUBJID"])))
ae.to_csv(RAW / "ae.csv", index=False)

vs_rows = []
for i, sj in enumerate(SUBJ):
    for visit, day in (("SCREENING", 0), ("CYCLE 1 DAY 1", 14)):
        for code, name, val, unit in (("SYSBP", "Systolic Blood Pressure", 118 + i * 3, "mmHg"),
                                      ("DIABP", "Diastolic Blood Pressure", 74 + i, "mmHg"),
                                      ("PULSE", "Pulse Rate", 66 + i * 2, "beats/min")):
            vs_rows.append({
                "STUDYID": STUDY, "SUBJID": sj,
                "USUBJID": f"{STUDY}-{sj}",
                "VISIT": visit, "VISITNUM": 1 if day == 0 else 2,
                "VSTESTCD": code, "VSTEST": name,
                "VSORRES": val, "VSORRESU": unit,
                "VSDAT": (pd.Timestamp("2024-01-15") + pd.Timedelta(days=day + i * 7)).strftime("%Y-%m-%d"),
                "VSTIM": "09:30",
            })
pd.DataFrame(vs_rows).to_csv(RAW / "vs.csv", index=False)

# ── a WIDE findings form: one record holds several measurements in separate columns.
# Collected as "egperf" while the spec references raw.eg.* — the real-world naming mismatch.
eg_rows = []
for i, sj in enumerate(SUBJ):
    for visit, day in (("SCREENING", 0), ("CYCLE 1 DAY 1", 14)):
        eg_rows.append({
            "STUDYID": STUDY, "SUBJID": sj, "USUBJID": f"{STUDY}-{sj}",
            "VISIT": visit, "VISITNUM": 1 if day == 0 else 2,
            "RECORDDATE": (pd.Timestamp("2024-01-15") + pd.Timedelta(days=day + i * 7)).strftime("%Y-%m-%d"),
            "EGPERF": "Y",
            "EGVR": 62 + i, "EGVRU": "beats/min",
            "EGPRI": 150 + i * 2, "EGPRIU": "msec",
            "EGQRSI": 88 + i, "EGQRSIU": "msec",
        })
pd.DataFrame(eg_rows).to_csv(RAW / "egperf.csv", index=False)

# ── a domain with NO raw dataset of its own: DS records come from three separate forms ──
ds_forms = {
    "consent": [(sj, "INFORMED CONSENT OBTAINED", "INFORMED CONSENT OBTAINED", "PROTOCOL MILESTONE",
                 "2024-01-0%d" % ((i % 8) + 1)) for i, sj in enumerate(SUBJ)],
    "enroll": [(sj, "RANDOMIZED", "RANDOMIZED", "PROTOCOL MILESTONE",
                dm["RFSTDAT"][i]) for i, sj in enumerate(SUBJ)],
    "studcomp": [(sj, "COMPLETED STUDY" if i % 2 == 0 else "ADVERSE EVENT",
                  "COMPLETED" if i % 2 == 0 else "ADVERSE EVENT", "DISPOSITION EVENT",
                  "2024-06-1%d" % (i % 10)) for i, sj in enumerate(SUBJ)],
}
for form, recs in ds_forms.items():
    pd.DataFrame([{
        "STUDYID": STUDY, "SUBJID": r[0], "USUBJID": f"{STUDY}-{r[0]}",
        "DSTERM": r[1], "DSDECOD": r[2], "DSCAT": r[3], "DSSTDAT": r[4],
    } for r in recs]).to_csv(RAW / f"{form}.csv", index=False)


# ── the mapping spec, in Designer core-spec layout ──────────────────────────
COLS = ["Variable", "Label", "Mapping Action", "Input Variables", "Mapping Rule",
        "Implemented SAS Code", "Codelist", "Role", "Origin", "Dataset", "Type", "Length", "Order"]


def row(var, label, action="ASSIGN", iv="", rule="", sas="", cl="", role="", origin="CRF",
        ds="", typ="text", length=200, order=0):
    return dict(zip(COLS, [var, label, action, iv, rule, sas, cl, role, origin, ds, typ, length, order]))


dm_spec = [
    row("STUDYID", "Study Identifier", iv="raw.dm.STUDYID", role="Identifier", ds="DM"),
    row("DOMAIN", "Domain Abbreviation", sas='"DM"', role="Identifier", ds="DM"),
    row("USUBJID", "Unique Subject Identifier", iv="raw.dm.USUBJID", role="Identifier", ds="DM"),
    row("SUBJID", "Subject Identifier for the Study", iv="raw.dm.SUBJID", role="Topic", ds="DM"),
    row("SITEID", "Study Site Identifier", iv="raw.dm.SITEID", role="Identifier", ds="DM"),
    row("BRTHDTC", "Date/Time of Birth", iv="raw.dm.BRTHDAT_YYYY", role="Timing", ds="DM"),
    row("SEX", "Sex", iv="raw.dm.SEXCD", cl="SEX", role="Qualifier", ds="DM"),
    row("RACE", "Race", iv="raw.dm.RACECD", role="Qualifier", ds="DM"),
    row("ARMCD", "Planned Arm Code", iv="raw.dm.ARMCD", role="Qualifier", ds="DM"),
    row("RFSTDTC", "Start Date/Time of Study Treatment", iv="raw.dm.RFSTDAT",
        role="Timing", origin="Derived", ds="DM"),
    # no Input Variables and no rule — only a name that resembles a collected column
    row("ETHNIC", "Ethnicity", "", role="Qualifier", ds="DM"),
    row("COUNTRY", "Country", "DROP", role="Qualifier", ds="DM"),
    row("AGE", "Age", "", rule="Derive AGE per the SAP.", role="Qualifier",
        origin="Derived", ds="DM"),
    row("AGEU", "Age Units", "", role="Qualifier", origin="Derived", ds="DM"),
    row("DTHFL", "Subject Death Flag", "", rule="Set to Y if the subject died on study.",
        role="Qualifier", origin="Derived", ds="DM"),
]

ae_spec = [
    row("STUDYID", "Study Identifier", iv="raw.ae.STUDYID", role="Identifier", ds="AE"),
    row("DOMAIN", "Domain Abbreviation", sas='"AE"', role="Identifier", ds="AE"),
    row("USUBJID", "Unique Subject Identifier", iv="raw.ae.USUBJID", role="Identifier", ds="AE"),
    row("AESEQ", "Sequence Number", "", role="Identifier", origin="Derived", ds="AE"),
    row("AETERM", "Reported Term for the Adverse Event", iv="raw.ae.AETERM",
        sas="strip(upcase(aeterm))", role="Topic", ds="AE"),
    row("AESEV", "Severity/Intensity", iv="raw.ae.AESEVCD", cl="AESEV", role="Qualifier", ds="AE"),
    row("AESER", "Serious Event", iv="raw.ae.AESER", cl="NY", role="Qualifier", ds="AE"),
    row("AESTDTC", "Start Date/Time of Adverse Event", iv="raw.ae.AESTDAT", role="Timing", ds="AE"),
    row("AEENDTC", "End Date/Time of Adverse Event", iv="raw.ae.AEENDAT", role="Timing", ds="AE"),
    row("AESTDY", "Study Day of Start of Adverse Event",
        iv="sdtm.ae.aestdtc, sdtm.dm.rfstdtc", role="Timing", origin="Derived", ds="AE"),
    row("AECOMM", "Investigator comment", iv="raw.ae.AECOMMENT", role="Qualifier", ds="QNAM"),
]

vs_spec = [
    row("STUDYID", "Study Identifier", iv="raw.vs.STUDYID", role="Identifier", ds="VS"),
    row("DOMAIN", "Domain Abbreviation", sas='"VS"', role="Identifier", ds="VS"),
    row("USUBJID", "Unique Subject Identifier", iv="raw.vs.USUBJID", role="Identifier", ds="VS"),
    row("VSSEQ", "Sequence Number", "", role="Identifier", origin="Derived", ds="VS"),
    row("VSTESTCD", "Vital Signs Test Short Name", iv="raw.vs.VSTESTCD", role="Topic", ds="VS"),
    row("VSTEST", "Vital Signs Test Name", iv="raw.vs.VSTEST", role="Topic", ds="VS"),
    row("VSORRES", "Result or Finding in Original Units", iv="raw.vs.VSORRES", role="Result", ds="VS"),
    row("VSORRESU", "Original Units", iv="raw.vs.VSORRESU", role="Variable Qualifier", ds="VS"),
    row("VISIT", "Visit Name", iv="raw.vs.VISIT", role="Timing", ds="VS"),
    row("VISITNUM", "Visit Number", iv="raw.vs.VISITNUM", role="Timing", ds="VS"),
    row("VSDTC", "Date/Time of Measurements", iv="raw.vs.VSDAT", role="Timing", ds="VS"),
    row("VSDY", "Study Day of Vital Signs", iv="sdtm.vs.vsdtc, sdtm.dm.rfstdtc",
        role="Timing", origin="Derived", ds="VS"),
]

eg_spec = [
    row("STUDYID", "Study Identifier", iv="raw.eg.STUDYID", role="Identifier", ds="EG"),
    row("DOMAIN", "Domain Abbreviation", sas='"EG"', role="Identifier", ds="EG"),
    row("USUBJID", "Unique Subject Identifier", iv="raw.eg.USUBJID", role="Identifier", ds="EG"),
    row("EGSEQ", "Sequence Number", "", role="Identifier", origin="Derived", ds="EG"),
    row("EGTESTCD", "ECG Test Short Name",
        iv="raw.eg.EGVR, raw.eg.EGPRI, raw.eg.EGQRSI", role="Topic", ds="EG"),
    row("EGTEST", "ECG Test Name",
        iv="raw.eg.EGVR, raw.eg.EGPRI, raw.eg.EGQRSI", role="Topic", ds="EG"),
    row("EGORRES", "Result or Finding in Original Units",
        iv="raw.eg.EGVR, raw.eg.EGPRI, raw.eg.EGQRSI", role="Result", ds="EG"),
    row("EGORRESU", "Original Units",
        iv="raw.eg.EGVRU, raw.eg.EGPRIU, raw.eg.EGQRSIU", role="Variable Qualifier", ds="EG"),
    row("VISIT", "Visit Name", iv="raw.eg.VISIT", role="Timing", ds="EG"),
    row("VISITNUM", "Visit Number", iv="raw.eg.VISITNUM", role="Timing", ds="EG"),
    row("EGDTC", "Date/Time of ECG", iv="raw.eg.RECORDDATE", role="Timing", ds="EG"),
]

ds_spec = [
    row("STUDYID", "Study Identifier",
        iv="raw.consent.STUDYID, raw.enroll.STUDYID, raw.studcomp.STUDYID",
        role="Identifier", ds="DS"),
    row("DOMAIN", "Domain Abbreviation", sas='"DS"', role="Identifier", ds="DS"),
    row("USUBJID", "Unique Subject Identifier",
        iv="raw.consent.USUBJID, raw.enroll.USUBJID, raw.studcomp.USUBJID",
        role="Identifier", ds="DS"),
    row("DSSEQ", "Sequence Number", "", role="Identifier", origin="Derived", ds="DS"),
    row("DSTERM", "Reported Term for the Disposition Event",
        iv="raw.consent.DSTERM, raw.enroll.DSTERM, raw.studcomp.DSTERM", role="Topic", ds="DS"),
    row("DSDECOD", "Standardized Disposition Term",
        iv="raw.consent.DSDECOD, raw.enroll.DSDECOD, raw.studcomp.DSDECOD",
        role="Qualifier", ds="DS"),
    row("DSCAT", "Category for Disposition Event",
        iv="raw.consent.DSCAT, raw.enroll.DSCAT, raw.studcomp.DSCAT", role="Qualifier", ds="DS"),
    row("DSSTDTC", "Start Date/Time of Disposition Event",
        iv="raw.consent.DSSTDAT, raw.enroll.DSSTDAT, raw.studcomp.DSSTDAT",
        role="Timing", ds="DS"),
]

codelists = pd.DataFrame([
    {"Codelist": "SEX", "Submission Value": "M", "Decode": "Male", "Synonyms": "m;male"},
    {"Codelist": "SEX", "Submission Value": "F", "Decode": "Female", "Synonyms": "f;female"},
    {"Codelist": "AESEV", "Submission Value": "MILD", "Decode": "Mild", "Synonyms": "mild;1"},
    {"Codelist": "AESEV", "Submission Value": "MODERATE", "Decode": "Moderate", "Synonyms": "moderate;2"},
    {"Codelist": "AESEV", "Submission Value": "SEVERE", "Decode": "Severe", "Synonyms": "severe;3"},
    {"Codelist": "NY", "Submission Value": "Y", "Decode": "Yes", "Synonyms": "yes;1"},
    {"Codelist": "NY", "Submission Value": "N", "Decode": "No", "Synonyms": "no;0"},
])

spec_path = OUT / "mapping_spec.xlsx"
with pd.ExcelWriter(spec_path, engine="openpyxl") as xw:
    pd.DataFrame([
        {"Active": "Y", "Dataset": "DM", "Label": "Demographics", "Class": "SPECIAL PURPOSE",
         "Structure": "One record per subject", "Display Order": 1},
        {"Active": "N", "Dataset": "DM-DATA", "Label": "Demographics", "Class": "SPECIAL PURPOSE",
         "Structure": "", "Display Order": 2},
        {"Active": "Y", "Dataset": "AE", "Label": "Adverse Events", "Class": "EVENTS",
         "Structure": "One record per event", "Display Order": 3},
        {"Active": "Y", "Dataset": "VS", "Label": "Vital Signs", "Class": "FINDINGS",
         "Structure": "One record per measurement", "Display Order": 4},
        {"Active": "Y", "Dataset": "EG", "Label": "ECG", "Class": "FINDINGS",
         "Structure": "One record per measurement", "Display Order": 5},
        {"Active": "Y", "Dataset": "DS", "Label": "Disposition", "Class": "EVENTS",
         "Structure": "One record per event", "Display Order": 6},
        {"Active": "N", "Dataset": "XX", "Label": "Not in this study", "Class": "FINDINGS",
         "Structure": "", "Display Order": 7},
    ]).to_excel(xw, sheet_name="TOC", index=False)
    for name, rows in (("DM", dm_spec), ("AE", ae_spec), ("VS", vs_spec),
                   ("EG", eg_spec), ("DS", ds_spec)):
        df = pd.DataFrame(rows)
        df["Order"] = range(1, len(df) + 1)
        df.to_excel(xw, sheet_name=name, index=False)
    codelists.to_excel(xw, sheet_name="Codelist", index=False)

# ── vendor delivery, with deliberate, documented differences ────────────────
def iso(y, m, d):
    parts = [p for p in (y, m, d) if str(p).strip()]
    return "-".join(parts) if parts else ""


v_dm = pd.DataFrame({
    "STUDYID": STUDY, "DOMAIN": "DM",
    "USUBJID": dm["USUBJID"],
    "SUBJID": dm["SUBJID"], "SITEID": dm["SITEID"],
    "BRTHDTC": [iso(y, m, d) for y, m, d in zip(dm["BRTHDAT_YYYY"], dm["BRTHDAT_MM"], dm["BRTHDAT_DD"])],
    "SEX": ["M", "F", "M", "F", "M", "F"],
    # DIFFERENCE 1: vendor abbreviated one race value
    "RACE": ["WHITE", "ASIAN", "BLACK", "WHITE", "ASIAN", "WHITE"],
    "ETHNIC": dm["ETHNICCD"],
    "ARMCD": dm["ARMCD"], "RFSTDTC": dm["RFSTDAT"],
})
v_dm.to_csv(VENDOR / "dm.csv", index=False)

v_ae = pd.DataFrame({
    "STUDYID": STUDY, "DOMAIN": "AE", "USUBJID": ae["USUBJID"],
    "AESEQ": ae.groupby("USUBJID").cumcount() + 1,
    "AETERM": [t.upper() for t in ae["AETERM"]],
    # DIFFERENCE 2: vendor left one severity in the raw spelling instead of the CT term
    "AESEV": ["MILD", "MODERATE", "MILD", "severe", "MILD", "MODERATE", "MILD", "MILD"],
    "AESER": ae["AESER"],
    "AESTDTC": ae["AESTDAT"], "AEENDTC": ae["AEENDAT"],
    # DIFFERENCE 3: vendor's study day is off by one (no +1 on/after RFSTDTC)
    "AESTDY": [5, 18, 8, 10, 15, 8, 6, 4],
})
# DIFFERENCE 4: vendor dropped one adverse event record
v_ae = v_ae[v_ae["AETERM"] != "COUGH"].reset_index(drop=True)
v_ae.to_csv(VENDOR / "ae.csv", index=False)

v_vs = pd.DataFrame(vs_rows)
v_vs = v_vs.rename(columns={"VSDAT": "VSDTC"})
v_vs["DOMAIN"] = "VS"
v_vs["VSDTC"] = v_vs["VSDTC"] + "T09:30"
v_vs["VSSEQ"] = v_vs.groupby("USUBJID").cumcount() + 1
v_vs = v_vs[["STUDYID", "DOMAIN", "USUBJID", "VSSEQ", "VSTESTCD", "VSTEST",
             "VSORRES", "VSORRESU", "VISIT", "VISITNUM", "VSDTC"]]
v_vs.to_csv(VENDOR / "vs.csv", index=False)

# vendor EG — the same wide form melted the same way, but one unit was dropped
v_eg = []
for r in eg_rows:
    # same order the spec lists the columns in, so --SEQ lines up on both sides
    for code, val, unit in (("VR", r["EGVR"], r["EGVRU"]),
                            ("PRI", r["EGPRI"], r["EGPRIU"]),
                            ("QRSI", r["EGQRSI"], r["EGQRSIU"])):
        v_eg.append({"STUDYID": STUDY, "DOMAIN": "EG", "USUBJID": r["USUBJID"],
                     "EGTESTCD": code, "EGTEST": code, "EGORRES": val,
                     # DIFFERENCE 6: the vendor left PRI without its unit
                     "EGORRESU": "" if code == "PRI" else unit,
                     "VISIT": r["VISIT"], "VISITNUM": r["VISITNUM"], "EGDTC": r["RECORDDATE"]})
v_eg = pd.DataFrame(v_eg)
v_eg["EGSEQ"] = v_eg.groupby("USUBJID").cumcount() + 1
v_eg.to_csv(VENDOR / "eg.csv", index=False)

# vendor DS — the three forms stacked, but one disposition record never made it
v_ds = []
for form, recs in ds_forms.items():
    for r in recs:
        v_ds.append({"STUDYID": STUDY, "DOMAIN": "DS", "USUBJID": f"{STUDY}-{r[0]}",
                     "DSTERM": r[1], "DSDECOD": r[2], "DSCAT": r[3], "DSSTDTC": r[4]})
v_ds = pd.DataFrame(v_ds)
# DIFFERENCE 7: the last subject's study-completion record is missing
v_ds = v_ds.drop(v_ds[(v_ds["USUBJID"] == f"{STUDY}-{SUBJ[-1]}")
                      & (v_ds["DSCAT"] == "DISPOSITION EVENT")].index).reset_index(drop=True)
v_ds = v_ds.sort_values(["USUBJID", "DSSTDTC"], kind="stable").reset_index(drop=True)
v_ds["DSSEQ"] = v_ds.groupby("USUBJID").cumcount() + 1
v_ds.to_csv(VENDOR / "ds.csv", index=False)

print(f"fixture written to {OUT.resolve()}")
print(f"  spec:   {spec_path.name}")
print(f"  raw:    {sorted(p.name for p in RAW.iterdir())}")
print(f"  vendor: {sorted(p.name for p in VENDOR.iterdir())}")
print("\nDeliberate vendor differences:")
print("  DM.RACE     — 'BLACK' vs the spec's raw value 'BLACK OR AFRICAN AMERICAN'")
print("  AE.AESEV    — one record left as 'severe' instead of the CT term 'SEVERE'")
print("  AE.AESTDY   — off by one (vendor did not add +1 on/after RFSTDTC)")
print("  AE          — one record (COUGH) missing from the delivery")
print("  VS.VSDY     — not delivered at all")
print("  EG.EGORRESU — unit dropped on every PRI record")
print("  DS          — one disposition record missing from the delivery")
print("\nAlso exercised:")
print("  DM.ETHNIC   — no Input Variables; only a name-similar raw column (ETHNICCD)")
print("  DM.DTHFL    — a narrative rule with no similar raw column at all")
