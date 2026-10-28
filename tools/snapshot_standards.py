"""Copy the standards data the studio's build reads from S3 into ./standards, read-only.

Run it with the STUDIO's Python, which has the S3 access (Oversight never touches the network):

    cd /path/to/sdtm-studio/Container/sdtm_api
    .venv/bin/python /path/to/SDTM_Oversight_local/tools/snapshot_standards.py

The objects are stored under their S3 keys, as they are in S3, so the vendored engine reads them
through the local store unchanged. Standards only — never a study's data.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
OUT = Path(__file__).resolve().parents[1] / "standards"
PREFIXES = ["specs/core_spec.xlsx", "specs/core/", "specs/ta_spec", "reference/domain_knowledge.json",
            "reference/construction_standards", "reference/biomedical_concepts.json"]


def main() -> None:
    from src.build_engine import _s3
    from src.config import settings
    c, b = _s3(), settings.S3_BUCKET
    got = []
    for prefix in PREFIXES:
        for page in c.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=prefix):
            for o in page.get("Contents", []):
                data = c.get_object(Bucket=b, Key=o["Key"])["Body"].read()
                p = OUT / o["Key"]
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                got.append({"key": o["Key"], "bytes": len(data),
                            "s3_last_modified": o["LastModified"].isoformat()})
                print(f"  {o['Key']}  {len(data):,} bytes")
    (OUT / "SNAPSHOT.json").write_text(json.dumps({
        "taken": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bucket": b, "objects": got}, indent=1))
    print(f"{len(got)} objects into {OUT}")


if __name__ == "__main__":
    main()
