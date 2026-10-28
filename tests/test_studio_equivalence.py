"""Oversight builds what SDTM Studio builds.

The studio's build engine is vendored into sdtm_builder/studio unchanged; only storage (S3 → local
folders) and the database (→ a local SQLite file) are swapped underneath it. This test loads
TEST-001 as the studio holds it (tests/fixtures/studio_test001, written by
tools/export_studio_fixture.py from the studio's own Domain Build) and checks that every domain
builds to the same records, column for column, offline. Refresh the fixture and re-vendor
together whenever the studio changes.
"""
from __future__ import annotations

import gzip
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from sdtm_builder import studio                                   # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "studio_test001"


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    """Values as text, missing as '' — the reference was saved with mixed cells as text."""
    out = df.copy()
    for c in out.columns:
        # exactly as tools/export_studio_fixture.py saved mixed cells: str(value)
        out[c] = out[c].map(lambda v: "" if v is None or (isinstance(v, float) and v != v) else str(v))
    return out.reset_index(drop=True)


def build_all(work: Path) -> dict:
    shutil.copytree(FIX / "store", work / "store")
    studio.configure(work)
    db = json.loads(gzip.decompress((FIX / "db.json.gz").read_bytes()))
    studio.load_study_tables(db["tables"])
    out = {}
    for ref in sorted((FIX / "reference").glob("*.parquet")):
        dom = ref.stem
        if dom.startswith("SUPP") and len(dom) > 4 and (FIX / "reference" / f"{dom[4:]}.parquet").exists():
            continue
        df, supp, res = studio.domain_build(db["study_id"], dom)
        out[dom] = (df, supp, res)
    return out


def test_every_domain_builds_as_the_studio_builds_it():
    report = json.loads((FIX / "report.json").read_text())
    with tempfile.TemporaryDirectory() as td:
        built = build_all(Path(td))
    diffs = []
    for dom, (df, supp, _res) in built.items():
        for name, got in ((dom, df), (f"SUPP{dom}", supp)):
            ref_p = FIX / "reference" / f"{name}.parquet"
            if not ref_p.exists():
                if got is not None and len(got):
                    diffs.append(f"{name}: built {len(got)} rows, the studio built none")
                continue
            want = _norm(pd.read_parquet(ref_p))
            got = _norm(got if got is not None else pd.DataFrame())
            if list(got.columns) != list(want.columns):
                diffs.append(f"{name}: columns differ {list(got.columns)[:6]}… vs {list(want.columns)[:6]}…")
            elif len(got) != len(want):
                diffs.append(f"{name}: {len(got)} rows vs the studio's {len(want)}")
            elif not got.equals(want):
                bad = [c for c in want.columns if not got[c].equals(want[c])]
                diffs.append(f"{name}: values differ in {bad[:6]}")
    assert set(built) == {d for d in report if report[d]["ok"]}, sorted(set(report) ^ set(built))
    assert not diffs, "\n".join(diffs)


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
    print(f"\n{'studio equivalence holds' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
