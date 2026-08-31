"""Every preparation operation, exercised against the fixture study.

These are the operations SDTM Designer's Domain Studio offers. They run here as named
functions, so a prepared dataset is as reproducible and as reviewable as a mapped one.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from sdtm_builder import prep                                  # noqa: E402
from sdtm_builder.rawio import RawStore                        # noqa: E402

HERE = Path(__file__).resolve().parent


def _store(tmp: Path) -> RawStore:
    subprocess.run([sys.executable, str(HERE / "make_fixture.py"), str(tmp)],
                   check=True, capture_output=True)
    return RawStore.discover(tmp / "raw")


def run(steps, store):
    return prep.run_pipeline(steps, store)[0]


def test_merge_collision_keeps_one_column_and_the_later_dataset_wins():
    """Two datasets both carrying a column must NOT produce a silent _r twin — the reader
    maps the original name, finds the twin instead, and gets a column of nothing. As in a
    SAS MERGE, one name survives and the later dataset's value wins where it has one."""
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        # both sides carry SEXCD: the right side gets a planted copy with new values
        ae = store.get(store.resolve("ae")).copy()
        ae["SEXCD"] = "X"
        store.put("ae2", ae)
        out = run([{"op": "merge", "name": "both", "params": {
            "how": "left", "on": ["USUBJID"],
            "inputs": [{"dataset": "dm"}, {"dataset": "ae2"}]}}], store)["both"]
        assert "SEXCD_r" not in out.columns                     # no twin
        assert "SEXCD" in out.columns
        matched = out["USUBJID"].isin(set(ae["USUBJID"]))
        assert (out.loc[matched, "SEXCD"] == "X").all()         # the later dataset won
        if (~matched).any():                                    # left value kept where no match
            assert (out.loc[~matched, "SEXCD"] != "X").all()


def test_stack_merge_and_column_ops():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        out = run([
            {"op": "stack", "name": "all_ds",
             "params": {"datasets": ["consent", "enroll", "studcomp"]}},
            {"op": "select", "name": "picked",
             "params": {"dataset": "all_ds", "columns": ["USUBJID", "DSTERM", "DSSTDAT"]}},
            {"op": "rename", "name": "named",
             "params": {"dataset": "picked", "renames": [{"from": "DSSTDAT", "to": "DSSTDTC"}]}},
            {"op": "drop", "name": "trimmed", "params": {"dataset": "named", "columns": ["DSTERM"]}},
        ], store)
        assert len(out["all_ds"]) == 18
        assert "__SOURCE_DATASET" in out["all_ds"].columns      # provenance of each record
        assert set(out["all_ds"]["__SOURCE_DATASET"]) == {"consent", "enroll", "studcomp"}
        assert list(out["picked"].columns) == ["USUBJID", "DSTERM", "DSSTDAT"]
        assert "DSSTDTC" in out["named"].columns
        assert "DSTERM" not in out["trimmed"].columns

        merged = run([
            {"op": "merge", "name": "ae_dm", "params": {
                "how": "left", "on": ["USUBJID"],
                "inputs": [{"dataset": "ae"}, {"dataset": "dm", "columns": ["SEXCD", "ARMCD"]}]}},
        ], store)["ae_dm"]
        assert len(merged) == 8                                # left join keeps every AE record
        assert {"SEXCD", "ARMCD"}.issubset(set(merged.columns))
        assert merged["ARMCD"].notna().all()


def test_filter_derive_and_conditions():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        out = run([
            {"op": "stack", "name": "all_ds",
             "params": {"datasets": ["consent", "enroll", "studcomp"]}},
            {"op": "derive", "name": "typed", "params": {
                "dataset": "all_ds", "target": "DSTYPE", "else_value": "OTHER",
                "rules": [
                    {"conds": [{"column": "DSCAT", "operator": "==",
                                "value": "PROTOCOL MILESTONE"}], "value": "MILESTONE"},
                    {"conds": [{"column": "DSDECOD", "operator": "contains",
                                "value": "ADVERSE"}], "value": "WITHDRAWAL"},
                ]}},
            {"op": "filter", "name": "kept", "params": {
                "dataset": "typed",
                "conds": [{"column": "DSTYPE", "operator": "in", "value": "MILESTONE,WITHDRAWAL"}]}},
        ], store)
        counts = out["typed"]["DSTYPE"].value_counts().to_dict()
        assert counts["MILESTONE"] == 12                       # consent + enrolment
        assert counts["WITHDRAWAL"] == 3                       # the odd-numbered subjects
        assert counts["OTHER"] == 3                            # completed study
        assert len(out["kept"]) == 15

        # every condition operator resolves
        ae = store.get("ae")
        for op, val, expect_any in (("==", "Rash", True), ("!=", "Rash", True),
                                    ("contains", "ash", True), ("startswith", "R", True),
                                    ("endswith", "sh", True), ("in", "Rash,cough", True),
                                    ("notin", "Rash", True), ("missing", "", False),
                                    ("notmissing", "", True)):
            mask = prep.cond_mask(ae, [{"column": "AETERM", "operator": op, "value": val}])
            assert bool(mask.any()) is expect_any, f"{op} {val!r}"


