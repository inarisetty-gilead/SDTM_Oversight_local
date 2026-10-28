# Vendored from sdtm-studio sdtm_api/src/sdtm_knowledge.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""Shared SDTM construction knowledge — the CDISC-IG + company standard derivations the app KNOWS,
so the build applies them automatically (the user should never have to hand-type a standard rule like
"DTHFL = 'Y' when the subject died"). Self-contained (no build_engine / build_exec / build_blueprint
import) so BOTH build paths and the Build Blueprint can use it without a circular dependency.

Two sources feed it, KB winning:
  • a small built-in table of UNIVERSAL CDISC standard derivations every study shares (_STANDARD);
  • per-domain `derivation` objects in reference/domain_knowledge.json (company/therapeutic overrides).

A derivation is expressed in the same shape the build already runs (a `cond` recipe): a list of
{when: "<condition>", value: "<literal|COLUMN>"} rules plus an else, referencing SDTM variables the
spec already builds (e.g. DTHFL reads the built DTHDTC), so it is study-agnostic.
"""
from __future__ import annotations

import contextvars
import copy
import json
import re

from .aws_clients import ClientError

from .aws_clients import get_client
from .config import settings

# ── condition parser + value compiler (single source of truth, imported by build_blueprint too) ──
CANON_OP = {"=": "eq", "==": "eq", "eq": "eq", "!=": "ne", "<>": "ne", "ne": "ne",
            ">=": "ge", "<=": "le", ">": "gt", "<": "lt", "ge": "ge", "le": "le",
            "gt": "gt", "lt": "lt", "in": "in", "notin": "notin", "not in": "notin",
            "contains": "contains", "starts": "starts", "ends": "ends",
            "missing": "missing", "notmissing": "notmissing"}
# canonical op → prep_engine._prep_cond_mask operator
PREP_OP = {"eq": "==", "ne": "!=", "in": "in", "notin": "notin", "contains": "contains",
           "missing": "missing", "notmissing": "notmissing"}
_MISS_RE = re.compile(r"^([A-Za-z0-9_.]+)\s+(?:is\s+)?(?:not\s*missing|notmissing|not\s+null|present|populated|non-?missing)\b", re.I)
_NULL_RE = re.compile(r"^([A-Za-z0-9_.]+)\s+(?:is\s+)?(?:missing|null|blank|empty)\b", re.I)
_OP_RE = re.compile(r"^([A-Za-z0-9_.]+)\s*(==|!=|<>|>=|<=|=|>|<|not\s+in|notin|\bin\b|\beq\b|\bne\b|\bcontains\b|\bstarts\b|\bends\b)\s*(.+)$", re.I)


def bare_col(tok: str) -> str:
    """A condition/value token → bare UPPER column name (strips a raw.<ds>. prefix)."""
    t = str(tok or "").strip().strip("'\"")
    m = re.match(r"(?:raw\.)?([a-z0-9_]+)\.([a-z0-9_]+)$", t, re.I)
    return (m.group(2) if m else t).upper()


def parse_condition(text: str):
    """Parse ONE condition clause → canonical {column, op, value}, or None for else/unparseable."""
    t = str(text or "").strip().replace("≠", "!=").replace("≥", ">=").replace("≤", "<=")
    if not t or t.lower() in ("else", "otherwise", "default", "-", "—"):
        return None
    m = _MISS_RE.match(t)
    if m:
        return {"column": bare_col(m.group(1)), "op": "notmissing", "value": ""}
    m = _NULL_RE.match(t)
    if m:
        return {"column": bare_col(m.group(1)), "op": "missing", "value": ""}
    m = _OP_RE.match(t)
    if m:
        op = CANON_OP.get(m.group(2).lower().strip().replace("  ", " "), "eq")
        return {"column": bare_col(m.group(1)), "op": op, "value": m.group(3).strip().strip("'\"")}
    return None


def split_conds(cond_text: str) -> list:
    """A condition cell may hold several ANDed clauses joined by ';' → list of canonical conds."""
    return [pc for part in re.split(r"\s*;\s*", str(cond_text or "")) if (pc := parse_condition(part))]


def value_result(val) -> dict:
    """A value → a build_exec `cond` result dict (text literal / missing / a same-row var reference)."""
    if val is None:
        return {"kind": "missing"}
    v = str(val).strip()
    if not v or v.lower() in ("missing", "null", "blank", "(missing)", "none"):
        return {"kind": "missing"}
    # A raw.<ds>.<col> result is a COLUMN COPY (assign that raw column), not a literal — so a curated
    # rule like "when X = A → raw.y.z" builds as a real column read in both engines, matching the
    # Domain Program's generated code.
    m = re.match(r"raw\.([a-z0-9_]+)\.([a-z0-9_]+)$", v, re.I)
    if m:
        return {"kind": "column", "dataset": m.group(1).lower(), "column": m.group(2).upper()}
    m = re.match(r"(?:upper|upcase|lower|lowcase|strip|trim)\(\s*([A-Za-z0-9_.]+)\s*\)$", v, re.I)
    if m:
        return {"kind": "var", "var": bare_col(m.group(1))}
    return {"kind": "text", "text": v}


def cond_src(column: str, spec_vars: set, source: str = "") -> dict:
    """Resolve a condition column → a build_exec source dict. A bare token that is a SDTM variable of
    THIS domain reads the built column (kind=var, row-aligned); raw.<ds>.<col> reads that raw column."""
    raw = re.match(r"(?:raw\.)?([a-z0-9_]+)\.([a-z0-9_]+)$", str(column or ""), re.I)
    if raw and raw.group(1).lower() != str(source or "").lower():
        return {"kind": "column", "dataset": raw.group(1).lower(), "column": raw.group(2).upper()}
    col = bare_col(column)
    if not spec_vars or col in spec_vars:
        return {"kind": "var", "var": col}
    if source:
        return {"kind": "column", "dataset": str(source).lower(), "column": col}
    return {"kind": "var", "var": col}


# ── The standard-derivation catalogue ────────────────────────────────────────────────────────
# UNIVERSAL CDISC standard derivations, keyed by domain → variable. Every study shares these; the
# build applies them with zero user authoring. Extend this table (and/or add `derivation` objects to
# domain_knowledge.json) as more standards are captured.
_STANDARD: dict = {
    "DM": {
        "DTHFL": {"recipe": "cond",
                  "rules": [{"source": "raw.death.DEATHDAT", "when": "DTHDTC not missing",
                             "value": "Y", "ref": "SDTMIG v3.4 §DM · Gilead IG-CORE"}], "else": None,
                  "rationale": "DTHFL = 'Y' when the subject died (a death date is recorded); else null."},
    },
    # DS is assembled per source by the Prep pipeline (consents / enrollment / study-drug completion),
    # so these standards are REFERENCE-ONLY documentation of the per-source mapping — shown in the
    # library, but the build uses the prep branches (never applied as a single condition).
    "DS": {
        "DSDECOD": {"reference_only": True,
                    "rules": [
                        {"source": "raw.consents.CONSYN", "when": "= Yes", "value": "INFORMED CONSENT OBTAINED", "ref": "Gilead IG-CORE §DS · consents"},
                        {"source": "raw.consents.CONSYN", "when": "= No", "value": "WITHDRAWAL OF CONSENT", "ref": "Gilead IG-CORE §DS · consents"},
                        {"source": "raw.enroll.ENROLLYN", "when": "= Yes", "value": "RANDOMIZED", "ref": "Gilead IG-CORE §DS · enroll"},
                        {"source": "raw.enroll.ENROLLYN", "when": "= No", "value": "SCREEN FAILURE DESPITE MEETING ALL INCLUSION/EXCLUSION CRITERIA", "ref": "Gilead IG-CORE §DS · enroll"},
                        {"source": "raw.sdrgcomp.SDRGREAS", "when": "not missing", "value": "study-drug disposition (mapped from SDRGREAS to CT)", "ref": "Gilead IG-CORE §DS · sdrgcomp"}],
                    "else": None,
                    "rationale": "Standardized disposition term; DSDECOD codelist depends on DSCAT (protocol-milestone vs disposition-event terms). One record per disposition status / protocol milestone per subject."},
        "DSCAT": {"reference_only": True,
                  "rules": [
                      {"source": "raw.consents.CONSYN", "when": "= Yes", "value": "PROTOCOL MILESTONE", "ref": "consents"},
                      {"source": "raw.consents.CONSYN", "when": "= No", "value": "DISPOSITION EVENT", "ref": "consents"},
                      {"source": "raw.enroll.ENROLLYN", "when": "= Yes", "value": "PROTOCOL MILESTONE", "ref": "enroll"},
                      {"source": "raw.enroll.ENROLLYN", "when": "= No", "value": "DISPOSITION EVENT", "ref": "enroll"},
                      {"source": "raw.sdrgcomp", "when": "", "value": "DISPOSITION EVENT", "ref": "sdrgcomp"}],
                  "else": None,
                  "rationale": "DSCAT = DISPOSITION EVENT | PROTOCOL MILESTONE | OTHER EVENT, by the kind of milestone the source records."},
        "DSTERM": {"reference_only": True,
                   "rules": [
                       {"source": "raw.consents", "when": "", "value": "verbatim consent term (e.g. INFORMED CONSENT OBTAINED / WITHDRAWN)", "ref": "consents"},
                       {"source": "raw.enroll.SFREAS", "when": "not missing", "value": "screen-failure reason (verbatim; SFREASSP when 'Other')", "ref": "enroll"},
                       {"source": "raw.sdrgcomp.SDRGREAS", "when": "not missing", "value": "study-drug completion reason (verbatim)", "ref": "sdrgcomp"}],
                   "else": None,
                   "rationale": "Reported disposition term, verbatim from the source milestone."},
    },
}

_KB_CACHE = None


def _s3():
    return get_client("s3", region_name=settings.S3_REGION)


def _kb() -> dict:
    global _KB_CACHE
    if _KB_CACHE is None:
        try:
            obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key="reference/domain_knowledge.json")
            _KB_CACHE = json.loads(obj["Body"].read())
        except Exception:  # noqa: BLE001
            _KB_CACHE = {}
    return _KB_CACHE


# ── Curated store: the GCDS-editable Construction Standards library (S3), VERSIONED ────────────
# The human-owned overlay on the built-in table + KB. This is what the "Construction Standards"
# Reference view reads and writes, so the standards team evolves the derivations WITHOUT a code
# change — and a save takes effect in the very next build (both engines call domain_derivations).
#
# Standards change on a yearly cadence (new Gilead IG-CORE / SDTMIG / TA releases, plus mid-cycle
# provisional amendments), so the store is VERSIONED: each release is an immutable snapshot anchored
# to the standard it implements. A build resolves rules from a chosen version (default = current
# released), so an old study rebuilt years later reproduces the same construction.
#
#   reference/construction_standards/index.json      → catalog {current, versions:[…]}
#   reference/construction_standards/{id}.json       → one immutable-once-released snapshot
#   reference/construction_standards.json            → legacy single store (migrated on first use)
_STORE_PREFIX = "reference/construction_standards/"
_INDEX_KEY = _STORE_PREFIX + "index.json"
_LEGACY_KEY = "reference/construction_standards.json"
_SEED_VERSION = "IGCORE-SDTMIG-3.4"          # id of the version migrated from the legacy store
_EDITABLE = {"draft", "provisional"}          # released/superseded/retired snapshots are immutable
_VERSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_INDEX_CACHE = None
_STORE_CACHE: dict = {}                        # {version_id: snapshot doc}


def _version_key(vid: str) -> str:
    return f"{_STORE_PREFIX}{vid}.json"


def _put_json(key: str, doc: dict) -> None:
    _s3().put_object(Bucket=settings.S3_BUCKET, Key=key, Body=json.dumps(doc, default=str).encode())


def _get_json(key: str):
    obj = _s3().get_object(Bucket=settings.S3_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def _get_json_if_exists(key: str):
    try:
        doc = _get_json(key)
        if doc is None:
            raise ValueError(f"Construction standards object {key} contains null, not a document")
        return doc
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("NoSuchKey", "404", "NotFound"):
            raise
        return None


def _ensure_index() -> dict:
    """Load the version catalog, creating it on first use — migrating the legacy single-file store
    into an initial version so no curated content is lost."""
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE
    idx = _get_json_if_exists(_INDEX_KEY)
    if idx is not None:
        if not isinstance(idx, dict) or not isinstance(idx.get("versions"), list) or not idx["versions"]:
            raise ValueError("Construction standards catalog is invalid; refusing to overwrite it")
        _INDEX_CACHE = idx
        return idx
    # First run → migrate. Seed one version from the legacy store (or empty) as the current draft.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # A previous attempt may have saved the snapshot but failed to save the catalog.
    snap = _get_json_if_exists(_version_key(_SEED_VERSION))
    if snap is None:
        legacy = _get_json_if_exists(_LEGACY_KEY)
        if legacy is not None and (not isinstance(legacy, dict)
                                   or not isinstance(legacy.get("domains", {}), dict)):
            raise ValueError("Legacy construction standards are invalid; refusing to migrate")
        domains = (legacy or {}).get("domains", {})
        snap = {"id": _SEED_VERSION, "base_standard": "Gilead IG-CORE / SDTMIG v3.4",
                "status": "draft", "domains": domains, "rev": 1, "updated_at": now}
        _put_json(_version_key(_SEED_VERSION), snap)
    elif (not isinstance(snap, dict) or snap.get("id") != _SEED_VERSION
          or not isinstance(snap.get("domains"), dict)):
        raise ValueError("Construction standards seed is invalid; refusing to overwrite it")
    idx = {"current": _SEED_VERSION, "versions": [{
        "id": _SEED_VERSION, "base_standard": snap["base_standard"], "status": snap["status"],
        "effective_date": None, "source_docs": [], "notes": "Migrated from the pre-versioning store.",
        "created_by": "system", "created_at": snap.get("updated_at", now),
        "released_at": snap.get("released_at"), "rev": snap["rev"]}]}
    _put_json(_INDEX_KEY, idx)
    _INDEX_CACHE = idx
    return idx


def _index_entry(idx: dict, vid: str) -> dict:
    return next((v for v in idx.get("versions", []) if v.get("id") == vid), None)


# The standards version a build resolves against — set once at the build entry point so every
# nested domain_derivations()/curated_derivations() call uses it without threading a param through
# the whole engine. None → the current released version.
_ACTIVE_VERSION: contextvars.ContextVar = contextvars.ContextVar("standards_version", default=None)


def set_active_version(vid: str | None):
    """Set the standards version for the current context (build run). Returns the ContextVar token
    so the caller can reset it. Pass None to clear (fall back to current released)."""
    return _ACTIVE_VERSION.set(vid or None)


def reset_active_version(token) -> None:
    try:
        _ACTIVE_VERSION.reset(token)
    except Exception:  # noqa: BLE001
        pass


def _resolve_version(version: str | None) -> str:
    idx = _ensure_index()
    return version or _ACTIVE_VERSION.get() or idx.get("current") or _SEED_VERSION


def _load_store(version: str | None = None) -> dict:
    """The snapshot doc for a version (default = current). Cached per version."""
    vid = _resolve_version(version)
    if vid not in _STORE_CACHE:
        try:
            doc = _get_json(_version_key(vid))
            _STORE_CACHE[vid] = doc if isinstance(doc, dict) else {}
        except Exception:  # noqa: BLE001
            _STORE_CACHE[vid] = {}
    return _STORE_CACHE[vid]


def _save_store(doc: dict, version: str | None = None) -> None:
    vid = _resolve_version(version)
    doc["id"] = vid
    _put_json(_version_key(vid), doc)
    _STORE_CACHE[vid] = doc


def _store_domains(doc: dict = None) -> dict:
    return (doc if doc is not None else _load_store()).get("domains", {}) or {}


def curated_derivations(domain: str, version: str | None = None) -> dict:
    """{VAR: derivation-spec} the standards team has curated for a domain (from the version snapshot)."""
    dom = str(domain).upper()
    out = {}
    for var, rec in (_store_domains(_load_store(version)).get(dom, {}) or {}).items():
        if isinstance(rec, dict) and rec.get("rules"):
            out[str(var).upper()] = rec
    return out


# ── Version catalog API (used by the Construction Standards view + build version picker) ───────
def list_versions() -> dict:
    """The version catalog: {current, versions:[…]} (newest activity first)."""
    idx = _ensure_index()
    return {"current": idx.get("current"),
            "versions": sorted(idx.get("versions", []),
                               key=lambda v: v.get("created_at") or "", reverse=True)}


def current_version() -> str:
    return _ensure_index().get("current") or _SEED_VERSION


def version_editable(vid: str) -> bool:
    e = _index_entry(_ensure_index(), vid)
    return bool(e and e.get("status") in _EDITABLE)


def create_version(new_id: str, base_standard: str = "", clone_from: str | None = None,
                   status: str = "draft", notes: str = "", source_docs: list | None = None,
                   user_email: str = "") -> dict:
    """Start a new standards version — cloning an existing snapshot (typically the current released
    one) so each yearly/provisional release begins from the last and only the deltas are curated."""
    from datetime import datetime, timezone
    vid = str(new_id).strip()
    if not _VERSION_ID_RE.match(vid):
        raise ValueError("version id must be letters/digits/._- (e.g. IGCORE-2026 or IGCORE-2026-P1)")
    if status not in _EDITABLE:
        raise ValueError("a new version starts as 'draft' or 'provisional'")
    idx = _ensure_index()
    if _index_entry(idx, vid):
        raise ValueError(f"version {vid!r} already exists")
    src = _load_store(clone_from) if clone_from else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Deep-copy the cloned content — a shallow copy would share the per-domain/per-variable dicts with
    # the source version's cache, so editing this new draft would mutate the source version in memory.
    snap = {"id": vid, "base_standard": base_standard or (src.get("base_standard") if clone_from else ""),
            "status": status, "domains": copy.deepcopy(src.get("domains", {}) or {}),
            "cloned_from": clone_from, "rev": 1, "updated_at": now}
    _put_json(_version_key(vid), snap)
    _STORE_CACHE[vid] = snap
    entry = {"id": vid, "base_standard": snap["base_standard"], "status": status,
             "effective_date": None, "source_docs": source_docs or [], "notes": notes,
             "cloned_from": clone_from, "created_by": user_email, "created_at": now,
             "released_at": None, "rev": 1}
    idx.setdefault("versions", []).append(entry)
    _put_json(_INDEX_KEY, idx)
    global _INDEX_CACHE
    _INDEX_CACHE = idx
    return entry


def release_version(vid: str, effective_date: str | None = None, user_email: str = "") -> dict:
    """Freeze a draft/provisional version as released and make it the default builds resolve to.
    The previously-current released version is marked superseded (kept, immutable, still selectable)."""
    from datetime import datetime, timezone
    idx = _ensure_index()
    entry = _index_entry(idx, vid)
    if not entry:
        raise ValueError(f"unknown version {vid!r}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev = idx.get("current")
    if prev and prev != vid:
        pe = _index_entry(idx, prev)
        if pe and pe.get("status") == "released":
            pe["status"] = "superseded"
    entry["status"] = "released"
    entry["released_at"] = now
    entry["effective_date"] = effective_date or entry.get("effective_date")
    entry["released_by"] = user_email
    idx["current"] = vid
    snap = _load_store(vid)
    snap["status"] = "released"
    _put_json(_version_key(vid), snap)
    _STORE_CACHE[vid] = snap
    _put_json(_INDEX_KEY, idx)
    global _INDEX_CACHE
    _INDEX_CACHE = idx
    return entry


def diff_versions(a: str, b: str) -> dict:
    """Per-domain/variable added / changed / removed rules between two versions — for release
    review and for impact analysis on studies built against the older version."""
    da, db = _store_domains(_load_store(a)), _store_domains(_load_store(b))
    added, changed, removed = [], [], []
    for dom in sorted(set(da) | set(db)):
        va, vb = da.get(dom, {}) or {}, db.get(dom, {}) or {}
        for var in sorted(set(va) | set(vb)):
            ra, rb = va.get(var), vb.get(var)
            if ra and not rb:
                removed.append(f"{dom}.{var}")
            elif rb and not ra:
                added.append(f"{dom}.{var}")
            elif ra and rb and json.dumps(ra.get("rules"), sort_keys=True, default=str) != \
                    json.dumps(rb.get("rules"), sort_keys=True, default=str):
                changed.append(f"{dom}.{var}")
    return {"from": a, "to": b, "added": added, "changed": changed, "removed": removed,
            "counts": {"added": len(added), "changed": len(changed), "removed": len(removed)}}


def domain_derivations(domain: str, include_reference: bool = False,
                       version: str | None = None) -> dict:
    """{VAR: derivation-spec} for a domain, by precedence: the built-in standard table (seed) < a KB
    `derivation` object < the GCDS-curated store (wins). By default REFERENCE-ONLY entries (documented
    but assembled by Prep, e.g. DS per-source mappings) are EXCLUDED — the build must never apply them
    as a single condition. Pass include_reference=True for the browse/display path."""
    dom = str(domain).upper()
    out = dict(_STANDARD.get(dom, {}))
    for v in (_kb().get(dom, {}) or {}).get("variables", []):
        d = v.get("derivation")
        if isinstance(d, dict) and d.get("rules"):
            out[str(v.get("variable", "")).upper()] = d
    out.update(curated_derivations(dom, version))
    if not include_reference:
        out = {k: d for k, d in out.items() if not d.get("reference_only")}
    return out


def _deriv_origin(dom: str, var: str, version: str | None = None) -> str:
    """Where a variable's EFFECTIVE derivation comes from — curated (human) / knowledge (KB) / built-in."""
    if var in curated_derivations(dom, version):
        return "curated"
    for v in (_kb().get(dom, {}) or {}).get("variables", []):
        if str(v.get("variable", "")).upper() == var and isinstance(v.get("derivation"), dict):
            return "knowledge"
    return "built-in" if var in _STANDARD.get(dom, {}) else ""


