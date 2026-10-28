"""Export a study from SDTM Studio, as the studio holds it, for Oversight to import.

Run it with the studio's Python (it reads the studio's S3 and database):

    cd /path/to/sdtm-studio/Container/sdtm_api
    .venv/bin/python /path/to/SDTM_Oversight_local/tools/export_studio_study.py STUDY_ID OUT.zip

The zip holds everything the studio's builds read for that study, under the same keys:
  store/synth/{study}/*.parquet, store/rawdata/{study}/*.parquet   the raw forms
  store/prep/{study}/*.parquet                                     saved prep outputs
  store/sdtm_build/{study}/{DOM}.json                              saved Domain Build edits,
                                                                   settings and prep steps
  store/sdtm/output/{study}/…                                      the newest built domains
  db.json.gz                                                       the study's database rows
                                                                   (spec, metadata, trial design…)
  reference/{DOM}.parquet            what the studio's Domain Build produces now, per domain
  reference_autobuild/{DOM}.parquet  what the studio's Auto-Build produces now
  manifest.json
Standards are not included: Oversight carries its own snapshot (./standards).

Nothing in the studio changes. The builds run against a throwaway copy of the studio's local
database, and every S3 write is held in memory — so Auto-Build runs exactly as its button runs it
(each domain saved as it is built, so later domains read it), without touching the study.

A real study's raw data is subject data. Keep the zip on this machine — out of cloud-synced
folders, e-mail and the code repository — exactly as Oversight's own runs/ and studies/.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
STUDY_TABLES = ("studies", "mapping_run", "study_metadata", "sdtm_output", "study_overrides", "trial_design")


def _sandbox_database() -> Path:
    """A copy of the studio's local SQLite database for the builds to write into."""
    real = Path(os.environ.get("LOCAL_DB_PATH", "./.local/sdtm_local.db")).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="studio_export_")) / "sdtm_local.db"
    shutil.copy2(real, tmp)
    os.environ["LOCAL_DB_PATH"] = str(tmp)
    return tmp


class _Body:
    def __init__(self, b):
        self._b = b

    def read(self, *a):
        return self._b


class OverlayS3:
    """The real S3 for reads; writes and deletes held in memory, and read back from there."""

    def __init__(self, real):
        self.real, self.written, self.deleted, self.reads = real, {}, set(), {}

    def get_object(self, Bucket, Key, **kw):
        if Key in self.deleted:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        if Key in self.written:
            return {"Body": _Body(self.written[Key]), "ContentLength": len(self.written[Key])}
        r = self.real.get_object(Bucket=Bucket, Key=Key, **kw)
        data = r["Body"].read()
        self.reads[Key] = data
        r["Body"] = _Body(data)
        return r

    def head_object(self, Bucket, Key, **kw):
        if Key in self.written:
            return {"ContentLength": len(self.written[Key]), "ETag": '"overlay"'}
        return self.real.head_object(Bucket=Bucket, Key=Key, **kw)

    def put_object(self, Bucket=None, Key=None, Body=b"", **kw):
        data = Body.read() if hasattr(Body, "read") else (Body.encode() if isinstance(Body, str) else bytes(Body))
        self.written[Key] = data
        self.deleted.discard(Key)
        return {"ETag": '"overlay"'}

    def delete_object(self, Bucket=None, Key=None, **kw):
        self.written.pop(Key, None)
        self.deleted.add(Key)
        return {}

    def delete_objects(self, Bucket=None, Delete=None, **kw):
        for o in (Delete or {}).get("Objects", []):
            self.delete_object(Key=o.get("Key"))
        return {}

    def copy_object(self, Bucket=None, Key=None, CopySource=None, **kw):
        src = CopySource.get("Key") if isinstance(CopySource, dict) else str(CopySource).split("/", 1)[-1]
        return self.put_object(Key=Key, Body=self.get_object(Bucket=Bucket, Key=src)["Body"].read())

    def upload_file(self, Filename, Bucket, Key, **kw):
        self.put_object(Key=Key, Body=Path(Filename).read_bytes())

    def download_file(self, Bucket, Key, Filename, **kw):
        Path(Filename).write_bytes(self.get_object(Bucket=Bucket, Key=Key)["Body"].read())

    def _merge(self, page, prefix):
        keys = {o["Key"] for o in page.get("Contents", [])}
        page["Contents"] = [o for o in page.get("Contents", []) if o["Key"] not in self.deleted] + [
            {"Key": k, "Size": len(v), "LastModified": datetime.now(timezone.utc)}
            for k, v in self.written.items() if k.startswith(prefix or "") and k not in keys]
        return page

    def list_objects_v2(self, **kw):
        return self._merge(self.real.list_objects_v2(**kw), kw.get("Prefix"))

    def get_paginator(self, name):
        outer = self

        class P:
            def paginate(_s, **kw):
                pages = list(outer.real.get_paginator(name).paginate(**kw)) or [{}]
                for i, page in enumerate(pages):
                    yield outer._merge(page, kw.get("Prefix")) if i == len(pages) - 1 else page
        return P()


