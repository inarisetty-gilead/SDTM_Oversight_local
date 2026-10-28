"""The one helper the engine uses from the studio's prep planner (which is not vendored: it
plans prep with AI). Copied from the studio's prep_orchestrator._step_inputs."""
from __future__ import annotations


def _step_inputs(st: dict) -> set:
    """Lowercase dataset names a prep step reads (`raw.` prefix stripped, as the engine tolerates it)."""
    p = st.get("params") or {}
    refs = [p.get("dataset"), p.get("left"), p.get("right"), *(p.get("datasets") or [])]
    refs += [s.get("dataset") for k in ("inputs", "sources") for s in (p.get(k) or []) if isinstance(s, dict)]
    return {r[4:] if r.startswith("raw.") else r for r in (str(x).strip().lower() for x in refs if x)}