def _validate_deriv(deriv: dict):
    """Ensure a derivation compiles to at least one executable rule (else the save is rejected)."""
    args, _ = compile_cond_args(deriv or {}, set())
    return bool(args.get("rules"))


def _rule_condition(r: dict):
    """Canonical {column, op, value} for a rule. A rule may carry its condition wholly in `when`
    ("DTHDTC not missing") OR give the input column via `source` (a raw origin like
    "raw.death.DEATHDAT") plus just the test in `when` ("not missing"). Returns None if unparseable.
    NOTE: `source` is the DISPLAYED raw origin — it never forces a cross-dataset read; the executable
    column is taken from `when`/`source`'s column so a flag keeps reading the row-aligned SDTM var."""
    when = str(r.get("when", "")).strip()
    col = bare_col(r.get("source", "")) if r.get("source") else ""
    # When a raw source gives the column, combine it with the test FIRST — so a bare test like
    # "not missing" attaches to that column instead of being mis-read (e.g. column "not").
    if col and when:
        pc = parse_condition(f"{col} {when}")
        if pc:
            return pc
    return parse_condition(when)


def compile_cond_args(deriv: dict, spec_vars: set):
    """A derivation-spec → (build_exec `cond` args, reads_var). Reused by both build engines and the
    Build Blueprint so a standard rule executes identically everywhere."""
    rules, reads_var = [], False
    for r in deriv.get("rules", []) or []:
        pc = _rule_condition(r)
        if not pc:
            continue
        src = cond_src(pc["column"], spec_vars)
        reads_var = reads_var or src.get("kind") == "var"
        rules.append({"src": src, "op": pc["op"], "value": pc.get("value", ""),
                      "then": value_result(r.get("value"))})
    return {"rules": rules, "else": value_result(deriv.get("else"))}, reads_var