def test_sort_dedup_aggregate_and_date_extreme():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        out = run([
            {"op": "sort", "name": "sorted_vs", "params": {
                "dataset": "vs", "columns": ["USUBJID", "VISITNUM"], "directions": ["asc", "desc"]}},
            {"op": "dedup", "name": "first_visit", "params": {
                "dataset": "sorted_vs", "keys": ["USUBJID", "VSTESTCD"], "keep": "first"}},
            {"op": "aggregate", "name": "last_date", "params": {
                "dataset": "vs", "group_by": ["USUBJID"], "column": "VSDAT",
                "func": "max", "out_col": "LASTVISIT"}},
            {"op": "date_extreme", "name": "first_contact", "params": {
                "group_by": ["USUBJID"], "func": "min", "out_col": "RFICDTC",
                "sources": [{"dataset": "ae", "date_col": "AESTDAT"},
                            {"dataset": "vs", "date_col": "VSDAT"}]}},
        ], store)
        # descending visit + keep first == the LAST visit per test
        assert set(out["first_visit"]["VISIT"]) == {"CYCLE 1 DAY 1"}
        assert len(out["first_visit"]) == 18
        assert len(out["last_date"]) == 6 and "LASTVISIT" in out["last_date"].columns
        assert len(out["first_contact"]) == 6
        # the earliest date across BOTH datasets, per subject
        row = out["first_contact"].set_index("USUBJID").loc["GS-TEST-001-101-001", "RFICDTC"]
        assert row == "2024-01-15"


def test_split_routes_records_first_match_wins():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        out = run([
            {"op": "stack", "name": "all_ds",
             "params": {"datasets": ["consent", "enroll", "studcomp"]}},
            {"op": "split", "name": "milestones", "params": {
                "dataset": "all_ds", "other_name": "leftover",
                "branches": [
                    {"name": "milestones",
                     "conds": [{"column": "DSCAT", "operator": "==", "value": "PROTOCOL MILESTONE"}]},
                    {"name": "withdrawals",
                     "conds": [{"column": "DSDECOD", "operator": "contains", "value": "ADVERSE"}]},
                ]}},
        ], store)
        assert len(out["milestones"]) == 12
        assert len(out["withdrawals"]) == 3
        assert len(out["leftover"]) == 3
        assert len(out["milestones"]) + len(out["withdrawals"]) + len(out["leftover"]) == 18


def test_transpose_long():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        out = run([{"op": "transpose_long", "name": "eg_long", "params": {
            "dataset": "egperf", "id_vars": ["USUBJID", "VISIT", "RECORDDATE"],
            "value_vars": ["EGVR", "EGPRI", "EGQRSI"],
            "var_name": "EGTESTCD", "value_name": "EGORRES"}}], store)["eg_long"]
        assert len(out) == 36                                  # 12 records x 3 measurements
        assert set(out["EGTESTCD"]) == {"EGVR", "EGPRI", "EGQRSI"}
        assert "VISIT" in out.columns


def test_a_broken_step_names_the_problem():
    with tempfile.TemporaryDirectory() as td:
        store = _store(Path(td))
        for steps, expect in (
            ([{"op": "filter", "name": "x",
               "params": {"dataset": "ae", "conds": [{"column": "NOPE", "operator": "==",
                                                      "value": "1"}]}}], "NOPE"),
            ([{"op": "merge", "name": "x", "params": {"inputs": [{"dataset": "ae"}]}}],
             "at least two"),
            ([{"op": "wobble", "name": "x", "params": {}}], "unknown preparation step"),
            ([{"op": "select", "name": "x", "params": {"dataset": "nosuchdata",
                                                       "columns": ["A"]}}], "neither"),
        ):
            try:
                run(steps, store)
            except prep.PrepError as exc:
                assert expect in str(exc), f"{steps[0]['op']}: {exc}"
            else:
                raise AssertionError(f"{steps[0]['op']} should have failed")


