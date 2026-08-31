"""Generated programs: the standalone Python must REPRODUCE the build, and the SAS
program must be a structured, honest hand-off (real statements, clearly-marked TODOs).

The gold-standard check runs the generated pandas program in a subprocess against the
fixture raw data and compares its output, column by column, with the tool's own build —
skipping only the variables the program itself declares as TODO.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from sdtm_builder import programs                              # noqa: E402
from sdtm_builder.build import build_domain                    # noqa: E402
from sdtm_builder.rawio import RawStore                        # noqa: E402
from sdtm_builder.spec import load_spec                        # noqa: E402

HERE = Path(__file__).resolve().parent


def _fixture(tmp: Path) -> Path:
    subprocess.run([sys.executable, str(HERE / "make_fixture.py"), str(tmp)],
                   check=True, capture_output=True)
    return tmp


def _gen_and_run(tmp: Path, dom: str):
    """Build one domain, generate its Python program, run it, return (tool, program) frames."""
    spec = load_spec(tmp / "mapping_spec.xlsx")
    store = RawStore.discover(tmp / "raw")
    res = build_domain(spec, store, dom, studyid="S1")
    assert res.ok, res.error
    text = programs.python_program(
        domain=dom, blocks=res.blocks, base_dataset=res.base_dataset,
        prep_step=res.prep_step.as_dict() if res.prep_step else None,
        pipeline=[], sort_by=[], dedup={}, codelists=spec.codelists,
        raw_path=str(tmp / "raw"), studyid="S1", version="test")
    todos = set(re.findall(r"# TODO \(hand-code\): (\w+)", text))
    workdir = tmp / f"prog_{dom}"
    workdir.mkdir(exist_ok=True)
    (workdir / f"{dom.lower()}_build.py").write_text(text)
    run = subprocess.run([sys.executable, f"{dom.lower()}_build.py"],
                         cwd=workdir, capture_output=True, text=True)
    assert run.returncode == 0, f"{dom} program failed:\n{run.stderr[-2000:]}\n---\n{text[-1500:]}"
    out = pd.read_csv(workdir / f"{dom}.csv", dtype=str, keep_default_na=False)
    return res, out, todos, text


def _clean(col: pd.Series) -> list[str]:
    return [("" if s in ("nan", "<NA>", "None") else s)
            for s in col.astype("string").fillna("").str.strip().tolist()]


def _compare(res, out: pd.DataFrame, todos: set, dom: str) -> int:
    """Column-by-column equality for everything the program claims to build."""
    tool = res.dataset
    assert len(out) == len(tool), f"{dom}: {len(out)} rows vs the tool's {len(tool)}"
    compared = 0
    for b in res.blocks:
        v = b.variable
        if b.supp or v in todos or b.status not in ("built", "empty"):
            continue
        assert v in out.columns, f"{dom}.{v} missing from the program's output"
        got, want = _clean(out[v]), _clean(tool[v])
        # numeric round-trip through CSV: 1 vs 1.0
        norm = lambda xs: [x[:-2] if x.endswith(".0") else x for x in xs]   # noqa: E731
        assert norm(got) == norm(want), (
            f"{dom}.{v} differs — program {got[:5]}… vs tool {want[:5]}…")
        compared += 1
    return compared


def test_generated_python_reproduces_the_dm_build():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        res, out, todos, _text = _gen_and_run(tmp, "DM")
        n = _compare(res, out, todos, "DM")
        assert n >= 8, f"only {n} DM columns were comparable — the program is mostly TODOs"


def test_generated_python_reproduces_a_stacked_build():
    """DS builds on the automatic stack of three raw forms — the program must stack too."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        res, out, todos, text = _gen_and_run(tmp, "DS")
        assert "pd.concat" in text                              # the stack is real code
        n = _compare(res, out, todos, "DS")
        assert n >= 4, f"only {n} DS columns were comparable"


def test_generated_python_reproduces_a_transposed_build():
    """EG builds on the wide-to-long findings transpose — one record per test."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        res, out, todos, _text = _gen_and_run(tmp, "EG")
        n = _compare(res, out, todos, "EG")
        assert n >= 3, f"only {n} EG columns were comparable"


def test_ct_in_programs_is_scoped_to_the_values_the_data_holds():
    """A 100-term codelist with 2 observed values inlines 2 entries, not 100 — in the
    Python CT dict and the SAS SELECT/WHEN alike. Without observed values, the full
    map is kept (never guess what the data holds)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        spec = load_spec(tmp / "mapping_spec.xlsx")
        store = RawStore.discover(tmp / "raw")
        res = build_domain(spec, store, "DM", studyid="S1")
        kwargs = dict(domain="DM", blocks=res.blocks, base_dataset=res.base_dataset,
                      prep_step=None, pipeline=[], sort_by=[], dedup={},
                      codelists=spec.codelists, raw_path=str(tmp / "raw"), studyid="S1")

        full = programs.python_program(**kwargs)
        scoped = programs.python_program(**kwargs, observed={"SEX": ["MALE", "F"]})
        # the map is inlined PER VARIABLE at the point of use — no global CT table
        assert "\nCT = {" not in full and "\nCT = {" not in scoped
        full_line = next(line for line in full.splitlines() if line.startswith('df["SEX"]'))
        assert '"M": "M"' in full_line and '"MALE": "M"' in full_line   # unprofiled: full map
        sex_line = next(line for line in scoped.splitlines() if line.startswith('df["SEX"]'))
        assert '"MALE": "M"' in sex_line and '"F": "F"' in sex_line
        assert '"M": "M"' not in sex_line and '"FEMALE"' not in sex_line
        assert "2 of 4 term(s)" in sex_line and "observed" in sex_line
        # a codelist on a CONSTANT never drags its map in — the build doesn't apply CT there
        dom_line = next(line for line in scoped.splitlines() if line.startswith('df["DOMAIN"]'))
        assert "apply_ct" not in dom_line

        sas_scoped = programs.sas_program(**kwargs, observed={"SEX": ["MALE", "F"]})
        assert re.search(r"when \('MALE'\) SEX = 'M';", sas_scoped)
        assert "'FEMALE'" not in sas_scoped
        assert "only the values observed in the data" in sas_scoped


