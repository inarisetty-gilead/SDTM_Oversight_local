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


def _page_text(writer, page, lines) -> None:
    """Draw text lines onto a page: [(x, y, size, text)] — a form title and questions,
    so question/form extraction has something real to find."""
    from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = writer._add_object(font)
    ops = "".join(f"BT /F1 {size} Tf {x} {y} Td ({text}) Tj ET\n"
                  for x, y, size, text in lines)
    stream = DecodedStreamObject()
    stream.set_data(ops.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})


def _make_acrf(path: Path) -> None:
    from pypdf import PdfWriter
    from pypdf.annotations import FreeText
    w = PdfWriter()
    p1 = w.add_blank_page(width=612, height=792)
    w.add_blank_page(width=612, height=792)
    _page_text(w, p1, [(50, 760, 12, "Ophthalmic Examination"),
                       (50, 700, 10, "What is the laterality of the eye?")])
    # the annotation box sits beside its question, as on a real aCRF
    w.add_annotation(0, FreeText(text="OE.OELAT", rect=(300, 690, 420, 710)))
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
        # the annotation carries the CRF question it answers, and the form name
        assert by["OELAT"]["question"] == "What is the laterality of the eye?"
        assert by["OELAT"]["form"] == "Ophthalmic Examination"
        assert by["OELAT"]["page"] == 1
        # NOT SUBMITTED is informational
        assert by["NOT SUBMITTED"]["verdict"] == "note"

        # reverse look: AE was annotated, so unannotated AE variables surface
        missing_vars = {m["variable"] for m in report["missing"] if m["domain"] == "AE"}
        assert "AEDECOD" in missing_vars or len(missing_vars) > 0
        assert report["counts"]["off_standard"] == 3   # AETRM + ZZ.ZZVAR + OE (not in fixture standards)


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
