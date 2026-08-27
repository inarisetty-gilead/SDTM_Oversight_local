"""The aCRF check: annotations come out of the PDF (annotation boxes and flattened
text), each is judged against the standards, and the advice says what to do."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdtm_builder import acrf                                  # noqa: E402

HERE = Path(__file__).resolve().parent


def _make_acrf(path: Path) -> None:
    from pypdf import PdfWriter
    from pypdf.annotations import FreeText
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    w.add_blank_page(width=612, height=792)
    boxes_p1 = ["AETERM", "AESTDTC", "AETRM", "NOT SUBMITTED"]          # AETRM: typo
    boxes_p2 = ["DM.ARMCD", "VSTESTCD = SYSBP", "ZZ.ZZVAR", "SUPPAE.AESOURCE"]
    y = 700
    for t in boxes_p1:
        w.add_annotation(0, FreeText(text=t, rect=(50, y - 20, 250, y)))
        y -= 40
    y = 700
    for t in boxes_p2:
        w.add_annotation(1, FreeText(text=t, rect=(50, y - 20, 250, y)))
        y -= 40
    with open(path, "wb") as fh:
        w.write(fh)


def _fixture(tmp: Path) -> Path:
    subprocess.run([sys.executable, str(HERE / "make_fixture.py"), str(tmp)],
                   check=True, capture_output=True)
    return tmp


def test_acrf_check_judges_every_annotation():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        pdf = tmp / "acrf.pdf"
        _make_acrf(pdf)
        report = acrf.check(pdf, tmp / "mapping_spec.xlsx")

        by = {}
        for r in report["rows"]:
            by.setdefault(r["variable"] or r["value"], r)

        assert report["pages"] == 2
        assert by["AETERM"]["verdict"] == "matched"
        assert by["AETERM"]["page"] == 1
        assert "AE" in by["AETERM"]["domain"]
        assert by["ARMCD"]["verdict"] == "matched" and by["ARMCD"]["domain"] == "DM"
        assert by["VSTESTCD"]["verdict"] == "matched"
        assert by["VSTESTCD"]["value"].startswith("SYSBP")
        # the typo is caught and the fix is named
        assert by["AETRM"]["verdict"] == "not_in_standard"
        assert "AETERM" in by["AETRM"]["advice"]
        # a domain the standards do not define
        assert by["ZZVAR"]["verdict"] == "unknown_domain"
        # supplemental annotations are recognised, not condemned
        assert by["AESOURCE"]["verdict"] == "supp"
        # NOT SUBMITTED is informational
        assert by["NOT SUBMITTED"]["verdict"] == "note"

        # reverse look: AE was annotated, so unannotated AE variables surface
        missing_vars = {m["variable"] for m in report["missing"] if m["domain"] == "AE"}
        assert "AEDECOD" in missing_vars or len(missing_vars) > 0
        assert report["counts"]["off_standard"] == 2   # AETRM + ZZ.ZZVAR


def test_acrf_refuses_bad_inputs():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        pdf = tmp / "acrf.pdf"
        _make_acrf(pdf)
        try:
            acrf.check(pdf, tmp / "nope.xlsx")
        except acrf.AcrfError as exc:
            assert "not exist" in str(exc)
        else:
            raise AssertionError("a missing standards file must refuse")
        try:
            acrf.check(tmp / "mapping_spec.xlsx", tmp / "mapping_spec.xlsx")
        except acrf.AcrfError as exc:
            assert "PDF" in str(exc)
        else:
            raise AssertionError("a non-PDF aCRF must refuse")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as exc:                       # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {exc}")
    sys.exit(1 if failed else 0)
