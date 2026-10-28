"""The SAS runtime: operators, missing values, conversions, formats, informats and dates
behave as SAS does, and the function catalog documents exactly what the runtime implements.

Shared with SDTM Studio (sdtm_builder/sas is the same code); these cases are ported from its
suite so the two copies are held to the same answers.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from sdtm_builder.sas import expr, functions, runtime            # noqa: E402
from sdtm_builder.sas.expr import SasExprError, evaluate, parse   # noqa: E402

IX = pd.RangeIndex(1)


def ev(sas, **cols):
    ix = next(iter(cols.values())).index if cols else IX
    out = evaluate(parse(sas), {k.upper(): v for k, v in cols.items()}, ix)
    return [None if pd.isna(x) else x for x in out]


def _cases(cases):
    for sas, expected in cases:
        got = ev(sas)
        assert got == [expected], f"{sas}: {got!r} != {expected!r}"


def test_the_catalog_documents_exactly_the_runtime_functions():
    assert set(functions.BY_NAME) == set(runtime._SX_F)
    assert {f["category"] for f in functions.FUNCTIONS} <= set(functions.CATEGORIES)


def test_every_documented_example_is_true():
    for f in [*functions.FUNCTIONS, *functions.LEGACY.values()]:
        assert ev(f["example"]) == [1.0], f["example"]


def test_sas_operators_and_missing_values():
    _cases([
        ("1 + 2 * 3", 7.0), ("-2 ** 2", -4.0), ("(1 + 2) * 3", 9.0), ("7 / 0", None), (". + 1", None),
        ("sum(., 1) + 1", 2.0), ("2 ** 3 ** 2", 512.0),
        (". < 0", 1.0), (". = .", 1.0), ("'AB  ' = 'AB'", 1.0), ("'ABC' =: 'AB'", 1.0), ("'A' < 'B'", 1.0),
        ("'X' in ('A', 'X')", 1.0), ("3 not in (1, 2)", 1.0), ("1 = 1 and 2 > 3", 0.0), ("0 or .", 0.0),
        ("not 0", 1.0), ("'5' + 1", 6.0), ("'01JAN1960'd", 0.0), ("'01JAN1960:00:01:00'dt", 60.0),
    ])


def test_sas_conversions_follow_sas():
    _cases([
        ("'A' || 1", "A           1"),                       # || converts with BEST12. (right-aligned)
        ("cats('A', 1)", "A1"),                              # … the CAT family strips it
        ("put(3.14159, 8.2)", "    3.14"),
        ("strip(put(1234567, comma12.))", "1,234,567"),
        ("put(., 8.)", "       ."),
        ("put(input('2020-02-03T04:05:06', e8601dt.), e8601dt16.)", "2020-02-03T04:05"),
        ("put(input('03FEB2020', date9.), yymmdd10.)", "2020-02-03"),
        ("put(input('02/03/2020', mmddyy10.), date9.)", "03FEB2020"),
        ("put(input('20200203', yymmdd8.), e8601da.)", "2020-02-03"),
        ("put(input('2020-02-03 junk', anydtdte.), date9.)", "03FEB2020"),
        ("scan('A,,B', 2, ',', 'm')", None),
        ("tranwrd('A-B', '-', '')", "A B"),
        ("transtrn('A-B', '-', trimn(''))", "AB"),
    ])


def test_informats_read_like_sas():
    _cases([
        ("input('12345', 3.)", 123.0), ("input('123', 5.2)", 1.23), ("input('12.5', 5.2)", 12.5),
        ("input('x', ?? best32.)", None), ("input('1,250', comma8.)", 1250.0),
    ])


def test_dates_are_sas_dates():
    assert ev("input('2020-01-01', yymmdd10.)") == [21915.0]
    assert ev("intck('month', '15JAN2020'd, '14FEB2020'd, 'c')") == [0.0]
    assert ev("intck('year', '31DEC2019'd, '01JAN2020'd)") == [1.0]
    assert ev("put(intnx('qtr', '15FEB2020'd, 0, 'e'), date9.)") == ["31MAR2020"]
    assert ev("floor(yrdif('29FEB1980'd, '28FEB2021'd, 'AGE'))") == [41.0]


def test_a_datetime_column_reads_as_a_sas_date_or_datetime():
    d = pd.Series(pd.to_datetime(["2020-01-05", None]))
    dt = pd.Series(pd.to_datetime(["2020-01-05 10:30", None]))
    assert ev("put(x, date9.)", x=d) == ["05JAN2020", ".".rjust(9)]
    assert ev("put(x, e8601dt.)", x=dt) == ["2020-01-05T10:30:00", ".".rjust(19)]


def test_whatever_is_not_sas_is_refused():
    for sas in ("nosuchfn(x)", "put(x, nosuchfmt.)", "input(x, date9)", "substr(x, 1, 2, 3)",
                "x in (y)", "'01XYZ2020'd", "best32.", "today(1)"):
        try:
            parse(sas)
        except SasExprError:
            continue
        raise AssertionError(f"{sas!r} should be refused")


def test_omitted_arguments_are_sas_syntax():
    tree = parse("compress(x, , 'kd')")
    assert expr.to_sas(tree) == "compress(X, , 'kd')"
    assert ev("compress(x, , 'kd')", x=pd.Series(["AB-12"])) == ["12"]


def test_retain_and_lag_work_within_by_groups():
    f = pd.DataFrame({"USUBJID": ["1", "1", "1", "2", "2"], "VISITNUM": ["2", "1", "3", "1", "2"],
                      "X": ["b", "a", "", "c", ""], "N": [20.0, 10.0, 35.0, 5.0, 7.0]})
    by, order = ["USUBJID"], ["VISITNUM"]
    assert runtime._sx_retain(f, f["X"], by, order).fillna("").tolist() == ["b", "a", "b", "c", "c"]
    assert runtime._sx_retain(f, f["X"], by, order, "first").fillna("").tolist() == ["a", "a", "a", "c", "c"]
    assert [None if pd.isna(x) else x for x in runtime._sx_lag(f, f["N"], by, order)] == \
        [10.0, None, 20.0, None, 5.0]
    assert [None if pd.isna(x) else x for x in runtime._sx_lag(f, f["N"], by, order, dif=True)] == \
        [10.0, None, 15.0, None, 2.0]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as exc:                      # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}: {exc}")
    print(f"\n{'all SAS runtime tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
