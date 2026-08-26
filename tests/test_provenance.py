"""Reproducibility: a run must be provable years later, or the finding is not defensible."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdtm_builder import __version__, provenance as pv     # noqa: E402
from sdtm_builder.build import build_study                 # noqa: E402
from sdtm_builder.rawio import RawStore                    # noqa: E402
from sdtm_builder.spec import load_spec                    # noqa: E402
from sdtm_builder.writers import write_dataset             # noqa: E402

HERE = Path(__file__).resolve().parent


def _fixture(tmp: Path) -> Path:
    subprocess.run([sys.executable, str(HERE / "make_fixture.py"), str(tmp)],
                   check=True, capture_output=True)
    return tmp


def _build(tmp: Path, out: Path, fmt: str = "xpt"):
    spec = load_spec(tmp / "mapping_spec.xlsx")
    store = RawStore.discover(tmp / "raw")
    results = build_study(spec, store)
    for dom, res in results.items():
        if res.ok:
            write_dataset(res.dataset, out / "datasets", dom, fmt,
                          {b.variable: b.label for b in res.blocks})
    return results


def test_a_run_records_its_inputs_by_content():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        out = tmp / "run"
        _build(tmp, out)
        rec = pv.record_run(out, __version__, str(tmp / "mapping_spec.xlsx"),
                            str(tmp / "raw"), {"format": "xpt"})
        pv.record_outputs(out)
        saved = json.loads((out / "provenance.json").read_text())

        assert saved["tool_version"] == __version__
        assert len(saved["spec"]["digest"]) == 64
        assert saved["raw"]["count"] == len(list((tmp / "raw").glob("*.csv")))
        assert saved["environment"]["libraries"]["pandas"]
        # the record survives the folder moving: it is keyed on content, not paths
        assert saved["raw"]["digest"] == pv.folder_digest(tmp / "raw")["digest"]
        assert rec["created"].endswith("+00:00")           # recorded in UTC, not local time


def test_a_transport_file_reproduces_by_content_not_bytes():
    """A SAS transport file stamps its own creation time. Comparing bytes would report every
    re-run as a difference and train the reader to ignore the check."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        a, b = tmp / "a", tmp / "b"
        _build(tmp, a)
        _build(tmp, b)

        ae_a, ae_b = a / "datasets" / "ae.xpt", b / "datasets" / "ae.xpt"

        # The transport header carries a creation timestamp, so two writes of the same data
        # are byte-identical only when they land in the same second. Asserting on the bytes
        # would make this test pass or fail on the clock; assert on what is actually true.
        head = ae_a.read_bytes()[:240]
        assert re.search(rb"\d{2}[A-Z]{3}\d{2}:\d{2}:\d{2}:\d{2}", head), \
            "expected a creation timestamp in the transport header"

        # what must hold regardless of when it was written:
        assert pv.dataset_digest(ae_a) == pv.dataset_digest(ae_b)

        rec = pv.record_run(a, __version__, str(tmp / "mapping_spec.xlsx"), str(tmp / "raw"), {})
        pv.record_outputs(a)
        outcome = pv.compare_to(json.loads((a / "provenance.json").read_text()), b)
        assert outcome["reproduced"] and not outcome["changed"]


def test_a_changed_input_is_detected_and_named():
    import pandas as pd

    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        out = tmp / "run"
        _build(tmp, out)
        pv.record_run(out, __version__, str(tmp / "mapping_spec.xlsx"), str(tmp / "raw"), {})
        pv.record_outputs(out)
        rec = json.loads((out / "provenance.json").read_text())

        # touching a file must NOT count as a change — only its content does
        (tmp / "raw" / "ae.csv").touch()
        assert pv.folder_digest(tmp / "raw")["digest"] == rec["raw"]["digest"]

        # editing one value must, and the file must be named
        frame = pd.read_csv(tmp / "raw" / "ae.csv")
        frame.loc[0, "AETERM"] = "SOMETHING ELSE"
        frame.to_csv(tmp / "raw" / "ae.csv", index=False)
        now = pv.folder_digest(tmp / "raw")
        assert now["digest"] != rec["raw"]["digest"]
        differing = [f for f in now["files"] if now["files"][f] != rec["raw"]["files"].get(f)]
        assert differing == ["ae.csv"]


def test_environment_drift_is_reported():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        out = tmp / "run"
        out.mkdir()
        pv.record_run(out, __version__, str(tmp / "mapping_spec.xlsx"), str(tmp / "raw"), {})
        rec = json.loads((out / "provenance.json").read_text())

        assert pv.describe_drift(rec) == []                # same machine, no drift
        rec["environment"]["python"] = "3.9.0"
        rec["environment"]["libraries"]["pandas"] = "1.5.3"
        notes = pv.describe_drift(rec)
        assert any("Python 3.9.0 then" in n for n in notes)
        assert any("pandas 1.5.3 then" in n for n in notes)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1; print(f"FAIL  {name}: {exc}")
    print(f"\n{'all provenance tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
