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


def test_a_studio_study_imports_builds_and_reviews_in_the_app():
    """The whole path a tester takes: import the studio export into a study, build, review."""
    import time
    from fastapi.testclient import TestClient
    from app import server
    with tempfile.TemporaryDirectory() as td:
        server.RUNS = Path(td) / "runs"
        server.RUNS.mkdir()
        server.STUDIES = server.StudyStore(Path(td) / "studies")
        server.SESSION = server.Session()
        c = TestClient(server.app)
        sid = c.post("/api/studies", json={"name": "TEST-001 from the studio"}).json()["id"]
        r = c.post(f"/api/studies/{sid}/studio-import", json={"path": str(FIX)})
        assert r.status_code == 200, r.text
        r = c.post(f"/api/studies/{sid}/studio-import", json={"path": str(FIX)})   # re-import
        assert r.status_code == 200, r.text
        assert r.json()["study_name"] == "TEST-001 Oncology Phase III"
        assert c.post("/api/build", json={"fmt": "csv"}).status_code == 200
        for _ in range(600):
            job = c.get("/api/job").json()
            if job["status"] != "running":
                break
            time.sleep(0.2)
        assert job["status"] == "done", job
        res = c.get("/api/build/results").json()["domains"]
        assert len(res) == 30
        assert all(d["studio_match"]["same"] for d in res), [
            (d["domain"], d["studio_match"]) for d in res if not d["studio_match"]["same"]]
        ae = c.get("/api/domain/AE").json()
        assert ae["engine"] == "studio" and ae["studio_prep"] and ae["rows"] == 16
        assert any(v["reason"].startswith("ae[") for v in ae["variables"])     # the studio's code
        prog = c.get("/api/domain/AE/program/sas").json()
        assert "data" in prog["program"].lower() and prog["filename"] == "ae_build.sas"
        assert c.post("/api/domain/AE/variable/AETERM", json={"mtype": "constant", "value": "X"}).status_code == 409
        # reopening the study keeps it a studio study
        c.post(f"/api/studies/{sid}/close")
        c.post(f"/api/studies/{sid}/open")
        assert c.get("/api/domain/AE").json()["engine"] == "studio"


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