def test_pipeline_output_feeds_a_domain_build():
    """The real point: a prepared dataset becomes the domain's record source."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store = _store(tmp)
        from sdtm_builder.build import build_domain
        from sdtm_builder.spec import load_spec
        spec = load_spec(tmp / "mapping_spec.xlsx")

        res = build_domain(spec, store, "DS", prep_mode="custom", prep_steps=[
            {"op": "stack", "name": "ds_all",
             "params": {"datasets": ["consent", "enroll", "studcomp"]}},
            {"op": "filter", "name": "ds",
             "params": {"dataset": "ds_all",
                        "conds": [{"column": "DSCAT", "operator": "==",
                                   "value": "PROTOCOL MILESTONE"}]}},
        ])
        assert res.ok, res.error
        assert res.base_dataset == "ds"
        assert len(res.dataset) == 12                          # the filter carried through
        assert set(res.dataset["DSCAT"]) == {"PROTOCOL MILESTONE"}
        assert [r["op"] for r in res.prep_reports] == ["stack", "filter"]


def _tiny_store(tmp: Path) -> RawStore:
    raw = tmp / "raw"
    raw.mkdir()
    pd.DataFrame({
        "SUBJID": ["001", "002", "003", "004"],
        "BRTHDTC": ["1962", "1962-05", "1962-05-14", "unknown"],
        "CITY": ["OSLO", "", "PARIS", "ROME"],
        "COUNTRY": ["NOR", "SWE", "FRA", "ITA"],
    }).to_csv(raw / "dm_raw.csv", index=False)
    return RawStore.discover(raw)


def test_compute_completes_partial_dates_and_joins_text():
    """The compute step makes temp columns a SAS programmer would make in a data step:
    a year-only BRTHDTC becomes year-01-01 (never a guess for junk), and concat with a
    separator behaves like CATX — blank parts are skipped, not doubled."""
    with tempfile.TemporaryDirectory() as td:
        store = _tiny_store(Path(td))
        out = run([
            {"op": "compute", "name": "with_dates", "params": {
                "dataset": "dm_raw", "func": "complete_date",
                "columns": ["BRTHDTC"], "out_col": "BRTHDTC_FULL"}},
            {"op": "compute", "name": "with_place", "params": {
                "dataset": "with_dates", "func": "concat",
                "columns": ["CITY", "COUNTRY"], "out_col": "PLACE", "sep": "-"}},
        ], store)["with_place"]
        assert out["BRTHDTC_FULL"].tolist() == ["1962-01-01", "1962-05-01", "1962-05-14", ""]
        assert out["PLACE"].tolist() == ["OSLO-NOR", "SWE", "PARIS-FRA", "ROME-ITA"]
        # the source column is untouched — the completed date lives beside it
        assert out["BRTHDTC"].tolist() == ["1962", "1962-05", "1962-05-14", "unknown"]


def test_compute_refuses_a_missing_column():
    with tempfile.TemporaryDirectory() as td:
        store = _tiny_store(Path(td))
        try:
            run([{"op": "compute", "name": "x", "params": {
                "dataset": "dm_raw", "func": "year", "columns": ["NOPE"]}}], store)
        except prep.PrepError as exc:
            assert "column" in str(exc)
        else:
            raise AssertionError("a missing source column must refuse, not guess")


def test_compute_speaks_sas():
    """Compute runs the SAME function engine as the variable recipes — SUBSTR, SCAN,
    CATX and friends with their SAS names and 1-based positions — and the condition set
    includes BETWEEN, so a SAS programmer finds what they expect."""
    with tempfile.TemporaryDirectory() as td:
        store = _tiny_store(Path(td))
        out = run([
            {"op": "compute", "name": "a", "params": {
                "dataset": "dm_raw", "func": "substr",
                "columns": ["CITY"], "start": "1", "len": "3", "out_col": "CITY3"}},
            {"op": "compute", "name": "b", "params": {
                "dataset": "a", "func": "scan",
                "columns": ["BRTHDTC"], "word": "1", "delim": "-", "out_col": "BYEAR"}},
        ], store)["b"]
        assert out["CITY3"].tolist() == ["OSL", "", "PAR", "ROM"]
        assert out["BYEAR"].tolist() == ["1962", "1962", "1962", "unknown"]


def test_between_condition():
    df = pd.DataFrame({"AGE": ["17", "45", "80", ""]})
    m = prep.cond_mask(df, [{"column": "AGE", "operator": "between", "value": "18, 65"}])
    assert m.tolist() == [False, True, False, False]


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
    print(f"\n{'all prep tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
