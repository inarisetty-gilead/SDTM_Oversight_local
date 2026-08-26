"""A study is a named piece of work that must survive closing the application."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.studies import Study, StudyStore                    # noqa: E402


def test_create_list_and_reopen():
    with tempfile.TemporaryDirectory() as td:
        store = StudyStore(Path(td))
        assert store.list() == []

        s = store.create("GS-US-576-4001 Oncology")
        assert s.id == "gs-us-576-4001-oncology"             # a readable folder name
        assert s.created and s.updated

        s.spec_path = "/specs/mapping.xlsx"
        s.edits = {"DM": {"RACE": {"mtype": "constant", "value": "WHITE"}}}
        s.pipelines = {"DS": [{"op": "stack", "name": "ds", "params": {}}]}
        s.overrides = {"LB": {"base": "lb_all", "sort": [], "prep_mode": "auto", "keys": []}}
        store.save(s)

        back = store.load(s.id)
        assert back is not None
        assert back.edits == s.edits and back.pipelines == s.pipelines
        assert back.spec_path == "/specs/mapping.xlsx"

        card = store.list()[0]
        assert card["counts"] == {"edits": 1, "pipelines": 1, "overrides": 1,
                                  "domains_touched": 3}
        assert card["spec_exists"] is False                  # the path is reported, not assumed


def test_names_never_collide_and_are_readable():
    with tempfile.TemporaryDirectory() as td:
        store = StudyStore(Path(td))
        a = store.create("Study One")
        b = store.create("Study One")
        c = store.create("Study/One!")
        assert a.id == "study-one" and b.id == "study-one-2" and c.id == "study-one-3"
        assert {s["name"] for s in store.list()} == {"Study One", "Study/One!"}


def test_the_file_is_plain_readable_json():
    """Independent of this application: a study must be readable without it."""
    with tempfile.TemporaryDirectory() as td:
        store = StudyStore(Path(td))
        s = store.create("Readable")
        s.edits = {"AE": {"AETERM": {"mtype": "assign", "dataset": "ae", "column": "TERM"}}}
        store.save(s)

        raw = json.loads((Path(td) / s.id / "study.json").read_text())
        assert raw["schema"] == 1
        assert raw["edits"]["AE"]["AETERM"]["column"] == "TERM"
        assert set(raw) >= {"id", "name", "spec_path", "raw_path", "edits", "pipelines"}


def test_a_half_written_file_cannot_lose_the_work():
    """Saving writes to a temporary file and renames, so an interrupted save keeps the last
    good copy rather than truncating it."""
    with tempfile.TemporaryDirectory() as td:
        store = StudyStore(Path(td))
        s = store.create("Atomic")
        s.notes = "first"
        store.save(s)
        s.notes = "second"
        store.save(s)
        assert store.load(s.id).notes == "second"
        assert not list((Path(td) / s.id).glob("*.tmp"))     # nothing left behind


def test_delete_removes_only_that_study():
    with tempfile.TemporaryDirectory() as td:
        store = StudyStore(Path(td))
        a, b = store.create("Keep"), store.create("Remove")
        assert store.delete(b.id) is True
        assert store.delete("no-such-study") is False
        assert [s["id"] for s in store.list()] == [a.id]


def test_unknown_fields_in_an_older_file_are_ignored():
    """A study written by a later version must still open, not crash."""
    with tempfile.TemporaryDirectory() as td:
        store = StudyStore(Path(td))
        s = store.create("Forward")
        p = Path(td) / s.id / "study.json"
        d = json.loads(p.read_text())
        d["something_from_the_future"] = {"a": 1}
        p.write_text(json.dumps(d))
        back = store.load(s.id)
        assert back is not None and back.name == "Forward"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1; print(f"FAIL  {name}: {exc}")
    print(f"\n{'all study tests passed' if not failures else f'{failures} test(s) failed'}")
    raise SystemExit(1 if failures else 0)
