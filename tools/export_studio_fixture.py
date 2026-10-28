"""Build the studio-equivalence fixture: run the STUDIO's real Domain Build on every domain of a
study, read-only, and record what it read.

Run it with the studio's Python (it needs the studio's S3 and database):

    cd /path/to/sdtm-studio/Container/sdtm_api
    .venv/bin/python /path/to/SDTM_Oversight_local/tools/export_studio_fixture.py [STUDY_ID] [OUT]

Writes OUT (default tests/fixtures/studio_test001):
  reference/{DOM}.parquet, SUPP{DOM}.parquet   what the studio's Domain Build produced
  store/<key>                                  every study object it read (standards excluded:
                                               ./standards is the snapshot of those)
  db.json                                      the study's rows in the tables the build queries
  report.json                                  per-domain status
Writes to S3 or the database are refused while it runs, so the studio's data cannot change.
For a synthetic study only — never export a study holding real subject data."""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, ".")
STUDY = sys.argv[1] if len(sys.argv) > 1 else "ae5b6eb8-b082-45d4-b877-db94aaf0d771"   # TEST-001
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else \
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "studio_test001"
(OUT / "reference").mkdir(parents=True, exist_ok=True)
(OUT / "store").mkdir(parents=True, exist_ok=True)
STANDARD_PREFIXES = ("specs/", "reference/")

import src.aws_clients as aws  # noqa: E402
import src.db as dbmod  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

_real_get_client = aws.get_client
reads, missing, db_reads = {}, set(), []


class Body:
    def __init__(self, b):
        self._b = b

    def read(self, *a):
        return self._b


class RecS3:
    def __init__(self, real):
        self.real = real

    def get_object(self, Bucket, Key, **kw):
        try:
            r = self.real.get_object(Bucket=Bucket, Key=Key, **kw)
        except ClientError:
            missing.add(Key)
            raise
        data = r["Body"].read()
        reads[Key] = data
        r["Body"] = Body(data)
        return r

    def head_object(self, Bucket, Key, **kw):
        return self.real.head_object(Bucket=Bucket, Key=Key, **kw)

    def list_objects_v2(self, **kw):
        r = self.real.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            reads.setdefault("__listed__:" + o["Key"], b"")
        return r

    def get_paginator(self, name):
        real_p = self.real.get_paginator(name)

        class P:
            def paginate(_s, **kw):
                for page in real_p.paginate(**kw):
                    for o in page.get("Contents", []):
                        reads.setdefault("__listed__:" + o["Key"], b"")
                    yield page
        return P()

    def __getattr__(self, name):
        if name in ("put_object", "delete_object", "delete_objects", "copy_object", "upload_file",
                    "upload_fileobj"):
            def refuse(*a, **k):
                raise RuntimeError(f"write refused during export: {name} {k.get('Key')}")
            return refuse
        return getattr(self.real, name)


def rec_get_client(service, *a, **k):
    c = _real_get_client(service, *a, **k)
    return RecS3(c) if service == "s3" else c


_real_get_db = dbmod.get_db


class RecDB:
    def __init__(self, real):
        self.real = real

    def execute(self, query, params=()):
        q = " ".join(str(query).split())
        if q.split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"):
            raise RuntimeError(f"database write refused during export: {q[:80]}")
        rows = self.real.execute(query, params)
        db_reads.append({"query": q, "params": [str(p) for p in params or ()], "rows": rows})
        return rows

    def transaction(self):
        raise RuntimeError("database transaction refused during export")


def rec_get_db():
    return RecDB(_real_get_db())


aws.get_client = rec_get_client
dbmod.get_db = rec_get_db
for modname in ("src.build_engine", "src.build_exec", "src.prep_engine", "src.sdtm_knowledge",
                "src.biomedical_concepts", "src.program_gen", "src.rule_library"):
    m = __import__(modname, fromlist=["x"])
    if hasattr(m, "get_client"):
        m.get_client = rec_get_client
    if hasattr(m, "get_db"):
        m.get_db = rec_get_db

from src import build_exec as be  # noqa: E402

saved = sorted({k.split("/")[-1][:-5] for k in
                [o["Key"] for o in rec_get_client("s3", region_name=__import__("src.config", fromlist=["x"]).settings.S3_REGION)
                 .real.list_objects_v2(Bucket=__import__("src.config", fromlist=["x"]).settings.S3_BUCKET,
                                       Prefix=f"sdtm_build/{STUDY}/").get("Contents", [])]
                if k.endswith(".json") and "/" not in k.split(f"sdtm_build/{STUDY}/")[1]})
report = {}
for dom in saved:
    try:
        c = be.compose_blocks(STUDY, dom)
        pre = be._prep_frames(STUDY, c["prep_steps"], persist=False)
        out = be._run_domain(STUDY, dom, c["blocks"], preloaded=pre, settings=c["settings"],
                             row_limit=200, prep_steps=c["prep_steps"])
        df, supp = out.get("df"), out.get("supp_df")
        def _save(frame, name):
            f = frame.copy()
            for col in f.columns:
                if f[col].dtype == object:      # mixed text/number cells: keep each value's text
                    f[col] = f[col].map(lambda v: None if v is None or (isinstance(v, float) and v != v) else str(v))
            f.to_parquet(OUT / "reference" / f"{name}.parquet", index=False)
        if df is not None:
            _save(df, dom)
        if supp is not None and len(supp):
            _save(supp, f"SUPP{dom}")
        report[dom] = {"ok": True, "rows": 0 if df is None else int(len(df)),
                       "supp": 0 if supp is None else int(len(supp)),
                       "prep_steps": len(c["prep_steps"] or []),
                       "error": out.get("error"), "warnings": (out.get("warnings") or [])[:5]}
    except Exception as exc:  # noqa: BLE001
        report[dom] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "trace": traceback.format_exc()[-1500:]}
    print(dom, report[dom].get("rows"), report[dom].get("error") or "", flush=True)

for key, data in reads.items():
    if key.startswith("__listed__:") or key.startswith(STANDARD_PREFIXES):
        continue
    if STUDY not in key:
        raise SystemExit(f"read a non-study, non-standards object: {key}")
    p = OUT / "store" / key
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)

# the study's rows in every table the build queried — the local database is loaded from these
tables = {}
for t in ("studies", "mapping_run", "study_metadata", "sdtm_output", "study_overrides"):
    try:
        order = " ORDER BY version DESC LIMIT 1" if t in ("mapping_run", "study_metadata") else ""
        tables[t] = _real_get_db().execute(f"SELECT * FROM {t} WHERE study_id = %s{order}", (STUDY,))
    except Exception as exc:  # noqa: BLE001
        tables[t] = []
        print(f"  {t}: {exc}")
# the build asks for the newest built file per domain — not every build's row, nor its preview
latest = {}
for r in sorted(tables.get("sdtm_output") or [], key=lambda r: str(r.get("created_at") or "")):
    latest[str(r.get("domain")).upper()] = {**r, "data_preview": None}
tables["sdtm_output"] = list(latest.values())
import gzip  # noqa: E402
(OUT / "db.json.gz").write_bytes(gzip.compress(json.dumps(
    {"study_id": STUDY, "tables": tables}, default=str).encode(), mtime=0))
(OUT / "db.json").unlink(missing_ok=True)
(OUT / "report.json").write_text(json.dumps(report, indent=1, default=str))
print("read", sum(1 for k in reads if not k.startswith("__listed__:")), "objects;",
      len(missing), "missing;", len(db_reads), "db reads")
