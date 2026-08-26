"""Synthetic raw data generated from the spec's own Input Variables."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdtm_builder.build import build_study                     # noqa: E402
from sdtm_builder.rawio import RawStore                        # noqa: E402
from sdtm_builder.spec import load_spec                        # noqa: E402
from sdtm_builder.synth import SynthOptions, generate, raw_schema, read_marker  # noqa: E402

HERE = Path(__file__).resolve().parent


def _spec(tmp: Path):
    subprocess.run([sys.executable, str(HERE / "make_fixture.py"), str(tmp)],
                   check=True, capture_output=True)
    return load_spec(tmp / "mapping_spec.xlsx")


def test_schema_comes_from_the_spec():
    with tempfile.TemporaryDirectory() as td:
        spec = _spec(Path(td))
        schema = raw_schema(spec)
        assert {"dm", "ae", "vs"} <= set(schema)
        assert "AETERM" in schema["ae"] and "AESEVCD" in schema["ae"]
        # a column is annotated with every SDTM variable that reads it
        assert "AETERM" in schema["ae"]["AETERM"]["variables"]


def test_generated_data_builds_the_spec():
    """The point of the feature: a spec with no extract yet can still be exercised."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        spec = _spec(tmp)
        out = tmp / "synth"
        res = generate(spec, out, SynthOptions(subjects=12, visits=3, studyid="SYN-1", seed="t"))
        assert res["subjects"] == 12 and res["rows"] > 0

        store = RawStore.discover(out)
        results = build_study(spec, store, studyid="SYN-1")
        assert all(r.ok for r in results.values()), {d: r.error for d, r in results.items() if not r.ok}
        dm, ae = results["DM"].dataset, results["AE"].dataset
        assert len(dm) == 12                                   # one record per subject
        assert dm["USUBJID"].nunique() == 12
        assert set(dm["SEX"]) <= {"M", "F"}                    # the spec's codelist was honoured
        assert len(ae) >= 12                                   # events: at least one per subject
        assert set(ae["USUBJID"]) <= set(dm["USUBJID"])        # subjects line up across datasets
        # dates are ISO and follow enrolment, so --DY derives to something sensible
        assert results["VS"].dataset["VSDTC"].str.match(r"\d{4}-\d{2}-\d{2}").all()


def test_generation_is_reproducible_and_seed_sensitive():
    import hashlib

    def digest(path: Path) -> str:
        blob = b"".join(sorted(p.read_bytes() for p in path.glob("*.csv")))
        return hashlib.sha256(blob).hexdigest()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        spec = _spec(tmp)
        opts = SynthOptions(subjects=8, visits=2, seed="alpha")
        generate(spec, tmp / "a", opts)
        generate(spec, tmp / "b", opts)
        generate(spec, tmp / "c", SynthOptions(subjects=8, visits=2, seed="beta"))
        assert digest(tmp / "a") == digest(tmp / "b")          # same seed, identical bytes
        assert digest(tmp / "a") != digest(tmp / "c")          # a different seed differs


def test_the_folder_is_marked_synthetic():
    """Nothing downstream may mistake invented data for a real extract."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        spec = _spec(tmp)
        out = tmp / "synth"
        generate(spec, out, SynthOptions(subjects=5, visits=2))
        marker = read_marker(out)
        assert marker and marker["synthetic"] is True
        assert "says nothing about the vendor" in marker["warning"]
        assert (out / "READ_ME_SYNTHETIC.txt").exists()
        assert read_marker(tmp / "raw") is None                # the real fixture is not marked


def test_a_spec_with_no_raw_sources_is_refused():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        spec = _spec(tmp)
        for rows in spec.domains.values():
            for r in rows:
                r.input_variables = ""
        try:
            generate(spec, tmp / "none", SynthOptions(subjects=2))
        except ValueError as exc:
            assert "no raw" in str(exc)
        else:
            raise AssertionError("a spec with no raw sources should be refused")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1; print(f"FAIL  {name}: {exc}")
    print(f"\n{'all synth tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
