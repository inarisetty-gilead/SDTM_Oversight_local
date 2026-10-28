# Vendored from sdtm-studio sdtm_api/src/rule_library.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""Spec-derived deterministic rule library — loader + study resolver.

Two artefacts were distilled from a validated CORE mapping spec (SDTMIG v3.4,
define2.1; a fingerprint is carried in each file's `info` block):

  RULE_ID_LIST.json                — 45 rule archetypes (index / taxonomy).
  RULE_CONDITIONS_EXPRESSIONS.json — per-variable expressions (1283), IF/THEN
                                     conditions (284) and findings TESTCD
                                     test-map rows, grouped under those 45 rules.

Each expression says, for a specific DOMAIN.VARIABLE: what SAS to emit
(IMPLEMENTED_SAS_CODE / GENERATED_SAS), at which pipeline STAGE (PS05 derive …
PS14 final … PS16 SUPP), and whether it is safe to auto-emit (STATUS = AUTO)
or needs a programmer (REVIEW / OPEN).

This module does two things:

  load_rule_library()      parse + cache both files, indexed for lookup.
  resolve_for_study(...)   map each expression's raw SOURCES token(s)
                           (`raw.<file>:<col>`) onto THIS study's metadata
                           columns, so the rules apply beyond the source study.
                           Returns applicable expressions with resolved SAS,
                           split by AUTO / REVIEW / OPEN, plus coverage stats.

The source-study expressions are keyed to that study's raw column names
(`x_subjid`, `aeser_std`, …). The resolver is what lets another study reuse
them: the generic archetypes (GEN-DV-*, the templates) always apply; a specific
expression applies when its raw token resolves to a real column in the target
study's metadata.

No Jinja: the 25 `EXPRESSION_TEMPLATE` strings are single-line `{placeholder}`
patterns, rendered here with a safe formatter that leaves non-placeholder
markers (`--testcd`, `<code>`) intact.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# File locations                                                              #
# --------------------------------------------------------------------------- #

_ID_LIST_NAME = "RULE_ID_LIST.json"
_COND_EXPR_NAME = "RULE_CONDITIONS_EXPRESSIONS.json"


def _candidate_dirs() -> list[str]:
    """Where the two JSON artefacts might live, most specific first.

    Override with SDTM_RULE_LIBRARY_DIR. Otherwise the copy packaged with the API
    (sdtm_api/reference/rule_library — inside the Docker build context, so it
    ships in the image), then the directories up from this module and the cwd."""
    dirs = []
    env = os.environ.get("SDTM_RULE_LIBRARY_DIR")
    if env:
        dirs.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.join(os.path.dirname(here), "reference", "rule_library"))
    # src -> sdtm_api -> Container -> <repo root>
    up = here
    for _ in range(5):
        up = os.path.dirname(up)
        dirs.append(up)
    dirs.append(os.getcwd())
    # de-dup, keep order
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _find_file(name: str) -> str | None:
    for d in _candidate_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


# --------------------------------------------------------------------------- #
# Parsed model                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class RuleLibrary:
    info: dict
    rules: list                                   # full RULE_CONDITIONS_EXPRESSIONS rules
    index: dict = field(default_factory=dict)     # RULE_ID -> RULE_ID_LIST meta
    templates: dict = field(default_factory=dict)  # RULE_ID -> EXPRESSION_TEMPLATE
    by_domain_var: dict = field(default_factory=dict)  # (DOMAIN, VARIABLE) -> [expr]
    by_domain: dict = field(default_factory=dict)      # DOMAIN -> [expr]
    fingerprint: str = ""

    @property
    def n_expressions(self) -> int:
        return sum(len(r.get("expressions", [])) for r in self.rules)


_CACHE: dict = {}
_LOCK = threading.Lock()


def _mtime(path: str | None) -> float:
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def load_rule_library(force: bool = False) -> RuleLibrary | None:
    """Parse and cache both artefacts. Returns None if the files aren't found
    (callers treat the library as simply unavailable — it's an additive tier)."""
    id_path = _find_file(_ID_LIST_NAME)
    ce_path = _find_file(_COND_EXPR_NAME)
    if not ce_path:
        return None
    key = (id_path, ce_path, _mtime(id_path), _mtime(ce_path))
    with _LOCK:
        if not force and _CACHE.get("key") == key:
            return _CACHE.get("lib")
        lib = _parse(id_path, ce_path)
        _CACHE["key"], _CACHE["lib"] = key, lib
        return lib


