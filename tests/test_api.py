"""Regression tests for the application's session handling.

These exist because of a real, user-visible failure: pressing "Load spec" a second time
silently discarded a completed build while the build table stayed on screen, so every
domain the user clicked answered "has not been built in this session".
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient                     # noqa: E402

HERE = Path(__file__).resolve().parent


def _fixture(tmp: Path) -> Path:
    subprocess.run([sys.executable, str(HERE / "make_fixture.py"), str(tmp)],
                   check=True, capture_output=True)
    return tmp


def _client(runs: Path):
    from app import server
    server.RUNS = runs
    runs.mkdir(parents=True, exist_ok=True)
    server.SESSION = server.Session()
    return TestClient(server.app), server


def _wait(client, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/job").json()
        if job["status"] != "running":
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def _load_and_build(client, tmp: Path):
    r = client.post("/api/spec", json={"path": str(tmp / "mapping_spec.xlsx")})
    assert r.status_code == 200, r.text
    r = client.post("/api/raw", json={"path": str(tmp / "raw")})
    assert r.status_code == 200, r.text
    assert client.post("/api/build", json={"fmt": "csv"}).status_code == 200
    job = _wait(client)
    assert job["status"] == "done", job


def test_reloading_an_unchanged_spec_keeps_the_build():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)
        assert client.get("/api/domain/EG").status_code == 200

        # pressing "Load spec" again on the SAME, UNCHANGED file must not lose the build
        again = client.post("/api/spec", json={"path": str(tmp / "mapping_spec.xlsx")}).json()
        assert again["cleared"] is False
        assert client.get("/api/state").json()["built"] == ["AE", "DM", "DS", "EG", "VS"]
        assert client.get("/api/domain/EG").status_code == 200

        # and the same for re-scanning the raw folder
        again = client.post("/api/raw", json={"path": str(tmp / "raw")}).json()
        assert again["cleared"] is False
        assert again["built"] == ["AE", "DM", "DS", "EG", "VS"]
        assert client.get("/api/domain/DS").status_code == 200


def test_a_changed_spec_invalidates_the_build_and_says_so():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)

        (tmp / "mapping_spec.xlsx").touch()                   # same path, new contents
        res = client.post("/api/spec", json={"path": str(tmp / "mapping_spec.xlsx")}).json()
        assert res["cleared"] is True
        assert client.get("/api/state").json()["built"] == []

        stale = client.get("/api/domain/EG")
        assert stale.status_code == 409
        assert "Run the build again" in stale.json()["detail"]


def test_session_survives_a_restart():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        runs = Path(td) / "runs"
        client, srv = _client(runs)
        _load_and_build(client, tmp)
        assert client.post("/api/compare", json={"path": str(tmp / "vendor")}).status_code == 200
        _wait(client)
        before = client.get("/api/state").json()

        # a fresh process would start with an empty session and resume from the run folder
        srv.SESSION = srv.Session()
        assert srv._restore_session()
        after = client.get("/api/state").json()
        assert after["built"] == before["built"]
        assert after["compared"] == before["compared"]

        # the transposed base is an in-memory frame — it has to be rebuilt on resume,
        # or the domain view for EG would fail after a restart
        eg = client.get("/api/domain/EG")
        assert eg.status_code == 200
        assert eg.json()["rows"] == 36
        assert eg.json()["prep"]["op"] == "transpose_findings"


def test_rebuilding_one_domain_leaves_the_others_alone():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)
        before = {d["domain"]: d["rows"] for d in client.get("/api/build/results").json()["domains"]}

        client.post("/api/domain/EG/settings",
                    json={"base": "egperf", "prep_mode": "off", "sort": [], "keys": []})
        assert client.post("/api/domain/EG/build", json={}).status_code == 200
        assert _wait(client)["status"] == "done"

        after = {d["domain"]: d["rows"] for d in client.get("/api/build/results").json()["domains"]}
        assert after["EG"] == 12 and before["EG"] == 36        # the override took effect
        for dom in ("AE", "DM", "DS", "VS"):
            assert after[dom] == before[dom], f"{dom} changed when only EG was rebuilt"
        # the comparison is dropped, so no one reads a report against data that has changed
        assert client.get("/api/state").json()["compared"] == []


def test_editing_a_variable_through_the_api():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)

        edit = {"mtype": "derived", "recipe": "cond", "note": "checking the vendor's short term",
                "args": {"rules": [{"src": {"dataset": "dm", "column": "RACECD"}, "op": "eq",
                                    "value": "BLACK OR AFRICAN AMERICAN",
                                    "then": {"kind": "text", "text": "BLACK"}}],
                         "else": {"dataset": "dm", "column": "RACECD"}}}

        # a preview shows the result but must not commit anything
        prev = client.post("/api/domain/DM/variable/RACE/preview", json=edit).json()
        assert prev["ok"] and "BLACK" in prev["samples"]
        assert "BLACK OR AFRICAN AMERICAN" not in prev["samples"]
        assert client.get("/api/domain/DM").json()["edits"] == {}

        # applying it takes effect and is marked as a hand edit
        assert client.post("/api/domain/DM/variable/RACE", json=edit).status_code == 200
        assert client.post("/api/domain/DM/build", json={}).status_code == 200
        assert _wait(client)["status"] == "done"
        dm = client.get("/api/domain/DM").json()
        race = next(v for v in dm["variables"] if v["variable"] == "RACE")
        assert race["edited"] is True and race["spec_method"] == "dm.RACECD"
        assert "BLACK" in race["samples"]
        rows = {d["domain"]: d for d in client.get("/api/build/results").json()["domains"]}
        assert rows["DM"]["edited"] == 1 and rows["AE"]["edited"] == 0

        # reverting puts the variable back under the spec
        assert client.delete("/api/domain/DM/variable/RACE").status_code == 200
        assert client.post("/api/domain/DM/build", json={}).status_code == 200
        _wait(client)
        race = next(v for v in client.get("/api/domain/DM").json()["variables"]
                    if v["variable"] == "RACE")
        assert race["edited"] is False
        assert "BLACK OR AFRICAN AMERICAN" in race["samples"]


def test_a_bad_edit_reports_instead_of_silently_emptying_the_column():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)
        bad = client.post("/api/domain/DM/variable/RACE/preview",
                          json={"mtype": "assign", "dataset": "dm", "column": "NOPE"}).json()
        assert bad["ok"] is False
        assert "NOPE" in (bad.get("error") or "")


def test_raw_and_domain_data_pages():
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)

        page = client.get("/api/domain/VS/data?offset=0&limit=10").json()
        assert page["nrows"] == 36 and len(page["rows"]) == 10
        assert page["columns"][0]["name"] == "STUDYID"
        assert all("populated" in c and "numeric" in c for c in page["columns"])

        tail = client.get("/api/domain/VS/data?offset=30&limit=10").json()
        assert len(tail["rows"]) == 6 and tail["offset"] == 30

        # a whole domain comes back in one request, so the table can hold every row
        whole = client.get("/api/domain/VS/data?limit=100000").json()
        assert len(whole["rows"]) == whole["nrows"] == 36

        # low-cardinality columns carry their distinct values, for a dropdown filter
        testcd = next(c for c in whole["columns"] if c["name"] == "VSTESTCD")
        assert sorted(testcd["distinct"]) == ["DIABP", "PULSE", "SYSBP"]
        usubjid = next(c for c in whole["columns"] if c["name"] == "USUBJID")
        assert usubjid["distinct"] is None or len(usubjid["distinct"]) <= 40

        supp = client.get("/api/domain/AE/data?part=supp").json()
        assert supp["part"] == "supp" and supp["nrows"] == 3

        raw = client.get("/api/raw/vs/data?limit=5").json()
        assert raw["dataset"] == "vs" and raw["nrows"] == 36 and len(raw["rows"]) == 5

        assert client.get("/api/raw/nosuchthing/data").status_code == 404


def test_every_recipe_field_is_explained():
    """A field labelled `y_col` with no help tells the reader nothing. Every field the editor
    renders must carry a human label, and every derivation must say what it does."""
    with tempfile.TemporaryDirectory() as td:
        client, _srv = _client(Path(td) / "runs")
        cat = client.get("/api/recipes").json()
        assert cat["mtypes"] == ["assign", "constant", "sequence", "derived", "drop"]

        for r in cat["recipes"]:
            assert r.get("desc"), f"{r['id']} has no description"
            for f in r["fields"]:
                assert f.get("label"), f"{r['id']}.{f['k']} has no label"
                assert f["label"] != f["k"], f"{r['id']}.{f['k']} is labelled with its raw key"

        de = next(r for r in cat["recipes"] if r["id"] == "date_extreme")
        srcs = next(f for f in de["fields"] if f["k"] == "sources")
        assert srcs["t"] == "sources"                # a row builder, not a JSON box
        assert "one row per dataset" in srcs["help"]

        iso = next(r for r in cat["recipes"] if r["id"] == "iso_date")
        labels = {f["k"]: f["label"] for f in iso["fields"]}
        assert labels["y_col"] == "Year column"
        assert labels["m_col"] == "Month column"
        assert labels["d_col"] == "Day column"
        # the parts are advanced overrides now — naming the date column is enough
        assert all(f.get("advanced") for f in iso["fields"] if f["k"] in ("y_col", "m_col", "d_col"))
        assert "partial" in iso["desc"]        # the behaviour is explained on the derivation


def test_max_date_across_several_datasets():
    """The reference dates (RFSTDTC, RFENDTC) are built this way."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)

        latest = client.post("/api/domain/DM/variable/RFSTDTC/preview", json={
            "mtype": "derived", "recipe": "date_extreme",
            "args": {"func": "max", "group_by": ["USUBJID"], "sources": [
                {"dataset": "ae", "date_col": "AESTDAT"},
                {"dataset": "vs", "date_col": "VSDAT"},
                {"dataset": "consent", "date_col": "DSSTDAT"}]}}).json()
        assert latest["ok"], latest
        assert latest["populated"] == 6                        # every subject got a date

        earliest = client.post("/api/domain/DM/variable/RFSTDTC/preview", json={
            "mtype": "derived", "recipe": "date_extreme",
            "args": {"func": "min", "group_by": ["USUBJID"], "sources": [
                {"dataset": "ae", "date_col": "AESTDAT"},
                {"dataset": "vs", "date_col": "VSDAT"},
                {"dataset": "consent", "date_col": "DSSTDAT"}]}}).json()
        assert earliest["ok"]
        # the earliest across all three must not be later than the latest across all three
        assert min(earliest["samples"]) <= min(latest["samples"])
        # consent dates are the earliest thing in the fixture, so min should find them
        assert any(v.startswith("2024-01-0") for v in earliest["samples"])


