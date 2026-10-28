"""Export a study from SDTM Studio, as the studio holds it, for Oversight to import.

Run it with the studio's Python (it reads the studio's S3 and database; it writes nothing there):

    cd /path/to/sdtm-studio/Container/sdtm_api
    .venv/bin/python /path/to/SDTM_Oversight_local/tools/export_studio_study.py STUDY_ID OUT.zip

The zip holds everything the studio's Domain Build reads for that study, under the same keys:
  store/synth/{study}/*.parquet, store/rawdata/{study}/*.parquet   the raw forms
  store/prep/{study}/*.parquet                                     saved prep outputs
  store/sdtm_build/{study}/{DOM}.json                              saved Domain Build edits,
                                                                   settings and prep steps
  store/sdtm/output/{study}/…                                      the newest built domains
  db.json.gz                                                       the study's database rows
  reference/{DOM}.parquet                                          what the studio's own Domain
                                                                   Build produces now — Oversight
                                                                   reports whether it agrees
  manifest.json
Standards are not included: Oversight carries its own snapshot (./standards).

A real study's raw data is subject data. Keep the zip on this machine — out of cloud-synced
folders, e-mail and the code repository — exactly as Oversight's own runs/ and studies/.
"""
from __future__ import annotations

import gzip
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, ".")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    study, out = sys.argv[1], sys.argv[2]
    from src import build_exec as be
    from src.build_engine import _s3
    from src.config import settings
    from src.db import get_db
    c, b, db = _s3(), settings.S3_BUCKET, get_db()

    st = db.execute("SELECT * FROM studies WHERE study_id = %s", (study,))
    if not st:
        raise SystemExit(f"no study {study} in the studio database")

    def keys(prefix, pattern):
        got = []
        for page in c.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=prefix):
            got += [o["Key"] for o in page.get("Contents", []) if re.fullmatch(pattern, o["Key"])]
        return got

    sid = re.escape(study)
    wanted = (keys(f"synth/{study}/", rf"synth/{sid}/[^/]+\.parquet")
              + keys(f"rawdata/{study}/", rf"rawdata/{sid}/[^/]+\.parquet")
              + keys(f"prep/{study}/", rf"prep/{sid}/[^/]+\.parquet")
              + keys(f"sdtm_build/{study}/", rf"sdtm_build/{sid}/[^/]+\.json"))

    tables = {}
    for t in ("studies", "mapping_run", "study_metadata", "sdtm_output", "study_overrides"):
        order = " ORDER BY version DESC LIMIT 1" if t in ("mapping_run", "study_metadata") else ""
        try:
            tables[t] = db.execute(f"SELECT * FROM {t} WHERE study_id = %s{order}", (study,))
        except Exception:  # noqa: BLE001
            tables[t] = []
    latest = {}                                  # the newest build of each domain, not every one
    for r in sorted(tables["sdtm_output"], key=lambda r: str(r.get("created_at") or "")):
        latest[str(r.get("domain")).upper()] = {**r, "data_preview": None}
    tables["sdtm_output"] = list(latest.values())
    wanted += [r["file_key"] for r in tables["sdtm_output"] if r.get("file_key")]

    domains = sorted(k.rsplit("/", 1)[1][:-5].upper() for k in wanted if k.startswith("sdtm_build/"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for k in sorted(set(wanted)):
            try:
                z.writestr(f"store/{k}", c.get_object(Bucket=b, Key=k)["Body"].read())
            except Exception as exc:  # noqa: BLE001
                print(f"  skipped {k}: {exc}")
        z.writestr("db.json.gz", gzip.compress(json.dumps(
            {"study_id": study, "tables": tables}, default=str).encode(), mtime=0))
        report = {}
        for dom in domains:              # the studio's own Domain Build, now — nothing is saved
            try:
                comp = be.compose_blocks(study, dom)
                pre = be._prep_frames(study, comp["prep_steps"], persist=False)
                res = be._run_domain(study, dom, comp["blocks"], preloaded=pre, settings=comp["settings"],
                                     row_limit=200, prep_steps=comp["prep_steps"])
                for name, frame in ((dom, res.get("df")), (f"SUPP{dom}", res.get("supp_df"))):
                    if frame is None or (name.startswith("SUPP") and not len(frame)):
                        continue
                    f = frame.copy()
                    for col in f.columns:
                        if f[col].dtype == object:
                            f[col] = f[col].map(lambda v: None if v is None or (isinstance(v, float) and v != v)
                                                else str(v))
                    buf = io.BytesIO()
                    f.to_parquet(buf, index=False)
                    z.writestr(f"reference/{name}.parquet", buf.getvalue())
                report[dom] = {"ok": True, "rows": 0 if res.get("df") is None else int(len(res["df"]))}
            except Exception as exc:  # noqa: BLE001
                report[dom] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(f"  {dom}: {report[dom]}")
        z.writestr("manifest.json", json.dumps({
            "format": "sdtm-studio-study/1", "study_id": study,
            "study_name": st[0].get("study_name"), "protocol_number": st[0].get("protocol_number"),
            "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "domains": domains, "reference": report}, indent=1, default=str))
    print(f"exported {study} ({len(set(wanted))} objects, {len(domains)} domains) to {out}")


if __name__ == "__main__":
    main()