# ── Construction Standards browse + curate (drives the Reference view) ────────────────────────
def _kb_var(dom: str, var: str) -> dict:
    for v in (_kb().get(dom, {}) or {}).get("variables", []):
        if str(v.get("variable", "")).upper() == var:
            return v
    return {}


# ── Gilead IG-CORE reference spec (specs/core/{DOMAIN}.parquet) — the authoritative construction ──
# source. Every variable of every domain carries its raw sources (Input Variables), Gilead Core, and
# the Gilead Mapping Rule. SDTM IG is only background knowledge; the citation is Gilead IG-CORE / TA.
_SPEC_CACHE: dict = {}
_SPEC_DOMAINS = None
# spec-workbook sheets that match the domain-code shape but are NOT SDTM domains
_NON_DOMAINS = {"TOC", "TS0", "POOL"}
_RAW_TOK = re.compile(r"raw\.\s*([a-z0-9_]+)\s*\.\s*([a-z0-9_]+)", re.I)


def _core_spec_rows(domain: str) -> list:
    dom = str(domain).upper()
    if dom in _SPEC_CACHE:
        return _SPEC_CACHE[dom]
    rows = []
    try:
        import io
        import pandas as pd
        body = _s3().get_object(Bucket=settings.S3_BUCKET, Key=f"specs/core/{dom}.parquet")["Body"].read()
        rows = pd.read_parquet(io.BytesIO(body)).to_dict("records")
    except Exception:  # noqa: BLE001
        rows = []
    _SPEC_CACHE[dom] = rows
    return rows


def _spec_domains() -> list:
    global _SPEC_DOMAINS
    if _SPEC_DOMAINS is None:
        doms = []
        try:
            paginator = _s3().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=settings.S3_BUCKET, Prefix="specs/core/"):
                for o in page.get("Contents", []):
                    name = o["Key"].split("/")[-1]
                    if name.endswith(".parquet"):
                        base = name[:-8]
                        # a real domain code, not a spec sheet (Codelist/Study/TOC/Maintenance/…)
                        if re.fullmatch(r"[A-Z]{2,4}", base) and base not in _NON_DOMAINS:
                            doms.append(base)
        except Exception:  # noqa: BLE001
            pass
        _SPEC_DOMAINS = sorted(set(doms))
    return _SPEC_DOMAINS


