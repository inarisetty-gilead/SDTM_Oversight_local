"""Oversight builds what SDTM Studio builds.

The studio's build engine is vendored into sdtm_builder/studio unchanged; only storage (S3 → local
folders) and the database (→ a local SQLite file) are swapped underneath it. The fixture is TEST-001
exported from the studio by tools/export_studio_study.py — its spec, saved Domain Build edits, prep
steps, raw data and trial design, plus what the studio's own Domain Build and Auto-Build produced at
export. Each build here must give the same records, column for column, offline. Re-export the
fixture and re-vendor together whenever the studio changes.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIX = Path(__file__).resolve().parent / "fixtures" / "studio_test001.zip"


def _import(work: Path) -> tuple[Path, dict]:
    from sdtm_builder.studio import oversight
    dest = work / "studio"
    return dest, oversight.import_export(FIX, dest)


def test_every_domain_builds_as_the_studios_domain_build():
    from sdtm_builder.studio import oversight
    with tempfile.TemporaryDirectory() as td:
        dest, info = _import(Path(td))
        diffs = []
        for dom in info["domains"]:
            res, _raw = oversight.build_domain(info["study_id"], dom)
            for name, got in ((dom, res.dataset), (f"SUPP{dom}", res.supp)):
                m = oversight.reference_match(dest, name, got)
                if m.get("checked") and not m.get("same"):
                    diffs.append(f"{name}: {m['why']}")
                if not m.get("checked") and got is not None and len(got):
                    diffs.append(f"{name}: built {len(got)} rows, the studio built none")
        assert len(info["domains"]) == 30
        assert not diffs, "\n".join(diffs)


def test_auto_build_builds_as_the_studios_auto_build():
    """Every dataset the studio's Auto-Build saved — raw-sourced domains, SUPP--, and the
    trial-design domains from the study's trial design — built offline, record for record."""
    from sdtm_builder.studio import oversight
    with tempfile.TemporaryDirectory() as td:
        dest, info = _import(Path(td))
        out = oversight.auto_build(dest, info["study_id"])
        built = {}
        for dom, r in out["results"].items():
            built[dom] = r.dataset
            if r.supp is not None:
                built[f"SUPP{dom}"] = r.supp
        refs = sorted(p.stem for p in (dest / "reference_autobuild").glob("*.parquet"))
        assert len(refs) == 37 and {"TA", "TE", "TI", "TV", "TS"} <= set(refs)
        diffs = [f"{n}: {oversight.reference_match(dest, n, built.get(n), 'reference_autobuild')}"
                 for n in refs + sorted(set(built) - set(refs))
                 if not oversight.reference_match(dest, n, built.get(n), "reference_autobuild").get("same")]
        assert not diffs, "\n".join(diffs)
        assert out["manifest"]["overall_score"] == 93
        # the import is untouched: Domain Build still gives the studio's Domain Build after it
        res, _ = oversight.build_domain(info["study_id"], "CE")
        assert oversight.reference_match(dest, "CE", res.dataset)["same"]


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
        for _ in range(2):                                         # import, then re-import
            r = c.post(f"/api/studies/{sid}/studio-import", json={"path": str(FIX)})
            assert r.status_code == 200, r.text
        assert r.json()["study_name"] == "TEST-001 Oncology Phase III"

        def build(**body):
            assert c.post("/api/build", json={"fmt": "csv", **body}).status_code == 200
            for _ in range(900):
                job = c.get("/api/job").json()
                if job["status"] != "running":
                    break
                time.sleep(0.2)
            assert job["status"] == "done", job
            return c.get("/api/build/results").json()["domains"]

        res = build()
        assert len(res) == 30 and all(d["studio_match"]["same"] for d in res), [
            (d["domain"], d["studio_match"]) for d in res if not d["studio_match"]["same"]]
        ae = c.get("/api/domain/AE").json()
        assert ae["engine"] == "studio" and ae["studio_prep"] and ae["rows"] == 16
        assert any(v["reason"].startswith("ae[") for v in ae["variables"])     # the studio's code
        prog = c.get("/api/domain/AE/program/sas").json()
        assert "data" in prog["program"].lower() and prog["filename"] == "ae_build.sas"
        # every domain's studio programs are written with the build, and download as one zip
        import io
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(c.get("/api/programs/download?lang=all").content))
        names = set(z.namelist())
        assert {f"python/{d['domain'].lower()}_build.py" for d in res} <= names
        assert {f"sas/{d['domain'].lower()}_build.sas" for d in res} <= names
        import re
        stamp = re.compile(r"Generated: \S+")             # the studio stamps each generation's time
        assert stamp.sub("", z.read("sas/ae_build.sas").decode()) == stamp.sub("", prog["program"])
        assert c.post("/api/domain/AE/variable/AETERM", json={"mtype": "constant", "value": "X"}).status_code == 409

        res = build(studio_mode="auto")
        doms = {d["domain"]: d for d in res}
        assert {"TA", "TV", "TS", "CV", "QS"} <= set(doms)                     # beyond Domain Build
        assert all(d["studio_match"]["same"] for d in res if d["studio_match"].get("checked")), [
            (d["domain"], d["studio_match"]) for d in res if not d["studio_match"].get("same")]
        assert c.get("/api/domain/TA").json()["build_mode"] == "auto"
        assert "trial design" in c.get("/api/domain/TA/program/sas").json()["program"]
        z = zipfile.ZipFile(io.BytesIO(c.get("/api/programs/download?lang=python").content))
        assert "python/cv_build.py" in z.namelist() and not any(n.startswith("sas/") for n in z.namelist())
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