def _parse(id_path: str | None, ce_path: str) -> RuleLibrary:
    ce = json.load(open(ce_path))
    rules = ce.get("rules", [])
    info = ce.get("info", {})
    index = {}
    templates = {}
    if id_path:
        try:
            idl = json.load(open(id_path))
            for r in idl.get("rules", []):
                index[r.get("RULE_ID")] = r
        except (OSError, ValueError):
            pass
    by_dv, by_dom = {}, {}
    for r in rules:
        rid = r.get("RULE_ID")
        t = r.get("EXPRESSION_TEMPLATE", "")
        if t and t != "(none: spec code or utility)":
            templates[rid] = t
        for e in r.get("expressions", []):
            dom, var = e.get("DOMAIN"), e.get("VARIABLE")
            e.setdefault("RULE_ID", rid)
            by_dv.setdefault((dom, var), []).append(e)
            by_dom.setdefault(dom, []).append(e)
    return RuleLibrary(
        info=info, rules=rules, index=index, templates=templates,
        by_domain_var=by_dv, by_domain=by_dom,
        fingerprint=info.get("core_fingerprint", ""),
    )


# --------------------------------------------------------------------------- #
# Template rendering (no Jinja)                                               #
# --------------------------------------------------------------------------- #

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def render_expression(template: str, ctx: dict) -> str:
    """Substitute {name} placeholders from ctx. Unknown placeholders and the
    literal markers (`--testcd`, `<code>`, `<TEST_MAP cells>`) are left intact,
    so a partially-resolved template is still readable."""
    def repl(m: re.Match) -> str:
        k = m.group(1)
        v = ctx.get(k)
        return str(v) if v is not None else m.group(0)
    return _PLACEHOLDER.sub(repl, template or "")


# --------------------------------------------------------------------------- #
# SOURCES parsing + study index                                              #
# --------------------------------------------------------------------------- #

_SRC_SPLIT = re.compile(r"[,\s;]+")


def parse_sources(src: str) -> list[dict]:
    """`raw.ae:x_subjid` -> {scope:'raw', file:'ae', col:'x_subjid'}.
    Multiple tokens may be whitespace/comma separated. Malformed tokens are
    returned with whatever parts could be read."""
    out = []
    for tok in _SRC_SPLIT.split((src or "").strip()):
        if not tok:
            continue
        scope, file, col = "", "", ""
        rest = tok
        if "." in rest:
            scope, rest = rest.split(".", 1)
        if ":" in rest:
            file, col = rest.split(":", 1)
        else:
            col = rest
        out.append({"scope": scope.lower(), "file": file.lower(), "col": col.lower(), "raw": tok})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


@dataclass
class StudyIndex:
    pairs: set                               # {(file_l, col_l)}
    by_col: dict = field(default_factory=dict)    # col_l -> {file_l}
    by_norm: dict = field(default_factory=dict)   # norm(col) -> {col_l}
    domains: set = field(default_factory=set)     # planned/built SDTM domains (for sdtm.* sources)


def build_study_index(columns: list, planned_domains=None) -> StudyIndex:
    """Build the resolver index from a study's raw metadata columns.

    `columns`: list of dicts. We accept the shapes this codebase produces —
    a column carries its name under column_name/column/name and its source
    file under file/dataset/data_key/source_file."""
    pairs: set = set()
    by_col: dict = {}
    by_norm: dict = {}
    for c in columns or []:
        if isinstance(c, str):
            col = c.strip().lower()
            file = ""
        else:
            col = str(c.get("column_name") or c.get("column") or c.get("name") or "").strip().lower()
            file = str(c.get("file") or c.get("dataset") or c.get("data_key")
                       or c.get("source_file") or "").strip().lower()
        if not col:
            continue
        pairs.add((file, col))
        by_col.setdefault(col, set()).add(file)
        by_norm.setdefault(_norm(col), set()).add(col)
    return StudyIndex(pairs=pairs, by_col=by_col, by_norm=by_norm,
                      domains={str(d).upper() for d in (planned_domains or [])})


# --------------------------------------------------------------------------- #
# Resolution                                                                  #
# --------------------------------------------------------------------------- #

