"""What the vendored engine uses from the studio's mapping engine (which is not vendored: it is the
AI mapping stage): a reference spec's local path, and the TOC builder (copied unchanged into
mapping_engine_spec_parts by tools/vendor_studio.py)."""
from __future__ import annotations

from .mapping_engine_spec_parts import _DOMAIN_LABELS, _build_toc, _is_dropped_qnam  # noqa: F401


def _download_spec(key: str) -> str | None:
    """The local path of a reference spec (e.g. specs/ta_spec_onc.xlsx), or None if absent."""
    from .aws_clients import _find
    p = _find(key)
    return str(p) if p else None