def _spec_raw_sources(iv) -> list:
    """The raw.dataset.column tokens named in a spec row's Input Variables, de-duplicated in order."""
    out, seen = [], set()
    for ds, col in _RAW_TOK.findall(str(iv or "")):
        tok = f"raw.{ds.lower()}.{col.lower()}"
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _clean_rule(text: str) -> str:
    """The Gilead Mapping Rule with whitespace collapsed (the frontend truncates + shows full on hover)."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _extract_rule_rows(var: str, domain: str, rule: str) -> list:
    """Parse a Gilead Mapping Rule's conditional logic — `If <cond> [then|;] _VAR = <val>` and
    `… _VAR is null` — into structured {source, cond, value} rows, flattening the merge-program `_VAR`
    into the actual construction. This is how the app captures the two-step xx-merge + xx.sas as ONE
    set of raw→condition→value rules, so Auto-Build / Domain Build can construct a study directly."""
    v = re.escape(var)
    dom = str(domain).lower()
    lines = [ln.strip() for ln in re.split(r"\n", str(rule or "")) if ln.strip()]
    merged, i = [], 0
    while i < len(lines):
        ln = lines[i]
        bare_if = re.match(r"if\b", ln, re.I) and "=" not in ln and " null" not in ln.lower()
        if bare_if and i + 1 < len(lines):
            merged.append(ln.rstrip(";:, ") + " then " + lines[i + 1]); i += 2
        else:
            merged.append(ln); i += 1
    rows = []
    for ln in merged:
        low = ln.lower()
        if ("_" + var.lower()) not in low and var.lower() not in low:
            continue
        m = re.search(r"\bif\s+(?P<cond>.+?)[,;]?\s*(?:then\s+)?_?" + v +
                      r"\s*(?:=\s*(?P<val>.+?)\s*$|is\s+null\b)", ln, re.I)
        if not m:
            continue
        cond = re.sub(r"\s+", " ", m.group("cond")).strip().strip(",;:. ")
        cond = re.sub(r"^note\s*:\s*", "", cond, flags=re.I)
        cond = re.sub(r"\bor\s+if\b", "or", cond, flags=re.I).strip()
        val = re.sub(r"\s+", " ", m.group("val")).strip().strip(",;. ") if m.groupdict().get("val") else ""
        src = ""
        mfrom = re.search(r"record\s+is\s+from\s+([a-z0-9_]+)", cond, re.I)
        mraw = re.search(r"raw\.\s*([a-z0-9_]+)\s*\.\s*([a-z0-9_]+)", cond, re.I)
        if mfrom:
            src, cond = f"raw.{mfrom.group(1).lower()}", ""
        elif mraw:                                   # the condition names a raw.<ds>.<col> — use it as the source
            src = f"raw.{mraw.group(1).lower()}.{mraw.group(2).lower()}"
            cond = re.sub(r"raw\.\s*[a-z0-9_]+\s*\.\s*[a-z0-9_]+\s*", "", cond, flags=re.I).strip() or "present"
        else:
            mcol = re.match(r"_?([a-z][a-z0-9_]*)\b", cond, re.I)
            if mcol and mcol.group(1).lower() not in ("your", "the", "both", "only", "record", "a", "an", "raw", "if"):
                src = f"raw.{dom}.{mcol.group(1).lower().lstrip('_')}"
        # value that is a bare raw.<ds>.<col> → keep just the column; strip a leading 'strip('/'upcase('
        val = re.sub(r"raw\.\s*[a-z0-9_]+\s*\.\s*([a-z0-9_]+)", r"\1", val, flags=re.I)
        rows.append({"source": src, "cond": cond, "value": val, "ref": "Gilead IG-CORE"})
    return rows


_REFDATE = {
    "RFSTDTC": "earliest study-treatment start — study Day 1 per SAP (min of exposure start dates)",
    "RFENDTC": "latest study-participation / end-of-study date (max of the relevant end dates)",
    "RFXSTDTC": "first study-treatment exposure start — min of EX start dates",
    "RFXENDTC": "last study-treatment exposure end — max of EX end dates",
    "RFPENDTC": "end of participation — the MOST RECENT date on record across ALL collected dates (max)",
    "RFICDTC": "informed consent date (earliest signed consent)",
    "RFCSTDTC": "first challenge-agent administration date",
    "RFCENDTC": "last challenge-agent administration date",
}


def _mechanical_value(var: str, raw_sources: list) -> str:
    """The clean, self-explanatory VALUE for the mechanical SDTM variables the app builds by the
    general rule (not by a merge-program `_var`) — dates→ISO 8601, --SEQ, --DY, standardized results.
    Used to replace leftover `_xxx` merge artifacts so the standard reads as one construction."""
    u = str(var).upper()
    raw = ""
    if raw_sources:
        parts = str(raw_sources[0]).split(".")
        raw = parts[-1] if parts else ""
    # DM reference dates — the GLOBAL construction (min/max across the contributing dates), not a
    # TA-tailored null. Auto-Build reads this to know how to derive them for any study.
    if u in _REFDATE:
        return _REFDATE[u]
    if re.search(r"DTC$", u) and u != "STUDYID":
        return "ISO 8601 date — YYYY-MM-DD (or YYYY-MM-DDThh:mm:ss with time)" + (f", from raw {raw}" if raw else "")
    if u.endswith("SEQ"):
        return "sequence number 1…n per subject (assigned after the domain sort)"
    if re.search(r"(DY|STDY|ENDY)$", u) and len(u) > 3:
        return "study day = event --DTC − DM.RFSTDTC (no day 0)"
    if u.endswith("STRESC"):
        return "standardized result from --ORRES, in standard units (character)"
    if u.endswith("STRESN"):
        return "standardized numeric result from --ORRES"
    if u.endswith("STRESU"):
        return "standard unit for --STRESC/--STRESN"
    if u == "EPOCH":
        return "trial epoch of the record, from Subject Elements (SE)"
    return ""


def _value_expr(var: str, sas_code: str, rule: str) -> str:
    """The construction VALUE — how the variable is actually built. The Gilead spec's Implemented SAS
    Code is the base (e.g. strip(_egrefid)); where it references a merge-program `_var` whose definition
    lives in the Mapping Rule (`_EGREFID = put(recordid,best.)`), substitute it in so the value reads
    `strip(put(recordid,best.))`."""
    sas = _clean_rule(sas_code)
    v = str(var)
    m = re.search(rf"_{re.escape(v)}\s*=\s*([^\n;]+?)(?:\s*(?:note:|if\b|otherwise\b|refer to\b)|$)", rule or "", re.I)
    definition = m.group(1).strip().rstrip(".").strip() if m else ""
    if definition and re.search(rf"_{re.escape(v)}\b", sas, re.I):
        # wrap only a bare multi-part expression, not one that's already a single function call
        needs_wrap = re.search(r"[|+]| - ", definition) and not re.fullmatch(r"[a-z_][a-z0-9_]*\(.*\)", definition, re.I)
        wrapped = f"({definition})" if needs_wrap else definition
        return re.sub(rf"_{re.escape(v)}\b", wrapped, sas, flags=re.I)
    if definition and not sas:
        return definition
    return sas or definition


# SDTM standard domain name + class, so the header names every domain (not just the KB's DM/DS).
_DOMAIN_META = {
    "AE": ("Adverse Events", "Events"), "CE": ("Clinical Events", "Events"),
    "DS": ("Disposition", "Events"), "DV": ("Protocol Deviations", "Events"),
    "DD": ("Death Details", "Findings"), "HO": ("Healthcare Encounters", "Events"),
    "MH": ("Medical History", "Events"), "AG": ("Procedure Agents", "Interventions"),
    "CM": ("Concomitant/Prior Medications", "Interventions"), "EC": ("Exposure as Collected", "Interventions"),
    "EX": ("Exposure", "Interventions"), "ML": ("Meal Data", "Interventions"),
    "PR": ("Procedures", "Interventions"), "SU": ("Substance Use", "Interventions"),
    "CV": ("Cardiovascular System Findings", "Findings"), "DA": ("Product Accountability", "Findings"),
    "EG": ("ECG Test Results", "Findings"), "FA": ("Findings About", "Findings About"),
    "FT": ("Functional Tests", "Findings"), "IE": ("Inclusion/Exclusion Not Met", "Findings"),
    "IS": ("Immunogenicity Specimen Assessments", "Findings"), "LB": ("Laboratory Test Results", "Findings"),
    "MB": ("Microbiology Specimen", "Findings"), "MI": ("Microscopic Findings", "Findings"),
    "MK": ("Musculoskeletal System Findings", "Findings"), "MO": ("Morphology", "Findings"),
    "MS": ("Microbiology Susceptibility", "Findings"), "NV": ("Nervous System Findings", "Findings"),
    "OE": ("Ophthalmic Examinations", "Findings"), "PC": ("PK Concentrations", "Findings"),
    "PE": ("Physical Examination", "Findings"), "PP": ("PK Parameters", "Findings"),
    "QS": ("Questionnaires", "Findings"), "RE": ("Respiratory System Findings", "Findings"),
    "RP": ("Reproductive System Findings", "Findings"), "RS": ("Disease Response", "Findings"),
    "SC": ("Subject Characteristics", "Findings"), "SR": ("Skin Response", "Findings"),
    "TR": ("Tumor/Lesion Results", "Findings"), "TU": ("Tumor/Lesion Identification", "Findings"),
    "UR": ("Urinary System Findings", "Findings"), "VS": ("Vital Signs", "Findings"),
    "LC": ("Lab Custom Findings", "Findings"), "OI": ("Non-host Organism Identifiers", "Findings"),
    "DM": ("Demographics", "Special Purpose"), "CO": ("Comments", "Special Purpose"),
    "SE": ("Subject Elements", "Special Purpose"), "SV": ("Subject Visits", "Special Purpose"),
    "SM": ("Subject Disease Milestones", "Special Purpose"), "SS": ("Subject Status", "Findings"),
    "APMH": ("Associated Persons Medical History", "Events"),
    "TA": ("Trial Arms", "Trial Design"), "TD": ("Trial Disease Assessments", "Trial Design"),
    "TE": ("Trial Elements", "Trial Design"), "TI": ("Trial Inclusion/Exclusion", "Trial Design"),
    "TM": ("Trial Disease Milestones", "Trial Design"), "TS": ("Trial Summary", "Trial Design"),
    "TV": ("Trial Visits", "Trial Design"), "RP2": ("", ""),
}


def list_domains() -> list:
    """Index for the Construction Standards view: every domain in the Gilead IG-CORE reference spec,
    with how many variables carry a structured derivation."""
    kb = _kb()
    store = _store_domains()
    doms = set(_spec_domains()) | set(kb.keys()) | set(_STANDARD.keys()) | set(store.keys())
    out = []
    for dom in sorted(d for d in doms if d):
        meta = kb.get(dom, {}) or {}
        spec_rows = _core_spec_rows(dom)
        nvars = len({str(r.get("Variable") or "").upper() for r in spec_rows if str(r.get("Variable") or "")}) \
            or len(meta.get("variables", []) or [])
        derivs = set(_STANDARD.get(dom, {})) | set(store.get(dom, {})) | {
            str(v.get("variable", "")).upper() for v in meta.get("variables", [])
            if isinstance(v.get("derivation"), dict)}
        dmeta = _DOMAIN_META.get(dom, ("", ""))
        out.append({"domain": dom, "name": meta.get("name") or dmeta[0], "class": meta.get("class") or dmeta[1],
                    "structure": meta.get("structure", ""), "keys": meta.get("keys", ""),
                    "n_vars": nvars, "n_derivations": len(derivs),
                    "n_curated": len(store.get(dom, {}) or {})})
    return out


_OP_TEXT = {"notmissing": "is not missing", "missing": "is missing", "eq": "=", "ne": "≠",
            "in": "in", "notin": "not in", "contains": "contains", "gt": ">", "lt": "<",
            "ge": "≥", "le": "≤", "starts": "starts with", "ends": "ends with"}


def _display_rows(deriv: dict) -> list:
    """Explode a derivation into readable rows so the table shows, PER RAW SOURCE, the raw origin it
    reads, the test on it (condition), the VALUE to assign, and that source's reference. A variable
    mapped from several raw sources gets one row per source."""
    rows = []
    for r in deriv.get("rules", []) or []:
        pc = _rule_condition(r)
        raw = str(r.get("source", "")).strip()
        if pc:
            t = _OP_TEXT.get(pc["op"], pc["op"])
            cond = t if pc["op"] in ("missing", "notmissing") else f"{t} {pc.get('value', '')}".strip()
            rows.append({"source": raw or pc["column"], "cond": cond,
                         "value": str(r.get("value", "")), "ref": str(r.get("ref", ""))})
        else:
            rows.append({"source": raw, "cond": str(r.get("when", "")),
                         "value": str(r.get("value", "")), "ref": str(r.get("ref", ""))})
    ev = deriv.get("else")
    if ev not in (None, ""):
        rows.append({"source": "", "cond": "otherwise", "value": str(ev), "ref": ""})
    return rows


def _clean_value(var: str, raw_sources: list, value_expr: str) -> str:
    """Replace a leftover merge-program artifact (a bare/unresolved `_xxx`) with the clean standard
    value — the general rule for that variable (ISO date, sequence, study day, …) or a plain
    'from <raw source>'. So the Value column never shows `strip(_dsstdtc)`."""
    ve = str(value_expr or "").strip()
    if re.search(r"_[a-z][a-z0-9_]*", ve, re.I) or not ve:
        mech = _mechanical_value(var, raw_sources)
        if mech:
            return mech
        if raw_sources:
            return "from " + ", ".join(s.split(".", 1)[-1] if s.startswith("raw.") else s for s in raw_sources[:2])
        return re.sub(r"_[a-z][a-z0-9_]*", "(derived)", ve) if ve else "(derived)"
    return ve


def _spec_val(r: dict, *keys) -> str:
    for k in keys:
        v = r.get(k)
        if v is not None and str(v).strip() and str(v).strip().lower() != "nan":
            return str(v).strip()
    return ""


def domain_standards(domain: str, version: str | None = None) -> dict:
    """Full Construction Standards for a domain, sourced from the Gilead IG-CORE reference spec: EVERY
    variable, with its raw sources, Gilead Core, and the Gilead Mapping Rule — overlaid with the
    structured derivations (built-in / curated) where they exist. Citation is Gilead IG-CORE, not SDTM
    IG (which is only the background knowledge). `version` selects which standards snapshot to show."""
    dom = str(domain).upper()
    kb = _kb().get(dom, {}) or {}
    spec_rows = _core_spec_rows(dom)
    effective = domain_derivations(dom, include_reference=True, version=version)
    store = _store_domains(_load_store(version)).get(dom, {}) or {}
    seen, variables = set(), []

    def _rec(var, r=None):
        var = str(var).upper()
        if not var or var in seen or (r and str(r.get("Dataset") or "").strip().upper() == "QNAM"):
            return
        seen.add(var)
        r = r or {}
        kv = _kb_var(dom, var)
        deriv = effective.get(var)
        cur = store.get(var, {}) if isinstance(store.get(var), dict) else {}
        rule_text = _spec_val(r, "Mapping Rule")
        raw_sources = _spec_raw_sources(r.get("Input Variables"))
        mech = _mechanical_value(var, raw_sources)   # a known standard (date / seq / study-day / ref-date)
        # Precedence: a built-in/curated derivation, then the clean mechanical standard (dates etc. must
        # NOT be parsed into bogus condition→value rows), then parsing the Mapping Rule's If…then logic.
        if deriv:
            derivation = {"rules": deriv.get("rules", []), "else": deriv.get("else"),
                          "rationale": deriv.get("rationale", ""),
                          "reference_only": bool(deriv.get("reference_only")),
                          "display_rows": _display_rows(deriv)}
            origin_tag = _deriv_origin(dom, var, version)
        elif mech:
            derivation, origin_tag = None, "spec"      # clean flat value; never parse a mechanical var
        else:
            parsed = _extract_rule_rows(var, dom, rule_text)
            if len(parsed) >= 2 or (len(parsed) == 1 and parsed[0]["cond"]):
                derivation = {"rules": [], "else": None, "reference_only": True,
                              "rationale": "Parsed from the Gilead IG-CORE mapping rule.",
                              "spec_derived": True, "display_rows": parsed}
                origin_tag = "gilead-spec"
            else:
                derivation, origin_tag = None, "spec"
        variables.append({
            "variable": var,
            "label": _spec_val(r, "Label") or kv.get("label", ""),
            "core": _spec_val(r, "Gilead Core") or kv.get("core", ""),
            "cdisc_core": _spec_val(r, "CDISC Core"),
            "role": _spec_val(r, "Role") or kv.get("role", ""),
            "codelist": _spec_val(r, "Codelist") or kv.get("codelist", ""),
            "cdisc_note": str(kv.get("cdisc_note", ""))[:600],
            "raw_sources": raw_sources,
            "mapping_rule": _clean_rule(rule_text)[:600],
            "value_expr": _clean_value(var, raw_sources,
                                       _value_expr(var, _spec_val(r, "Implemented SAS Code"), rule_text))[:220],
            "comment": _clean_rule(_spec_val(r, "Comment"))[:300],
            "spec_origin": _spec_val(r, "Origin"),
            "gilead_usage": _spec_val(r, "Gilead Mapping Usage"),
            "company_rule": cur.get("company_rule", ""),
            "source": cur.get("source", ""),
            "reference": "Gilead IG-CORE",
            "derivation": derivation,
            "origin": origin_tag,
            "updated_by": cur.get("updated_by", ""), "updated_at": cur.get("updated_at", ""),
        })

    # Gilead spec order first (Display/Run order as stored); then any KB-only or curated extras.
    for r in spec_rows:
        _rec(r.get("Variable") or r.get("Variable Name") or "", r)
    for v in kb.get("variables", []):
        _rec(v.get("variable", ""))
    for var in list(_STANDARD.get(dom, {})) + list(store):
        _rec(var)
    meta = _DOMAIN_META.get(dom, ("", ""))
    return {"domain": dom, "name": kb.get("name") or meta[0], "class": kb.get("class") or meta[1],
            "structure": kb.get("structure", ""), "keys": kb.get("keys", ""),
            "ig_source": "Gilead IG-CORE (SDTMIG v3.4)", "purpose": str(kb.get("purpose", ""))[:800],
            "variables": variables}


# ── Domain Program — the whole-domain reference build program, assembled from the domain's ──────────
# construction standards (structured derivations) + the Gilead IG-CORE reference mapping rules. This is
# the study-agnostic canonical program that constructs a domain end to end, so the standards team can
# review the CONSTRUCTION, not just per-variable rules. Grounded, never invented: every step cites its
# source. Conditional logic that can't be safely translated is emitted as an annotated np.select
# scaffold carrying the exact source/condition/value the standard specifies.
def _prog_src(tok: str) -> str:
    """A raw.dataset.column token → a pandas accessor, e.g. raw.dm.age_rep → raw['dm']['age_rep']."""
    m = re.match(r"raw\.([a-z0-9_]+)\.([a-z0-9_]+)\s*$", str(tok or "").strip(), re.I)
    return f"raw['{m.group(1).lower()}']['{m.group(2).lower()}']" if m else ""


def _pyq(val) -> str:
    """A safe Python string literal for a standard value — already-quoted values pass through; anything
    else is JSON-encoded so apostrophes/quotes in prose values can't break the generated code."""
    import json
    v = str(val)
    return v if re.fullmatch(r"'[^']*'|\"[^\"]*\"", v) else json.dumps(v, ensure_ascii=False)