def _frame_bytes(frame) -> bytes:
    f = frame.copy()
    for col in f.columns:
        if f[col].dtype == object:     # mixed text/number cells: keep each value's text
            f[col] = f[col].map(lambda v: None if v is None or (isinstance(v, float) and v != v) else str(v))
    buf = io.BytesIO()
    f.to_parquet(buf, index=False)
    return buf.getvalue()


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    study, out = sys.argv[1], sys.argv[2]
    sandbox = _sandbox_database()

    import src.aws_clients as aws
    real_client = aws.get_client("s3", region_name=__import__("src.config", fromlist=["x"]).settings.S3_REGION)
    s3 = OverlayS3(real_client)
    aws.get_client = lambda service, *a, **k: s3 if service == "s3" else real_client
    for modname in ("src.build_engine", "src.build_exec", "src.prep_engine", "src.sdtm_knowledge",
                    "src.biomedical_concepts", "src.program_gen", "src.trial_design_build"):
        m = __import__(modname, fromlist=["x"])
        if hasattr(m, "get_client"):
            m.get_client = aws.get_client
    from src import build_engine as ab
    from src import build_exec as be
    from src.config import settings
    from src.db import get_db
    b, db = settings.S3_BUCKET, get_db()

    st = db.execute("SELECT * FROM studies WHERE study_id = %s", (study,))
    if not st:
        raise SystemExit(f"no study {study} in the studio database")

    def keys(prefix, pattern):
        got = []
        for page in real_client.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=prefix):
            got += [o["Key"] for o in page.get("Contents", []) if re.fullmatch(pattern, o["Key"])]
        return got

    sid = re.escape(study)
    wanted = set(keys(f"synth/{study}/", rf"synth/{sid}/[^/]+\.parquet")
                 + keys(f"rawdata/{study}/", rf"rawdata/{sid}/[^/]+\.parquet")
                 + keys(f"prep/{study}/", rf"prep/{sid}/[^/]+\.parquet")
                 + keys(f"sdtm_build/{study}/", rf"sdtm_build/{sid}/[^/]+\.json"))

    tables = {}
    for t in STUDY_TABLES:
        order = " ORDER BY version DESC LIMIT 1" if t in ("mapping_run", "study_metadata") else ""
        try:
            tables[t] = db.execute(f"SELECT * FROM {t} WHERE study_id = %s{order}", (study,))
        except Exception:  # noqa: BLE001
            tables[t] = []
    latest = {}                                  # the newest build of each domain, not every one
    for r in sorted(tables["sdtm_output"], key=lambda r: str(r.get("created_at") or "")):
        latest[str(r.get("domain")).upper()] = {**r, "data_preview": None}
    tables["sdtm_output"] = list(latest.values())
    wanted |= {r["file_key"] for r in tables["sdtm_output"] if r.get("file_key")}
    domains = sorted(k.rsplit("/", 1)[1][:-5].upper() for k in wanted if k.startswith("sdtm_build/"))

    refs, report = {}, {"domain_build": {}, "auto_build": {}}
    # Domain Build first, on the study as it stands — nothing is saved
    for dom in domains:
        try:
            comp = be.compose_blocks(study, dom)
            pre = be._prep_frames(study, comp["prep_steps"], persist=False)
            res = be._run_domain(study, dom, comp["blocks"], preloaded=pre, settings=comp["settings"],
                                 row_limit=200, prep_steps=comp["prep_steps"])
            for name, frame in ((dom, res.get("df")), (f"SUPP{dom}", res.get("supp_df"))):
                if frame is not None and not (name.startswith("SUPP") and not len(frame)):
                    refs[f"reference/{name}.parquet"] = _frame_bytes(frame)
            report["domain_build"][dom] = {"ok": True, "rows": 0 if res.get("df") is None else len(res["df"])}
        except Exception as exc:  # noqa: BLE001
            report["domain_build"][dom] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"  Domain Build {dom}: {report['domain_build'][dom]}")

    # then Auto-Build, as its button runs it after prep design: every domain saved as it is built
    # (into the sandbox), each captured as it is saved
    built = {}
    real_persist = ab._persist

    def capture(study_id, domain, df):
        built[str(domain).upper()] = df.copy()
        return real_persist(study_id, domain, df)
    ab._persist = capture
    try:
        manifest = ab.build_all(study, persist=True)
    finally:
        ab._persist = real_persist
    for name, frame in built.items():
        refs[f"reference_autobuild/{name}.parquet"] = _frame_bytes(frame)
    report["auto_build"] = {"order": manifest.get("order"), "overall_score": manifest.get("overall_score"),
                            "domains": manifest.get("domains")}
    print(f"  Auto-Build: {len(built)} datasets, score {manifest.get('overall_score')}")

    # every study file the builds read must travel with the export
    standard = ("specs/", "reference/")
    read_study = {k for k in s3.reads if study in k and not k.startswith(standard)}
    wanted |= read_study
    unexpected = sorted(k for k in s3.reads if study not in k and not k.startswith(standard))
    if unexpected:
        raise SystemExit(f"the builds read objects that are neither the study's nor standards: {unexpected[:5]}")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for k in sorted(wanted):
            data = s3.reads.get(k)
            if data is None:
                try:
                    data = real_client.get_object(Bucket=b, Key=k)["Body"].read()
                except Exception as exc:  # noqa: BLE001
                    print(f"  skipped {k}: {exc}")
                    continue
            z.writestr(f"store/{k}", data)
        z.writestr("db.json.gz", gzip.compress(json.dumps(
            {"study_id": study, "tables": tables}, default=str).encode(), mtime=0))
        for name, data in sorted(refs.items()):
            z.writestr(name, data)
        z.writestr("manifest.json", json.dumps({
            "format": "sdtm-studio-study/2", "study_id": study,
            "study_name": st[0].get("study_name"), "protocol_number": st[0].get("protocol_number"),
            "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "domains": domains, "reference": report}, indent=1, default=str))
    shutil.rmtree(sandbox.parent, ignore_errors=True)
    print(f"exported {study} ({len(wanted)} objects, {len(domains)} domains) to {out}")


if __name__ == "__main__":
    main()