# Tiers, best first. Callers can gate on `tier` to allow only confident matches.
_TIER_EXACT = "exact"        # same file + same column
_TIER_COL = "col"            # same column name, any file
_TIER_NORM = "norm"          # same column ignoring case/underscores
_TIER_FUZZY = "fuzzy"        # difflib close match above cutoff
_TIER_DOMAIN = "sdtm_domain"  # sdtm.<dom>:<var> and that domain is in the plan


def resolve_token(tok: dict, idx: StudyIndex, fuzzy_cutoff: float = 0.86) -> dict | None:
    """Resolve one SOURCES token to a study column. Returns
    {from, to, file, tier, score} or None if nothing matched."""
    scope, file, col = tok["scope"], tok["file"], tok["col"]
    if scope == "sdtm":
        # Cross-domain dependency, not a raw column: resolvable iff that
        # upstream domain is in this study's build plan.
        dom = file.upper()
        if dom in idx.domains:
            return {"from": tok["raw"], "to": col, "file": file, "tier": _TIER_DOMAIN, "score": 1.0}
        return None
    if not col:
        return None
    if file and (file, col) in idx.pairs:
        return {"from": col, "to": col, "file": file, "tier": _TIER_EXACT, "score": 1.0}
    if col in idx.by_col:
        f = sorted(idx.by_col[col])[0]
        return {"from": col, "to": col, "file": f, "tier": _TIER_COL, "score": 1.0}
    hits = idx.by_norm.get(_norm(col))
    if hits:
        to = sorted(hits)[0]
        f = sorted(idx.by_col.get(to, {""}))[0]
        return {"from": col, "to": to, "file": f, "tier": _TIER_NORM, "score": 0.95}
    close = difflib.get_close_matches(col, list(idx.by_col), n=1, cutoff=fuzzy_cutoff)
    if close:
        to = close[0]
        score = difflib.SequenceMatcher(None, col, to).ratio()
        f = sorted(idx.by_col.get(to, {""}))[0]
        return {"from": col, "to": to, "file": f, "tier": _TIER_FUZZY, "score": round(score, 3)}
    return None


_STAGE_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")


def _strip_stage_tag(sas: str) -> str:
    """Remove a leading `[PS14 final]`-style display tag from a GENERATED_SAS
    line, leaving runnable SAS."""
    return _STAGE_TAG.sub("", sas or "")


def rule_sas(expr: dict) -> str:
    """The runnable SAS for an expression: IMPLEMENTED_SAS_CODE when present
    (the RHS for ASSIGN rows, a full statement for CODE rows), else the
    stage-tag-stripped GENERATED_SAS."""
    impl = (expr.get("IMPLEMENTED_SAS_CODE") or "").strip()
    return impl if impl else _strip_stage_tag(expr.get("GENERATED_SAS") or "")


_WORD = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])"


def _swap_tokens(code: str, subs: dict) -> str:
    """Rewrite raw tokens in a SAS expression to the resolved study columns.
    subs: {original_token: study_col}. Only differing names are swapped."""
    out = code or ""
    for frm, to in subs.items():
        if frm and to and frm.lower() != to.lower():
            out = re.sub(_WORD.format(re.escape(frm)), to, out, flags=re.IGNORECASE)
    return out


