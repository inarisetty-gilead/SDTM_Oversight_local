"""The findings transpose a mapping spec writes in its --TESTCD Mapping Rule.

A Gilead VS spec says which columns are tests in prose — `if height_raw ne ' ' then
_vstestcd = "HEIGHT"` — names the tests in the VSTEST rule and marks a visit not done with
`if vsperf_std = 'N' then _vstestcd = "VSALL"`. The build must read that rule: test codes and
names from the spec, the date from the form that holds it (by subject AND visit), and ONE
VSALL record for a visit not done, instead of a silent gap.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from sdtm_builder.build import build_domain                     # noqa: E402
from sdtm_builder.rawio import RawStore                         # noqa: E402
from sdtm_builder.spec import load_spec                         # noqa: E402

COLS = ["Variable", "Label", "Mapping Action", "Input Variables", "Mapping Rule",
        "Implemented SAS Code", "Codelist", "Role", "Origin", "Dataset", "Type", "Length", "Order"]

TESTCD_RULE = ("Transpose the raw dataset vsperf. if height_raw ne ' ' then _vstestcd = \"HEIGHT\"; "
               "if weight_raw ne ' ' then _vstestcd = \"WEIGHT\"; "
               "if sysbp_raw ne ' ' then _vstestcd = \"SYSBP\"; "
               "if tempx_raw ne ' ' then _vstestcd = \"TEMP\"; "
               "if vsperf_std = 'N' then _vstestcd = \"VSALL\";")
TEST_RULE = ('if vstestcd is "HEIGHT" then _vstest = "Height"; if vstestcd is "WEIGHT" then '
             '_vstest = "Weight"; if vstestcd is "SYSBP" then _vstest = "Systolic Blood Pressure"; '
             'if vstestcd is "VSALL" then _vstest = "Vital Signs";')


def row(var, iv="", rule="", action="ASSIGN"):
    return dict(zip(COLS, [var, var, action, iv, rule, "", "", "", "CRF", "VS", "text", 200, 0]))


def _study(tmp: Path) -> tuple:
    raw = tmp / "raw"
    raw.mkdir()
    # visit 2 for subject 1 was not done: no results, a reason instead
    pd.DataFrame({
        "STUDYID": ["S1"] * 4, "X_SUBJID": ["S1-001", "S1-001", "S1-002", "S1-002"],
        "FOLDERNAME": ["Screening", "Week 4", "Screening", "Week 4"],
        "VSPERF_STD": ["Y", "N", "Y", "Y"],
        "VSREASND": ["", "subject refused", "", ""],
        "HEIGHT_RAW": ["170", "", "158", ""], "HEIGHT_UN": ["cm", "", "cm", ""],
        "WEIGHT_RAW": ["70", "", "55.5", "56"], "WEIGHT_UN": ["kg", "", "kg", "kg"],
        "SYSBP_RAW": ["120", "", "", "118"], "SYSBPU": ["mmHg", "", "", "mmHg"],
        "VSTIM": ["09:30", "", "10:00", ""],
    }).to_csv(raw / "vsperf.csv", index=False)
    pd.DataFrame({
        "X_SUBJID": ["S1-001", "S1-001", "S1-002", "S1-002"],
        "FOLDERNAME": ["Screening", "Week 4", "Screening", "Week 4"],
        "VISITDAT": ["05JAN2024", "02FEB2024", "2024-01-07", "2024-02"],
    }).to_csv(raw / "visdt.csv", index=False)
    spec = [
        row("STUDYID", "raw.vsperf.STUDYID"),
        row("DOMAIN", rule='"VS"'),
        row("USUBJID", "raw.vsperf.X_SUBJID"),
        row("VSTESTCD", "raw.vsperf.HEIGHT_RAW, raw.vsperf.WEIGHT_RAW, raw.vsperf.VSPERF_STD", TESTCD_RULE),
        row("VSTEST", "raw.vsperf.HEIGHT_RAW", TEST_RULE),
        row("VSORRES", "raw.vsperf.HEIGHT_RAW, raw.vsperf.WEIGHT_RAW",
            'if vstestcd is "HEIGHT" then _vsorres = strip(height_raw);'),
        row("VSORRESU", "raw.vsperf.HEIGHT_UN, raw.vsperf.WEIGHT_UN, raw.vsperf.SYSBPU"),
        row("VSSTAT", "raw.vsperf.VSPERF_STD", "if vsperf_std = 'N' then _vsstat = 'NOT DONE';"),
        row("VSREASND", "raw.vsperf.VSREASND"),
        row("VISIT", "raw.vsperf.FOLDERNAME"),
        row("VSDTC", "raw.visdt.VISITDAT, raw.vsperf.VSTIM"),
    ]
    with pd.ExcelWriter(tmp / "spec.xlsx") as xw:
        pd.DataFrame([{"Dataset": "VS", "Label": "Vital Signs"}]).to_excel(xw, sheet_name="TOC", index=False)
        pd.DataFrame(spec).to_excel(xw, sheet_name="VS", index=False)
    return load_spec(tmp / "spec.xlsx"), RawStore.discover(raw)


def test_the_rule_transpose_reads_the_spec():
    with tempfile.TemporaryDirectory() as td:
        spec, store = _study(Path(td))
        res = build_domain(spec, store, "VS", studyid="S1")
        assert res.ok, res.error
        assert res.prep_step is not None and res.prep_step.op == "transpose_rule", res.prep_step
        ds = res.dataset
        recs = list(zip(ds["USUBJID"], ds["VISIT"], ds["VSTESTCD"]))
        assert recs == [
            ("S1-001", "Screening", "HEIGHT"), ("S1-001", "Screening", "WEIGHT"),
            ("S1-001", "Screening", "SYSBP"),
            ("S1-001", "Week 4", "VSALL"),                          # not done: ONE record
            ("S1-002", "Screening", "HEIGHT"), ("S1-002", "Screening", "WEIGHT"),
            ("S1-002", "Week 4", "WEIGHT"), ("S1-002", "Week 4", "SYSBP"),
        ], recs
        assert "TEMP" not in set(ds["VSTESTCD"])                    # not collected: left out
        by = ds.set_index(["USUBJID", "VISIT", "VSTESTCD"])
        assert by.loc[("S1-001", "Screening", "SYSBP"), "VSTEST"] == "Systolic Blood Pressure"
        assert by.loc[("S1-001", "Screening", "WEIGHT"), "VSORRESU"] == "kg"
        nd = by.loc[("S1-001", "Week 4", "VSALL")]
        assert nd["VSTEST"] == "Vital Signs" and nd["VSSTAT"] == "NOT DONE"
        assert nd["VSREASND"] == "SUBJECT REFUSED" and nd["VSORRES"] in ("", None) or pd.isna(nd["VSORRES"])
        # the date from the other form, by subject + visit, with the time on a full date
        assert by.loc[("S1-001", "Screening", "HEIGHT"), "VSDTC"] == "2024-01-05T09:30"
        assert nd["VSDTC"] == "2024-02-02"
        assert by.loc[("S1-002", "Week 4", "SYSBP"), "VSDTC"] == "2024-02"   # partial kept
        assert set(ds.loc[ds["VSSTAT"].fillna("") == "", "VSTESTCD"]) <= {"HEIGHT", "WEIGHT", "SYSBP"}


def test_a_rule_without_two_collected_tests_is_not_a_transpose():
    with tempfile.TemporaryDirectory() as td:
        spec, store = _study(Path(td))
        from sdtm_builder import spec_transpose
        from sdtm_builder.translate import translate_domain
        blocks = translate_domain(spec.rows("VS"), studyid="S1")
        for b in blocks:
            if b.variable == "VSTESTCD":
                b.mapping_rule = "Transpose. if height_raw ne ' ' then _vstestcd = \"HEIGHT\";"
        assert spec_transpose.detect(blocks, store, "VS") is None


def test_the_generated_programs_reproduce_the_rule_transpose():
    """The standalone Python program gives the tool's records; the SAS writes the not-done
    record and the visit-date merge instead of a TODO."""
    import re
    import subprocess
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_programs import _compare
    from sdtm_builder import programs
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        spec, store = _study(tmp)
        res = build_domain(spec, store, "VS", studyid="S1")
        assert res.ok, res.error
        kw = dict(domain="VS", blocks=res.blocks, base_dataset=res.base_dataset,
                  prep_step=res.prep_step.as_dict(), pipeline=[], sort_by=[], dedup={},
                  codelists=spec.codelists, raw_path=str(tmp / "raw"), studyid="S1", version="t")
        text = programs.python_program(**kw)
        (tmp / "vs_build.py").write_text(text)
        run = subprocess.run([sys.executable, "vs_build.py"], cwd=tmp, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr[-2000:]
        out = pd.read_csv(tmp / "VS.csv", dtype=str, keep_default_na=False)
        todos = set(re.findall(r"# TODO \(hand-code\): (\w+)", text))
        assert _compare(res, out, todos, "VS") >= 8
        sas = programs.sas_program(**kw)
        assert "TODO (hand-code): prep step 'transpose_rule'" not in sas
        assert 'VSTESTCD = "VSALL";' in sas and 'VSSTAT = "NOT DONE";' in sas
        assert "merge" in sas and "by X_SUBJID FOLDERNAME;" in sas


def test_the_detected_transpose_applied_as_a_pipeline_builds_the_same():
    """Taking the automatic step into the Prepare-the-data editor and applying it unchanged
    must not change the domain: the transposed variables still read the transposed records."""
    with tempfile.TemporaryDirectory() as td:
        spec, store = _study(Path(td))
        auto = build_domain(spec, store, "VS", studyid="S1")
        step = auto.prep_step.as_dict()
        mine = build_domain(spec, store, "VS", studyid="S1", prep_mode="custom",
                            prep_steps=[{"op": step["op"], "name": step["name"], "params": step["params"]}])
        assert mine.ok, mine.error
        a, m = auto.dataset.fillna(""), mine.dataset.fillna("")
        assert list(a.columns) == list(m.columns)
        assert a.astype(str).equals(m.astype(str)), (a, m)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
    print(f"\n{'all spec-transpose tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
