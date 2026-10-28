"""SDTM Studio's own build engine, run locally.

The engine modules here (build_exec, build_engine, prep_engine, sdtm_knowledge, …) are copied
unchanged from SDTM Studio by tools/vendor_studio.py — testers exercise the studio's code, not a
re-implementation of it. What they talk to is replaced underneath them, and only that:

  aws_clients  S3 → a folder: the study's working files, over the bundled standards snapshot
  db           the database → a SQLite file in the study's working folder (studio schema)
  config       bucket and region names only

Nothing here reaches the network, and no AI is involved: the AI build paths are not vendored.
Call configure() before using the engine.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from ..sas import expr as _sas_expr, functions as _sas_functions, runtime as _sas_runtime

# the SAS runtime exists once (sdtm_builder/sas); the studio modules import it by their names
for _name, _mod in (("sas_runtime", _sas_runtime), ("sas_expr", _sas_expr),
                    ("sas_functions", _sas_functions)):
    sys.modules[f"{__name__}.{_name}"] = _mod
    globals()[_name] = _mod

HERE = Path(__file__).resolve().parent
STANDARDS = Path(os.environ.get("SDTM_STANDARDS_DIR") or HERE.parents[1] / "standards")
os.environ.setdefault("SDTM_RULE_LIBRARY_DIR", str(HERE / "rule_library_data"))


def configure(work_dir: str | Path, standards_dir: str | Path | None = None) -> None:
    """Point the engine at a study's working folder (read-write) over the standards (read-only)."""
    from . import aws_clients, db
    aws_clients.configure(Path(work_dir) / "store", Path(standards_dir or STANDARDS))
    db.configure(Path(work_dir) / "studio.sqlite")


def engine(name: str):
    """A vendored engine module, e.g. engine('build_exec')."""
    return importlib.import_module(f"{__name__}.{name}")


def load_study_tables(tables: dict) -> None:
    """Insert a study's rows ({table: [row, …]}) into the local database — how a study the
    studio exported (tools/export_studio_fixture.py) is brought in."""
    from .db import get_db
    db = get_db()
    for table, rows in tables.items():
        # a row exported from an older database may carry a column the schema has since
        # renamed or dropped — only the current table's columns are loaded
        have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        for row in rows or []:
            cols = [c for c in row if c in have]
            db.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES "
                       f"({', '.join(['%s'] * len(cols))})", tuple(row[c] for c in cols))


def domain_build(study: str, domain: str):
    """The studio's Domain Build for one domain, as its Build button runs it: the saved blocks,
    settings and prep steps, the prep pipeline, then the domain. Returns (df, supp_df, result)."""
    be = engine("build_exec")
    c = be.compose_blocks(study, domain)
    pre = be._prep_frames(study, c["prep_steps"], persist=False)
    out = be._run_domain(study, domain, c["blocks"], preloaded=pre, settings=c["settings"],
                         row_limit=200, prep_steps=c["prep_steps"])
    return out.pop("df", None), out.pop("supp_df", None), out