def resolve_expression(expr: dict, idx: StudyIndex, allow_tiers=None,
                       fuzzy_cutoff: float = 0.86) -> dict:
    """Decide whether one expression applies to this study and, if so, produce
    the resolved SAS.

    An expression APPLIES when every raw token in its SOURCES resolves at a
    permitted tier. Expressions with no SOURCES are structural/utility (STUDYID,
    DOMAIN, --SEQ, global macros) and apply by domain membership alone.

    Returns a dict with: applicable, reason, tier (worst tier used),
    resolved_sas, token_map, and the carried STATUS/STAGE/RULE_ID."""
    allow = set(allow_tiers or {_TIER_EXACT, _TIER_COL, _TIER_NORM, _TIER_DOMAIN})
    toks = parse_sources(expr.get("SOURCES", ""))
    base = {
        "domain": expr.get("DOMAIN"), "variable": expr.get("VARIABLE"),
        "rule_id": expr.get("RULE_ID"), "status": expr.get("STATUS"),
        "stage": expr.get("STAGE"), "action": expr.get("ACTION"),
    }
    src_sas = rule_sas(expr)
    source_study_sas = _strip_stage_tag(expr.get("GENERATED_SAS") or "")
    if not toks:
        # Structural/utility — no raw inputs to resolve.
        return {**base, "applicable": True, "reason": "structural",
                "tier": None, "token_map": {}, "resolved_sas": src_sas,
                "source_study_sas": source_study_sas}
    token_map, worst = {}, None
    rank = [_TIER_EXACT, _TIER_COL, _TIER_NORM, _TIER_FUZZY, _TIER_DOMAIN]
    for t in toks:
        r = resolve_token(t, idx, fuzzy_cutoff=fuzzy_cutoff)
        if not r or r["tier"] not in allow:
            return {**base, "applicable": False,
                    "reason": f"unresolved token {t['raw']}", "tier": None,
                    "token_map": token_map, "resolved_sas": ""}
        token_map[t["col"] or t["raw"]] = r
        if worst is None or rank.index(r["tier"]) > rank.index(worst):
            worst = r["tier"]
    subs = {frm: r["to"] for frm, r in token_map.items()}
    resolved = _swap_tokens(src_sas, subs)
    return {**base, "applicable": True, "reason": "resolved", "tier": worst,
            "token_map": token_map, "resolved_sas": resolved,
            "source_study_sas": source_study_sas}


def resolve_for_study(columns: list, domains=None, planned_domains=None,
                      allow_tiers=None, fuzzy_cutoff: float = 0.86) -> dict:
    """Apply the whole rule library to a study's metadata.

    columns        : study raw metadata columns (see build_study_index).
    domains        : restrict to these target SDTM domains (default: all).
    planned_domains: domains in the build plan — lets `sdtm.*` cross-domain
                     sources resolve.

    Returns {fingerprint, counts, by_status, expressions} where each expression
    is the resolve_expression() result for an applicable rule expression."""
    lib = load_rule_library()
    if lib is None:
        return {"available": False, "reason": "rule library files not found"}
    idx = build_study_index(columns, planned_domains=planned_domains or domains)
    want = {str(d).upper() for d in domains} if domains else None
    applicable, by_status = [], {"AUTO": 0, "REVIEW": 0, "OPEN": 0}
    by_tier: dict = {}
    considered = 0
    for (dom, _var), exprs in lib.by_domain_var.items():
        if want and dom not in want:
            continue
        for e in exprs:
            considered += 1
            res = resolve_expression(e, idx, allow_tiers=allow_tiers, fuzzy_cutoff=fuzzy_cutoff)
            if res["applicable"]:
                applicable.append(res)
                st = (res.get("status") or "").upper()
                if st in by_status:
                    by_status[st] += 1
                if res["tier"]:
                    by_tier[res["tier"]] = by_tier.get(res["tier"], 0) + 1
    return {
        "available": True,
        "fingerprint": lib.fingerprint,
        "counts": {
            "considered": considered,
            "applicable": len(applicable),
            "auto": by_status["AUTO"],
            "review": by_status["REVIEW"],
            "open": by_status["OPEN"],
        },
        "by_tier": by_tier,
        "by_status": by_status,
        "expressions": applicable,
    }


# --------------------------------------------------------------------------- #
# Reference view (Reference → Rule Library)                                    #
# --------------------------------------------------------------------------- #

_META_FIELDS = ("CLASS", "CATEGORY", "OWNER", "CURRENT_NAME", "PURPOSE", "APPLIES_TO", "IG_SECTIONS",
                "USED_BY_DOMAINS", "OWNER_MEANS", "NOTE")


def library_summary() -> dict:
    """The rule index for the Reference page: each rule's catalogue entry (RULE_ID_LIST) with the
    count of expressions by STATUS, conditions and test-map rows (RULE_CONDITIONS_EXPRESSIONS)."""
    lib = load_rule_library()
    if lib is None:
        return {"available": False, "rules": []}
    out = []
    for r in lib.rules:
        rid = r.get("RULE_ID")
        meta = lib.index.get(rid) or {}
        st: dict = {}
        for e in r.get("expressions", []):
            k = str(e.get("STATUS") or "").upper() or "—"
            st[k] = st.get(k, 0) + 1
        out.append({"rule_id": rid, **{f.lower(): meta.get(f) or r.get(f) or "" for f in _META_FIELDS},
                    "n_expressions": len(r.get("expressions", [])),
                    "n_conditions": len(r.get("conditions", [])),
                    "n_test_map": len(r.get("test_map", [])), "by_status": st})
    return {"available": True, "info": lib.info, "fingerprint": lib.fingerprint, "rules": out}


