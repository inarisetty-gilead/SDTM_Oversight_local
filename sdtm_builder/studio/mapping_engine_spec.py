"""The one function the vendored engine uses from the studio's mapping engine (which is not
vendored: it is the AI mapping stage). A reference spec is a file in the local store."""
from __future__ import annotations


def _download_spec(key: str) -> str | None:
    """The local path of a reference spec (e.g. specs/ta_spec_onc.xlsx), or None if absent."""
    from .aws_clients import _find
    p = _find(key)
    return str(p) if p else None