def test_editor_arguments_are_suggested_from_the_spec():
    """Picking a derivation should fill the form from Input Variables, not hand the reader a
    blank one to copy the spec into."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)

        got = client.get("/api/domain/AE/variable/AESTDTC/suggest?recipe=iso_date").json()
        assert got["args"]["dataset"] == "ae"
        assert got["args"]["date_col"] == "AESTDAT"
        assert "raw.ae.AESTDAT" in got["input_variables"]

        dy = client.get("/api/domain/AE/variable/AESTDY/suggest?recipe=study_day").json()
        assert dy["args"]["dtc_var"] == "AESTDTC" and dy["args"]["ref_var"] == "RFSTDTC"

        ext = client.get("/api/domain/DM/variable/RFSTDTC/suggest?recipe=date_extreme").json()
        assert ext["args"]["func"] == "min" and ext["args"]["group_by"] == ["USUBJID"]


def test_the_entry_page_is_never_cached():
    """A cached index.html keeps loading whichever bundle was current when it was stored, so
    the application silently stops updating while appearing to work."""
    with tempfile.TemporaryDirectory() as td:
        client, _srv = _client(Path(td) / "runs")
        res = client.get("/")
        assert res.status_code == 200
        cache = res.headers.get("cache-control", "")
        assert "no-store" in cache and "must-revalidate" in cache
        assert res.headers.get("pragma") == "no-cache"

        # and the served build is reportable, so a stale page can be spotted rather than guessed
        ident = client.get("/api/build-id").json()
        assert ident["version"] and isinstance(ident["assets"], list)


def test_a_prepared_dataset_is_usable_as_a_variable_source():
    """Watching a dataset being built in the pipeline and then not finding it in the variable
    editor is a trap with no way to diagnose it. A preview registers what it produced."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)

        before = client.get("/api/domain/AE").json()
        assert "ae_plus_dm" not in before["datasets"]

        # preview only — the pipeline is NOT applied
        prev = client.post("/api/domain/AE/pipeline/preview", json={"steps": [
            {"op": "merge", "name": "ae_plus_dm", "params": {
                "how": "left", "on": ["USUBJID"],
                "inputs": [{"dataset": "ae"}, {"dataset": "dm", "columns": ["SEXCD"]}]}}]}).json()
        assert prev["ok"], prev

        after = client.get("/api/domain/AE").json()
        assert "ae_plus_dm" in after["datasets"]              # offered as a source at once
        assert "ae_plus_dm" in after["prepared_datasets"]     # grouped, not lost among the raw
        assert "ae_plus_dm" in after["unapplied_datasets"]    # and marked as not yet applied

        cols = client.get("/api/domain/AE/columns/ae_plus_dm").json()["columns"]
        assert "SEXCD" in cols and "AETERM" in cols

        # and a variable can be mapped to it immediately
        got = client.post("/api/domain/AE/variable/AESEV/preview",
                          json={"mtype": "assign", "dataset": "ae_plus_dm",
                                "column": "SEXCD"}).json()
        assert got["ok"] and got["populated"] == 8

        # applying the pipeline promotes it out of the unapplied state
        client.post("/api/domain/AE/pipeline", json={"steps": [
            {"op": "merge", "name": "ae_plus_dm", "params": {
                "how": "left", "on": ["USUBJID"],
                "inputs": [{"dataset": "ae"}, {"dataset": "dm", "columns": ["SEXCD"]}]}}]})
        applied = client.get("/api/domain/AE").json()
        assert "ae_plus_dm" in applied["prepared_datasets"]
        assert "ae_plus_dm" not in applied["unapplied_datasets"]