def rule_detail(rule_id: str) -> dict | None:
    """One rule in full: catalogue entry, how it is assigned, its template/logic, and every
    expression (with the runnable SAS), IF/THEN condition and test-map row."""
    lib = load_rule_library()
    if lib is None:
        return None
    r = next((x for x in lib.rules if str(x.get("RULE_ID")) == str(rule_id)), None)
    if r is None:
        return None
    exprs = [{**e, "SAS": rule_sas(e)} for e in r.get("expressions", [])]
    return {"rule_id": r.get("RULE_ID"), "meta": lib.index.get(r.get("RULE_ID")) or {},
            "assigned_when": r.get("ASSIGNED_WHEN", ""), "template": r.get("EXPRESSION_TEMPLATE", ""),
            "template_file": r.get("TEMPLATE_FILE", ""), "logic": r.get("LOGIC", ""),
            "expressions": exprs, "conditions": r.get("conditions", []), "test_map": r.get("test_map", [])}


def study_fit(columns: list, domain: str, planned_domains=None, spec_vars=None) -> dict:
    """How the library lands on one domain of a study: every expression for the domain, whether its
    raw sources resolve to this study's columns (and how), the SAS it resolves to, and whether the
    build already derives that variable from the study spec (`spec_vars`); the build overlay only
    fills variables it does not."""
    lib = load_rule_library()
    if lib is None:
        return {"available": False, "rows": []}
    dom = str(domain).upper()
    idx = build_study_index(columns, planned_domains=planned_domains or [dom])
    spec = {str(v).upper() for v in (spec_vars or [])}
    rows = []
    for e in lib.by_domain.get(dom, []):
        res = resolve_expression(e, idx)
        rows.append({"variable": e.get("VARIABLE"), "rule_id": e.get("RULE_ID"), "status": e.get("STATUS"),
                     "stage": e.get("STAGE"), "action": e.get("ACTION"), "sources": e.get("SOURCES", ""),
                     "applicable": res["applicable"], "reason": res["reason"], "tier": res.get("tier"),
                     "resolved_sas": res.get("resolved_sas", ""), "source_sas": rule_sas(e),
                     "in_spec": str(e.get("VARIABLE") or "").upper() in spec})
    return {"available": True, "domain": dom, "fingerprint": lib.fingerprint,
            "domains": sorted(lib.by_domain), "rows": rows}


# --------------------------------------------------------------------------- #
# Self-test                                                                    #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # python -m src.rule_library  (from sdtm_api/)
    import argparse
    ap = argparse.ArgumentParser(description="Rule library loader/resolver self-test")
    ap.add_argument("--domain", help="restrict to one SDTM domain, e.g. AE")
    ap.add_argument("--self", action="store_true",
                    help="resolve against the SOURCE study's own raw columns "
                         "(every raw token should resolve — proves the engine)")
    args = ap.parse_args()

    lib = load_rule_library()
    if lib is None:
        raise SystemExit("rule library files not found on any candidate path")
    print(f"loaded fingerprint={lib.fingerprint}  rules={len(lib.rules)}  "
          f"expressions={lib.n_expressions}  templates={len(lib.templates)}")

    # Reconstruct the source study's columns from the files' own SOURCES tokens,
    # so a self-resolution should land ~everything at the EXACT tier.
    cols = []
    for r in lib.rules:
        for e in r.get("expressions", []):
            for t in parse_sources(e.get("SOURCES", "")):
                if t["scope"] == "raw" and t["col"]:
                    cols.append({"file": t["file"], "column_name": t["col"]})
    plan = sorted({d for (d, _v) in lib.by_domain_var})
    out = resolve_for_study(cols, domains=[args.domain] if args.domain else None,
                            planned_domains=plan)
    print("counts:", json.dumps(out["counts"]))
    print("by_tier:", json.dumps(out["by_tier"]))
    # show a few resolved lines
    for res in out["expressions"][:8]:
        print(f"  [{res['status']}/{res['stage']}] {res['domain']}.{res['variable']} "
              f"({res['tier']}) -> {res['resolved_sas'][:80]}")