def _pval(val) -> str:
    """A rule's assigned value as code: a raw.ds.col token becomes a column copy; else a safe literal."""
    se = _prog_src(val)
    return se if se else _pyq(val)


def _prog_cond(src_expr: str, cond: str) -> str | None:
    """Best-effort translation of a construction-standard condition to a pandas mask, for the common
    shapes; returns None when the condition is free-text that must stay a comment."""
    raw = re.sub(r"\s+", " ", str(cond or "").strip())
    c = raw.lower()
    if not src_expr or not c:
        return None
    if c in ("exists",) or "not missing" in c or "is not null" in c or "not null" in c:
        return f"{src_expr}.notna()"
    if "is missing" in c or "is null" in c or c == "missing":
        return f"{src_expr}.isna()"
    # equality / inequality — optional leading var/col token, optional quotes around the value
    m = re.match(r"^[a-z0-9_]* ?(!=|≠|<>) ?['\"]?([^'\"]+?)['\"]?$", raw)
    if m:
        return f"{src_expr} != '{m.group(2).strip()}'"
    m = re.match(r"^[a-z0-9_]* ?(==|=) ?['\"]?([^'\"]+?)['\"]?$", raw)
    if m:
        return f"{src_expr} == '{m.group(2).strip()}'"
    m = re.match(r"^[a-z0-9_]* ?(>=|<=|>|<) ?([0-9.]+)$", raw)
    if m:
        return f"{src_expr} {m.group(1)} {m.group(2)}"
    m = re.match(r"^[a-z0-9_]* ?in ?\[?([^\]]+?)\]?$", c)
    if m:
        items = ", ".join(f"'{x.strip()}'" for x in m.group(1).split(",") if x.strip())
        return f"{src_expr}.isin([{items}])"
    return None


def _prog_code_for_var(v: dict, domain: str) -> list:
    """The pandas line(s) that construct one variable, from its construction standard. Real code where
    the mapping is unambiguous (constant / single-source assign / translatable conditions); an annotated
    scaffold otherwise — always faithful to the standard, never a guess dressed up as executable code."""
    var = v["variable"]
    raw = [t for t in (v.get("raw_sources") or []) if _prog_src(t)]
    deriv = v.get("derivation") or {}
    rows = deriv.get("display_rows") or []
    cond_rows = [r for r in rows if str(r.get("cond") or "").strip()]
    flat_rows = [r for r in rows if not str(r.get("cond") or "").strip() and str(r.get("value") or "").strip()]

    if var == "DOMAIN":
        return [f"df['DOMAIN'] = '{domain}'"]
    if var == "STUDYID":
        return ["df['STUDYID'] = study_id"]

    # Conditional construction → np.select, translating what we safely can and annotating the rest.
    if cond_rows:
        out = ["conds, choices = [], []"]
        for r in cond_rows:
            src, cond, val = r.get("source") or "", r.get("cond") or "", r.get("value") or ""
            se = _prog_src(src)
            mask = _prog_cond(se, cond)
            valpy = _pval(val)
            out.append(f"# when {src or '<source>'} {cond} → {var} = {val or '<value>'}")
            if mask:
                out.append(f"conds.append({mask}); choices.append({valpy})")
            else:
                out.append(f"# conds.append(<{cond}>); choices.append({valpy})   # translate this condition")
        default = deriv.get("else")
        dflt = "np.nan" if default in (None, "") else _pval(default)
        out.append(f"df['{var}'] = np.select(conds, choices, default={dflt})")
        return out

    # A single flat value (no condition) — e.g. a constant standard value.
    if flat_rows and not raw:
        return [f"df['{var}'] = {_pval(flat_rows[0].get('value') or '')}"]

    # Direct copy from a single raw source.
    if len(raw) == 1:
        return [f"df['{var}'] = {_prog_src(raw[0])}"]

    # Several raw sources with no parsed conditions — coalesce in the order the standard lists them.
    if len(raw) > 1:
        chain = ", ".join(_prog_src(t) for t in raw)
        return [f"df['{var}'] = coalesce({chain})   # first non-missing of the standard's sources"]

    # No structured construction — carry the reference intent as a TODO grounded in the mapping rule.
    ve = (v.get("value_expr") or "").strip()
    return [f"# {var}: {ve or 'per the Gilead IG-CORE mapping rule (see reference below)'}  — TODO"]


def domain_program(domain: str, version: str | None = None) -> dict:
    """Assemble the whole-domain reference build program from the construction standards + IG-CORE
    reference rules. Returns per-variable steps (raw sources, reference mapping rule, structured
    construction, generated code) and a single assembled `program` string — the one built from both."""
    detail = domain_standards(domain, version=version)
    dom = detail["domain"]
    steps = []
    for v in detail.get("variables", []):
        deriv = v.get("derivation") or {}
        steps.append({
            "variable": v["variable"],
            "label": v.get("label", ""),
            "core": v.get("core", ""),
            "role": v.get("role", ""),
            "codelist": v.get("codelist", ""),
            "raw_sources": v.get("raw_sources", []),
            "mapping_rule": v.get("mapping_rule", ""),          # the IG-CORE reference program text
            "value_expr": v.get("value_expr", ""),
            "construction": deriv.get("display_rows", []) if deriv else [],  # the structured standard
            "rationale": (deriv or {}).get("rationale", ""),
            "reference": v.get("reference", "Gilead IG-CORE"),
            "code": _prog_code_for_var(v, dom),                 # the program built from both
            # editable curation fields (parity with the Domains tab) — a save regenerates the program
            "derivation": v.get("derivation") or None,
            "company_rule": v.get("company_rule", ""),
            "source": v.get("source", ""),
            "origin": v.get("origin", ""),
        })

    # The assembled, readable program — header + an ordered, annotated construction per variable.
    from datetime import datetime, timezone
    L = [
        f'"""SDTM {dom} — reference build program (construction standard {detail.get("version", "current")}).',
        f'{detail.get("name", "")} · class {detail.get("class", "")}.',
        'Assembled from the Gilead IG-CORE reference rules + the domain construction standards.',
        'Study-agnostic reference: adapt raw dataset/column names to the study. Grounded — every step',
        'cites Gilead IG-CORE; conditions the standard states in prose are left as annotated scaffolds.',
        f'Generated: {datetime.now(timezone.utc).date().isoformat()}',
        '"""',
        'import numpy as np',
        'import pandas as pd',
        '',
        '',
        f'def build_{dom.lower()}(raw: dict, study_id: str) -> pd.DataFrame:',
        f'    """Construct the SDTM {dom} domain from raw source datasets (a dict of DataFrames)."""',
        '    df = pd.DataFrame()',
        '',
    ]
    for s in steps:
        hdr = f"    # ── {s['variable']}"
        if s.get("label"):
            hdr += f" · {s['label']}"
        if s.get("core"):
            hdr += f"  [{s['core']}]"
        L.append(hdr)
        rule = (s.get("mapping_rule") or "").strip()
        if rule:
            L.append(f"    # IG-CORE rule: {rule[:200]}")
        for line in s["code"]:
            L.append("    " + line if not line.startswith("#") else "    " + line)
        L.append("")
    L.append(f"    return df")
    program = "\n".join(L)

    return {"domain": dom, "name": detail.get("name", ""), "class": detail.get("class", ""),
            "structure": detail.get("structure", ""), "keys": detail.get("keys", ""),
            "version": detail.get("version", ""), "ig_source": detail.get("ig_source", ""),
            "n_vars": len(steps), "steps": steps, "program": program}


def _editable_target(version: str | None) -> str:
    """Resolve + guard the version a write targets: it must be an editable (draft/provisional)
    snapshot — a released/superseded version is immutable, so curation goes to a new draft."""
    vid = _resolve_version(version)
    if not version_editable(vid):
        raise ValueError(f"version {vid!r} is released and immutable — create a new draft "
                         f"(clone it) and curate that, then release it")
    return vid