def test_sas_cross_dataset_inputs_are_premerged_not_todo():
    """With the datasets' column lists available, SAS gets the same deterministic
    alignment the build performs: a column from another dataset is pre-merged onto the
    base by the shared subject key, and date_extreme becomes PROC SQL — not a TODO."""
    from sdtm_builder.blocks import Block
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        spec = load_spec(tmp / "mapping_spec.xlsx")
        store = RawStore.discover(tmp / "raw")
        res = build_domain(spec, store, "DM", studyid="S1")
        source_columns = {name: list(store.columns(name)) for name in store.refs}
        extra = [
            Block(variable="ZZDAT", domain="DM", mtype="assign",
                  dataset="consent", column="DSSTDAT", label="cross-dataset check"),
            Block(variable="ZZEND", domain="DM", mtype="derived", recipe="date_extreme",
                  args={"func": "max", "sources": [
                      {"dataset": "consent", "date_col": "DSSTDAT"},
                      {"dataset": "studcomp", "date_col": "DSSTDAT"}]}),
        ]
        for b in extra:
            b.status = "built"
        blocks = list(res.blocks) + extra
        text = programs.sas_program(
            domain="DM", blocks=blocks, base_dataset=res.base_dataset,
            prep_step=None, pipeline=[], sort_by=[], dedup={},
            codelists=spec.codelists, raw_path=str(tmp / "raw"), studyid="S1",
            source_columns=source_columns)
        # the cross-dataset column arrives via a keyed pre-merge, then a plain statement
        assert "merged onto the base" in text
        assert re.search(r"proc sort data=consent\(keep=\w+ DSSTDAT rename=\(DSSTDAT=__\w+\)",
                         text), text
        assert re.search(r"ZZDAT = strip\(__\w+\);", text)
        # date_extreme is real PROC SQL per source + a min/max over the merged columns
        assert "proc sql;" in text and "group by" in text
        assert re.search(r"__DN_ZZEND_\d", text)
        assert "max(of __DN_ZZEND_:" in text
        # neither is a TODO (the fixture DM now translates completely)
        assert "TODO (hand-code): ZZDAT" not in text
        assert "TODO (hand-code): ZZEND" not in text


def test_sas_program_is_structured_and_honest():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        spec = load_spec(tmp / "mapping_spec.xlsx")
        store = RawStore.discover(tmp / "raw")
        res = build_domain(spec, store, "DM", studyid="S1")
        text = programs.sas_program(
            domain="DM", blocks=res.blocks, base_dataset=res.base_dataset,
            prep_step=res.prep_step.as_dict() if res.prep_step else None,
            pipeline=[], sort_by=[], dedup={}, codelists=spec.codelists,
            raw_path=str(tmp / "raw"), studyid="S1", version="test")
        assert "%macro fetch" in text and "%fetch(dm);" in text
        assert "data dm_work;" in text and "data DM;" in text
        # controlled terminology is a real SELECT/WHEN block, from the spec's codelist
        assert "select (upcase(strip(SEX)));" in text
        assert re.search(r"when \('M', 'MALE'\) SEX = 'M';", text)
        # the fixture DM translates COMPLETELY — parity with the Python program
        assert "TODO (hand-code)" not in text
        # …and what genuinely cannot be translated is an explicit TODO, never a silent gap
        from sdtm_builder.blocks import Block
        odd = Block(variable="ZZODD", domain="DM", mtype="derived", recipe="lobxfl",
                    args={}, mapping_rule="a rule only the tool runs")
        odd.status = "built"
        text_odd = programs.sas_program(
            domain="DM", blocks=list(res.blocks) + [odd], base_dataset=res.base_dataset,
            prep_step=None, pipeline=[], sort_by=[], dedup={},
            codelists=spec.codelists, raw_path=str(tmp / "raw"), studyid="S1")
        assert "TODO (hand-code): ZZODD" in text_odd
        assert "a rule only the tool runs" in text_odd
        # --SEQ numbered on the final sorted records (DS repeats; DM has no --SEQ)
        res_ds = build_domain(spec, store, "DS", studyid="S1")
        text_ds = programs.sas_program(
            domain="DS", blocks=res_ds.blocks, base_dataset=res_ds.base_dataset,
            prep_step=res_ds.prep_step.as_dict() if res_ds.prep_step else None,
            pipeline=[], sort_by=[], dedup={}, codelists=spec.codelists,
            raw_path=str(tmp / "raw"), studyid="S1", version="test")
        assert "retain DSSEQ" in text_ds and "DSSEQ + 1;" in text_ds
        # the stacked base is real SAS, not a TODO
        assert "indsname=__src" in text_ds


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
    print(f"\n{'all tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
