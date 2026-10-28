"""SAS code as a mapping, on the SAS runtime: the spec's Implemented SAS Code with operators,
formats, dates and IF/ELSE chains builds; a typed expression builds; RETAIN and LAG carry values
within BY groups; and the generated Python and SAS programs write the same derivations.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from sdtm_builder import programs, sas_recipe                     # noqa: E402
from sdtm_builder.build import build_domain                      # noqa: E402
from sdtm_builder.rawio import RawStore                          # noqa: E402
from sdtm_builder.spec import load_spec                          # noqa: E402

COLS = ["Variable", "Label", "Mapping Action", "Input Variables", "Mapping Rule",
        "Implemented SAS Code", "Codelist", "Role", "Origin", "Dataset", "Type", "Length", "Order"]


def row(var, iv="", sas="", typ="text"):
    return dict(zip(COLS, [var, var, "ASSIGN", iv, "", sas, "", "", "CRF", "XX", typ, 200, 0]))


def _study(tmp: Path):
    raw = tmp / "raw"
    raw.mkdir()
    pd.DataFrame({
        "STUDYID": ["S1"] * 5, "USUBJID": ["S1-1", "S1-1", "S1-1", "S1-2", "S1-2"],
        "VISITNUM": ["2", "1", "3", "1", "2"],
        "BRTHDAT": ["14MAY1960", "14MAY1960", "14MAY1960", "01JAN1990", "01JAN1990"],
        "VISDAT": ["2024-02-05", "2024-01-08", "2024-03-04", "2024-01-15", ""],
        "WEIGHT": ["70", "71", "", "60", "61.5"],
        "SEVCD": ["1", "2", "3", "", "2"],
    }).to_csv(raw / "xx.csv", index=False)
    spec = [
        row("STUDYID", "raw.xx.STUDYID"), row("USUBJID", "raw.xx.USUBJID"),
        row("VISITNUM", "raw.xx.VISITNUM"),
        row("XXDTC", "raw.xx.VISDAT", sas="put(input(visdat, yymmdd10.), e8601da10.)"),
        row("AGE", "raw.xx.BRTHDAT, raw.xx.VISDAT", typ="integer",
            sas="floor(yrdif(input(brthdat, date9.), input(visdat, yymmdd10.), 'AGE'))"),
        row("AGEGR", "raw.xx.BRTHDAT",
            sas="if age = . then _agegr = ''; else if age < 40 then _agegr = '<40'; "
                "else if age ge 40 and age < 65 then _agegr = '40-64'; else _agegr = '>=65';"),
        row("XXSEV", "raw.xx.SEVCD",
            sas="ifc(sevcd in ('1', '2'), choosec(input(sevcd, best.), 'MILD', 'MODERATE'), "
                "ifc(missing(sevcd), '', 'SEVERE'))"),
        row("XXWT", "raw.xx.WEIGHT"), row("XXLAST"), row("XXDIF"),
        row("XXODD", "raw.xx.SEVCD", sas="call symput('x', sevcd)"),
    ]
    with pd.ExcelWriter(tmp / "spec.xlsx") as xw:
        pd.DataFrame([{"Dataset": "XX", "Label": "Test"}]).to_excel(xw, sheet_name="TOC", index=False)
        pd.DataFrame(spec).to_excel(xw, sheet_name="XX", index=False)
    return load_spec(tmp / "spec.xlsx"), RawStore.discover(raw)


EDITS = {
    "XXLAST": {"mtype": "derived", "recipe": "retain",
               "args": {"source": {"var": "XXWT"}, "by": ["USUBJID"], "order": ["VISITNUM"]}},
    "XXDIF": {"mtype": "derived", "recipe": "lag",
              "args": {"source": {"var": "XXWT"}, "by": ["USUBJID"], "order": ["VISITNUM"], "dif": True}},
}


def _vals(ser):
    return [None if pd.isna(x) or x == "" else x for x in ser.tolist()]


def test_spec_sas_builds_on_the_runtime():
    with tempfile.TemporaryDirectory() as td:
        spec, store = _study(Path(td))
        res = build_domain(spec, store, "XX", studyid="S1", edits=EDITS)
        assert res.ok, res.error
        ds = res.dataset
        by = {b.variable: b for b in res.blocks}
        assert by["AGE"].recipe == "sas" and by["AGEGR"].recipe == "sas"
        # SAS prints a missing date as '.', and so does this build — as the vendor's SAS would
        assert _vals(ds["XXDTC"]) == ["2024-02-05", "2024-01-08", "2024-03-04", "2024-01-15", "."]
        assert [None if pd.isna(x) else int(x) for x in ds["AGE"]] == [63, 63, 63, 34, None]
        assert _vals(ds["AGEGR"]) == ["40-64", "40-64", "40-64", "<40", None]
        assert _vals(ds["XXSEV"]) == ["MILD", "MODERATE", "SEVERE", None, "MODERATE"]
        # what the grammar does not understand never runs as SAS (the raw input stands)
        assert by["XXODD"].recipe != "sas"


def test_retain_and_lag_within_subject_by_visit():
    with tempfile.TemporaryDirectory() as td:
        spec, store = _study(Path(td))
        ds = build_domain(spec, store, "XX", studyid="S1", edits=EDITS).dataset
        # S1-1 by visit: v1 71, v2 70, v3 missing  →  last carried: 71, 70, 70
        assert _vals(ds["XXLAST"]) == ["70", "71", "70", "60", "61.5"]
        assert [None if pd.isna(x) else round(float(x), 1) for x in ds["XXDIF"]] == [-1.0, None, None, None, 1.5]


def test_the_programs_write_the_same_derivations():
    from test_programs import _compare
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        spec, store = _study(tmp)
        res = build_domain(spec, store, "XX", studyid="S1", edits=EDITS)
        kw = dict(domain="XX", blocks=res.blocks, base_dataset=res.base_dataset, prep_step=None,
                  pipeline=[], sort_by=[], dedup={}, codelists=spec.codelists,
                  raw_path=str(tmp / "raw"), studyid="S1", version="t")
        text = programs.python_program(**kw)
        (tmp / "xx_build.py").write_text(text)
        run = subprocess.run([sys.executable, "xx_build.py"], cwd=tmp, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr[-2000:]
        out = pd.read_csv(tmp / "XX.csv", dtype=str, keep_default_na=False)
        todos = set(re.findall(r"# TODO \(hand-code\): (\w+)", text))
        assert not ({"AGE", "AGEGR", "XXSEV", "XXLAST", "XXDIF"} & todos), todos
        assert _compare(res, out, todos, "XX") >= 9
        sas = programs.sas_program(**kw)
        assert "AGE = floor(yrdif(input(BRTHDAT, date9.), input(VISDAT, yymmdd10.), 'AGE'));" in sas
        assert "else if (AGE >= 40) and (AGE < 65) then AGEGR = '40-64';" in sas
        assert "retain _rt;" in sas and "_lg = lag1(XXWT);" in sas and "XXDIF = XXWT - _lg;" in sas


def test_check_reports_what_a_reader_typed():
    assert sas_recipe.check("upcase(strip(aeterm))", "AETERM") == {
        "ok": True, "names": ["AETERM"], "sas": ["AETERM = upcase(strip(AETERM));"]}
    bad = sas_recipe.check("upcase(", "AETERM")
    assert not bad["ok"] and bad["error"]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as exc:                      # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'all SAS recipe tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