def test_reopening_a_study_resumes_its_last_build():
    """Opening a study must land the reader where they left off — built domains and all —
    and must NOT resume a build whose inputs have changed since."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, srv = _client(Path(td) / "runs")
        srv.STUDIES = srv.StudyStore(Path(td) / "studies")

        client.post("/api/studies", json={"name": "Resume Test"})
        sid = client.get("/api/studies").json()["studies"][0]["id"]
        _load_and_build(client, tmp)
        assert client.get("/api/state").json()["built"]

        client.post(f"/api/studies/{sid}/close")
        assert client.get("/api/state").json()["built"] == []

        reopened = client.post(f"/api/studies/{sid}/open").json()
        assert reopened["built"] == ["AE", "DM", "DS", "EG", "VS"]
        # the domain view answers immediately, without a rebuild
        assert client.get("/api/domain/DM").json()["rows"] == 6

        # a changed spec means the cached build no longer describes the inputs — refuse it
        (tmp / "mapping_spec.xlsx").touch()
        client.post(f"/api/studies/{sid}/close")
        stale = client.post(f"/api/studies/{sid}/open").json()
        assert stale["built"] == []


def test_compare_can_be_restricted_to_named_domains():
    """Compare only DM: the result covers DM (and its SUPP) and nothing else — no
    'delivered but not built' noise from the rest of the vendor folder."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _fixture(Path(td))
        client, _srv = _client(Path(td) / "runs")
        _load_and_build(client, tmp)

        # unrestricted covers everything, including vendor-only rows
        client.post("/api/compare", json={"path": str(tmp / "vendor")})
        _wait(client)
        full = {d["domain"] for d in client.get("/api/compare/results").json()["domains"]}
        assert {"DM", "AE", "VS", "SUPPAE"} <= full

        # restricted to DM covers exactly DM
        client.post("/api/compare", json={"path": str(tmp / "vendor"), "domains": ["DM"]})
        _wait(client)
        only = {d["domain"] for d in client.get("/api/compare/results").json()["domains"]}
        assert only == {"DM"}

        # AE brings its SUPP along
        client.post("/api/compare", json={"path": str(tmp / "vendor"), "domains": ["AE"]})
        _wait(client)
        ae = {d["domain"] for d in client.get("/api/compare/results").json()["domains"]}
        assert ae == {"AE", "SUPPAE"}

        # naming a domain that was not built is a clear error, not an empty result
        bad = client.post("/api/compare", json={"path": str(tmp / "vendor"), "domains": ["ZZ"]})
        assert bad.status_code == 400 and "not built" in bad.json()["detail"]


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
    print(f"\n{'all API tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
