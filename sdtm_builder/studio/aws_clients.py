"""S3 for the vendored engine, as folders on this machine.

The engine reads and writes S3 keys (`synth/{study}/ae.parquet`, `sdtm_build/{study}/AE.json`,
`specs/core_spec.xlsx` …). Here a key is a file path under two layers: the study's working folder,
which every write goes to and which is read first, and the standards snapshot, which is read-only.
Only the calls the engine makes are implemented, with boto3's shapes; a missing key raises
ClientError(NoSuchKey) exactly as S3 does, so the engine's own fallbacks behave the same.
"""
from __future__ import annotations

import io
import shutil
from datetime import datetime, timezone
from pathlib import Path

_WORK: Path | None = None
_LAYERS: list[Path] = []


class ClientError(Exception):
    """botocore.exceptions.ClientError, as far as the engine looks at it."""

    def __init__(self, code: str, operation: str = "GetObject"):
        self.response = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": 404}}
        self.operation_name = operation
        super().__init__(f"An error occurred ({code}) when calling the {operation} operation")


def configure(work: Path, standards: Path) -> None:
    global _WORK, _LAYERS
    _WORK = Path(work)
    _WORK.mkdir(parents=True, exist_ok=True)
    _LAYERS = [_WORK, Path(standards)]


def _safe(key: str) -> str:
    k = str(key).lstrip("/")
    if ".." in Path(k).parts:
        raise ClientError("InvalidKey")
    return k


def _find(key: str) -> Path | None:
    k = _safe(key)
    for layer in _LAYERS:
        p = layer / k
        if p.is_file():
            return p
    return None


class _Body:
    def __init__(self, data: bytes):
        self._io = io.BytesIO(data)

    def read(self, *a):
        return self._io.read(*a)

    def iter_chunks(self, chunk_size=1024 * 1024):
        while True:
            b = self._io.read(chunk_size)
            if not b:
                return
            yield b

    def close(self):
        pass


def _meta(p: Path, key: str) -> dict:
    st = p.stat()
    return {"Key": key, "Size": st.st_size, "ETag": f'"{int(st.st_mtime_ns)}-{st.st_size}"',
            "LastModified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)}


class LocalS3:
    def _need(self):
        if _WORK is None:
            raise RuntimeError("the local store is not configured — call sdtm_builder.studio.configure()")

    def get_object(self, Bucket=None, Key=None, **kw):
        self._need()
        p = _find(Key)
        if p is None:
            raise ClientError("NoSuchKey", "GetObject")
        data = p.read_bytes()
        m = _meta(p, Key)
        return {"Body": _Body(data), "ContentLength": len(data), "ETag": m["ETag"],
                "LastModified": m["LastModified"]}

    def head_object(self, Bucket=None, Key=None, **kw):
        self._need()
        p = _find(Key)
        if p is None:
            raise ClientError("404", "HeadObject")
        m = _meta(p, Key)
        return {"ContentLength": m["Size"], "ETag": m["ETag"], "LastModified": m["LastModified"]}

    def put_object(self, Bucket=None, Key=None, Body=b"", **kw):
        self._need()
        p = _WORK / _safe(Key)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = Body.read() if hasattr(Body, "read") else (Body.encode() if isinstance(Body, str) else bytes(Body))
        p.write_bytes(data)
        return {"ETag": _meta(p, Key)["ETag"]}

    def upload_file(self, Filename, Bucket, Key, **kw):
        self.put_object(Key=Key, Body=Path(Filename).read_bytes())

    def download_file(self, Bucket, Key, Filename, **kw):
        p = _find(Key)
        if p is None:
            raise ClientError("404", "HeadObject")
        shutil.copyfile(p, Filename)

    def copy_object(self, Bucket=None, Key=None, CopySource=None, **kw):
        src = CopySource.get("Key") if isinstance(CopySource, dict) else str(CopySource).split("/", 1)[-1]
        p = _find(src)
        if p is None:
            raise ClientError("NoSuchKey", "CopyObject")
        return self.put_object(Key=Key, Body=p.read_bytes())

    def delete_object(self, Bucket=None, Key=None, **kw):
        self._need()
        p = _WORK / _safe(Key)               # the standards snapshot is never deleted from
        if p.is_file():
            p.unlink()
        return {}

    def delete_objects(self, Bucket=None, Delete=None, **kw):
        for o in (Delete or {}).get("Objects", []):
            self.delete_object(Key=o.get("Key"))
        return {"Deleted": (Delete or {}).get("Objects", [])}

    def _list(self, prefix: str) -> list[dict]:
        self._need()
        seen: dict[str, dict] = {}
        pre = _safe(prefix) if prefix else ""
        for layer in _LAYERS:
            if not layer.is_dir():
                continue
            for p in layer.rglob("*"):
                if p.is_file():
                    k = p.relative_to(layer).as_posix()
                    if k.startswith(pre) and k not in seen:
                        seen[k] = _meta(p, k)
        return [seen[k] for k in sorted(seen)]

    def list_objects_v2(self, Bucket=None, Prefix="", Delimiter=None, MaxKeys=None, **kw):
        items = self._list(Prefix or "")
        out = {"KeyCount": len(items), "IsTruncated": False, "Prefix": Prefix or ""}
        if Delimiter:
            pre = Prefix or ""
            dirs = sorted({pre + i["Key"][len(pre):].split(Delimiter, 1)[0] + Delimiter
                           for i in items if Delimiter in i["Key"][len(pre):]})
            items = [i for i in items if Delimiter not in i["Key"][len(pre):]]
            out["CommonPrefixes"] = [{"Prefix": d} for d in dirs]
        if items:
            out["Contents"] = items
        return out

    def get_paginator(self, name):
        client = self

        class _Pager:
            def paginate(self, **kw):
                yield client.list_objects_v2(**kw)
        return _Pager()


_CLIENT = LocalS3()


def get_client(service_name: str, region_name: str | None = None, config=None):
    if service_name != "s3":
        # no other AWS service — in particular no Bedrock: the build is deterministic here
        raise RuntimeError(f"'{service_name}' is not available offline")
    return _CLIENT


def get_resource(service_name: str, region_name: str | None = None):
    raise RuntimeError(f"'{service_name}' is not available offline")