def upsert_standard(domain: str, variable: str, payload: dict, user_email: str = "",
                    version: str | None = None) -> dict:
    """Curate (create/update) a variable's standard derivation IN AN EDITABLE VERSION. Validates that
    it compiles, writes the version snapshot, and invalidates the cache so the next build uses it."""
    from datetime import datetime, timezone
    dom, var = str(domain).upper(), str(variable).upper()
    vid = _editable_target(version)

    def _norm_rule(r):
        return {"source": str(r.get("source", "")).strip(), "when": str(r.get("when", "")).strip(),
                "value": r.get("value", ""), "ref": str(r.get("ref", "")).strip()}
    rules = [_norm_rule(r) for r in (payload.get("rules") or [])
             if str(r.get("when", "")).strip() or str(r.get("source", "")).strip()]
    # An unconditional executable expression (e.g. promoted from a study's Implemented SAS Code:
    # scan(USUBJID,-2,'-')). construction_map surfaces it so the build fills the variable from it.
    expr = str(payload.get("expr", "") or "").strip()
    deriv = {"rules": rules, "else": payload.get("else"),
             "rationale": str(payload.get("rationale", "")).strip()}
    if not expr and not _validate_deriv(deriv):
        raise ValueError("no usable rule — supply an executable `expr`, or a rule with a condition the "
                         "engine understands (e.g. raw.death.DEATHDAT 'not missing', 'AESER = Y')")
    rec = {**deriv, "expr": expr, "company_rule": str(payload.get("company_rule", "")).strip(),
           "source": str(payload.get("source", "")).strip(),
           "updated_by": user_email, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    doc = dict(_load_store(vid))
    doc.setdefault("domains", {}).setdefault(dom, {})[var] = rec
    doc["rev"] = int(doc.get("rev", 0)) + 1
    doc["updated_at"] = rec["updated_at"]
    _save_store(doc, vid)
    return rec


def delete_standard(domain: str, variable: str, version: str | None = None) -> bool:
    """Remove a curated derivation from an editable version (the build falls back to KB / built-in /
    spec). Idempotent."""
    dom, var = str(domain).upper(), str(variable).upper()
    vid = _editable_target(version)
    doc = dict(_load_store(vid))
    dm = (doc.get("domains") or {}).get(dom, {})
    if var not in dm:
        return False
    del dm[var]
    doc["rev"] = int(doc.get("rev", 0)) + 1
    _save_store(doc, vid)
    return True


def invalidate_cache() -> None:
    """Drop the in-process caches so a curated change or KB update is picked up without a restart."""
    global _STORE_CACHE, _INDEX_CACHE, _KB_CACHE
    _STORE_CACHE = {}
    _INDEX_CACHE = None
    _KB_CACHE = None


# ── CDISC SDTMIG v3.4 GENERAL rules (the assumptions that apply before/across all domains) ────
# The app's understanding of the cross-domain conventions, so the reviewer can see what it applies
# when constructing any domain. Curatable later; today it is the authored knowledge baseline.
_GENERAL_RULES = [
    {"category": "Dates & times — ISO 8601", "rules": [
        "Every date/time is ISO 8601. A date is YYYY-MM-DD; with time it is YYYY-MM-DDThh:mm:ss (--DTC variables).",
        "Partial/unknown parts are DROPPED, not imputed: keep YYYY, YYYY-MM, or YYYY-MM-DD as far as known; never invent a day or month.",
        "Times use a 24-hour clock; a time zone, when collected, is the ISO offset. Durations use ISO duration (P#Y#M#DT#H#M), intervals join two datetimes.",
        "Convert the raw collected date/time to this format — the raw value's own layout (DDMMMYYYY, MM/DD/YYYY, …) is normalized to YYYY-MM-DD.",
    ]},
    {"category": "Study day & reference dates", "rules": [
        "--DY (study day) = (event --DTC − DM.RFSTDTC) + 1 when the event is on/after RFSTDTC, else the plain difference. There is NO day 0.",
        "DM reference dates: RFSTDTC = first study treatment, RFENDTC = last; RFXSTDTC/RFXENDTC = first/last exposure; RFICDTC = informed consent; RFPENDTC = end of participation.",
        "--STDY/--ENDY are study days of --STDTC/--ENDTC relative to RFSTDTC by the same rule.",
    ]},
    {"category": "Controlled terminology", "rules": [
        "Coded variables use the exact CDISC CT submission value (case-sensitive). Map raw values to the codelist; keep the raw value in --ORRES/--TERM where applicable.",
        "--TESTCD ≤ 8 characters, begins with a letter, uses only letters/digits/underscores — no spaces or other special characters.",
        "Yes/No flag variables use codelist (NY): value is 'Y' (or 'N'/'U' where allowed); a presence flag is 'Y' or null, never 'N'.",
    ]},
    {"category": "Identifiers & keys", "rules": [
        "STUDYID, DOMAIN, USUBJID appear on every subject-level record. USUBJID is unique across the ENTIRE submission (all studies).",
        "--SEQ uniquely numbers a subject's records within a domain (1…n, assigned after the domain's key sort).",
        "--SPID = sponsor identifier (often the EDC record position); --REFID = internal/external reference; --GRPID ties related records for a subject.",
        "Natural keys identify one row — e.g. STUDYID + USUBJID + --TESTCD + --DTC (Findings), or STUDYID + USUBJID + --DECOD + --STDTC (DS).",
    ]},
    {"category": "Observation classes & structure", "rules": [
        "Three general observation classes: Interventions (EX, EC, CM, …), Events (AE, DS, MH, CE, …), Findings (LB, VS, EG, PC, …).",
        "Plus Special Purpose (DM, CO, SE, SV), Trial Design (TA, TE, TV, TS, TI, …) and Relationship datasets (RELREC, SUPP--).",
        "Grain: Findings = one record per test per visit/timepoint per subject; Events/Interventions = one per event/intervention; DM/SE = one per subject.",
    ]},
    {"category": "Results & 'not done' (Findings)", "rules": [
        "--ORRES = result as collected, --ORRESU its unit; --STRESC = standardized result in standard units (character), --STRESN the numeric form when applicable, --STRESU the standard unit.",
        "When a test/assessment was not done: --STAT = 'NOT DONE', --REASND gives the reason, and the result variables are null.",
        "--BLFL = 'Y' on the baseline record; --LOBXFL = 'Y' on the last observation on/before first study treatment (per --TESTCD).",
    ]},
    {"category": "Core, origin & missing data", "rules": [
        "Core: Required (always present, never null), Expected (present, may be null), Permissible (optional). (Gilead Core mirrors this: Required / Optional / Excluded.)",
        "Origin records where a value comes from: Collected (CRF), Derived, Assigned, Protocol, or eDT.",
        "Do not fabricate data. A value that was not collected/derivable stays null; a whole variable with no data may be dropped (Gilead 'Excluded').",
    ]},
    {"category": "Supplemental & relationships", "rules": [
        "Non-standard qualifiers go to SUPP-- (RDOMAIN, IDVAR/IDVARVAL, QNAM/QLABEL/QVAL, QORIG; QEVAL only when an evaluator assessed it).",
        "RELREC relates records across domains; CO holds comments linked by RDOMAIN/IDVAR/IDVARVAL.",
        "EPOCH classifies a record's trial epoch, derived from Subject Elements (SE) intervals.",
    ]},
]


# Domain-level assumptions (the rules specific to each domain, applied on top of the general ones).
_DOMAIN_RULES = {
    "DM": ["One record per subject (the parent domain; every USUBJID appears exactly once).",
           "ARM/ARMCD come from Trial Arms (TA); if the subject was not assigned to an arm, ARMCD/ARM are null and ARMNRS is populated. ACTARM/ACTARMCD are the actual arm.",
           "Reference dates: RFSTDTC = study Day 1 (first treatment per SAP); RFXSTDTC/RFXENDTC = first/last exposure; RFPENDTC = most recent date on record (end of participation); RFICDTC = informed consent.",
           "AGE is collected or derived from BRTHDTC & RFSTDTC (AGEU its unit). DTHFL = 'Y' when the subject died (a death date is recorded), else null; DTHDTC is the ISO death date.",
           "COUNTRY uses ISO 3166 alpha-3; SEX/RACE/ETHNIC use CDISC CT."],
    "DS": ["One record per disposition status or protocol milestone per subject; keys STUDYID, USUBJID, DSDECOD, DSSTDTC.",
           "DSDECOD is standardized; its codelist depends on DSCAT — protocol-milestone terms vs completion/discontinuation (NCOMPLT) terms. DSTERM is the verbatim term.",
           "DSCAT = PROTOCOL MILESTONE (e.g. INFORMED CONSENT OBTAINED, RANDOMIZED), DISPOSITION EVENT (completion/withdrawal), or OTHER EVENT.",
           "Assembled from several sources (consent, enrollment/randomization, study-drug & study completion, follow-up) stacked into one domain."],
    "AE": ["One record per adverse event per subject. AETERM = verbatim; AEDECOD = MedDRA Preferred Term, AEBODSYS = SOC.",
           "AESER = 'Y' for a serious AE (with the AESxxx seriousness-criteria flags); AEREL = causality; AEOUT = outcome; AEACN = action taken.",
           "AESTDTC/AEENDTC are ISO dates; --DY relative to RFSTDTC. Treatment-emergent status is per the SAP (typically on/after first dose)."],
    "EX": ["One record per constant-dosing interval of study treatment per subject (protocol-specified). EXTRT = treatment.",
           "EXDOSE + EXDOSU (or EXDOSTXT), EXROUTE, EXDOSFRM; EXSTDTC/EXENDTC bound the interval. EC (Exposure as Collected) holds the as-collected detail."],
    "CM": ["One record per medication (or unique regimen) per subject. CMTRT = verbatim; CMDECOD = coded (WHODrug).",
           "CMINDC = indication; CMDOSE/CMDOSU, CMROUTE; CMSTDTC/CMENDTC. Prior vs concomitant is by timing relative to RFSTDTC."],
    "LB": ["One record per lab test per visit/timepoint per subject. LBTESTCD/LBTEST from CT.",
           "LBORRES/LBORRESU as collected; LBSTRESC/LBSTRESN/LBSTRESU standardized to common units; LBNRIND vs reference range; LBFAST fasting flag; LBBLFL baseline."],
    "VS": ["One record per vital sign per visit/timepoint per subject. VSTESTCD/VSTEST from CT (SYSBP, DIABP, PULSE, TEMP, HEIGHT, WEIGHT…).",
           "VSORRES/VSSTRESN standardized; VSPOS position, VSLOC location; VSBLFL baseline."],
    "EG": ["One record per ECG measurement per visit/timepoint per subject; EGTESTCD/EGTEST from CT. Interpretation (INTP) is a separate record.",
           "EGORRES/EGSTRESN standardized; EGPOS, EGMETHOD; regular vs Holter ECG use different codelists."],
    "MH": ["One record per medical-history event per subject; MHTERM verbatim, MHDECOD MedDRA. Generally pre-study/ongoing conditions.",
           "MHSTDTC (often partial), MHENRF/MHENRTPT relative timing to a reference."],
    "IE": ["One record ONLY for each inclusion/exclusion criterion the subject did NOT meet (not every criterion). IETESTCD/IETEST + IECAT = INCLUSION/EXCLUSION.",
           "IEORRES = the actual result; the domain documents protocol waivers/deviations at entry."],
    "PC": ["One record per PK sample analyte per timepoint per subject; PCTESTCD analyte, PCSPEC specimen.",
           "PCDTC needs precise date+time; PCORRES/PCSTRESN concentration; nominal vs actual time (PCTPT/PCTPTNUM, --ELTM)."],
}


# A short worked example per domain: a plausible raw record → the SDTM record it constructs.
_DOMAIN_EXAMPLES = {
    "DM": {"raw": "enroll: X_SUBJID='001', SEX='M', BRTHDAT='15JAN1970'; ex: EXSTDAT='02JAN2024'",
           "sdtm": "USUBJID='STUDYX-001', SUBJID='001', SEX='M', BRTHDTC='1970-01-15', AGE=53, AGEU='YEARS', RFSTDTC='2024-01-02', COUNTRY='USA'"},
    "DS": {"raw": "consents: CONSYN='Yes', CONSDAT='01JAN2024'",
           "sdtm": "DSTERM='INFORMED CONSENT OBTAINED', DSDECOD='INFORMED CONSENT OBTAINED', DSCAT='PROTOCOL MILESTONE', DSSTDTC='2024-01-01', DSSEQ=1"},
    "AE": {"raw": "ae: AETERM='Headache', AESTDAT='03JAN2024', AESER='N', AEREL='POSSIBLE'",
           "sdtm": "AETERM='Headache', AEDECOD='Headache' (MedDRA PT), AEBODSYS='Nervous system disorders', AESTDTC='2024-01-03', AESER='N', AEREL='POSSIBLE', AESEQ=1"},
    "EX": {"raw": "ex: EXTRT='Drug X', EXDOSE='100', EXDOSU='mg', EXSTDAT='02JAN2024', EXENDAT='15JAN2024'",
           "sdtm": "EXTRT='DRUG X', EXDOSE=100, EXDOSU='mg', EXROUTE='ORAL', EXSTDTC='2024-01-02', EXENDTC='2024-01-15', EXSEQ=1"},
    "CM": {"raw": "cm: CMTRT='Aspirin', CMDOSE='100', CMSTDAT='20DEC2023'",
           "sdtm": "CMTRT='Aspirin', CMDECOD='ACETYLSALICYLIC ACID' (WHODrug), CMDOSE=100, CMDOSU='mg', CMSTDTC='2023-12-20', CMSEQ=1"},
    "LB": {"raw": "lb: LBTEST='Glucose', LBORRES='5.5', LBORRESU='mmol/L', LBDAT='02JAN2024'",
           "sdtm": "LBTESTCD='GLUC', LBTEST='Glucose', LBORRES='5.5', LBORRESU='mmol/L', LBSTRESN=99, LBSTRESU='mg/dL', LBDTC='2024-01-02', LBSEQ=1"},
    "VS": {"raw": "vs: SYSBP_RAW='120', DIABP_RAW='80', VSDAT='02JAN2024'",
           "sdtm": "two records — VSTESTCD='SYSBP', VSORRES='120', VSSTRESN=120, VSORRESU='mmHg'; VSTESTCD='DIABP', VSORRES='80'; both VSDTC='2024-01-02'"},
    "EG": {"raw": "eg: EGVR_RAW='' , EGQTI_RAW='420', EGDAT='02JAN2024'",
           "sdtm": "EGTESTCD='QTSB', EGTEST='QT Interval, Aggregate', EGORRES='420', EGORRESU='msec', EGSTRESN=420, EGDTC='2024-01-02'"},
    "MH": {"raw": "mh: MHTERM='Hypertension', MHSTDAT='2019'",
           "sdtm": "MHTERM='Hypertension', MHDECOD='Hypertension' (MedDRA PT), MHCAT='GENERAL', MHSTDTC='2019', MHSEQ=1"},
    "IE": {"raw": "enroll: subject did NOT meet inclusion criterion 3",
           "sdtm": "IETESTCD='INCL03', IECAT='INCLUSION', IEORRES='N', IESTRESC='N' (only the failed criterion is recorded)"},
    "PC": {"raw": "pc: analyte='Drug X', conc='12.3' ng/mL, sample at 0.5 h post-dose on 02JAN2024 08:30",
           "sdtm": "PCTESTCD='DRUGX', PCTEST='Drug X', PCSPEC='PLASMA', PCORRES='12.3', PCORRESU='ng/mL', PCSTRESN=12.3, PCDTC='2024-01-02T08:30', PCTPT='0.5H'"},
    # ── Interventions ──
    "AG": {"raw": "ag: agent='Iodinated contrast', dose='100', unit='mL' given during a scan on 05JAN2024",
           "sdtm": "AGTRT='IODINATED CONTRAST', AGDOSE=100, AGDOSU='mL', AGDTC='2024-01-05', AGSEQ=1"},
    "EC": {"raw": "ec: planned Drug X 100 mg; subject actually took 90 mg on 02JAN2024",
           "sdtm": "ECTRT='DRUG X', ECDOSE=90, ECDOSU='mg', ECMOOD='PERFORMED', ECSTDTC='2024-01-02', ECSEQ=1 (as-collected; EX holds the derived protocol dose)"},
    "ML": {"raw": "ml: meal='Standard breakfast', consumed on 02JAN2024 08:00",
           "sdtm": "MLTRT='STANDARD BREAKFAST', MLCAT='BREAKFAST', MLSTDTC='2024-01-02T08:00', MLSEQ=1"},
    "PR": {"raw": "pr: procedure='Liver biopsy', PRDAT='05JAN2024'",
           "sdtm": "PRTRT='Liver biopsy', PRDECOD='Biopsy of liver' (coded), PRSTDTC='2024-01-05', PRSEQ=1"},
    "SU": {"raw": "su: substance='Tobacco', use='Current', ~10/day",
           "sdtm": "SUTRT='TOBACCO', SUCAT='TOBACCO', SUDOSE=10, SUDOSU='CIGARETTES', SUDOSFRQ='QD', SUSTDTC (start), SUSEQ=1"},
    # ── Events ──
    "CE": {"raw": "ce: clinical event (not an AE)='Migraine', CESTDAT='03JAN2024'",
           "sdtm": "CETERM='Migraine', CEDECOD='Migraine' (MedDRA PT), CECAT='NEUROLOGIC', CESTDTC='2024-01-03', CESEQ=1"},
    "DV": {"raw": "dv: deviation='Missed a scheduled dose', DVDAT='10JAN2024'",
           "sdtm": "DVTERM='Missed a scheduled dose', DVDECOD (coded category), DVSTDTC='2024-01-10', DVSEQ=1"},
    "HO": {"raw": "ho: healthcare encounter='Hospitalization', HOSTDAT='08JAN2024', HOENDAT='12JAN2024'",
           "sdtm": "HOTERM='Hospitalization', HODECOD, HOSTDTC='2024-01-08', HOENDTC='2024-01-12', HOSEQ=1"},
    "APMH": {"raw": "apmh: associated person (mother) with 'Type 2 Diabetes'",
             "sdtm": "APID='FAM-MOTHER', SREL='MOTHER', MHTERM='Type 2 Diabetes', MHDECOD (MedDRA), MHCAT='FAMILY HISTORY'"},
    # ── Findings ──
    "CV": {"raw": "cv: LVEF='55', unit='%', CVDAT='02JAN2024'",
           "sdtm": "CVTESTCD='LVEF', CVTEST='Left Ventricular Ejection Fraction', CVORRES='55', CVORRESU='%', CVSTRESN=55, CVDTC='2024-01-02'"},
    "DA": {"raw": "da: study drug dispensed amount='30', returned='4' on 02JAN2024",
           "sdtm": "DATESTCD='DISPAMT', DATEST='Dispensed Amount', DAORRES='30', DASTRESN=30, DADTC='2024-01-02' (a second record for RETAMT=4)"},
    "DD": {"raw": "dd: primary cause of death='Disease Progression', death date 01MAY2024",
           "sdtm": "DDTESTCD='PRCDTH', DDTEST='Primary Cause of Death', DDORRES='DISEASE PROGRESSION', DDDTC='2024-05-01'"},
    "FA": {"raw": "fa: duration of the adverse event 'Rash' = 3 days",
           "sdtm": "FAOBJ='RASH', FATESTCD='DUR', FATEST='Duration', FAORRES='P3D', linked to the AE record via RELREC"},
    "FT": {"raw": "ft: 6-minute walk distance='420', unit='m', FTDAT='02JAN2024'",
           "sdtm": "FTTESTCD='6MWTOT', FTTEST='6 Minute Walk Total Distance', FTORRES='420', FTORRESU='m', FTSTRESN=420, FTDTC='2024-01-02'"},
    "IS": {"raw": "is: anti-drug antibody result='Positive', specimen='Serum', ISDAT='02JAN2024'",
           "sdtm": "ISTESTCD='ADA', ISTEST='Anti-Drug Antibody', ISORRES='POSITIVE', ISSPEC='SERUM', ISDTC='2024-01-02'"},
    "LC": {"raw": "lc: custom lab test='Custom Marker'='12', unit='U/L'",
           "sdtm": "LCTESTCD, LCTEST='Custom Marker', LCORRES='12', LCORRESU='U/L', LCSTRESN=12, LCDTC (Findings, like LB)"},
    "MB": {"raw": "mb: organism identified='Escherichia coli', specimen='Urine'",
           "sdtm": "MBTESTCD='ORGANISM', MBTEST='Organism Identified', MBORRES='ESCHERICHIA COLI', MBSPEC='URINE', MBDTC"},
    "MI": {"raw": "mi: liver biopsy histopathology='Steatosis, mild'",
           "sdtm": "MITESTCD, MIORRES='Steatosis', MISEV='MILD', MISPEC='LIVER', MIDTC (microscopic finding)"},
    "MK": {"raw": "mk: joint assessment='Right knee'='Swelling present'",
           "sdtm": "MKTESTCD, MKORRES='SWELLING', MKLOC='KNEE', MKLAT='RIGHT', MKDTC (musculoskeletal finding)"},
    "MO": {"raw": "mo: target lesion longest diameter on imaging='22', unit='mm'",
           "sdtm": "MOTESTCD='LDIAM', MOTEST='Longest Diameter', MOORRES='22', MOORRESU='mm', MOSTRESN=22, MODTC (morphology)"},
    "MS": {"raw": "ms: susceptibility MIC of 'E. coli' to 'Ciprofloxacin'='0.25' µg/mL",
           "sdtm": "MSTESTCD='MIC', MSTEST='Minimum Inhibitory Concentration', MSORRES='0.25', MSORRESU='ug/mL', MSAGENT='CIPROFLOXACIN'"},
    "NV": {"raw": "nv: visual field defect='Present', eye='Left'",
           "sdtm": "NVTESTCD, NVORRES='PRESENT', NVLOC='EYE', NVLAT='LEFT', NVDTC (nervous-system finding)"},
    "OE": {"raw": "oe: best-corrected visual acuity='20/20', eye='Right', OEDAT='02JAN2024'",
           "sdtm": "OETESTCD='BCVA', OETEST='Best Corrected Visual Acuity', OEORRES='20/20', OELAT='RIGHT', OEDTC='2024-01-02'"},
    "OI": {"raw": "oi: non-host organism identifier for a pathogen strain",
           "sdtm": "OITESTCD, OIORRES (identifier/attribute of the organism), OISPEC, linked to MB (non-host organism identifiers)"},
    "PE": {"raw": "pe: body system='Abdomen', finding='Normal', PEDAT='02JAN2024'",
           "sdtm": "PETESTCD='ABDOMEN', PETEST='Abdomen', PEORRES='NORMAL', PEDTC='2024-01-02'"},
    "PP": {"raw": "pp (derived from PC): Cmax='45.2' ng/mL, AUC(0-t)='310' for Drug X",
           "sdtm": "PPTESTCD='CMAX', PPTEST='Max Concentration', PPORRES='45.2', PPORRESU='ng/mL', PPSTRESN=45.2, PPCAT='DRUG X' (one record per parameter)"},
    "QS": {"raw": "qs: EQ-5D item 'Mobility'='2', QSDAT='02JAN2024'",
           "sdtm": "QSTESTCD='EQ5D0101', QSTEST='EQ-5D-5L Mobility', QSCAT='EQ-5D-5L', QSORRES='2', QSSTRESN=2, QSDTC='2024-01-02'"},
    "RE": {"raw": "re: FEV1='3.2', unit='L', REDAT='02JAN2024'",
           "sdtm": "RETESTCD='FEV1', RETEST='Forced Expiratory Volume 1s', REORRES='3.2', REORRESU='L', RESTRESN=3.2, REDTC='2024-01-02'"},
    "RP": {"raw": "rp: pregnancy test result='Negative', RPDAT='02JAN2024'",
           "sdtm": "RPTESTCD='PREGIND', RPTEST='Pregnancy Test Indicator', RPORRES='NEGATIVE', RPDTC='2024-01-02'"},
    "RS": {"raw": "rs: overall tumor response='Partial Response' per RECIST on 15FEB2024",
           "sdtm": "RSTESTCD='OVRLRESP', RSTEST='Overall Response', RSORRES='PR', RSCAT='RECIST 1.1', RSEVAL='INVESTIGATOR', RSDTC='2024-02-15'"},
    "SC": {"raw": "sc: education level='College', handedness='Right'",
           "sdtm": "SCTESTCD='EDLEVEL', SCTEST='Education Level', SCORRES='College' (one record per characteristic)"},
    "SR": {"raw": "sr: skin test induration='8', unit='mm', reading on 04JAN2024",
           "sdtm": "SRTESTCD='INDur', SRTEST='Induration', SRORRES='8', SRORRESU='mm', SRSTRESN=8, SRDTC='2024-01-04'"},
    "SS": {"raw": "ss: survival status='Alive' at last contact 01JUN2024",
           "sdtm": "SSTESTCD='SURVSTAT', SSTEST='Survival Status', SSORRES='ALIVE', SSDTC='2024-06-01'"},
    "TR": {"raw": "tr: target lesion 'T01' longest diameter='22', unit='mm', TRDAT='02JAN2024'",
           "sdtm": "TRTESTCD='LDIAM', TRTEST='Longest Diameter', TRORRES='22', TRORRESU='mm', TRSTRESN=22, TRLNKID='T01', TRDTC='2024-01-02'"},
    "TU": {"raw": "tu: a target lesion identified in the liver at baseline, id 'T01'",
           "sdtm": "TUTESTCD='TUMIDENT', TUTEST='Tumor Identification', TUORRES='TARGET', TULOC='LIVER', TULNKID='T01', TUDTC (baseline)"},
    "UR": {"raw": "ur: urine flow rate='15', unit='mL/s', URDAT='02JAN2024'",
           "sdtm": "URTESTCD, URORRES='15', URORRESU='mL/s', URSTRESN=15, URDTC='2024-01-02' (urinary-system finding)"},
    # ── Special Purpose ──
    "CO": {"raw": "co: free-text comment on an AE record: 'Patient recovered fully.'",
           "sdtm": "COVAL='Patient recovered fully.', RDOMAIN='AE', IDVAR='AESEQ', IDVARVAL='1', USUBJID='STUDYX-001'"},
    "SE": {"raw": "derived from EX/DS: subject was in the Treatment element 02JAN–01FEB2024",
           "sdtm": "ETCD='TRT', ELEMENT='Treatment', SESTDTC='2024-01-02', SEENDTC='2024-02-01', TAETORD=2, EPOCH='TREATMENT'"},
    "SV": {"raw": "sv: Visit 2 occurred on 15JAN2024",
           "sdtm": "VISITNUM=2, VISIT='WEEK 2', SVSTDTC='2024-01-15', SVENDTC='2024-01-15'"},
    "SM": {"raw": "derived: subject's first disease progression on 01MAR2024",
           "sdtm": "MIDS='PROGRESSION1', MIDSTYPE='DISEASE PROGRESSION', SMSTDTC='2024-03-01', SMSEQ=1"},
    # ── Trial Design (study-level, from the protocol) ──
    "TA": {"raw": "protocol: Arm A = Screening → Treatment → Follow-up",
           "sdtm": "ARMCD='A', ARM='Arm A', TAETORD=1, ETCD='SCRN', ELEMENT='Screening'; TAETORD=2, ETCD='TRT' … (one record per element in the arm)"},
    "TE": {"raw": "protocol: element 'Treatment' starts at first dose, lasts 28 days",
           "sdtm": "ETCD='TRT', ELEMENT='Treatment', TESTRL='First dose of study drug', TEDUR='P28D'"},
    "TV": {"raw": "protocol: Visit 2 is planned for study Day 15 (Week 2)",
           "sdtm": "VISITNUM=2, VISIT='WEEK 2', VISITDY=15, ARMCD='A', TVSTRL='Start of Week 2'"},
    "TI": {"raw": "protocol: inclusion criterion 1 = 'Age ≥ 18 years'",
           "sdtm": "IETESTCD='INCL01', IETEST='Age ≥ 18 years', IECAT='INCLUSION'"},
    "TS": {"raw": "protocol: trial title, phase, planned number of subjects",
           "sdtm": "TSPARMCD='TITLE', TSPARM='Trial Title', TSVAL='A Phase 3 Study of …'; TSPARMCD='TPHASE', TSVAL='PHASE III' (one record per parameter)"},
    "TD": {"raw": "protocol: disease assessment schedule — tumor assessment every 8 weeks",
           "sdtm": "TDORDER=1, TDANCVAR='TRLNKID', TDSTOFF='P0W', TDTGTPAI='P8W' (planned timing of disease assessments)"},
    "TM": {"raw": "protocol: disease milestone 'Disease Progression' defined by RECIST PD",
           "sdtm": "MIDS='PROGRESSION', MIDSTYPE='DISEASE PROGRESSION', TMDEF='RECIST 1.1 progressive disease', TMRPT='Y'"},
}


def _class_rules(dom: str, name: str, cls: str) -> list:
    """A baseline rule set generated from a domain's SDTM class + prefix, so EVERY domain has guidance
    even before hand-authored rules exist."""
    p, nm = dom, (name.lower() if name else dom)
    if cls == "Findings":
        return [f"Findings class — one record per {nm} test per visit/timepoint per subject.",
                f"{p}TESTCD/{p}TEST identify the test (CDISC CT); {p}ORRES/{p}ORRESU as collected, "
                f"{p}STRESC/{p}STRESN/{p}STRESU standardized; {p}DTC is the ISO date; {p}BLFL flags baseline."]
    if cls == "Findings About":
        return [f"Findings About — one record per finding about an event/intervention; {p}OBJ names the object, "
                f"and the record links to its parent domain."]
    if cls == "Events":
        return [f"Events class — one record per {nm} per subject.",
                f"{p}TERM is the verbatim term, {p}DECOD the coded (dictionary) term; {p}STDTC/{p}ENDTC are ISO "
                f"dates and {p}DY is relative to DM.RFSTDTC."]
    if cls == "Interventions":
        return [f"Interventions class — one record per {nm} (or constant-dosing interval) per subject.",
                f"{p}TRT is the treatment; {p}DOSE/{p}DOSU, {p}ROUTE, {p}DOSFRM describe the dose; "
                f"{p}STDTC/{p}ENDTC bound it."]
    if cls == "Trial Design":
        return [f"Trial Design — study-level design metadata (not per subject); one record per {nm} element."]
    if cls == "Special Purpose":
        return [f"Special-purpose domain — {nm}; structure per the SDTM IG for this domain."]
    return [f"{nm} — general SDTM rules apply; STUDYID/DOMAIN/USUBJID identifiers, ISO dates, CDISC CT."]


def construction_map(domain: str) -> dict:
    """{VAR: executable construction expression} from the Gilead IG-CORE spec — the Implemented SAS
    Code with merge-program `_var`s resolved (e.g. SITEID → scan(usubjid, -2, '-')). The build uses
    this to fill a variable the STUDY spec left without a source/recipe, so the app constructs it
    from the standard. Descriptive standards (ISO dates, min/max ref-dates) are excluded — the caller's
    evaluator returns None for anything non-executable anyway."""
    out = {}
    for r in _core_spec_rows(domain):
        var = str(r.get("Variable") or r.get("Variable Name") or "").upper()
        if not var:
            continue
        ve = _value_expr(var, _spec_val(r, "Implemented SAS Code"), _spec_val(r, "Mapping Rule"))
        if ve and ve.lower() not in ("seq",):     # --SEQ handled by the engine's own sequence op
            out[var] = ve
    # Promoted standards: a GCDS user can lift a study's recurring Implemented SAS Code into the
    # curated store (upsert_standard with `expr`). Those override the IG-CORE spec for this domain
    # so the next study inherits the construction deterministically.
    try:
        doc = _load_store()
        for var, rec in ((doc.get("domains") or {}).get(str(domain).upper(), {}) or {}).items():
            e = str((rec or {}).get("expr") or "").strip()
            if e and e.lower() not in ("seq",):
                out[str(var).upper()] = e
    except Exception:
        pass
    return out


def general_rules() -> dict:
    """The CDISC SDTMIG v3.4 general (cross-domain) rules PLUS the per-domain assumptions the app
    applies — for EVERY domain (hand-authored where captured, else a class-based baseline). The
    knowledge baseline the reviewer can inspect, returned for the 'CDISC General Rules' tab."""
    codes = sorted(set(_spec_domains()) | set(_DOMAIN_META) | set(_DOMAIN_RULES))
    domains = []
    for d in codes:
        if not re.fullmatch(r"[A-Z]{2,4}", d):
            continue
        name, cls = _DOMAIN_META.get(d, ("", ""))
        rules = _DOMAIN_RULES.get(d) or _class_rules(d, name, cls)
        domains.append({"domain": d, "name": name, "class": cls, "rules": rules,
                        "example": _DOMAIN_EXAMPLES.get(d), "authored": d in _DOMAIN_RULES})
    return {"source": "CDISC SDTMIG v3.4 — general assumptions (Section 4) + per-domain assumptions + Gilead IG-CORE conventions",
            "categories": _GENERAL_RULES, "domains": domains}
