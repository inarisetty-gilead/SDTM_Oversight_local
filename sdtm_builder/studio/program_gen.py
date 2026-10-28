# Vendored from sdtm-studio sdtm_api/src/program_gen.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""Program generation — turns a domain's mapping blocks into a complete, readable program
that reproduces the build. The Python program is assembled from the EXEC ENGINE's own
per-variable snippets (`build_exec._code_from_struct`), so it is exactly the logic that ran
in the Domain Studio / Auto-Build (not a re-implementation). Ported from SDTM Designer's
`_build_python_script`. Programs are auto-saved on every build.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import re
import textwrap
from datetime import datetime, timezone

from .build_exec import (CARRY_RECIPES, _TEMP_FINAL_FUNCS, _carry_args, _code_from_struct, _iso_parts, _prep_sources,
                            _seq_collapse_args, _seq_collapse_code, _upper_raw_cols, compose_blocks)
from . import sas_expr, sas_functions, sas_runtime
from .db import get_db

# Columns of a SUPP-- dataset, in submission order (mirrors build_exec._SUPP_COLS).
_SUPP_COLS = ["STUDYID", "RDOMAIN", "USUBJID", "IDVAR", "IDVARVAL",
              "QNAM", "QLABEL", "QVAL", "QORIG", "QEVAL"]


# ── Prep pipeline + SUPP shared helpers ──────────────────────────────────────
def _pn(name) -> str:
    """Normalise a dataset name to a Python/SAS-safe identifier stem (lower)."""
    return re.sub(r"[^0-9a-zA-Z_]", "_", str(name).strip().lower())


def _saved_prep_steps(study, domain) -> list:
    """The domain's saved Prep pipeline (so codegen emits it even when a caller
    did not thread prep_steps through). Best-effort — never raises."""
    try:
        from .build_exec import _load_saved
        return (_load_saved(study, str(domain).upper()) or {}).get("prep_steps") or []
    except Exception:  # noqa: BLE001
        return []


def _enrich_supp(study, domain, blocks) -> None:
    """Flag the spec's QNAM rows as SUPP + carry qlabel/qorig, exactly as the
    exec engine does in `_run_domain` (so the generated SUPP matches the build).
    Idempotent and best-effort."""
    try:
        from .build_exec import _qnam_meta
        qmeta = _qnam_meta(study, domain)
    except Exception:  # noqa: BLE001
        qmeta = {}
    for b in blocks:
        v = str(b.get("variable", "")).upper()
        if not b.get("supp") and v in qmeta:
            b["supp"] = True
            b.setdefault("qlabel", qmeta[v]["qlabel"])
            b.setdefault("qorig", qmeta[v]["qorig"])


def _step_inputs(st) -> list:
    """Every dataset name a prep step reads (raw.<x>, sdtm.<x> or a prior step's output)."""
    p = st.get("params") or {}
    ins = []
    if p.get("dataset"):
        ins.append(p["dataset"])
    ins += list(p.get("datasets") or [])
    for spec in (p.get("inputs") or []):
        if spec.get("dataset"):
            ins.append(spec["dataset"])
    for side in ("left", "right"):
        if p.get(side):
            ins.append(p[side])
    for s in (p.get("sources") or []):
        if s.get("dataset"):
            ins.append(s["dataset"])
    return [str(i) for i in ins]


def _base_lineage_steps(prep_steps, domain, ops=None) -> tuple:
    """The prep steps feeding `<dom>_base`, walked back from the base output name — only
    through steps whose op is in `ops` when given. Returns (steps, raw dataset names reached)."""
    df = domain.lower()
    base_names = {f"{df}_base", f"{df}base"}
    by_name: dict = {}
    for st in prep_steps or []:
        by_name.setdefault(_pn(st.get("name")), []).append(st)
    lineage_steps, seen, raws = [], set(), set()
    frontier = [n for n in base_names if n in by_name]
    while frontier:
        nm = frontier.pop()
        if nm in seen:
            continue
        seen.add(nm)
        for st in by_name.get(nm, []):
            if ops is not None and st.get("op") not in ops:
                continue
            lineage_steps.append(st)
            for i in _step_inputs(st):
                if i.lower().startswith("raw."):
                    raws.add(i.split(".", 1)[1].lower())
                pin = _pn(i.split(".")[-1]) if i.startswith(("raw.", "sdtm.")) else _pn(i)
                if pin in by_name:
                    frontier.append(pin)
    return lineage_steps, raws


# Prep ops that keep every column of their input, so raw columns ride through to the base.
_PREP_KEEPS_COLUMNS = {"stack", "rename", "filter", "derive", "uppercase", "compute", "carry"}


def _prep_base_carries(prep_steps, domain, ds, cols) -> bool:
    """True when raw.<ds> reaches `<dom>_base` only through column-preserving steps and none of
    them renames one of `cols` away — so those raw columns (RECORDID, ATCSEQ …) are on the base."""
    steps, raws = _base_lineage_steps(prep_steps, domain, ops=_PREP_KEEPS_COLUMNS)
    if str(ds).lower() not in raws:
        return False
    want = {str(c).upper() for c in cols}
    for st in steps:
        for x in ((st.get("params") or {}).get("renames") or []) if st.get("op") == "rename" else []:
            f, t = str(x.get("from") or "").upper(), str(x.get("to") or "").upper()
            if f in want and t != f:
                return False
    return True


def _base_lineage_cols(prep_steps, domain) -> set:
    """SDTM-named columns the prep pipeline writes into the `<dom>_base` lineage
    (derive targets, rename `to`s, findings-transpose outputs). Used by the SAS
    emitter to know which parent derivations the prepared base already supplies
    (the Python emitter decides this at runtime from the base's real columns)."""
    lineage_steps, _raws = _base_lineage_steps(prep_steps, domain)
    cols: set = set()
    for st in lineage_steps:
        op, p = st.get("op"), (st.get("params") or {})
        if op in ("derive", "compute", "carry") and p.get("target"):
            cols.add(str(p["target"]).strip().upper())
        elif op == "rename":
            for x in (p.get("renames") or []):
                if x.get("to"):
                    cols.add(str(x["to"]).strip().upper())
        elif op == "transpose_findings":
            for k in ("testcd_col", "test_col", "orres_col", "orresu_col", "cat_col",
                      "dtc_col", "date_raw_col"):
                if p.get(k):
                    cols.add(str(p[k]).strip().upper())
        elif op in ("transpose_long",):
            for k in ("var_name", "value_name"):
                if p.get(k):
                    cols.add(str(p[k]).strip().upper())
        elif op == "aggregate" and p.get("out_col"):
            cols.add(str(p["out_col"]).strip().upper())
    return cols


# ── Python prep-pipeline emitter ─────────────────────────────────────────────
def _py_prep_helpers(study: str, needs_iso: bool) -> list:
    """Source of the prep helpers ported verbatim from prep_engine (guarantees the
    generated pipeline reproduces run_prep's semantics byte-for-byte)."""
    from .prep_engine import (_FN_VALUE, _ci_col, _derive_value,
                                  _prep_cond_mask, _prep_op, _resolve_col)
    L = ["# ── prep-pipeline helpers (ported verbatim from prep_engine.run_prep) ──",
         "import re",
         f"_FN_VALUE = re.compile({_FN_VALUE.pattern!r}, re.I)",
         "",
         inspect.getsource(_ci_col),
         inspect.getsource(_derive_value),
         inspect.getsource(_prep_op),
         inspect.getsource(_prep_cond_mask),
         inspect.getsource(_resolve_col),
         "def _prep_norm(df):",
         "    for _c in df.columns:",
         "        if df[_c].dtype == object:",
         "            df[_c] = df[_c].astype(str).where(df[_c].notna(), None)",
         "    df.columns = [str(_c).upper() for _c in df.columns]",
         "    return df",
         "",
         "def _prep_fallback(_name):",
         "    # mirrors build_engine._load_source: a step input not built THIS run is",
         "    # resolved from the persisted datasets (synth -> rawdata -> prep).",
         f"    for _pfx in ('synth', 'rawdata', 'prep'):",
         f"        try:",
         f"            _d = pd.read_parquet(f'{{_pfx}}/{study}/{{_name.lower()}}.parquet')",
         "            _d.columns = [str(_c).upper() for _c in _d.columns]",
         "            return _d",
         "        except Exception:",
         "            continue",
         "    raise ValueError(f\"dataset '{_name}' not found in this study's data\")",
         ""]
    if needs_iso:
        from .build_engine import _iso
        L.append(inspect.getsource(_iso))
        L.append("")
    return L


def _py_prep_pipeline(prep_steps: list) -> dict:
    """Translate a saved Prep pipeline into pandas code that reproduces
    prep_engine.run_prep: each step builds a frame bound to `raw_<name>`, reading
    prior outputs / raw sources / built SDTM domains, preserving the chain.

    Returns {lines, produced, raw_reads, sdoms, needs_iso}."""
    produced: set = set()
    raw_reads: dict = {}
    sdoms: set = set()
    state = {"needs_iso": False}

    def srcref(ref) -> str:
        key = str(ref or "").strip().lower()
        if not key:
            return "pd.DataFrame()"
        if key.startswith("sdtm."):
            d = _pn(key.split(".", 1)[1])
            sdoms.add(d)
            return f"sdtm_{d}"
        rawkey = key[4:] if key.startswith("raw.") else key
        pn = _pn(rawkey)
        if pn in produced:
            # prior output this run, else fall back to the persisted dataset
            # (exactly as the exec engine's _prep_load / _load_source resolves it).
            return f"(raw_{pn} if {pn!r} in _prep_ok else _prep_fallback({rawkey!r}))"
        raw_reads[pn] = rawkey
        return f"raw_{pn}"

    L: list = ["_prep_ok = set()  # prep outputs that built (run_prep is per-step tolerant)", ""]
    for k, st in enumerate(prep_steps or []):
        op = st.get("op")
        name = (st.get("name") or f"prep{k + 1}").strip().lower()
        pn = _pn(name)
        out = "_out"
        p = st.get("params", {}) or {}
        L.append(f"# [{k}] {op} -> {name}")
        body_start = len(L)
        if op == "stack":
            L.append("_parts = []")
            for d in (p.get("datasets") or []):
                if not d:
                    continue
                expr = srcref(d)
                # run_prep stacks what resolves and skips what does not
                L.append(f"try: _parts.append({expr})")
                L.append("except Exception: pass")
            L.append(f"{out} = pd.concat(_parts, ignore_index=True) if _parts else pd.DataFrame()")
        elif op == "rename":
            src = srcref(p.get("dataset"))
            from .prep_engine import _rename_plan
            ren, copies = _rename_plan(p.get("renames"))
            L.append(f"{out} = {src}.rename(columns={ren!r})")
            for first, extra in copies:      # a column listed twice: renamed once, copied to the rest
                L.append(f"if {first!r} in {out}.columns: {out}[{extra!r}] = {out}[{first!r}]")
        elif op == "drop":
            src = srcref(p.get("dataset"))
            L.append(f"{out} = {src}.drop(columns=[_c for _c in {list(p.get('columns') or [])!r} "
                     f"if _c in {src}.columns])")
        elif op == "select":
            src = srcref(p.get("dataset"))
            L.append(f"{out} = {src}[[_c for _c in {list(p.get('columns') or [])!r} "
                     f"if _c in {src}.columns]]")
        elif op == "filter" and str(p.get("expression") or "").strip():
            src = srcref(p.get("dataset"))
            tree = sas_expr.parse(p["expression"])
            L += [f"# subsetting IF {sas_expr.to_sas(tree)};",
                  f"{out} = {src}.rename(columns=str.upper)",
                  f"_ix = {out}.index",
                  f"{out} = {out}[_sx_n(_ix, {sas_expr.to_py(tree, lambda n: f'{out}.get({n!r})')}).fillna(0).ne(0).values]"]
        elif op == "filter":
            src = srcref(p.get("dataset"))
            cond = [{"column": p.get("column"), "operator": p.get("operator", "=="),
                     "value": p.get("value", "")}]
            L.append(f"{out} = {src}[_prep_cond_mask({src}, {cond!r})]")
        elif op == "compute":
            src = srcref(p.get("dataset"))
            tgt = str(p.get("target") or "").strip().upper()
            tree = sas_expr.parse(p.get("expression") or "")
            L += [f"# {tgt} = {sas_expr.to_sas(tree)};",
                  f"{out} = {src}.rename(columns=str.upper)",
                  f"_ix = {out}.index",
                  f"{out}[{tgt!r}] = _sx_res(_ix, {sas_expr.to_py(tree, lambda n: f'{out}.get({n!r})')})"]
        elif op == "carry":
            src = srcref(p.get("dataset"))
            tgt, col = str(p.get("target") or "").strip().upper(), str(p.get("column") or "").strip().upper()
            by, order = _carry_args({"by": p.get("by", "USUBJID"), "order": p.get("order")})[1:]
            mode = str(p.get("mode") or "retain").lower()
            L.append(f"{out} = {src}.rename(columns=str.upper)")
            if mode in ("lag", "dif"):
                L.append(f"{out}[{tgt!r}] = _sx_lag({out}, {out}[{col!r}], {by!r}, {order!r}, "
                         f"{max(int(float(p.get('n') or 1)), 1)}, {mode == 'dif'!r})")
            else:
                L.append(f"{out}[{tgt!r}] = _sx_retain({out}, {out}[{col!r}], {by!r}, {order!r}, "
                         f"{'first' if mode == 'first' else 'locf'!r})")
        elif op == "derive":
            src = srcref(p.get("dataset"))
            tgt = str(p.get("target") or "").strip().upper()
            L.append(f"{out} = {src}.copy()")
            if str(p.get("else_value") or "") != "":
                L.append(f"{out}[{tgt!r}] = _derive_value({out}, {p.get('else_value')!r})")
            else:
                L.append(f"if {tgt!r} not in {out}.columns:")
                L.append(f"    {out}[{tgt!r}] = ''")
            if p.get("rules"):
                L.append(f"for _r in {list(p.get('rules'))!r}:")
                L.append(f"    _m = _prep_cond_mask({out}, _r.get('conds'))")
                L.append(f"    _v = _derive_value({out}, _r.get('value', ''))")
                L.append(f"    {out}.loc[_m, {tgt!r}] = (_v[_m] if isinstance(_v, pd.Series) else str(_v))")
        elif op == "uppercase":
            src = srcref(p.get("dataset"))
            L.append(f"{out} = {src}.copy()")
            L.append(f"for _c in {list(p.get('columns') or [])!r}:")
            L.append(f"    _cc = _ci_col({out}, _c)")
            L.append("    if _cc:")
            L.append(f"        _m = _prep_cond_mask({out}, [{{'column': _cc, 'operator': '!=', 'value': ''}}])")
            L.append(f"        _vv = {out}[_cc].astype('string').str.upper()")
            L.append(f"        {out}.loc[_m, _cc] = _vv[_m]")
        elif op == "merge":
            how = p.get("how", "left")
            on_all = list(p.get("on") or [])
            specs = p.get("inputs") or [{"dataset": p.get("left"), "columns": []},
                                        {"dataset": p.get("right"), "columns": []}]
            specs = [s for s in specs if s.get("dataset")]
            L.append('_KEYISH = ("USUBJID", "STUDYID", "SUBJID", "X_SUBJID", "SUBJECTID")')
            fvars = []
            for i, spec in enumerate(specs):
                ref = srcref(spec.get("dataset"))
                fv = f"_mf{i}"
                keep = [c for c in (spec.get("columns") or [])]
                if keep:
                    L.append(f"_d = {ref}")
                    L.append(f"_keep = [_c for _c in {keep!r} if _c in _d.columns]")
                    L.append(f"_keep = list(dict.fromkeys(_keep + [_c for _c in {on_all!r} if _c in _d.columns]"
                             f" + [_c for _c in _KEYISH if _c in _d.columns]))")
                    L.append(f"{fv} = _d[_keep]")
                else:
                    L.append(f"{fv} = {ref}")
                fvars.append(fv)
            if p.get("on_map"):
                L.append("# NOTE: this merge used a left/right column map (on_map); "
                         "verify the join keys below")
            if fvars:
                L.append(f"{out} = {fvars[0]}")
                for fv in fvars[1:]:
                    L.append(f"_keys = [_c for _c in {on_all!r} if _c in {out}.columns and _c in {fv}.columns]")
                    if on_all:   # as prep_engine: a named key one side lacks is an error, not a looser join
                        L.append(f"if len(_keys) < {len(on_all)}: raise ValueError('merge key(s) missing on one side: '"
                                 f" + ', '.join(_c for _c in {on_all!r} if _c not in _keys))")
                    L.append(f"if not _keys:")
                    L.append(f"    _common = [_c for _c in {out}.columns if _c in {fv}.columns]")
                    L.append(f"    _keys = [_c for _c in _KEYISH if _c in _common] or _common")
                    L.append(f"{out} = {out}.merge({fv}, on=_keys, how={how!r}, suffixes=('', '_r'))")
            else:
                L.append(f"{out} = pd.DataFrame()")
        elif op == "transpose_long":
            src = srcref(p.get("dataset"))
            L.append(f"{out} = {src}.melt("
                     f"id_vars=[_c for _c in {list(p.get('id_vars') or [])!r} if _c in {src}.columns], "
                     f"value_vars=([_c for _c in {list(p.get('value_vars') or [])!r} if _c in {src}.columns] or None), "
                     f"var_name={p.get('var_name', 'TESTCD')!r}, value_name={p.get('value_name', 'ORRES')!r})")
        elif op == "transpose_findings":
            src = srcref(p.get("dataset"))
            tcol = p.get("testcd_col", "TESTCD")
            tname = p.get("test_col", "TEST")
            ocol = p.get("orres_col", "ORRES")
            ucol = p.get("orresu_col", "ORRESU")
            dtraw = p.get("date_raw_col")
            catc = p.get("cat_col")
            L.append(f"_src = {src}")
            L.append(f"_idv = [_c for _c in {list(p.get('id_vars') or [])!r} if _c in _src.columns]")
            L.append("_frames = []")
            L.append(f"for _me in {list(p.get('measures') or [])!r}:")
            L.append("    _vc = _resolve_col(_me.get('value_col'), _src.columns)")
            L.append("    if not _vc:")
            L.append("        continue")
            L.append("    _f = _src[_idv].copy()")
            L.append(f"    _f[{tcol!r}] = _me.get('testcd', '')")
            L.append(f"    _f[{tname!r}] = _me.get('test') or _me.get('testcd', '')")
            L.append(f"    _f[{ocol!r}] = _src[_vc].astype(object)")
            L.append("    _uc = _resolve_col(_me.get('unit_col'), _src.columns)")
            L.append(f"    _f[{ucol!r}] = _src[_uc].astype(object) if _uc else ''")
            if dtraw:
                L.append("    _dc = _resolve_col(_me.get('date_col'), _src.columns)")
                L.append(f"    _f[{dtraw!r}] = _src[_dc].astype(object) if _dc else ''")
            if catc:
                L.append(f"    if _me.get('cat') is not None:")
                L.append(f"        _f[{catc!r}] = _me.get('cat', '')")
            L.append("    _frames.append(_f)")
            L.append(f"{out} = pd.concat(_frames, ignore_index=True) if _frames else _src")
            L.append(f"if _frames and {ocol!r} in {out}.columns:")
            L.append(f"    _keep = {out}[{ocol!r}].astype(str).str.strip().replace({{'nan': '', 'None': ''}}) != ''")
            L.append(f"    {out} = {out}[_keep].reset_index(drop=True)")
            dtc_col, dtc_src = p.get("dtc_col"), p.get("dtc_source_col")
            if dtc_col and dtc_src:
                state["needs_iso"] = True
                L.append(f"if {dtc_src!r} in {out}.columns and {dtc_col!r} not in {out}.columns:")
                L.append(f"    {out}[{dtc_col!r}] = _iso({out}[{dtc_src!r}]).values")
        elif op == "transpose_wide":
            src = srcref(p.get("dataset"))
            L.append(f"{out} = {src}.pivot_table("
                     f"index=[_c for _c in {list(p.get('index') or [])!r} if _c in {src}.columns], "
                     f"columns={p.get('columns')!r}, values={p.get('values')!r}, aggfunc='first').reset_index()")
            L.append(f"{out}.columns = [str(_c).upper() for _c in {out}.columns]")
        elif op == "aggregate":
            src = srcref(p.get("dataset"))
            out_col = p.get("out_col")
            L.append(f"_src = {src}")
            L.append(f"_gb = [_x for _x in (_ci_col(_src, _c) for _c in {list(p.get('group_by') or [])!r}) if _x]")
            L.append(f"_col = _ci_col(_src, {p.get('column')!r}) or {p.get('column')!r}")
            L.append(f"_fn = {p.get('func', 'min')!r}")
            L.append("if _fn in ('min', 'max'):")
            L.append("    _dt = pd.to_datetime(_src[_col], errors='coerce')")
            L.append("    if _dt.notna().any():")
            L.append("        _g = _src.assign(_v=_dt).groupby(_gb)['_v'].agg(_fn).dt.strftime('%Y-%m-%d')")
            L.append("    else:")
            L.append("        _g = _src.groupby(_gb)[_col].agg(_fn)")
            L.append("else:")
            L.append("    _g = _src.groupby(_gb)[_col].agg(_fn)")
            L.append(f"_oc = {out_col!r} or _col")
            L.append(f"{out} = _g.reset_index().rename(columns={{_col: _oc, '_v': _oc}})")
        elif op == "date_extreme":
            fn = p.get("func", "min")
            grp_req = p.get("group_by") or [str(p.get("subject") or "SUBJID").strip() or "SUBJID"]
            grp_req = [str(g).strip().upper() for g in grp_req if str(g).strip()]
            out_col = p.get("out_col") or ("RFSTDTC" if fn == "min" else "RFENDTC")
            L.append('_SUBJ_ALTS = ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID", "SCRNID")')
            L.append("_frames = []")
            for s in (p.get("sources") or []):
                if not s.get("dataset"):
                    continue
                ref = srcref(s.get("dataset"))
                want0 = str(s.get("subject_col") or grp_req[0]).upper()
                L.append(f"_d = {ref}")
                L.append("_cu = {str(_c).upper(): _c for _c in _d.columns}")
                L.append("_resolved, _ok = [], True")
                L.append(f"_hit = _cu.get({want0!r}) or next((_cu[_a] for _a in _SUBJ_ALTS if _a in _cu), None)")
                L.append("if _hit is None:")
                L.append("    _ok = False")
                L.append("else:")
                L.append("    _resolved.append(_hit)")
                L.append(f"for _g in {grp_req[1:]!r}:")
                L.append("    _h = _cu.get(_g)")
                L.append("    if _h is None:")
                L.append("        _ok = False")
                L.append("        break")
                L.append("    _resolved.append(_h)")
                L.append(f"_dc = {s.get('date_col')!r}")
                L.append("if _ok and (not _dc or _dc not in _d.columns):")
                L.append("    _ok = False")
                L.append("if _ok:")
                L.append("    _t = _d[_resolved + [_dc]].copy()")
                L.append("    _t.columns = [f'__g{_gi}' for _gi in range(len(_resolved))] + ['__d']")
                L.append("    _frames.append(_t)")
            L.append("_alld = pd.concat(_frames, ignore_index=True)")
            L.append("_alld['__d'] = pd.to_datetime(_alld['__d'], errors='coerce')")
            L.append(f"_gcols = [f'__g{{_gi}}' for _gi in range(len({grp_req!r}))]")
            L.append(f"_agg = _alld.groupby(_gcols)['__d'].agg({fn!r})")
            L.append(f"{out} = _agg.dt.strftime('%Y-%m-%d').reset_index()")
            L.append(f"{out}.columns = {grp_req!r} + [{out_col!r}]")
        else:
            # Unknown / unsupported op: load the input unchanged (run_prep's fallback).
            L.append(f"{out} = {srcref(p.get('dataset', ''))}   # op {op!r} not specialised")
        # Wrap the step: run_prep tolerates a failing step (e.g. a derive whose
        # condition column a prior rename never produced), skipping its output and
        # continuing. Mirror that so one dead branch does not sink the pipeline.
        body = L[body_start:]
        del L[body_start:]
        L.append("try:")
        L += ["    " + ln for ln in body]
        L.append(f"    raw_{pn} = _prep_norm(_out)")
        L.append(f"    _prep_ok.add({pn!r})")
        L.append("except Exception as _e:")
        L.append(f"    print({f'prep step {name} skipped:'!r}, repr(_e))")
        L.append("")
        produced.add(pn)
    return {"lines": L, "produced": produced, "raw_reads": raw_reads,
            "sdoms": sdoms, "needs_iso": state["needs_iso"]}


def _py_supp_block(study: str, domain: str, df: str, supp_blocks: list) -> list:
    """Code emitting SUPP{DOM} from the built domain frame: one row per parent
    --SEQ where QVAL is non-missing (mirrors build_exec._supp_from_built)."""
    dom = domain.upper()
    specs = [(str(b["variable"]).upper(),
              str(b.get("qlabel") or b.get("label") or ""),
              str(b.get("qorig") or b.get("origin") or "CRF"))
             for b in supp_blocks if b.get("variable")]
    L = [f"# --- SUPP{dom}: supplemental qualifiers (one row per parent --SEQ, non-missing QVAL) ---",
         f"_supp_specs = {specs!r}",
         "_supp_rows = []",
         f"_scols = {{str(_c).upper(): _c for _c in {df}.columns}}",
         "_usub = _scols.get('USUBJID')",
         f"_seqc = _scols.get({f'{dom}SEQ'!r})",
         "_studyc = _scols.get('STUDYID')",
         f"_idvar = {f'{dom}SEQ'!r} if _seqc else ''",
         "def _supp_blank(_x):",
         "    _s = '' if _x is None else str(_x).strip()",
         "    return _s in ('', 'nan', 'none', 'None', '<NA>', 'NaN', 'NaT')",
         "if _usub:",
         f"    for _, _row in {df}.iterrows():",
         "        _u = _row[_usub]",
         "        if _supp_blank(_u):",
         "            continue",
         "        _sid = '' if not _studyc or _supp_blank(_row[_studyc]) else str(_row[_studyc])",
         "        _idval = '' if not _seqc or _supp_blank(_row[_seqc]) else str(_row[_seqc]).split('.')[0]",
         "        for _qn, _ql, _qo in _supp_specs:",
         "            if _qn not in _scols:",
         "                continue",
         "            _val = _row[_scols[_qn]]",
         "            if _supp_blank(_val):",
         "                continue",
         f"            _supp_rows.append({{'STUDYID': _sid, 'RDOMAIN': {dom!r}, 'USUBJID': str(_u),",
         "                               'IDVAR': _idvar, 'IDVARVAL': _idval, 'QNAM': _qn,",
         "                               'QLABEL': _ql, 'QVAL': _val, 'QORIG': _qo, 'QEVAL': ''})",
         f"supp_{df} = pd.DataFrame(_supp_rows, columns={_SUPP_COLS!r})",
         f"if len(supp_{df}):",
         f"    supp_{df} = supp_{df}.sort_values(['USUBJID', 'QNAM', 'IDVARVAL'], kind='stable').reset_index(drop=True)",
         f"# supp_{df}.to_parquet('sdtm/output/{study}/SUPP{dom}.parquet', index=False)",
         f"print('SUPP{dom}:', len(supp_{df}), 'rows')"]
    return L


# ── Python program (the real build logic) ────────────────────────────────────
def python_program(study: str, domain: str, blocks: list, settings: dict | None = None,
                   prep_steps: list | None = None) -> str:
    df = domain.lower()
    if prep_steps is None:
        prep_steps = _saved_prep_steps(study, domain)
    _enrich_supp(study, domain, blocks)
    for b in blocks:
        b["code"] = _upper_raw_cols(_code_from_struct(b, domain))
    full = "\n".join(b.get("code", "") for b in blocks)

    # Prep pipeline (KNIME-style) that assembles the domain base before the build.
    prep = _py_prep_pipeline(prep_steps) if prep_steps else None
    produced = prep["produced"] if prep else set()
    # the assembled record-grain base, if the pipeline builds one (<dom>_base)
    abase = next((n for n in (f"{df}_base", f"{df}base") if n in produced), None)

    # raw-source reads: block references + prep inputs, minus prep-produced names
    raw_reads: dict = {}
    for ds in re.findall(r"\braw_([A-Za-z0-9_]+)\b", full):
        if ds not in produced:
            raw_reads[ds] = ds
    if prep:
        for pn, raw in prep["raw_reads"].items():
            if pn not in produced:
                raw_reads.setdefault(pn, raw)
    if abase:
        # raw_<df> is the prepared base (bound below), never a parquet read.
        raw_reads.pop(df, None)

    L = ["# " + "=" * 68,
         f"# SDTM {domain.upper()} — mapping program (Python / pandas)",
         "# Auto-generated by SDTM Studio from the build engine. Review before use.",
         "# " + "=" * 68,
         "import pandas as pd", "import numpy as np", ""]
    # partial-aware ISO date helper (used by iso_date derivations)
    if "_iso_parts(" in full:
        L += [inspect.getsource(_iso_parts).replace("_ISO_BAD_TOKENS", repr(
            {"", "NAN", "NONE", "NAT", "UN", "UNK", "UNKN", "UNKNOWN", ".", "--", "NA"})), ""]
    if prep:
        L += _py_prep_helpers(study, prep["needs_iso"])
        L.append("")
    if raw_reads:
        L.append("# --- raw (source) datasets ---")
        L += [f"raw_{pn} = pd.read_parquet('synth/{study}/{raw}.parquet')"
              for pn, raw in sorted(raw_reads.items())]
        L.append("")
    _sdoms = set(re.findall(r"\bsdtm_([a-z0-9]+)\b", full)) | (prep["sdoms"] if prep else set())
    if _sdoms:
        L.append("# --- built SDTM domains referenced by derivations / prep ---")
        L += [f"sdtm_{d} = pd.read_parquet('sdtm/output/{study}/{d.upper()}.parquet')"
              for d in sorted(_sdoms)]
        L.append("")
    if "_sx_" in full or (prep and any("_sx_" in ln for ln in prep["lines"])):
        # SAS functions / operators / formats with SAS semantics (sas_runtime) — before the prep
        # pipeline, which may compute columns with it
        L += ["# --- SAS runtime: functions, operators, formats and informats as SAS evaluates them ---",
              inspect.getsource(sas_runtime), ""]
    if prep:
        L.append("# --- prep pipeline: assemble the domain base (reproduces run_prep) ---")
        L += prep["lines"]
    refs = sorted({str((b.get("args", {}) or {}).get("ref_var") or "RFSTDTC").upper()
                   for b in blocks if b.get("recipe") == "study_day"}
                  | {str((b.get("args", {}) or {}).get("ref_var") or "RFXSTDTC").upper()
                     for b in blocks if b.get("recipe") == "lobxfl"})
    if refs and domain.upper() != "DM":
        L += ["# --- DM reference dates (by USUBJID) for --DY / baseline flags ---",
              f"_dm = pd.read_parquet('sdtm/output/{study}/DM.parquet').drop_duplicates('USUBJID').set_index('USUBJID')",
              f"_DMREF = {{c: _dm[c] for c in {sorted(refs)!r} if c in _dm.columns}}", ""]
    else:
        L += ["_DMREF = {}", ""]
    if "_sref(" in full:
        L += ["# cross-domain SDTM lookup by subject (fill in built domains as needed)",
              "def _sref(dom, var):",
              "    return pd.Series(dtype='object')", ""]

    # A variable the prepared base may supply verbatim (the engine's non-destructive
    # Prep integration): anything but STUDYID/DOMAIN and the --SEQ/sequence blocks.
    def _base_candidate(b):
        v = str(b.get("variable", "")).upper()
        return not (v in ("STUDYID", "DOMAIN") or v.endswith("SEQ")
                    or b.get("mtype") == "sequence")

    if abase:
        cands = [str(b["variable"]).upper() for b in blocks if _base_candidate(b)]
        # raw_<df> is the exec engine's pre-overlay record-grain pick (the
        # most-common assign source that has data), which row-aligned assigns read
        # — NOT the assembled base. Preserve that so per-recipe derivations match.
        from collections import Counter as _Counter
        _assign_ds = [str(b.get("dataset") or "").lower() for b in blocks
                      if b.get("mtype") == "assign" and b.get("dataset")]
        _cand_order = [d for d, _n in _Counter(_assign_ds).most_common()]
        _prep_cands = [c for c in _cand_order if _pn(c) in produced]
        _exprs = [f"(raw_{_pn(c)} if {_pn(c)!r} in _prep_ok else _prep_fallback({c!r}))"
                  for c in _prep_cands] + \
                 [(f"(raw_{_pn(c)} if {_pn(c)!r} in _prep_ok else _prep_fallback({c!r}))"
                   if _pn(c) in produced else f"raw_{_pn(c)}") for c in _cand_order]
        L += ["# --- domain base: the prepared base supplies matching SDTM variables verbatim ---",
              f"_base = raw_{abase}",
              "def _nz(_x):",
              "    try:",
              "        return _x is not None and len(_x) > 0",
              "    except Exception:",
              "        return False",
              f"raw_{df} = _base   # this domain's base, as Domain Build binds it",
              f"_PREP_SRC = {sorted(_prep_sources(prep_steps))!r}   # forms the base was built from",
              f"{df} = pd.DataFrame(index=_base.index)",
              "_ROWS = _base   # the frame whose rows the output follows",
              "_bcols = {str(_c).upper(): _c for _c in _base.columns}",
              "_from_base = set()",
              f"for _v in {cands!r}:",
              "    if _v in _bcols:",
              f"        {df}[_v] = _base[_bcols[_v]].values",
              "        _from_base.add(_v)", ""]
    else:
        base = f"raw_{df}" if df in raw_reads else (f"raw_{sorted(raw_reads)[0]}" if raw_reads else None)
        L.append("# --- target dataset (one row per source record) ---")
        L.append(f"{df} = pd.DataFrame(index={base}.index)" if base else f"{df} = pd.DataFrame(index=range(20))")
        if base:
            L.append(f"_ROWS = {base}   # the frame whose rows the output follows")
        L.append("")

    def _emit_block(b, note=""):
        v = str(b.get("variable", "")).upper()
        L.append(f"# {b['variable']}: {b.get('label') or ''}{note}".rstrip())
        code = b.get("code", "") or f"# {b['variable']}: (no code)"
        # A comment-only / no-op block (e.g. a dropped variable) produces nothing,
        # so there is nothing to guard — wrapping it in `if:` would be an empty body.
        has_stmt = any(ln.strip() and not ln.strip().startswith("#") for ln in code.splitlines())
        if abase and _base_candidate(b) and has_stmt:
            # taken from the prepared base when present, else derived (engine parity)
            L.append(f"if {v!r} not in _from_base:")
            L.append(textwrap.indent(code, "    "))
        else:
            L.append(code)

    L.append("# --- variable derivations ---")
    seq_blocks, late_blocks = [], []
    for b in blocks:
        if b.get("mtype") == "sequence":
            seq_blocks.append(b)
            continue
        if b.get("recipe") in ("lobxfl", "sas_expr", *CARRY_RECIPES) or b.get("_late"):
            late_blocks.append(b)
            continue
        _emit_block(b)
    late_blocks.sort(key=lambda b: b.get("recipe") in CARRY_RECIPES)   # RETAIN / LAG last
    for b in late_blocks:
        _emit_block(b, note="  (reads other variables of this domain — after them)")
    _pv_args = _seq_collapse_args(blocks, df)
    if _pv_args:
        L += ["", _seq_collapse_code(df, _pv_args)]
    L.append("")
    settings = settings or {}
    sort = [s for s in (settings.get("sort") or []) if s.get("var")]
    if sort:
        L += ["# --- finalize: sort the dataset ---",
              f"{df} = {df}.sort_values(by={[s['var'] for s in sort]!r}, "
              f"ascending={[str(s.get('dir', 'asc')).lower() != 'desc' for s in sort]!r}, "
              "kind='mergesort', na_position='last')"]
    dd = settings.get("dedup") or {}
    if dd.get("enabled") and dd.get("keys"):
        L += ["# --- finalize: keep first/last row per group ---",
              f"{df} = {df}.drop_duplicates(subset={list(dd['keys'])!r}, keep={(dd.get('keep') or 'first')!r})"]
    if sort or (dd.get("enabled") and dd.get("keys")):
        L.append("")
    if seq_blocks:
        L.append("# --- sequence (--SEQ), numbered on the final order ---")
        L += [b.get("code", "") for b in seq_blocks]
        L.append("")
    # SUPP-- is built from the full frame (supp columns still present, --SEQ assigned)
    supp_blocks = [b for b in blocks if b.get("supp")]
    if supp_blocks:
        L += _py_supp_block(study, domain, df, supp_blocks)
        L.append("")
    order = [b["variable"] for b in blocks if not b.get("supp")]
    L += ["# --- column order (per spec) ---",
          f"{df} = {df}[[c for c in {order!r} if c in {df}.columns]].reset_index(drop=True)", "",
          f"# {df}.to_parquet('sdtm/output/{study}/{domain.upper()}.parquet', index=False)",
          f"print({df}.head(20))"]
    return "\n".join(L)


# ── SAS program (deterministic where possible; comments for complex derivations) ──

# Deliberately stricter than float(). Anything emitted unquoted becomes a
# NUMERIC assignment in SAS, so float() — which accepts '0002', 'nan', 'inf'
# and '1e5' — was turning character values into numbers:
#   '0002' -> STUDYID = 0002;  i.e. the number 2, leading zeros gone, on
#             identifiers that carry them.
#   'nan'  -> X = nan;         not a SAS literal at all. It parses as an
#             unresolved variable reference: a missing value and a log NOTE,
#             never an error.
#
# \Z rather than $: Python's $ also matches just before a trailing newline, so
# "5\n" matched and was emitted bare, putting a line break into the program.
_SAS_NUMERIC = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?\Z")

# SAS names: 32 characters, letter or underscore first.
_SAS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}\Z")


def _sas_quote(v):
    """Render a mapping-spec value as a SAS literal, character unless certain.

    SINGLE-quoted, deliberately. SAS resolves macro references inside
    double-quoted literals, so "Nausea & vomiting" — an ordinary adverse-event
    term — makes SAS look up a macro variable named `vomiting`, and a value
    containing `%` can invoke a macro. Single-quoted literals are inert, which
    is the only correct treatment for a value that is data.
    """
    if isinstance(v, bool):
        # bool is an int subclass; `X = True;` is not SAS.
        return "'" + str(v) + "'"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v) if math.isfinite(v) else "'" + str(v) + "'"
    s = "" if v is None else str(v)
    if _SAS_NUMERIC.match(s):
        return s
    return "'" + s.replace("'", "''") + "'"


def _sas_comment_safe(v) -> str:
    """Neutralise comment delimiters in text bound for a /* ... */ comment.

    A `*/` inside a comment closes it, and everything after it becomes
    executable SAS. Nothing validates block identifiers upstream, and the
    generated program is run by a different person than the one who authored
    the spec, so this is not a theoretical concern.
    """
    return str(v).replace("*/", "* /").replace("/*", "/ *")


def _sas_banner(gaps: list[str]) -> list[str]:
    """A loud header naming everything the generator could not express in SAS.

    Studio does not run this program — the programmer pastes it into SAS Viya.
    So anything we cannot generate has to be stated here, at the top, before
    the code. The alternative is a DATA step that runs cleanly and produces the
    wrong dataset, which is the one outcome this product cannot afford.

    No banner at all when there is nothing to report: a warning that always
    appears is a warning nobody reads.
    """
    out = ["/* " + "=" * 68,
           " * INCOMPLETE — DO NOT RUN AS-IS",
           " *",
           " * SDTM Studio could not express the whole mapping for this domain in",
           " * SAS. Each item below must be written by hand. Until then this program",
           " * produces a dataset that is wrong in ways the SAS log will NOT report",
           " * as errors.",
           " *"]
    # Wrapped to the same width as the rest of the file's headers. An
    # unwrapped 160-column line in a SAS editor is a line nobody finishes.
    for g in gaps:
        for i, para in enumerate(_sas_comment_safe(g).split("\n")):
            lines = textwrap.wrap(para, width=64) or [""]
            bullet = "   - " if i == 0 else "     "
            out.append(f" *{bullet}{lines[0]}")
            out += [f" *     {line}" for line in lines[1:]]
    out += [" *",
            " * The Python program generated for this domain implements all of the",
            " * above; use it as the reference.",
            " " + "=" * 68 + " */", ""]
    return out


# Subject-key candidates, in the priority the run-engines use (build_exec `_SK`
# / build_engine `_subj_col`). A source is joined to the spine on the first of
# these it shares; the SAS emitter standardises each source onto USUBJID.
_SUBJECT_KEYS = ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID")

# Reference-date variables a study_day may read from DM. (--DY = (--DTC - ref)+1.)
_REF_DATE_VARS = ("RFSTDTC", "RFXSTDTC", "RFICDTC", "RFENDTC", "RFXENDTC")


def resolve_subject_keys(study: str, datasets) -> dict:
    """{dataset(lower) -> its subject-key column(UPPER)} by peeking raw columns.

    The SAS MERGE needs every source keyed on one name. The run-engines pick,
    per source, the first of `_SUBJECT_KEYS` the frame actually has; mirror that
    here so the emitter renames each source's real key to USUBJID. Best-effort:
    a dataset we cannot load is simply absent (the emitter then flags it rather
    than guessing a join), so this never raises."""
    from .build_exec import _load_source
    out: dict = {}
    for ds in {str(d).lower() for d in datasets if d}:
        try:
            df = _load_source(study, ds)
        except Exception:  # noqa: BLE001 — missing source handled by the emitter
            df = None
        if df is None:
            continue
        cols = {str(c).upper() for c in df.columns}
        key = next((k for k in _SUBJECT_KEYS if k in cols), None)
        if key:
            out[ds] = key
    return out


def _blocks_datasets(blocks) -> set:
    """Every raw source dataset a block list references (assign column, value
    sources, coalesce/date_extreme/iso_date args)."""
    out = set()
    for b in blocks:
        a = b.get("args") or {}
        if b.get("dataset"):
            out.add(str(b["dataset"]).lower())
        for x in (a.get("value_sources") or []):
            if x.get("dataset"):
                out.add(str(x["dataset"]).lower())
        for x in (a.get("sources") or []):
            if x.get("dataset"):
                out.add(str(x["dataset"]).lower())
        if a.get("dataset"):
            out.add(str(a["dataset"]).lower())
    return out


def _sas_iso_date_expr(raw_expr: str, tmp: str = "_dt") -> list:
    """SAS lines assigning the partial-aware ISO date of `raw_expr` into `tmp`
    (SAS date number, missing when unparseable) — the input()+anydtdte path that
    mirrors the Python `_iso_parts` full-date case."""
    return [f"  {tmp} = input(strip({raw_expr}), ?? anydtdte32.);"]


# ── SAS prep-pipeline emitter ────────────────────────────────────────────────
_SAS_FN_VALUE = re.compile(r"^\s*(upper|upcase|lower|lowcase|strip|trim)\s*\(\s*([A-Za-z0-9_]+)\s*\)\s*$", re.I)


def _sas_str_lit(v) -> str:
    """Always a single-quoted SAS character literal (prep values are data)."""
    return "'" + str("" if v is None else v).replace("'", "''") + "'"


def _sas_derive_value(value, known_cols: set | None = None) -> str:
    """A prep derive value -> SAS expression (mirrors prep_engine._derive_value):
    UPPER/LOWER/STRIP(col) transforms, a bare column reference, or a literal.

    A bare single token is a column COPY only when it names a column the pipeline
    is known to have produced (`known_cols`); otherwise it is a literal — the same
    distinction prep_engine makes at runtime via `_ci_col` (a CT term like
    'RANDOMIZED' stays a quoted literal, a carried column like ENROLDAT_RAW copies)."""
    s = "" if value is None else str(value)
    m = _SAS_FN_VALUE.match(s)
    if m and _SAS_NAME.match(m.group(2).upper()):
        col, fn = m.group(2).upper(), m.group(1).lower()
        if fn in ("upper", "upcase"):
            return f"upcase({col})"
        if fn in ("lower", "lowcase"):
            return f"lowcase({col})"
        return f"strip({col})"
    if (re.match(r"^[A-Za-z0-9_]+$", s) and _SAS_NAME.match(s.upper())
            and known_cols is not None and s.upper() in known_cols):
        return s.upper()          # a bare token naming a produced column -> copy it
    return _sas_str_lit(s)


def _sas_prep_cond(column, operator, value) -> str:
    """One prep condition -> a SAS boolean on upcase(strip(col)) (mirrors
    prep_engine._prep_cond_mask's case-insensitive, trimmed comparison)."""
    from .prep_engine import _prep_op
    col = str(column or "").upper()
    if not _SAS_NAME.match(col):
        return "1"
    try:
        opr = _prep_op(operator)
    except ValueError:
        return f"0 /* operator {_sas_comment_safe(str(operator))[:20]} is not supported */"
    vraw = value
    val = ",".join(str(x) for x in vraw) if isinstance(vraw, list) else str("" if vraw is None else vraw)
    s = f"upcase(strip({col}))"
    blank = f"(missing({col}) or upcase(strip({col})) in ('NAN','NONE','NAT'))"
    if opr == "missing":
        return blank
    if opr == "notmissing":
        return f"not {blank}"
    if opr == "contains":
        return f"index(upcase({col}), {_sas_str_lit(val.upper())}) > 0"
    if opr in ("in", "notin"):
        items = [x.strip().upper() for x in val.split(",") if x.strip()]
        inlist = "(" + ", ".join(_sas_str_lit(x) for x in items) + ")"
        return f"{s} {'not in' if opr == 'notin' else 'in'} {inlist}"
    if opr == "!=":
        return f"{s} ne {_sas_str_lit(val.strip().upper())}"
    if opr in (">", ">=", "<", "<="):   # another column, a number, or text — as prep_engine compares
        v = val.strip()
        if _SAS_NAME.match(v.upper()) and not _SAS_NUMERIC.match(v):
            return f"strip({col}) {opr} strip({v.upper()})"
        if _SAS_NUMERIC.match(v):
            return f"input(strip({col}), ?? best32.) {opr} {v}"
        return f"strip({col}) {opr} {_sas_str_lit(v)}"
    return f"{s} = {_sas_str_lit(val.strip().upper())}"


def _nrb(sas_literal: str) -> str:
    """Wrap an (already single-quoted, inert) SAS literal so it survives the macro
    processor's parameter parsing — %nrbquote masks the comma/paren/&/%/; that
    would otherwise split the call or trigger macro resolution. The value is still
    a plain SAS character literal once the macro drops it into the DATA step."""
    return f"%nrbquote({sas_literal})"


# ── Canonical SDTM Studio SAS macro library ──────────────────────────────────
# ONE source of truth for the reusable build macros. The same patterns recur
# across every domain AND every study, so they are defined once here, surfaced in
# the in-app Functions reference (routers/functions.py reads this), and emitted
# into a generated program only when it uses them. Design rule: a macro's STRUCTURE
# is fixed; identifiers (var/column names) pass as plain tokens, the few controlled
# labels pass %nrbquote-protected, and patient data is always read as data via
# vvalue(col) — clinical/spec text never reaches the SAS macro processor.
SDTM_MACRO_LIBRARY = [
    {"id": "sdtm_ren", "function_id": "rename", "title": "Rename columns on a copy",
     "sas": [
         "%macro sdtm_ren(out=, src=, pairs=);   /* rename columns on a copy */",
         "  data &out; set &src; rename &pairs; run;",
         "%mend sdtm_ren;"]},
    {"id": "sdtm_fnd", "function_id": "transpose_findings", "title": "One findings measure -> a row",
     "sas": [
         "%macro sdtm_fnd(tcdv=, tcd=, tstv=, tst=, orv=, src=, oruv=, unit=,",
         "                 catv=, cat=, dtcv=, dtcs=);   /* one findings measure -> a row */",
         "  &tcdv = &tcd;",
         "  &tstv = &tst;",
         "  &orv = strip(&src);",
         "  %if %length(%superq(oruv)) %then %do;",
         "    %if %length(%superq(unit)) %then %do; &oruv = strip(&unit); %end;",
         "    %else %do; &oruv = ''; %end;",
         "  %end;",
         "  %if %length(%superq(catv)) and %length(%superq(cat)) %then %do; &catv = &cat; %end;",
         "  %if %length(%superq(dtcv)) and %length(%superq(dtcs)) %then %do;",
         "    &dtcv = put(input(strip(&dtcs), ?? anydtdte32.), E8601DA10.);",
         "  %end;",
         "  if strip(&orv) ne '' then output;",
         "%mend sdtm_fnd;"]},
    {"id": "sdtm_supp", "function_id": "supp", "title": "One SUPP-- qualifier row",
     "sas": [
         "%macro sdtm_supp(var=, qlabel=, qorig=);   /* one SUPP qualifier row */",
         "  if not missing(&var) and strip(vvalue(&var)) ne '' then do;",
         "    QNAM = \"&var\"; QLABEL = &qlabel; QORIG = &qorig;",
         "    QVAL = strip(vvalue(&var)); output;",
         "  end;",
         "%mend sdtm_supp;"]},
    {"id": "sdtm_dy", "function_id": "study_day", "title": "Study day (--DY), no day 0",
     "sas": [
         "%macro sdtm_dy(dy=, dtc=, ref=);   /* --DY = (--DTC - ref) + 1, no day 0 */",
         "  _ev = input(substr(strip(&dtc), 1, 10), ?? E8601DA.);",
         "  _rf = input(substr(strip(&ref), 1, 10), ?? E8601DA.);",
         "  if not missing(_ev) and not missing(_rf) then do;",
         "    &dy = _ev - _rf;",
         "    if &dy >= 0 then &dy = &dy + 1;",
         "  end;",
         "%mend sdtm_dy;"]},
    {"id": "sdtm_iso", "function_id": "iso_date", "title": "Raw date -> ISO 8601 --DTC (partial kept)",
     "sas": [
         "%macro sdtm_iso(var=, src=, time=);   /* raw date -> ISO 8601 --DTC (partial kept) */",
         "  length &var $19;",
         "  _dt = input(strip(&src), ?? anydtdte32.);",
         "  if not missing(_dt) then &var = put(_dt, E8601DA10.);",
         "  else if prxmatch('/^\\d{4}(-\\d{2}){0,2}$/', strip(&src)) then &var = strip(&src);",
         "  %if %length(%superq(time)) %then %do;",
         "    _tm = strip(&time);",
         "    if length(&var) >= 10 and not missing(_tm)",
         "       and upcase(_tm) not in ('NAN','NONE','NAT','UN','UNK') then &var = cats(&var, 'T', _tm);",
         "  %end;",
         "%mend sdtm_iso;"]},
    # Post-build derivations (run on the finished domain; read other SDTM domains).
    {"id": "sdtm_epoch", "function_id": "epoch", "title": "EPOCH from the SE element interval",
     "sas": [
         "%macro sdtm_epoch(ds=, refc=);",
         "  /* EPOCH = the SE element whose [SESTDTC, SEENDTC] interval contains the record's &refc",
         "   * (SDTMIG / PharmaSUG SE-interval method). Fills only blank EPOCH; keeps row order. */",
         "  data _ep_base; set &ds; _ep_ord = _n_;",
         "    _ep_dt = input(substr(strip(&refc), 1, 10), ?? E8601DA.); run;",
         "  proc sql;",
         "    create table _ep_j as",
         "      select a._ep_ord, b.EPOCH as _ep_val length=40,",
         "             input(substr(b.SESTDTC, 1, 10), ?? E8601DA.) as _ep_st",
         "      from _ep_base a inner join sdtm.se b",
         "        on strip(a.USUBJID) = strip(b.USUBJID)",
         "       and not missing(a._ep_dt)",
         "       and a._ep_dt >= input(substr(b.SESTDTC, 1, 10), ?? E8601DA.)",
         "       and (missing(b.SEENDTC) or a._ep_dt <= input(substr(b.SEENDTC, 1, 10), ?? E8601DA.));",
         "  quit;",
         "  proc sort data=_ep_j; by _ep_ord _ep_st; run;   /* earliest-starting containing element wins */",
         "  data _ep_pick; set _ep_j; by _ep_ord; if first._ep_ord; keep _ep_ord _ep_val; run;",
         "  proc sort data=_ep_base; by _ep_ord; run;",
         "  data &ds;",
         "    merge _ep_base(drop=_ep_dt) _ep_pick; by _ep_ord;",
         "    if missing(EPOCH) and not missing(_ep_val) then EPOCH = _ep_val;",
         "    drop _ep_ord _ep_val;",
         "  run;",
         "%mend sdtm_epoch;"]},
    {"id": "sdtm_lobxfl", "function_id": "lobxfl", "title": "Last-obs-before-exposure flag (--LOBXFL)",
     "sas": [
         "%macro sdtm_lobxfl(ds=, flag=, by=, dtc=, res=, ref=RFXSTDTC);",
         "  /* --LOBXFL = 'Y' on the last non-missing record per &by on/before first exposure",
         "   * (DM.&ref) — SDTMIG baseline-flag method. Keeps the domain's row order. */",
         "  proc sort data=sdtm.dm(keep=USUBJID &ref) out=_lbx_ref nodupkey; by USUBJID; run;",
         "  data _lbx_base; set &ds; _lbx_ord = _n_; run;",
         "  proc sort data=_lbx_base; by USUBJID; run;",
         "  data _lbx_all;",
         "    merge _lbx_base(in=_a) _lbx_ref; by USUBJID; if _a;",
         "    _lbx_obs = input(substr(strip(&dtc), 1, 10), ?? E8601DA.);",
         "    _lbx_rfx = input(substr(strip(&ref), 1, 10), ?? E8601DA.);",
         "    _lbx_elig = (not missing(_lbx_obs)) and (not missing(_lbx_rfx)) and (_lbx_obs <= _lbx_rfx)",
         "      %if %length(%superq(res)) %then %do; and (strip(&res) ne '') %end; ;",
         "  run;",
         "  proc sort data=_lbx_all(where=(_lbx_elig)) out=_lbx_elg; by &by _lbx_obs _lbx_ord; run;",
         "  data _lbx_win; set _lbx_elg; by &by;",
         "    if last.%scan(&by, -1, %str( )); keep _lbx_ord; run;",
         "  proc sort data=_lbx_win; by _lbx_ord; run;",
         "  proc sort data=_lbx_all out=_lbx_ord_s; by _lbx_ord; run;",
         "  data &ds;",
         "    merge _lbx_ord_s(drop=&ref _lbx_obs _lbx_rfx _lbx_elig) _lbx_win(in=_w); by _lbx_ord;",
         "    if _w then &flag = 'Y';",
         "    drop _lbx_ord;",
         "  run;",
         "%mend sdtm_lobxfl;"]},
]

# macro id -> its SAS source lines (built from the canonical library above)
_SAS_MACRO_BY_ID = {m["id"]: m["sas"] for m in SDTM_MACRO_LIBRARY}
# the short need-key used by the emitters ("ren"/"fnd"/…) -> macro id ("sdtm_ren"/…)
_MACRO_NEED_TO_ID = {m["id"].replace("sdtm_", ""): m["id"] for m in SDTM_MACRO_LIBRARY}


def sdtm_macro_library_sas() -> str:
    """The whole shared macro library as one SAS snippet (for the Functions
    reference and for anyone deploying it as an autocall / %include library)."""
    out = ["/* " + "=" * 64,
           " * SDTM Studio — shared SAS build macro library",
           " * Reusable across every domain and study. Deploy once as an autocall",
           " * library or %include; generated programs call these macros.",
           " " + "=" * 64 + " */"]
    for m in SDTM_MACRO_LIBRARY:
        out += m["sas"] + [""]
    return "\n".join(out)


def _sas_macro_library(need: set) -> list:
    """The subset of the shared macro library a generated program actually uses,
    emitted once near the top (self-contained program). Definitions come from the
    canonical SDTM_MACRO_LIBRARY so there is a single source of truth."""
    L = ["/* " + "=" * 64,
         " * SDTM Studio shared macros (canonical copies live in the app's",
         " * Functions reference; a site can instead autocall/%include them).",
         " " + "=" * 64 + " */"]
    for key in ("ren", "fnd", "supp", "dy", "iso", "epoch", "lobxfl"):   # stable order
        if key in need:
            L += _SAS_MACRO_BY_ID[_MACRO_NEED_TO_ID[key]] + [""]
    return L



def _sas_carry_step(src, dst, tgt, col, by, order, mode, n=1) -> list:
    """RETAIN / LAG / DIF of `col` into `tgt` within BY groups, in BY + ORDER order, as SAS:
    tag the input order, sort, one BY-group DATA step, sort back. mode: retain | first | lag | dif."""
    last = by[-1] if by else None
    L = [f"data _cf; set {src}; _ord_ = _n_; run;",
         f"proc sort data=_cf; by {' '.join([*by, *order, '_ord_'])}; run;",
         f"data {dst};", "  set _cf;"] + ([f"  by {' '.join(by)};"] if by else [])
    if mode in ("retain", "locf", "first"):
        L += ["  retain _rt;", f"  if 0 then _rt = {col};          /* _rt takes {col}'s type and length */"]
        if last:
            L.append(f"  if first.{last} then call missing(_rt);")
        L += [f"  if missing(_rt) then _rt = {col};" if mode == "first" else f"  if not missing({col}) then _rt = {col};",
              f"  {tgt} = _rt;", "  drop _rt;"]
    else:
        L += [f"  _lg = lag{n}({col});                /* always executed, so the queue stays in step */"]
        L += [f"  if first.{last} then _gn = 0;", "  _gn + 1;"] if last else ["  _gn = _n_;"]
        L += [f"  if _gn <= {n} then call missing(_lg);   /* no earlier record in this group */",
              f"  {tgt} = {col} - _lg;" if mode == "dif" else f"  {tgt} = _lg;", "  drop _lg _gn;"]
    return L + ["run;", f"proc sort data={dst} out={dst}(drop=_ord_); by _ord_; run;"]

_SAS_RESHAPE_OPS = ("transpose_long", "transpose_wide", "aggregate", "date_extreme", "split")


def _sas_names(cols) -> list:
    return [str(c).upper() for c in (cols or []) if c and _SAS_NAME.match(str(c).upper())]


def _sas_reshape_step(op, p, ref, out, name) -> tuple:
    """The reshaping prep steps as SAS (mirroring prep_engine): melt, pivot, a grouped summary,
    the earliest/latest date across datasets, and a split into one dataset per condition.
    Returns (lines, extra dataset stems the step writes)."""
    if op == "transpose_long":
        # melt: one record per (row, column) for every column that is not an id; both character and
        # numeric columns, the numbers converted with BEST12. — what pandas.melt keeps.
        src, idv = ref(p.get("dataset")), _sas_names(p.get("id_vars"))
        vn, vv = str(p.get("var_name") or "TESTCD").upper(), str(p.get("value_name") or "ORRES").upper()
        only = _sas_names(p.get("value_vars"))
        skip = ", ".join(_sas_str_lit(c) for c in [*idv, vn, vv, "_NZ"])
        test = (f"upcase({vn}) in ({', '.join(_sas_str_lit(c) for c in only)})" if only
                else f"upcase({vn}) not in ({skip})")
        loop = []
        for arr, val in (("_mc", f"_mc{{_i}}"), ("_mn", f"strip(put(_mn{{_i}}, best12.))")):
            loop += [f"  do _i = 1 to dim({arr});", f"    {vn} = vname({arr}{{_i}});",
                     f"    if {test} then do; {vv} = {val}; output; end;", "  end;"]
        return ([f"data {out};", f"  set {src};", f"  length {vn} $32 {vv} $200 _nz 8;",
                 "  array _mc{*} _character_;", "  array _mn{*} _numeric_;   /* _nz keeps it non-empty */",
                 *loop, f"  keep {' '.join([*idv, vn, vv])};", "run;"], [])
    if op == "transpose_wide":
        src, idx = ref(p.get("dataset")), _sas_names(p.get("index"))
        col, val = (_sas_names([p.get("columns")]) or [""])[0], (_sas_names([p.get("values")]) or [""])[0]
        if not (col and val):
            return ([f"/* transpose_wide '{name}': needs an ID column and a VAR column */",
                     f"data {out}; set {src}; run;"], [])
        by = " ".join(idx)
        return ([f"proc sort data={src}(where=(not missing({val}))) out=_tw; by {by} {col}; run;",
                 f"data _tw; set _tw; by {by} {col}; if first.{col}; run;   /* the first value, as pandas */",
                 f"proc transpose data=_tw out={out}(drop=_name_);", *([f"  by {by};"] if idx else []),
                 f"  id {col};", f"  var {val};", "run;"], [])
    if op == "aggregate":
        src, gb = ref(p.get("dataset")), _sas_names(p.get("group_by"))
        col = (_sas_names([p.get("column")]) or [""])[0]
        fn = str(p.get("func") or "min").lower()
        oc = (_sas_names([p.get("out_col")]) or [col])[0]
        sfn = {"min": "min", "max": "max", "sum": "sum", "mean": "mean", "count": "count",
               "nunique": "count(distinct", "first": None, "last": None}.get(fn)
        if not col or not gb:
            return ([f"/* aggregate '{name}': needs a column and BY variables */", f"data {out}; set {src}; run;"], [])
        if sfn is None:          # the first / last non-missing value of each group
            by = " ".join(gb)
            return ([f"proc sort data={src}(keep={by} {col} where=(not missing({col}))) out=_ag; by {by}; run;",
                     f"data {out};", f"  set _ag" + (f"(rename=({col}={oc}))" if oc != col else "") + ";",
                     f"  by {by};", f"  if {fn}.{gb[-1]};", "run;"], [])
        cols = ", ".join(gb)
        if fn in ("min", "max"):   # dates when the column holds any (pandas' rule), else the values
            d = f"input(strip({col}), ?? anydtdte32.)"
            return (["proc sql noprint;",
                     f"  select count(*) into :_agd from {src} where not missing({d});",
                     f"  create table {out} as select {cols},",
                     f"    case when &_agd > 0 then put({fn}({d}), e8601da10.) else {fn}({col}) end as {oc}",
                     f"  from {src} group by {cols};", "quit;"], [])
        agg = f"count(distinct {col})" if fn == "nunique" else f"{sfn}({col})"
        return (["proc sql;", f"  create table {out} as select {cols}, {agg} as {oc}",
                 f"  from {src} group by {cols};", "quit;"], [])
    if op == "date_extreme":
        fn = "max" if str(p.get("func") or "min").lower() == "max" else "min"
        grp = _sas_names(p.get("group_by") or [p.get("subject") or "SUBJID"]) or ["SUBJID"]
        oc = (_sas_names([p.get("out_col")]) or ["RFSTDTC" if fn == "min" else "RFENDTC"])[0]
        sel = []
        for s_ in (p.get("sources") or []):
            dc = (_sas_names([s_.get("date_col")]) or [""])[0]
            if not (s_.get("dataset") and dc):
                continue
            g0 = (_sas_names([s_.get("subject_col") or grp[0]]) or [grp[0]])[0]
            keys = ", ".join([f"{g0} as {grp[0]}" if g0 != grp[0] else g0, *grp[1:]])
            sel.append(f"    select {keys}, input(strip({dc}), ?? anydtdte32.) as _d from {ref(s_.get('dataset'))}")
        if not sel:
            return ([f"/* date_extreme '{name}': pick at least one dataset and date column */",
                     f"data {out}; stop; run;"], [])
        cols = ", ".join(grp)
        return (["proc sql;", f"  create table {out} as",
                 f"  select {cols}, case when missing({fn}(_d)) then '' else put({fn}(_d), e8601da10.) end"
                 f" as {oc} length=10",
                 "  from (", "\n    outer union corr\n".join(sel), "  )",
                 f"  where not missing({grp[0]})", f"  group by {cols};", "quit;"], [])
    # split: the first branch is the step's own output, later ones their own names, then the rest
    src = ref(p.get("dataset"))
    branches = p.get("branches") or [{"conds": p.get("conds") or []}]
    outs, stmts, extra = [out], [], []
    for bx, br in enumerate(branches):
        conds = [_sas_prep_cond(c.get("column"), c.get("operator", "=="), c.get("value", ""))
                 for c in (br.get("conds") or [])]
        cexpr = " and ".join(f"({c})" for c in conds) if conds else "1"
        dst = out
        if bx:
            stem = _pn(br.get("name") or f"{name}_{bx + 1}")
            dst = f"prep_{stem[:27]}"
            outs.append(dst)
            extra.append(stem)
        stmts.append(f"  {'if' if bx == 0 else 'else if'} {cexpr} then output {dst};")
    rest = _pn(p.get("other_name") or f"{name}_rest")
    outs.append(f"prep_{rest[:27]}")
    extra.append(rest)
    return ([f"data {' '.join(outs)};", f"  set {src};", *stmts, f"  else output prep_{rest[:27]};", "run;"], extra)


def _sas_prep_pipeline(study: str, domain: str, prep_steps: list) -> dict:
    """Emit SAS that assembles the domain base from the saved Prep pipeline. Each
    step writes a WORK dataset `prep_<name>`, chained. Ops SAS can express
    deterministically (rename/drop/select/filter/derive/compute/carry/uppercase/
    stack/merge/the transposes/aggregate/date_extreme/split) are generated; the
    rest get an honest gap. Returns
    {lines, produced(set), gaps(list)}."""
    produced: set = set()
    gaps: list = []
    L: list = []
    need: set = set()         # which reusable macros this pipeline uses
    known_cols: set = set()   # columns the pipeline is known to create/rename (for value resolution)

    def ref(r) -> str:
        key = str(r or "").strip().lower()
        if not key:
            return "_empty"
        if key.startswith("sdtm."):
            d = key.split(".", 1)[1]
            return f"sdtm.{d}" if _SAS_NAME.match(d.upper()) else "_empty"
        rawkey = key[4:] if key.startswith("raw.") else key
        pn = _pn(rawkey)
        if pn in produced:
            return f"prep_{pn[:27]}"
        return f"raw.{rawkey}" if _SAS_NAME.match(rawkey.upper()) else "_empty"

    steps = list(prep_steps or [])
    out_pn = [_pn((st.get("name") or f"prep{k + 1}").strip().lower()) for k, st in enumerate(steps)]

    def _inputs(st) -> list:
        """Normalised names of the datasets a step reads (for consumer counting)."""
        q = st.get("params", {}) or {}
        names = []
        for key in ("dataset", "left", "right"):
            if q.get(key):
                names.append(q[key])
        names += list(q.get("datasets") or [])
        for spec in (q.get("inputs") or []):
            if spec.get("dataset"):
                names.append(spec["dataset"])
        for s in (q.get("sources") or []):
            if s.get("dataset"):
                names.append(s["dataset"])
        out = []
        for n in names:
            key = str(n).strip().lower()
            key = key[5:] if key.startswith("sdtm.") else (key[4:] if key.startswith("raw.") else key)
            out.append(_pn(key))
        return out

    # How many steps read each produced output (a prep output can be inlined into
    # its producing DATA step only when the single reader is its immediate
    # successor and it is not the assembled domain base).
    consumers: dict = {}
    for st in steps:
        for n in _inputs(st):
            consumers[n] = consumers.get(n, 0) + 1
    base_names = {_pn(f"{domain.lower()}_base"), _pn(f"{domain.lower()}base")}

    def _derive_body(p):
        """Inner statements (no DATA/SET/RUN) + length targets for a `derive`.
        A length is declared only for a genuinely NEW target column — redeclaring
        the length of one already carried in (from SET / an earlier step) is a SAS
        error, which is exactly what a hand-written step avoids."""
        tgt = str(p.get("target") or "").strip().upper()
        if not _SAS_NAME.match(tgt):
            return [], ["  /* derive: target not a SAS name — row passthrough */"]
        is_new = tgt not in known_cols
        stmts = []
        if str(p.get("else_value") or "") != "":
            stmts.append(f"  {tgt} = {_sas_derive_value(p.get('else_value'), known_cols)};")
        # rules in order; a later match overwrites an earlier one (pandas .loc order)
        for r in (p.get("rules") or []):
            conds = [_sas_prep_cond(c.get("column"), c.get("operator", "=="), c.get("value", ""))
                     for c in (r.get("conds") or [])]
            cexpr = " and ".join(f"({c})" for c in conds) if conds else "1"
            stmts.append(f"  if {cexpr} then {tgt} = {_sas_derive_value(r.get('value', ''), known_cols)};")
        known_cols.add(tgt)
        return ([tgt] if is_new else []), stmts

    def _upper_body(p):
        cols = [str(c).upper() for c in (p.get("columns") or []) if _SAS_NAME.match(str(c).upper())]
        known_cols.update(cols)
        return [], [f"  if {c} ne '' then {c} = upcase({c});" for c in cols]

    k = 0
    n = len(steps)
    while k < n:
        st = steps[k]
        op = st.get("op")
        name = (steps[k].get("name") or f"prep{k + 1}").strip().lower()
        pn = out_pn[k]
        out = f"prep_{pn[:27]}"
        p = st.get("params", {}) or {}

        # ── Consolidate a run of consecutive row-wise derive/uppercase steps that
        # chain linearly into ONE DATA step (what a SAS programmer writes by hand),
        # keeping any intermediate a later step actually reads as its own step. ──
        if op in ("derive", "uppercase"):
            run = [k]
            while run[-1] + 1 < n:
                prev = run[-1]
                nxt = prev + 1
                nst = steps[nxt]
                if nst.get("op") not in ("derive", "uppercase"):
                    break
                nin = _inputs(nst)
                # linear chain: the next step's sole input is this step's output,
                # and that output is read only here (and is not the domain base)
                if (nin != [out_pn[prev]] or consumers.get(out_pn[prev], 0) != 1
                        or out_pn[prev] in base_names):
                    break
                run.append(nxt)
            first_src = ref((steps[run[0]].get("params") or {}).get("dataset"))
            last_pn = out_pn[run[-1]]
            lens, body = [], []
            for idx in run:
                sp = steps[idx].get("params", {}) or {}
                bl, bs = (_derive_body(sp) if steps[idx].get("op") == "derive" else _upper_body(sp))
                for t in bl:
                    if t not in lens:
                        lens.append(t)
                nm = (steps[idx].get("name") or "").strip().lower()
                if len(run) > 1:
                    body.append(f"  /* {steps[idx].get('op')} -> {nm} */")
                body += bs
            label = ("derive/uppercase" if len({steps[i].get("op") for i in run}) > 1
                     else steps[run[0]].get("op"))
            span = f"{run[0]}" if len(run) == 1 else f"{run[0]}-{run[-1]}"
            L.append(f"/* [{span}] {label} -> {last_pn} */")
            L.append(f"data prep_{last_pn[:27]}; set {first_src};")
            if lens:
                L.append("  length " + " ".join(f"{t} $200" for t in lens) + ";")
            L += body
            L.append("run;")
            L.append("")
            for idx in run:
                produced.add(out_pn[idx])
            k = run[-1] + 1
            continue

        L.append(f"/* [{k}] {op} -> {name} */")
        if op == "rename":
            from .prep_engine import _rename_plan
            src = ref(p.get("dataset"))
            ren, copies = _rename_plan(p.get("renames"))
            pairs = [(str(f).upper(), str(t).upper()) for f, t in ren.items()
                     if _SAS_NAME.match(str(f).upper()) and _SAS_NAME.match(str(t).upper())
                     and str(f).upper() != str(t).upper()]
            copies = [(str(f).upper(), str(t).upper()) for f, t in copies
                      if _SAS_NAME.match(str(f).upper()) and _SAS_NAME.match(str(t).upper())]
            if pairs:
                need.add("ren")
                L.append(f"%sdtm_ren(out={out}, src={src}, "
                         f"pairs=%str({' '.join(f'{f}={t}' for f, t in pairs)}));")
            else:
                L.append(f"data {out}; set {src}; run;")
            if copies:    # a column listed twice: renamed once, copied to the rest
                L.append(f"data {out}; set {out}; {' '.join(f'{t} = {f};' for f, t in copies)} run;")
            known_cols.update(t for _f, t in pairs + copies)
        elif op == "drop":
            src = ref(p.get("dataset"))
            cols = [str(c).upper() for c in (p.get("columns") or []) if _SAS_NAME.match(str(c).upper())]
            L.append(f"data {out}; set {src};" + (f" drop {' '.join(cols)};" if cols else "") + " run;")
        elif op == "select":
            src = ref(p.get("dataset"))
            cols = [str(c).upper() for c in (p.get("columns") or []) if _SAS_NAME.match(str(c).upper())]
            L.append(f"data {out}; set {src}" + (f"(keep={' '.join(cols)})" if cols else "") + "; run;")
        elif op == "filter" and str(p.get("expression") or "").strip():
            src = ref(p.get("dataset"))
            L += [f"data {out}; set {src};", f"  if {sas_expr.to_sas(sas_expr.parse(p['expression']))};", "run;"]
        elif op == "filter":
            src = ref(p.get("dataset"))
            cond = _sas_prep_cond(p.get("column"), p.get("operator", "=="), p.get("value", ""))
            L += [f"data {out}; set {src};", f"  if {cond};", "run;"]
        elif op == "compute":
            src = ref(p.get("dataset"))
            tgt = str(p.get("target") or "").strip().upper()
            L += [f"data {out}; set {src};",
                  f"  {tgt} = {sas_expr.to_sas(sas_expr.parse(p.get('expression') or ''))};", "run;"]
        elif op == "carry":
            src = ref(p.get("dataset"))
            tgt, col = str(p.get("target") or "").strip().upper(), str(p.get("column") or "").strip().upper()
            by, order = _carry_args({"by": p.get("by", "USUBJID"), "order": p.get("order")})[1:]
            L += _sas_carry_step(src, out, tgt, col, by, order, str(p.get("mode") or "retain").lower(),
                                 max(int(float(p.get("n") or 1)), 1))
        elif op == "stack":
            srcs = [ref(d) for d in (p.get("datasets") or []) if d]
            srcs = [s for s in srcs if s != "_empty"]
            L.append(f"data {out}; set {' '.join(srcs)}; run;" if srcs
                     else f"data {out}; stop; run;   /* stack: no resolvable inputs */")
        elif op == "merge":
            how = str(p.get("how", "left")).lower()
            on = [str(c).upper() for c in (p.get("on") or []) if _SAS_NAME.match(str(c).upper())]
            specs = p.get("inputs") or [{"dataset": p.get("left")}, {"dataset": p.get("right")}]
            specs = [s for s in specs if s.get("dataset")]
            keys = on or ["USUBJID"]
            srefs = []
            for i, spec in enumerate(specs):
                s = ref(spec.get("dataset"))
                keep = [str(c).upper() for c in (spec.get("columns") or [])
                        if _SAS_NAME.match(str(c).upper())]
                keep = list(dict.fromkeys(keep + [kk for kk in keys])) if keep else []
                srt = f"_mg{i}"
                L.append(f"proc sort data={s}" + (f"(keep={' '.join(keep)})" if keep else "")
                         + f" out={srt}; by {' '.join(keys)}; run;")
                srefs.append(srt)
            if p.get("on_map"):
                gaps.append(f"prep merge '{name}' uses a left/right column map (on_map) that is not "
                            "generated in SAS; verify the BY keys")
            inflags = " ".join(f"{s}(in=_in{i})" for i, s in enumerate(srefs))
            L += [f"data {out};", f"  merge {inflags};", f"  by {' '.join(keys)};"]
            if how == "left" and srefs:
                L.append("  if _in0;")
            elif how == "inner":
                L.append("  if " + " and ".join(f"_in{i}" for i in range(len(srefs))) + ";")
            elif how == "right" and srefs:
                L.append(f"  if _in{len(srefs) - 1};")   # each join keeps the right side's records
            L.append("run;")
        elif op == "transpose_findings":
            src = ref(p.get("dataset"))
            idv = [str(c).upper() for c in (p.get("id_vars") or []) if _SAS_NAME.match(str(c).upper())]
            tcol = str(p.get("testcd_col", "TESTCD")).upper()
            tname = str(p.get("test_col", "TEST")).upper()
            ocol = str(p.get("orres_col", "ORRES")).upper()
            ucol = str(p.get("orresu_col", "ORRESU")).upper()
            catc = str(p.get("cat_col") or "").upper()
            dtc_col = str(p.get("dtc_col") or "").upper()
            dtc_src = str(p.get("dtc_source_col") or "").upper()
            has_cat = bool(catc and _SAS_NAME.match(catc))
            has_dtc = bool(dtc_col and dtc_src and _SAS_NAME.match(dtc_col) and _SAS_NAME.match(dtc_src))
            L.append(f"/* findings transpose: one row per measure via %sdtm_fnd (drop empty {ocol}) */")
            L.append(f"data {out};")
            L.append(f"  set {src};")
            L.append(f"  length {tcol} $8 {tname} $40 {ocol} $200 {ucol} $40"
                     + (f" {catc} $200" if has_cat else "")
                     + (f" {dtc_col} $19" if has_dtc else "") + ";")
            for m in (p.get("measures") or []):
                vc = str(m.get("value_col") or "").upper()
                if not _SAS_NAME.match(vc):
                    continue
                need.add("fnd")
                uc = str(m.get("unit_col") or "").upper()
                args = [f"tcdv={tcol}", f"tcd={_nrb(_sas_str_lit(m.get('testcd', '')))}",
                        f"tstv={tname}", f"tst={_nrb(_sas_str_lit(m.get('test') or m.get('testcd', '')))}",
                        f"orv={ocol}", f"src={vc}", f"oruv={ucol}",
                        f"unit={uc if _SAS_NAME.match(uc) else ''}"]
                if has_cat:
                    args.append(f"catv={catc}")
                    if m.get("cat") is not None:
                        args.append(f"cat={_nrb(_sas_str_lit(m.get('cat', '')))}")
                if has_dtc:
                    args += [f"dtcv={dtc_col}", f"dtcs={dtc_src}"]
                L.append(f"  %sdtm_fnd({', '.join(args)});")
            L.append("run;")
            known_cols.update(c for c in (tcol, tname, ocol, ucol, catc, dtc_col) if c)
            if idv:
                L.append(f"/* (id vars carried: {' '.join(idv)}) */")
        elif op in _SAS_RESHAPE_OPS:
            lines, extra = _sas_reshape_step(op, p, ref, out, name)
            L += lines
            produced.update(extra)                 # split writes one dataset per branch
            if op == "transpose_long":
                known_cols.update(str(p.get(c) or d).upper() for c, d in
                                  (("var_name", "TESTCD"), ("value_name", "ORRES")))
            elif p.get("out_col"):
                known_cols.add(str(p["out_col"]).upper())
        else:
            gaps.append(f"prep step '{name}' ({op}) is not generated in SAS — see the Python program")
            L.append(f"/* [{k}] {op} '{name}': not generated in SAS — see the Python program */")
        L.append("")
        produced.add(pn)
        k += 1
    return {"lines": L, "produced": produced, "gaps": gaps, "need": need}


# ── Spec-derived rule-library overlay ─────────────────────────────────────────
# The recipe emitter above expresses the mappings the spec states. Many SDTM
# variables, though, are house-standard derivations that no study spec spells
# out (AESER coding, --LLT/--DECOD pass-through, action-taken decodes, …). The
# rule library (src/rule_library) carries those as validated SAS extracted from
# a CORE define2.1 spec, each tagged AUTO / REVIEW / OPEN and by pipeline stage.
# This overlay fills variables the recipe emitter left unmapped with the AUTO
# rules whose raw source resolves to a real column in this study's merged base,
# and surfaces the REVIEW/OPEN ones as honest banner items. It is strictly
# additive — it never rewrites a recipe-emitted statement — and best-effort, so
# any failure (no library, no DB) leaves the program exactly as it was. Disable
# with SDTM_RULE_LIBRARY_OVERLAY=0.

# Only plain DATA-step stages are safe to inline here; SEQ / SUPP / transpose /
# aggregate / global-macro stages are produced by the emitter's own machinery.
_RL_PLAIN_STAGES = {"PS14 final", "PS05 derive + PS14 final"}
_RL_SKIP_VARS = {"STUDYID", "DOMAIN", "USUBJID"}
_RL_TIER_RANK = {"exact": 0, "col": 1, "norm": 2, "sdtm_domain": 2, "fuzzy": 3, None: 0}
# DM reference-date vars rules may read (they also exist in the library, but add
# them so a cross-domain dependency is recognised even if the DM rows aren't loaded).
_RL_REF_VARS = {"RFSTDTC", "RFENDTC", "RFXSTDTC", "RFXENDTC", "RFICDTC", "RFPENDTC"}
_RL_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _rl_sdtm_deps(sas: str, vocab: set, target: str, raw_cols: set) -> set:
    """SDTM variables a rule's SAS references (so we can order it after they are
    built): identifiers that are known SDTM variable names, excluding the rule's
    own target and any raw base column (those are supplied by the merge)."""
    deps = set()
    for tok in _RL_IDENT.findall(sas or ""):
        u = tok.upper()
        if u == target or u in raw_cols:
            continue
        if u in vocab:
            deps.add(u)
    return deps


def _rule_library_overlay(study: str, dom: str, mapped: set, base: str,
                          base_sdtm_cols: set | None = None) -> tuple:
    """Fill variables the recipe emitter left unmapped with AUTO rules from the
    CORE-spec rule library, ordered so each rule emits only after its inputs
    exist. Covers two kinds of output:

      * PARENT variables — emitted as dependency-ordered DATA-step statements.
      * SUPP-- qualifiers (rules staged `PS16 SUPP`) — a char pass-through
        `QNAM = strip(<raw>)` derived in the DATA step and registered as a
        supplemental qualifier, so the emitter's own SUPP transpose carries it
        into SUPP<dom> and drops it from the parent.

    Returns (body_lines, filled_vars, notes, fingerprint, stats, supp_adds):
      body_lines  DATA-step statements (parent fills first, then SUPP derives).
      filled_vars every variable the overlay added (parent + SUPP), so the caller
                  can mark them mapped and drop the recipe's "not generated" stubs.
      notes       (var, status, rule_id) for items NOT emitted — REVIEW / OPEN
                  rules, and AUTO rules deferred for an unmet dependency.
      stats       {filled, supp, deferred, review, open} counts for this run.
      supp_adds   [(qnam, qlabel, qorig)] to extend the emitter's supp_specs.

    A rule emits only when every raw input is a verified column in the merged
    base AND (for parents) every SDTM-variable input is already available."""
    empty = ([], [], [], "",
             {"filled": 0, "supp": 0, "deferred": 0, "review": 0, "open": 0}, [])
    if os.environ.get("SDTM_RULE_LIBRARY_OVERLAY", "1") == "0":
        return empty
    try:
        from .rule_library import load_rule_library, resolve_for_study
        from .build_engine import _meta_columns
    except Exception:  # noqa: BLE001
        return empty
    lib = load_rule_library()
    if lib is None:
        return empty
    try:
        colmap = _meta_columns(study) or {}
    except Exception:  # noqa: BLE001
        colmap = {}
    columns = [{"file": f, "column_name": c} for f, cs in colmap.items() for c in cs]
    if not columns:
        return empty
    base_cols = {str(c).upper() for c in colmap.get(str(base).lower(), [])}
    res = resolve_for_study(columns, domains=[dom])
    if not res.get("available"):
        return empty

    vocab = {str(v).upper() for (_d, v) in lib.by_domain_var} | _RL_REF_VARS

    # Collect the best candidate per variable (prefer the most confident tier).
    cand: dict = {}          # parent var -> {"r":expr, "deps":set}
    supp_cand: dict = {}     # qnam  var  -> {"r":expr, "raw":study_col}
    review: dict = {}        # var -> (var, status, rule_id)
    n_review = n_open = 0
    for r in res["expressions"]:
        var = str(r.get("variable") or "").upper()
        if var in mapped or var in _RL_SKIP_VARS or var.endswith("SEQ") \
                or not _SAS_NAME.match(var):
            continue
        st = str(r.get("status") or "").upper()
        if st != "AUTO":
            if st in ("REVIEW", "OPEN") and var not in review:
                review[var] = (var, st, r.get("rule_id"))
                n_review += st == "REVIEW"
                n_open += st == "OPEN"
            continue
        stage = r.get("stage") or ""
        tm = r.get("token_map") or {}
        # Every raw input must be a verified column in the merged base.
        if any(str(t.get("to", "")).upper() not in base_cols for t in tm.values()):
            continue
        if "SUPP" in stage:
            # Supplemental qualifier -> derive the column `QNAM = <expr>` and let
            # the transpose carry it into SUPP<dom>. Only a clean single-level
            # expression qualifies: skip PIVOT / aggregate archetypes (whose
            # `max(case when …)` needs real vertical logic) and any expression
            # that references an undefined scratch `_temp` (which would need a
            # separate derive step we don't emit). Conditional CODE SUPP rules
            # likewise don't map to a plain column.
            if r.get("action") != "ASSIGN":
                continue
            tmpl = lib.templates.get(r.get("rule_id"), "")
            if any(x in tmpl for x in ("case when", "max(", " as ", "union")):
                continue
            sas = (r.get("resolved_sas") or "").strip().rstrip(";")
            if not sas or re.search(r"\b_[A-Za-z]\w*", sas):
                continue
            cur = supp_cand.get(var)
            if cur is None or _RL_TIER_RANK.get(r.get("tier")) < _RL_TIER_RANK.get(cur["r"].get("tier")):
                supp_cand[var] = {"r": r, "sas": sas}
            continue
        if stage not in _RL_PLAIN_STAGES or r.get("action") not in ("ASSIGN", "CODE"):
            continue
        sas = (r.get("resolved_sas") or "").strip()
        if not sas:
            continue
        deps = _rl_sdtm_deps(sas, vocab, var, base_cols)
        cur = cand.get(var)
        if cur is None or _RL_TIER_RANK.get(r.get("tier")) < _RL_TIER_RANK.get(cur["r"].get("tier")):
            cand[var] = {"r": r, "deps": deps}

    # A variable that qualifies as both a parent fill and a SUPP qualifier is a
    # parent (the stronger claim); drop it from the SUPP set.
    for v in list(supp_cand):
        if v in cand:
            del supp_cand[v]

    # Topological fixpoint for PARENT fills: emit any candidate whose SDTM deps
    # are all available, add it to the available set, repeat.
    available = set(mapped) | {str(c).upper() for c in (base_sdtm_cols or set())}
    ordered: list = []
    remaining = dict(cand)
    progress = True
    while progress and remaining:
        progress = False
        for var in sorted(remaining):
            if remaining[var]["deps"] <= available:
                ordered.append(var)
                available.add(var)
                del remaining[var]
                progress = True

    body_lines = []
    for var in ordered:
        r = cand[var]["r"]
        sas = r["resolved_sas"].strip()
        tag = f"rule {r['rule_id']} [{r['stage']}]"
        if r["action"] == "ASSIGN":
            body_lines.append(f"  {var} = {sas.rstrip(';')};   /* {tag} */")
        else:  # CODE — already a full statement (if/then, macro call, …)
            body_lines.append(f"  {sas if sas.endswith(';') else sas + ';'}   /* {tag} */")

    # SUPP qualifiers: derive the column, register it for the transpose. Real
    # QLABEL from the spec's QNAM rows when present, else a prettified fallback.
    try:
        from .build_exec import _qnam_meta
        qmeta = _qnam_meta(study, dom) or {}
    except Exception:  # noqa: BLE001
        qmeta = {}
    supp_adds, supp_filled = [], []
    for var in sorted(supp_cand):
        sas = supp_cand[var]["sas"]
        r = supp_cand[var]["r"]
        meta = qmeta.get(var) or {}
        qlabel = meta.get("qlabel") or var.replace("_", " ").title()
        qorig = meta.get("qorig") or "CRF"
        body_lines.append(f"  {var} = {sas};   /* SUPP qualifier — "
                          f"rule {r['rule_id']} [{r['stage']}] */")
        supp_adds.append((var, qlabel, qorig))
        supp_filled.append(var)

    notes = [review[v] for v in review if v not in ordered and v not in supp_filled]
    # Deferred AUTO rules: an input SDTM variable was never built this run.
    for var in sorted(remaining):
        miss = ", ".join(sorted(remaining[var]["deps"] - available)) or "an input"
        notes.append((var, f"DEFERRED (needs {miss})", cand[var]["r"].get("rule_id")))
    stats = {"filled": len(ordered), "supp": len(supp_filled),
             "deferred": len(remaining), "review": n_review, "open": n_open}
    return body_lines, ordered + supp_filled, notes, res.get("fingerprint", ""), stats, supp_adds


def sas_program(study: str, domain: str, blocks: list, settings: dict | None = None,
                subject_keys: dict | None = None, prep_steps: list | None = None,
                per_var: dict | None = None) -> str:
    """Emit a faithful, runnable SAS build program for a domain (DESIGN.md §10,
    Stage 3). Replaces the old one-`set`-and-warn skeleton with a real
    subject-key MERGE across every source dataset plus deterministic emitters for
    the derivation recipes (copy / value-source coalesce / iso_date / date_extreme
    / cond / fn:coalesce / study_day / sequence / concat). Anything still
    unexpressible keeps the honest INCOMPLETE banner, so a partial emitter never
    ships silently-wrong SAS.

    `subject_keys` maps each source dataset to its subject-key column; when not
    supplied, each source is assumed keyed on USUBJID (and that assumption is
    stated in the header).

    `per_var`, when a dict is passed, is filled with each variable's share of the
    program — {VAR: {"pre": [steps that build it before the DATA step], "lines":
    [its DATA-step statements], "gap": banner reason or ''}} — for the Build Spec
    view. The returned program is the same either way."""
    dom = domain.upper()
    if not _SAS_NAME.match(dom):
        # There is no program to generate without a valid target dataset name,
        # and interpolating this into `data sdtm.x;` would be injection.
        raise ValueError(f"not a valid SDTM domain name: {domain!r}")
    settings = settings or {}
    if prep_steps is None:
        prep_steps = _saved_prep_steps(study, domain)
    _enrich_supp(study, domain, blocks)
    # the prepared base the exec engine assembles (<dom>_base) and the SDTM
    # variables it supplies verbatim — those parent derivations are then taken
    # from the base instead of re-derived.
    prep = _sas_prep_pipeline(study, domain, prep_steps) if prep_steps else None
    prep_base = None
    base_sdtm_cols: set = set()
    if prep and (_pn(f"{domain.lower()}_base") in prep["produced"]
                 or _pn(f"{domain.lower()}base") in prep["produced"]):
        prep_base = f"prep_{_pn(domain.lower() + '_base')[:27]}" \
            if _pn(f"{domain.lower()}_base") in prep["produced"] \
            else f"prep_{_pn(domain.lower() + 'base')[:27]}"
        base_sdtm_cols = _base_lineage_cols(prep_steps, domain)
    sk = {str(k).lower(): str(v).upper() for k, v in (subject_keys or {}).items()
          if _SAS_NAME.match(str(v).upper())}
    skmap_supplied = bool(subject_keys)

    gaps: list[str] = []          # banner: everything not rendered as working SAS
    mapped: set[str] = set()      # vars assigned in the DATA step
    body: list[str] = []          # DATA-step statements, in dependency order
    tmps: set[str] = set()        # scratch vars to DROP before run
    body_macros: set[str] = set()  # reusable macros the DATA step body calls
    # merge inputs, built as we classify blocks:
    lookups: dict = {}            # ds(lower) -> {col(UPPER) -> alias(UPPER)} for ds != base
    alias_n = [0]                 # unique merged-column alias counter
    aggregates: list = []         # (var, func, [(ds, date_col)]) for date_extreme
    dmrefs: set = set()           # DM reference-date vars needed (study_day), domain != DM
    seq_specs: list = []          # (var, group) for --SEQ proc steps
    lobxfl_specs: list = []       # (flag, by, dtc, res, ref) for post-build %sdtm_lobxfl
    epoch_spec: dict | None = None  # {refc} for post-build %sdtm_epoch (SE-interval EPOCH)
    # xx-merge `_temp`s rebuilt onto the base before the DATA step (keyed_lookup / seq_pivot):
    temp_lookups: list = []       # (var, temp, args) — lookup merge on a (scan-derived) key
    temp_pivots: dict = {}        # (ds, seq_col, seq, record keys) -> [(temp, column)]
    temp_var: dict = {}           # temp -> the variable it is rebuilt for
    late_lines: list = []         # (var, statement) reading other vars of this domain — run last
    carry_specs: list = []        # (var, recipe, args) RETAIN / LAG / DIF — own BY-group DATA steps
    collapse: dict | None = None  # seq_pivot args whose vertical base keeps one row per record
    # EPOCH is derived from SE (not a subject-data source) for every clinical domain; trial-design
    # and special-purpose domains either define SE or have no SE-based epoch.
    _EPOCH_SKIP = {"DM", "SE", "SV", "CO", "TA", "TE", "TV", "TI", "TS", "TM", "TD", "RELREC", "SM"}

    def _supp(b):
        return bool(b.get("supp"))

    parent = [b for b in blocks if not _supp(b)]
    has_supp = any(_supp(b) for b in blocks)

    # ── Resolve the record-grain base dataset from the USUBJID mapping. The
    # base keeps all its rows; every other source is a per-subject lookup merged
    # by USUBJID (first value per subject — the same by-subject resolution the
    # run-engines apply to a cross-dataset value source). ──────────────────────
    base = None
    base_key = "USUBJID"
    usubj_block = next((b for b in parent if str(b.get("variable", "")).upper() == "USUBJID"
                        and b.get("mtype") == "assign" and b.get("dataset") and b.get("column")), None)
    if usubj_block:
        base = str(usubj_block["dataset"]).lower()
        base_key = str(usubj_block["column"]).upper()
    else:
        cand = [str(b.get("dataset") or "").lower() for b in parent
                if b.get("mtype") == "assign" and b.get("dataset")]
        base = max(set(cand), key=cand.count) if cand else dom.lower()
        base_key = sk.get(base, "USUBJID")
    if not _SAS_NAME.match(base):
        gaps.append(f"the base source dataset name is not a valid SAS name: "
                    f"{_sas_comment_safe(base)[:60]!r}")
        base = dom.lower()
    if not _SAS_NAME.match(base_key):
        base_key = "USUBJID"

    def _ref(ds, col):
        """Reference a (dataset, column) source inside the DATA step: the real
        name when it is the base, else a unique alias fed by a lookup merge.
        Returns None (and records a gap) when the source cannot be joined."""
        ds, col = str(ds).lower(), str(col).upper()
        if not _SAS_NAME.match(col):
            return None
        if ds == base:
            return col
        if not _SAS_NAME.match(ds):
            return None
        key = sk.get(ds)
        if key is None and skmap_supplied:
            # We looked and the source has no shared subject key — refuse to
            # invent a join rather than silently blank the column.
            return None
        alias_n[0] += 1
        alias = f"_M{alias_n[0]}_{col}"[:32]
        lookups.setdefault(ds, {})[col] = (alias, key or "USUBJID")
        return alias

    def _parse_src(src):
        """A cond/result source spec -> (sas_expr_or_None, gap_or_None). Handles
        kind var/self/column/text/missing."""
        s = src or {}
        kind = (s.get("kind") or "").lower()
        if kind == "missing":
            return "''", None
        if kind == "text" or (not kind and s.get("text") is not None and not s.get("column")):
            return _sas_quote(s.get("text", "")), None
        if kind in ("var", "self") or (not kind and s.get("var")):
            vv = str(s.get("var") or "").upper()
            if kind == "self":
                return None, "self-reference"
            return (f"strip({vv})", None) if _SAS_NAME.match(vv) else (None, "bad var name")
        ds, col = s.get("dataset"), s.get("column")
        if ds and col:
            r = _ref(ds, col)
            return (f"strip({r})", None) if r else (None, f"unjoinable source {ds}.{col}")
        return "''", None

    def _raw_src(src, v, steps=0):
        """A function/step input as an UNstripped SAS reference -> (expr, gap). `steps` is how
        many pipeline steps precede it (their values are _pst1…)."""
        s_ = src or {}
        kind = (s_.get("kind") or "").lower()
        if kind == "self":
            return v, None
        if kind == "step":
            n = int(s_.get("step") or 0)
            if 1 <= n <= steps:
                tmps.add(f"_pst{n}")
                return f"_pst{n}", None
            return None, "a step input refers to a step that does not precede it"
        if kind == "missing":
            return "''", None
        if kind in ("var",) or (not kind and s_.get("var")):
            vv = str(s_.get("var") or "").upper()
            return (vv, None) if _SAS_NAME.match(vv) else (None, "bad variable name")
        if kind == "text" or (not kind and s_.get("text") is not None and not s_.get("column")):
            return _sas_quote(s_.get("text", "")), None
        ds, col = s_.get("dataset"), s_.get("column")
        if ds and col:
            r = _ref(ds, col)
            return (r, None) if r else (None, f"unjoinable source {ds}.{col}")
        return None, "an input is not set"

    def _fn_sas(v, a, steps=0):
        """A Transform-function recipe -> (SAS assignment, None) or (None, why)."""
        try:
            tree, n = sas_functions.fn_tree(a)
        except ValueError as e:
            return None, str(e)
        srcs = list(a.get("sources") or [])
        if n and not srcs:
            return None, "no input"
        exprs = []
        for s_ in srcs:
            e_, g_ = _raw_src(s_, v, steps)
            if e_ is None:
                return None, g_
            exprs.append(e_)
        expr = sas_expr.to_sas(tree, lambda nm: exprs[int(nm[3:])] if int(nm[3:]) < len(exprs) else "''")
        return f"  {v} = {expr};", None

    def _pipeline_sas(v, steps):
        """Multiple steps on one variable -> SAS statements, each step reading the value so far
        (and _pst<n> for an earlier step's value)."""
        if not steps:
            return None, "no steps"
        out = []
        for idx, st in enumerate(steps, 1):
            op = str(st.get("op", "fn")).lower()
            sa = st.get("args") or {}
            if op == "assign":
                e_, g_ = _raw_src({"dataset": st.get("dataset"), "column": st.get("column")}, v, idx - 1)
                if e_ is None:
                    return None, f"step {idx}: {g_}"
                out.append(f"  {v} = {e_};")
            elif op == "constant":
                out.append(f"  {v} = {_sas_quote(st.get('value', ''))};")
            elif op == "fn":
                line, why = _fn_sas(v, sa, idx - 1)
                if not line:
                    return None, f"step {idx}: {why}"
                out.append(line)
            elif op == "cond":
                ps = lambda s_, _i=idx: _raw_src(s_, v, _i - 1)  # noqa: E731
                els, g_ = ps(sa.get("else") or {"kind": "self"})
                if els is None:
                    return None, f"step {idx}: {g_}"
                chain = []
                for k, r in enumerate(sa.get("rules") or []):
                    c_, gc = _cond_sas(r, ps)
                    t_, gt = ps((r or {}).get("then"))
                    if c_ is None or t_ is None:
                        return None, f"step {idx}: {gc or gt}"
                    chain.append(f"  {'if' if k == 0 else 'else if'} {c_} then {v} = {t_};")
                # one assignment per row, and every condition reads the value before this step
                out += chain + ([f"  else {v} = {els};"] if chain else [f"  {v} = {els};"])
            else:
                return None, f"step {idx}: {op!r} steps are not generated"
            out.append(f"  _pst{idx} = {v};")
            tmps.add(f"_pst{idx}")
        out[0] = out[0] + f"   /* {len(steps)}-step derivation */"
        return out, None

    # Iterate ALL blocks (incl. SUPP qualifiers) so their values are derived in
    # the DATA step; SUPP vars are transposed into SUPP<dom> and then dropped from
    # the parent dataset below.
    _marks: list = []             # (var, index of its first body line) — for `per_var`
    for b in blocks:
        v = str(b.get("variable", "")).upper()
        _marks.append((v, len(body)))
        mt, a = b.get("mtype"), (b.get("args") or {})
        rc = b.get("recipe")

        if not _SAS_NAME.match(v):
            gaps.append(f"a block maps a variable whose name is not a valid SAS "
                        f"name and was skipped entirely: {_sas_comment_safe(v)[:60]!r}")
            continue

        # When the prepared base supplies this SDTM variable, take it from the base
        # (which the DATA step SETs) instead of re-deriving it — exactly the exec
        # engine's non-destructive Prep integration. STUDYID/DOMAIN/--SEQ still derive.
        if (prep_base and v in base_sdtm_cols and v not in ("STUDYID", "DOMAIN")
                and not v.endswith("SEQ") and b.get("mtype") != "sequence"):
            body.append(f"  /* {v}: supplied verbatim by the prepared base {prep_base} */")
            mapped.add(v)
            continue

        # USUBJID is the MERGE by-variable (the base key, renamed). It is not an
        # ordinary assignment — emitting `USUBJID = strip(X_SUBJID)` would read a
        # column the base prep has already renamed away.
        if v == "USUBJID" and b.get("mtype") == "assign":
            mapped.add(v)
            continue

        # EPOCH is derived from the SE element intervals (SDTMIG / PharmaSUG), not a
        # subject-data source — unless the spec maps it as a plain constant/assign.
        # Initialise it blank here; a post-build %sdtm_epoch fills it from SE.
        if (v == "EPOCH" and dom not in _EPOCH_SKIP
                and not (mt == "constant" or (mt == "assign" and b.get("column")))):
            refc = (next((bv for bv in (str(x.get("variable", "")).upper() for x in parent) if bv == "RFSTDTC"), None)
                    or next((bv for bv in (str(x.get("variable", "")).upper() for x in parent) if bv.endswith("STDTC")), None)
                    or next((bv for bv in (str(x.get("variable", "")).upper() for x in parent) if bv.endswith("DTC")), None))
            if refc and _SAS_NAME.match(refc):
                epoch_spec = {"refc": refc}
                body.append("  length EPOCH $40;")
                body.append("  EPOCH = '';   /* filled from SE by %sdtm_epoch (post-build) */")
                mapped.add(v)
                continue

        if mt == "constant":
            body.append(f"  {v} = {_sas_quote(b.get('value', ''))};")
            mapped.add(v)
            continue
        if mt == "drop":
            body.append(f"  /* {v}: dropped (not mapped) */")
            continue
        if mt == "sequence":
            grp = str((a.get("group") or "USUBJID")).upper() or "USUBJID"
            if not _SAS_NAME.match(grp):
                grp = "USUBJID"
            seq_specs.append((v, grp))
            mapped.add(v)
            continue

        if mt == "assign":
            col = str(b.get("column") or "").upper()
            vsrc = [(str(x.get("dataset", "")).lower(), str(x.get("column", "")).upper())
                    for x in (a.get("value_sources") or [])
                    if x.get("dataset") and x.get("column")]
            if len(vsrc) > 1:
                refs, bad = [], []
                for ds, c in vsrc:
                    r = _ref(ds, c)
                    (refs if r else bad).append(r or f"{ds}.{c}")
                refs = [r for r in refs if r]
                if refs:
                    body.append(f"  {v} = coalescec({', '.join('strip(%s)' % r for r in refs)});"
                                f"   /* first non-missing value source */")
                    mapped.add(v)
                    if bad:
                        gaps.append(f"{v}: value source(s) {', '.join(bad)} share no subject "
                                    "key and were left out of the coalesce")
                else:
                    body.append(f"  /* {v}: no joinable value source */")
                    gaps.append(f"{v}: none of its value sources could be joined on a subject key")
                continue
            ds = str(b.get("dataset") or "").lower()
            if not col:
                body.append(f"  /* {v}: no source column */")
                gaps.append(f"{v}: mapped from a source column that is blank")
            else:
                r = _ref(ds, col)
                if r:
                    body.append(f"  {v} = strip({r});")
                    mapped.add(v)
                elif not _SAS_NAME.match(col):
                    body.append(f"  /* {v}: unusable source column */")
                    gaps.append(f"{v}: its source column name is not a valid SAS name: "
                                f"{_sas_comment_safe(col)[:60]!r}")
                else:
                    body.append(f"  /* {v}: source {_sas_comment_safe(ds)}.{col} not joinable */")
                    gaps.append(f"{v}: source dataset {ds!r} shares no subject key with the "
                                f"base {base!r}, so it cannot be merged")
            continue

        if mt == "derived":
            if rc == "constant":
                body.append(f"  {v} = {_sas_quote(a.get('value', ''))};")
                mapped.add(v)
                continue
            if rc == "concat":
                cols = [str(c).upper() for c in (a.get("columns") or []) if c]
                ds = str(a.get("dataset") or base).lower()
                refs = [_ref(ds, c) for c in cols]
                if cols and all(r for r in refs):
                    body.append(f"  {v} = catx({_sas_quote(a.get('sep', ' '))}, {', '.join(refs)});")
                    mapped.add(v)
                else:
                    body.append(f"  /* {v}: concat has an unusable/unjoinable source column */")
                    gaps.append(f"{v}: one of its concat sources is not a valid, joinable SAS "
                                "column, so the derivation was skipped")
                continue
            if rc == "iso_date":
                ds = str(a.get("dataset") or base).lower()
                dcol = str(a.get("date_col") or "").upper()
                tcol = str(a.get("time_col") or "").upper()
                if a.get("y_col"):
                    body.append(f"  /* {v}: part-based ISO date (year/month/day columns) */")
                    gaps.append(f"{v}: iso_date assembled from separate year/month/day columns "
                                "is not generated; see the Python program")
                    continue
                r = _ref(ds, dcol) if dcol else None
                if not r:
                    body.append(f"  /* {v}: iso_date has no joinable raw date column */")
                    gaps.append(f"{v}: its raw date column could not be resolved")
                    continue
                tmps.update({"_dt"})
                body_macros.add("iso")
                tr = _ref(ds, tcol) if tcol else None
                if tr:
                    tmps.add("_tm")
                    body.append(f"  %sdtm_iso(var={v}, src={r}, time={tr});")
                else:
                    body.append(f"  %sdtm_iso(var={v}, src={r});")
                mapped.add(v)
                continue
            if rc == "date_extreme":
                func = str(a.get("func") or "min").lower()
                if func not in ("min", "max"):
                    func = "min"
                srcs = [(str(s.get("dataset", "")).lower(), str(s.get("date_col", "")).upper())
                        for s in (a.get("sources") or []) if s.get("dataset") and s.get("date_col")]
                srcs = [(d, c) for d, c in srcs if _SAS_NAME.match(d) and _SAS_NAME.match(c)]
                if not srcs:
                    body.append(f"  /* {v}: date_extreme has no usable source */")
                    gaps.append(f"{v}: no usable dataset + date column for the reference date")
                    continue
                aggregates.append((v, func, srcs))
                mapped.add(v)   # merged in from the aggregate dataset; no body line
                continue
            if rc == "sas_if":
                try:
                    chains = sas_expr.chains_from_json(a.get("chains"))
                except sas_expr.SasExprError as e:
                    chains, why = None, str(e)
                ds_ = base if a.get("on_base") and prep_base else str(a.get("dataset") or "").lower()
                refs_ = {n: _ref(ds_, n) for n in sas_expr.chain_names(chains)} if chains else {}
                if chains and all(refs_.values()):
                    body += [f"  {ln}" for ln in sas_expr.chains_to_sas(chains, v, lambda n: refs_[n])]
                    mapped.add(v)
                else:
                    why = why if not chains else f"a column it reads is not joinable from {ds_}"
                    body.append(f"  /* {v}: IF / ELSE not generated — {_sas_comment_safe(why)} */")
                    gaps.append(f"{v}: IF / ELSE not generated — {why}")
                continue
            if rc in CARRY_RECIPES:
                src_, by_, ord_ = _carry_args(a)
                bad = [n for n in [src_["var"], *by_, *ord_] if not _SAS_NAME.match(n or "")]
                if bad or not src_["var"]:
                    why = "no variable to carry" if not src_["var"] else f"not a SAS name: {bad[0]!r}"
                    body.append(f"  /* {v}: {_sas_comment_safe(why)} */")
                    gaps.append(f"{v}: {rc.upper()} not generated — {why}")
                    continue
                carry_specs.append((v, rc, a))
                mapped.add(v)
                continue
            if rc == "fn":
                line, why = _fn_sas(v, a)
                if line:
                    body.append(line)
                    mapped.add(v)
                else:
                    body.append(f"  /* {v}: {_sas_comment_safe(why)} */")
                    gaps.append(f"{v}: function not generated — {why}")
                continue
            if rc == "pipeline":
                lines, why = _pipeline_sas(v, a.get("steps") or [])
                if lines:
                    body += lines
                    mapped.add(v)
                else:
                    body.append(f"  /* {v}: {_sas_comment_safe(why)} */")
                    gaps.append(f"{v}: multi-step derivation not generated — {why}")
                continue
            if rc == "cond":
                rules = a.get("rules") or []
                els_expr, _g = _parse_src(a.get("else") or {"kind": "missing"})
                lines = [f"  {v} = {els_expr or chr(39) + chr(39)};"]
                ok = True
                for i, r in enumerate(rules):
                    cexpr, gg = _cond_sas(r, _parse_src)
                    texpr, gt = _parse_src((r or {}).get("then"))
                    if cexpr is None or texpr is None:
                        ok = False
                        break
                    kw = "if" if i == 0 else "else if"
                    lines.append(f"  {kw} {cexpr} then {v} = {texpr};")
                if ok and rules:
                    body += lines
                    mapped.add(v)
                else:
                    body.append(f"  /* {v}: conditional rule not fully expressible in SAS */")
                    gaps.append(f"{v}: its conditional logic uses an operator or source that "
                                "is not generated; see the Python program")
                continue
            if rc == "study_day":
                dtc = str(a.get("dtc_var") or "").upper().split(".")[-1]
                ref = (str(a.get("ref_var") or "RFSTDTC").upper().split(".")[-1]) or "RFSTDTC"
                if not dtc.endswith("DTC") and v.endswith("DY") and len(v) > 2:
                    dtc = v[:-2] + "DTC"
                if not (_SAS_NAME.match(dtc) and _SAS_NAME.match(ref)):
                    body.append(f"  /* {v}: study_day needs a --DTC and reference var */")
                    gaps.append(f"{v}: its event --DTC or reference date could not be resolved")
                    continue
                if dom != "DM":
                    dmrefs.add(ref)
                tmps.update({"_ev", "_rf"})
                body_macros.add("dy")
                body.append(f"  %sdtm_dy(dy={v}, dtc={dtc}, ref={ref});")
                mapped.add(v)
                continue
            if rc == "lobxfl":
                tc = str(a.get("testcd_var") or "").upper()
                dtc = str(a.get("dtc_var") or "").upper()
                res = str(a.get("result_var") or "").upper()
                ref = (str(a.get("ref_var") or "RFXSTDTC").upper()) or "RFXSTDTC"
                gv = [str(g).upper() for g in (a.get("group_vars") or ["USUBJID", tc]) if g]
                gv = [g for g in gv if _SAS_NAME.match(g)] or (["USUBJID", tc] if tc else [])
                if not (_SAS_NAME.match(tc) and _SAS_NAME.match(dtc) and _SAS_NAME.match(ref) and gv):
                    body.append(f"  /* {v}: lobxfl needs --TESTCD, --DTC and a reference var */")
                    gaps.append(f"{v}: its --TESTCD / --DTC / reference date could not be resolved")
                    continue
                body.append(f"  length {v} $2; {v} = '';   /* set by %sdtm_lobxfl (post-build) */")
                lobxfl_specs.append({"flag": v, "by": gv, "dtc": dtc,
                                     "res": res if _SAS_NAME.match(res) else "", "ref": ref})
                mapped.add(v)
                continue
            if rc == "sas_expr":
                # the spec's own SAS over other variables of this domain, re-rendered from its parse
                # (so only the supported grammar is ever emitted); placed after every other statement
                try:
                    tree = sas_expr.parse(str(a.get("sas") or ""))
                except sas_expr.SasExprError as e:
                    body.append(f"  /* {v}: spec SAS not understood */")
                    gaps.append(f"{v}: its spec SAS could not be read ({_sas_comment_safe(str(e))})")
                    continue
                refs = ", ".join(sas_expr.names(tree))
                late_lines.append((v, f"  {v} = {sas_expr.to_sas(tree)};   /* spec SAS over {refs} */"))
                mapped.add(v)
                continue
            if rc in ("keyed_lookup", "seq_pivot"):
                # xx-merge temp: rebuilt onto the base by a pre-step (assembled below), then the
                # spec's Implemented SAS assigns the variable from it — one consolidated program.
                temp = str(a.get("temp") or "").lower()
                funcs = [f for f in (a.get("final") or []) if f in _TEMP_FINAL_FUNCS]
                names = [temp, str(a.get("column") or "")]
                names += ([str(a.get("key_col") or ""), str(a.get("base_col") or "")] if rc == "keyed_lookup"
                          else [str(a.get("seq_col") or "")] + list(a.get("record_keys") or []))
                ds_ok = [str(a.get("dataset") or "").lower()] + (
                    [str(a.get("base_dataset") or base).lower()] if rc == "keyed_lookup" else [])
                why = None
                if not all(_SAS_NAME.match(n) for n in names + ds_ok) or not temp.startswith("_"):
                    why = "a dataset/column name is not a valid SAS name"
                elif rc == "keyed_lookup" and prep_base:
                    why = "the domain is built from a Prep-assembled base"
                elif rc == "keyed_lookup" and ds_ok[1] != base:
                    why = f"its key comes from {ds_ok[1]!r}, not the base {base!r}"
                elif rc == "seq_pivot" and prep_base and not _prep_base_carries(
                        prep_steps, dom, ds_ok[0], [a["seq_col"]] + list(a["record_keys"])):
                    why = (f"the Prep-assembled base does not carry raw.{ds_ok[0]} "
                           f"{'/'.join([a['seq_col']] + list(a['record_keys']))} through unchanged")
                elif rc == "seq_pivot" and not prep_base and ds_ok[0] != base:
                    why = f"the vertical source {ds_ok[0]!r} is not the base {base!r}"
                if why:
                    body.append(f"  /* {v}: xx-merge {_sas_comment_safe(temp)} not generated — {why} */")
                    gaps.append(f"{v}: its xx-merge temporary {_sas_comment_safe(temp)} is not generated "
                                f"because {why}; see the Python program")
                    continue
                temp_var[temp] = v
                if rc == "keyed_lookup":
                    temp_lookups.append((v, temp, a))
                else:
                    pk = (ds_ok[0], str(a["seq_col"]).upper(), int(a["seq"]),
                          tuple(str(k).upper() for k in a["record_keys"]))
                    temp_pivots.setdefault(pk, []).append((temp, str(a["column"]).upper()))
                    collapse = collapse or a
                expr = temp
                for f in reversed(funcs):
                    expr = f"{f}({expr})"
                body.append(f"  {v} = {expr};   /* xx-merge {temp} (rebuilt above) */")
                tmps.add(temp)
                mapped.add(v)
                continue
            if b.get("needs_review") and b.get("merge_temps"):
                # an xx-merge temp whose derivation was not reproduced — say so, never guess
                tl = _sas_comment_safe(", ".join(b["merge_temps"]))
                body.append(f"  /* {v}: NEEDS REVIEW — xx-merge {tl} derivation not reproduced */")
                gaps.append(f"{v}: reads the xx-merge temporary {tl}, whose Mapping Rule derivation "
                            "was not reproduced — needs review, so the variable is absent")
                continue
            # Unexpressed derivations (bare `derived`, `code`, `pipeline`,
            # unknown fn): honest banner, variable absent.
            safe_rc = _sas_comment_safe(rc or "derived")
            body.append(f"  /* {v}: {safe_rc} — implement derivation (see the Python program) */")
            gaps.append(f"{v}: derivation {safe_rc!r} is not generated, so the variable "
                        "is absent from the output")
            continue

        safe_mt = _sas_comment_safe(mt)
        body.append(f"  /* {v}: mapping type {safe_mt} not generated */")
        gaps.append(f"{v}: mapping type {safe_mt!r} is not generated, so the variable "
                    "is absent from the output")

    # per-variable view of what was just emitted (Build Spec): its DATA-step lines
    if per_var is not None:
        for k, (v, s) in enumerate(_marks):
            e = _marks[k + 1][1] if k + 1 < len(_marks) else len(body)
            per_var.setdefault(v, {"pre": [], "lines": [], "gap": ""})["lines"] = body[s:e]
    for v, line in late_lines:
        body.append(line)
        if per_var is not None:
            per_var.setdefault(v, {"pre": [], "lines": [], "gap": ""})["lines"] = [line]

    # ── Rule-library overlay: fill unmapped variables with validated SAS from the
    # CORE-spec rule library (dependency-ordered) and surface what it did not
    # auto-emit. Additive — never rewrites a recipe statement. ───────────────────
    rl_body, rl_filled, rl_notes, rl_fp, rl_stats, rl_supp_adds = \
        _rule_library_overlay(study, dom, mapped, base, base_sdtm_cols)
    if rl_body:
        # drop the "not generated" stubs the recipe loop left for the now-filled
        # variables (comment line in the body, gap line in the banner).
        body[:] = [ln for ln in body
                   if not any(ln.strip().startswith(f"/* {v}:") for v in rl_filled)]
        gaps[:] = [g for g in gaps
                   if not any(g.startswith(f"{v}:") for v in rl_filled)]
        body.append(
            f"  /* ---- rule-library overlay vs spec-only build: +{rl_stats['filled']} "
            f"parent + {rl_stats['supp']} SUPP qualifier(s) filled, "
            f"{rl_stats['deferred']} deferred, {rl_stats['review'] + rl_stats['open']} "
            f"review/open (CORE {rl_fp}, deterministic) ---- */")
        body += rl_body
        mapped.update(rl_filled)
    for var, st, rid in rl_notes:
        gaps.append(f"{var}: rule-library rule {rid} is {st} — not auto-emitted")

    if prep:
        gaps += prep["gaps"]

    # SUPP-- qualifier variables that were derived into the DATA step (one QVAL per):
    # the spec's QNAM blocks, plus the SUPP qualifiers the rule-library overlay added.
    supp_specs = [(str(b["variable"]).upper(),
                   str(b.get("qlabel") or b.get("label") or ""),
                   str(b.get("qorig") or b.get("origin") or "CRF"))
                  for b in blocks
                  if _supp(b) and str(b.get("variable", "")).upper() in mapped
                  and _SAS_NAME.match(str(b.get("variable", "")).upper())]
    _supp_seen = {v for v, _l, _o in supp_specs}
    for v, ql, qo in rl_supp_adds:
        if v not in _supp_seen:
            supp_specs.append((v, ql, qo))
            _supp_seen.add(v)
    supp_vars = [v for v, _l, _o in supp_specs]

    # ── Assemble the program ──────────────────────────────────────────────────
    L = _sas_banner(gaps) if gaps else []
    L += ["/" + "*" * 68,
          f" * SDTM {dom} domain build — Auto-generated by SDTM Studio",
          f" * Study: {study}   Generated: {datetime.now(timezone.utc).isoformat()}",
          f" * Deterministic SAS: subject-key MERGE + per-recipe derivations.",
          (f" * Base (record grain): {prep_base} (assembled by the Prep pipeline below)"
           if prep_base else
           f" * Base (record grain): raw.{base}" + (f"  (subject key {base_key} -> USUBJID)"
                                                     if base_key != "USUBJID" else "")),
          " " + "*" * 68 + "/", ""]
    if not skmap_supplied:
        L += ["/* NOTE: each source is assumed keyed on USUBJID. Adjust the key on",
              " * any source that carries the subject under a different column name. */", ""]
    L += ["libname raw 'rawdata';", "libname sdtm 'output';", ""]
    # reusable macros (emitted once, before any use)
    macro_need = set(prep["need"]) if prep else set()
    macro_need |= body_macros
    if supp_specs:
        macro_need.add("supp")
    if epoch_spec:
        macro_need.add("epoch")
    if lobxfl_specs:
        macro_need.add("lobxfl")
    if macro_need:
        L += _sas_macro_library(macro_need)
    if prep:
        L += ["/* " + "=" * 64,
              " * PREP PIPELINE — assemble the domain base before the build",
              " * (reproduces the saved Data-Prep steps / build_engine run_prep)",
              " " + "=" * 64 + " */"]
        L += prep["lines"]
        L.append("")

    # per-subject lookups (one row per subject). The subject key is standardised
    # onto USUBJID by ADDING it (keeping the original column), so a variable that
    # maps from that key column still resolves. Value columns are aliased to
    # avoid name collisions when several sources are merged.
    for ds in sorted(lookups):
        cols = lookups[ds]
        key = next(iter(cols.values()))[1]
        keep = " ".join(["USUBJID"] + sorted(cols.keys()))
        rn = " ".join(f"{c}={alias}" for c, (alias, _k) in sorted(cols.items()))
        if key == "USUBJID":
            L += [f"/* per-subject lookup from {ds} */",
                  f"proc sort data=raw.{ds}(keep={keep} rename=({rn})) out=_lk_{ds} nodupkey;",
                  f"  by USUBJID;", f"run;", ""]
        else:
            L += [f"/* per-subject lookup from {ds} (subject key {key} -> USUBJID) */",
                  f"data _lk_{ds}0; set raw.{ds}; USUBJID = {key}; run;",
                  f"proc sort data=_lk_{ds}0(keep={keep} rename=({rn})) out=_lk_{ds} nodupkey;",
                  f"  by USUBJID;", f"run;", ""]

    # date_extreme aggregates (min/max across one-or-more raw date columns, per subject)
    for v, func, srcs in aggregates:
        _at = len(L)
        unions = []
        for ds, c in srcs:
            key = sk.get(ds, "USUBJID")
            key_sel = "USUBJID" if key == "USUBJID" else f"{key} as USUBJID"
            unions.append(f"    select {key_sel}, input(strip({c}), ?? anydtdte32.) as _d "
                          f"from raw.{ds}")
        union_sql = "\n    outer union corr\n".join(unions)
        L += [f"/* {v}: {func}() of the reference date across source(s), per subject */",
              f"proc sql;",
              f"  create table _dx_{v} as",
              f"  select USUBJID, put({func}(_d), E8601DA10.) as {v} length=10",
              f"  from (",
              f"{union_sql}",
              f"  )",
              f"  where not missing(_d)",
              f"  group by USUBJID;",
              f"quit;",
              f"proc sort data=_dx_{v}; by USUBJID; run;", ""]
        if per_var is not None:
            per_var.setdefault(v, {"pre": [], "lines": [], "gap": ""})["pre"] += L[_at:]

    # DM reference dates for study_day (non-DM domains)
    if dmrefs:
        keep = sorted(r for r in dmrefs if _SAS_NAME.match(r))
        L += [f"/* DM reference dates for --DY derivations */",
              f"proc sort data=sdtm.dm(keep=USUBJID {' '.join(keep)}) out=_dmref nodupkey;",
              f"  by USUBJID;", f"run;", ""]

    # base (record grain), sorted by USUBJID for the merge. Standardise its key
    # onto USUBJID by ADDING the column (keeping the original), so a variable that
    # maps from the base's subject-id column (e.g. INVNAM = X_SUBJID) still works.
    if prep_base and usubj_block and base_key != "USUBJID" \
            and str(usubj_block.get("dataset") or "").lower() in (f"{dom.lower()}_base", f"{dom.lower()}base"):
        # the assembled base carries the subject id under another name (a stack of raw forms keeps X_SUBJID)
        L += [f"/* base (record grain): the Prep-assembled {prep_base}; USUBJID from its {base_key} */",
              f"data _base0; set {prep_base}; USUBJID = strip({base_key}); run;",
              f"proc sort data=_base0 out=_base; by USUBJID; run;", ""]
    elif prep_base:
        L += [f"/* base (record grain): the Prep-assembled {prep_base} (already keyed on USUBJID) */",
              f"proc sort data={prep_base} out=_base; by USUBJID; run;", ""]
    elif base_key == "USUBJID":
        L += [f"/* base (record grain) */",
              f"proc sort data=raw.{base} out=_base; by USUBJID; run;", ""]
    else:
        L += [f"/* base (record grain); subject key {base_key} -> USUBJID (original kept) */",
              f"data _base0; set raw.{base}; USUBJID = {base_key}; run;",
              f"proc sort data=_base0 out=_base; by USUBJID; run;", ""]

    # xx-merge temporaries: rebuild each `_temp` onto the base as xx-merge.sas did — collapse a
    # vertical base to one row per record, pivot its other sequence rows wide, and merge each lookup
    # on its (scan-derived) key — so the DATA step assigns the variable from it (the spec's
    # Implemented SAS). This is what makes the one consolidated program self-contained.
    base_in = "_base"
    if temp_lookups or temp_pivots:
        def _keyx(alias, col, scan):
            x = f"cats({alias + '.' if alias else ''}{col})"
            return (f"strip(scan({x}, {int(scan['n'])}, {_sas_quote(str(scan['sep']))}))"
                    if scan else x)

        sel, joins = [], []
        if collapse:
            sq = str(collapse["seq_col"]).upper()
            rk = [str(k).upper() for k in collapse["record_keys"]]
            L += [f"/* vertical->wide: one row per {'+'.join(rk)} record (lowest {sq}); the other "
                  f"{sq} rows are pivoted into columns below */",
                  "proc sql;",
                  f"  create table _base_r as select * from _base",
                  f"  group by {', '.join(rk)}",
                  f"  having input(cats({sq}), ?? best32.) = min(input(cats({sq}), ?? best32.))"
                  f" or missing({sq});",
                  "quit;", ""]
            base_in = "_base_r"
        for i, ((ds, sq, n, rk), cols) in enumerate(sorted(temp_pivots.items()), 1):
            t = f"_pv{i}"
            _at = len(L)
            L += [f"/* xx-merge pivot: {', '.join(tp for tp, _c in cols)} — the {sq} = {n} row, per record */",
                  "proc sql;",
                  f"  create table {t} as select {', '.join(rk)}, "
                  + ", ".join(f"max({c}) as {tp}" for tp, c in cols),
                  f"  from raw.{ds} where input(cats({sq}), ?? best32.) = {n}",
                  f"  group by {', '.join(rk)};",
                  "quit;", ""]
            joins.append(f"left join {t} on " + " and ".join(f"a.{k} = {t}.{k}" for k in rk))
            if per_var is not None:
                for tp, _c in cols:
                    per_var.setdefault(temp_var.get(tp, tp.upper()), {"pre": [], "lines": [], "gap": ""})["pre"] += L[_at:]
            sel += [f"{t}.{tp}" for tp, _c in cols]
        for i, (v, temp, a) in enumerate(temp_lookups, 1):
            t = f"_kl{i}"
            _at = len(L)
            ds, col = str(a["dataset"]).lower(), str(a["column"]).upper()
            kc, bcol, sc = str(a["key_col"]).upper(), str(a["base_col"]).upper(), a.get("scan")
            oc = str((a.get("dedup") or {}).get("order_col") or "").upper()
            L += [f"/* {v}: xx-merge {temp} = raw.{ds}.{col} merged on {kc} = "
                  f"{_keyx('', bcol, sc)}{f' (highest {oc} record per subject)' if oc else ''} */",
                  "proc sql;",
                  f"  create table {t} as select {_keyx('', kc, None)} as _klk length=200, "
                  f"max({col}) as {temp}",
                  f"  from raw.{ds} group by 1;",
                  "quit;"]
            if oc and _SAS_NAME.match(oc):
                keep = " ".join(dict.fromkeys(["USUBJID", bcol, oc]))
                L += [f"proc sort data={base_in}(keep={keep}) out={t}s; by USUBJID {oc}; run;",
                      f"data {t}s;",
                      f"  set {t}s; by USUBJID;",
                      f"  if last.USUBJID;   /* the highest {oc} */",
                      f"  length _klk $200; _klk = {_keyx('', bcol, sc)};",
                      f"  keep USUBJID _klk;",
                      "run;"]
                joins.append(f"left join {t}s on a.USUBJID = {t}s.USUBJID "
                             f"left join {t} on {t}s._klk = {t}._klk")
            else:
                joins.append(f"left join {t} on {_keyx('a', bcol, sc)} = {t}._klk")
            sel.append(f"{t}.{temp}")
            L.append("")
            if per_var is not None:
                per_var.setdefault(v, {"pre": [], "lines": [], "gap": ""})["pre"] += L[_at:]
        L += ["/* the base, carrying every rebuilt xx-merge temporary */",
              "proc sql;",
              f"  create table _base_t as select a.*, {', '.join(sel)}",
              f"  from {base_in} as a"] + [f"  {j}" for j in joins] + [
              "  order by a.USUBJID;",
              "quit;", ""]
        base_in = "_base_t"

    merge_inputs = [f"{base_in}(in=_inbase)"]
    merge_inputs += [f"_lk_{ds}" for ds in sorted(lookups)]
    merge_inputs += [f"_dx_{v}" for v, _f, _s in aggregates]
    if dmrefs:
        merge_inputs.append("_dmref")

    L += [f"data sdtm.{dom.lower()};",
          f"  merge {' '.join(merge_inputs)};",
          f"  by USUBJID;",
          f"  if _inbase;                 /* keep the base's record grain */", ""]
    L += body
    L.append("")
    if "DOMAIN" not in mapped:
        L.append(f"  DOMAIN = {_sas_quote(dom)};")
    if "STUDYID" not in mapped:
        L.append(f"  STUDYID = {_sas_quote(study)};")
    drop = sorted(t for t in tmps)
    if drop:
        L.append(f"  drop {' '.join(drop)};")
    L += ["run;", ""]

    # RETAIN / LAG / DIF: a BY-group DATA step each, on the finished values, in BY + ORDER order;
    # the input order is restored afterwards (_ord_), as the build does.
    for v, rc, a in carry_specs:
        _at = len(L)
        src_, by_, ord_ = _carry_args(a)
        sv, last = src_["var"], (by_[-1] if by_ else None)
        dl = dom.lower()
        if rc == "retain":
            first = str(a.get("mode") or "").lower() == "first"
            what = f"RETAIN the {'first' if first else 'last'} non-missing {sv}"
        else:
            n = max(int(float(a.get("n") or 1)), 1)
            dif = str(a.get("fn") or "lag").lower() == "dif"
            what = f"{'DIF' if dif else 'LAG'}{n}({sv})"
        L += [f"/* {v}: {what} within {' '.join(by_) or 'the whole dataset'}"
              f"{', ordered by ' + ' '.join(ord_) if ord_ else ''} */"]
        mode = ("first" if first else "retain") if rc == "retain" else ("dif" if dif else "lag")
        L += _sas_carry_step(f"sdtm.{dl}", f"sdtm.{dl}", v, sv, by_, ord_, mode,
                             1 if rc == "retain" else n) + [""]
        if per_var is not None:
            per_var.setdefault(v, {"pre": [], "lines": [], "gap": ""})["lines"] = L[_at:]

    # finalize: the spec's own sort + dedup (applied, not just flagged). In SAS
    # BY syntax `descending` precedes its variable.
    dom_l = dom.lower()
    sort = [s for s in (settings.get("sort") or [])
            if s.get("var") and _SAS_NAME.match(str(s.get("var")).upper())]
    dd = settings.get("dedup") or {}
    dd_keys = [str(k).upper() for k in (dd.get("keys") or []) if _SAS_NAME.match(str(k).upper())]
    if sort:
        order = " ".join(
            (("descending " + str(s["var"]).upper()) if str(s.get("dir", "asc")).lower() == "desc"
             else str(s["var"]).upper()) for s in sort)
        L += [f"proc sort data=sdtm.{dom_l}; by {order}; run;", ""]
    else:
        L += [f"proc sort data=sdtm.{dom_l}; by STUDYID USUBJID; run;", ""]
    if dd.get("enabled") and dd_keys:
        keep_last = str(dd.get("keep") or "first").lower() == "last"
        # Keep the first/last row per key IN THE CURRENT (just-sorted) order —
        # which `proc sort nodupkey` cannot do, as it reorders. Tag the sorted
        # order with _ord_, re-sort by the dedup keys (+ _ord_, descending for
        # keep=last), and keep the first row of each key group.
        last_key = dd_keys[-1]
        ord_by = f"{' '.join(dd_keys)} " + ("descending _ord_" if keep_last else "_ord_")
        L += [f"/* dedup: keep {'last' if keep_last else 'first'} per "
              f"{', '.join(dd_keys)} (in the sorted order) */",
              f"data _dd; set sdtm.{dom_l}; _ord_ = _n_; run;",
              f"proc sort data=_dd; by {ord_by}; run;",
              f"data sdtm.{dom_l};",
              f"  set _dd;",
              f"  by {' '.join(dd_keys)};",
              f"  if first.{last_key};",
              f"  drop _ord_;",
              f"run;", ""]

    # --SEQ: numbered on the FINAL row order (after sort + dedup), per group.
    for v, grp in seq_specs:
        _at = len(L)
        L += [f"/* {v}: sequence on the final order, per {grp} */",
              f"data _seqsrc; set sdtm.{dom_l}; _ord_ = _n_; run;",
              f"proc sort data=_seqsrc; by {grp} _ord_; run;",
              f"data sdtm.{dom_l};",
              f"  set _seqsrc;",
              f"  by {grp};",
              f"  if first.{grp} then {v} = 0;",
              f"  {v} + 1;",
              f"  drop _ord_;",
              f"run;", ""]
        if per_var is not None:
            per_var.setdefault(v, {"pre": [], "lines": [], "gap": ""})["lines"] = L[_at:]

    # ── Post-build derivations that read other SDTM domains (SE / DM): EPOCH from
    # the SE element interval, --LOBXFL baseline flags. Run on the finished domain. ─
    if epoch_spec:
        body_macros.add("epoch")
        L += [f"/* EPOCH: the SE element interval containing {epoch_spec['refc']} */",
              f"%sdtm_epoch(ds=sdtm.{dom_l}, refc={epoch_spec['refc']});", ""]
    for sp in lobxfl_specs:
        body_macros.add("lobxfl")
        res_arg = f", res={sp['res']}" if sp["res"] else ""
        L += [f"/* {sp['flag']}: last obs on/before DM.{sp['ref']} per {' '.join(sp['by'])} */",
              f"%sdtm_lobxfl(ds=sdtm.{dom_l}, flag={sp['flag']}, by={' '.join(sp['by'])}, "
              f"dtc={sp['dtc']}{res_arg}, ref={sp['ref']});", ""]

    # ── SUPP<dom>: transpose the supplemental qualifiers, then drop them from the
    # parent dataset (STUDYID/RDOMAIN/USUBJID/IDVAR=<dom>SEQ/IDVARVAL/QNAM/QLABEL/
    # QVAL/QORIG/QEVAL — one record per parent row where QVAL is non-missing). ────
    if supp_specs:
        seqv = next((v for v, _g in seq_specs), None)
        idvar = seqv if seqv and _SAS_NAME.match(seqv) else ""
        L += [f"/* SUPP{dom}: supplemental qualifiers -> sdtm.supp{dom_l}"
              f" (one row per {('parent ' + idvar) if idvar else 'parent row'}, non-missing QVAL) */",
              f"data _supp{dom_l};",
              f"  set sdtm.{dom_l};",
              "  length RDOMAIN $8 IDVAR $8 IDVARVAL $200 QNAM $8 QLABEL $200 "
              "QVAL $200 QORIG $200 QEVAL $200;",
              f"  RDOMAIN = {_sas_quote(dom)};",
              f"  IDVAR = {_sas_quote(idvar)};",
              (f"  if not missing({idvar}) then IDVARVAL = strip(put({idvar}, best12.));"
               f"  else IDVARVAL = '';" if idvar else "  IDVARVAL = '';"),
              "  QEVAL = '';"]
        for v, ql, qo in supp_specs:
            L.append(f"  %sdtm_supp(var={v}, qlabel={_nrb(_sas_quote(ql))}, "
                     f"qorig={_nrb(_sas_quote(qo or 'CRF'))});")
        L += ["  keep STUDYID RDOMAIN USUBJID IDVAR IDVARVAL QNAM QLABEL QVAL QORIG QEVAL;",
              "run;",
              f"proc sort data=_supp{dom_l} out=sdtm.supp{dom_l}; by USUBJID QNAM IDVARVAL; run;",
              f"/* drop the qualifier columns from the parent domain */",
              f"data sdtm.{dom_l}; set sdtm.{dom_l}(drop={' '.join(supp_vars)}); run;", ""]

    # Variable labels (SDTM convention): assign each built variable its spec label on the final
    # dataset. Non-destructive (modifies in place, output intact). Labels come from the blocks.
    def _sas_label(text: str) -> str:
        return "'" + str(text).replace("'", "''") + "'"

    labels, seen_lab = [], set()
    for b in blocks:
        if _supp(b):
            continue   # qualifier columns were transposed into SUPP<dom> and dropped
        v = str(b.get("variable") or "").strip().upper()
        lab = str(b.get("label") or "").strip()
        if v and v not in seen_lab and _SAS_NAME.match(v) and lab:
            seen_lab.add(v)
            labels.append(f"    {v} = {_sas_label(lab)}")
    if labels:
        L += [f"/* assign SDTM variable labels on {dom_l} */",
              f"proc datasets lib=sdtm nolist;",
              f"  modify {dom_l};",
              f"  label"] + labels + [
              f"  ;",
              f"  run;",
              f"quit;", ""]

    L += [f"/* {dom} build complete -> sdtm.{dom_l} */"]
    if per_var is not None:
        emitted = set(L)
        for v, pv in per_var.items():
            # drop stubs a later pass replaced, add statements it emitted (e.g. an overlay fill),
            # and attach the banner reason
            assign = re.compile(rf"^\s*{re.escape(v)}\s*=")
            pv["lines"] = [ln for ln in pv["lines"] if ln in emitted]
            pv["lines"] += [ln for ln in body if assign.match(ln) and ln not in pv["lines"]]
            pv["gap"] = next((g[len(v) + 1:].strip() for g in gaps if g.startswith(f"{v}:")), "")
    return "\n".join(L)


def _cond_sas(rule: dict, parse_src) -> tuple:
    """A single cond rule -> (SAS boolean expr, gap) mirroring build_exec `_cond_expr`."""
    r = rule or {}
    op = str(r.get("op", "eq")).lower()
    sexpr, g = parse_src(r.get("src"))
    if sexpr is None:
        return None, g or "unresolvable condition source"
    val = r.get("value", "")
    if op == "missing":
        return f"missing({sexpr})", None
    if op == "notmissing":
        return f"not missing({sexpr})", None
    if op in ("in", "notin"):
        items = [x.strip() for x in str(val).split(",") if x.strip()]
        inlist = "(" + ", ".join(_sas_quote(x) for x in items) + ")"
        return (f"{sexpr} {'not in' if op == 'notin' else 'in'} {inlist}", None)
    if op == "contains":
        return f"index({sexpr}, {_sas_quote(str(val))}) > 0", None
    if op == "starts":
        return f"{sexpr} =: {_sas_quote(str(val))}", None
    if op in ("gt", "lt", "ge", "le"):
        sym = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}[op]
        return f"input({sexpr}, ?? best32.) {sym} {_sas_float(val)}", None
    if op == "ne":
        return f"{sexpr} ne {_sas_quote(str(val))}", None
    if op in ("ends", "between"):
        return None, f"operator {op!r} not generated"
    return f"{sexpr} = {_sas_quote(str(val))}", None


def _sas_float(val) -> str:
    try:
        return str(float(val))
    except (TypeError, ValueError):
        return "."


# ── Persistence (auto-called on build) ───────────────────────────────────────
def save_programs(study: str, domain: str, blocks: list, settings: dict | None = None,
                  by: str = "build_engine", prep_steps: list | None = None) -> dict:
    """Generate + upsert the Python and SAS programs for a domain (one row per language)."""
    dom = domain.upper()
    if prep_steps is None:
        prep_steps = _saved_prep_steps(study, dom)
    py = python_program(study, dom, [dict(b) for b in blocks], settings, prep_steps=prep_steps)
    sk = resolve_subject_keys(study, _blocks_datasets(blocks))
    sas = sas_program(study, dom, blocks, settings, subject_keys=sk, prep_steps=prep_steps)
    db = get_db()
    try:
        db.execute("DELETE FROM generated_programs WHERE study_id=%s AND domain=%s", (study, dom))
        for lang, code in (("python", py), ("sas", sas)):
            db.execute(
                "INSERT INTO generated_programs (study_id, domain, language, program_name, code, description, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (study, dom, lang, f"{dom.lower()}_build.{ 'py' if lang=='python' else 'sas' }", code,
                 f"Auto-generated {lang.upper()} build program for {dom}", by))
    except Exception:  # noqa: BLE001 — never let program-save break a build
        return {"ok": False}
    return {"ok": True, "python_lines": py.count("\n") + 1, "sas_lines": sas.count("\n") + 1}


def generate_for_domain(study: str, domain: str, blocks: list | None = None, settings: dict | None = None) -> dict:
    """On-demand generation for the Code Generation view — from supplied or composed blocks."""
    prep_steps = None
    if blocks is None:
        comp = compose_blocks(study, domain)
        blocks, settings, prep_steps = comp["blocks"], comp.get("settings"), comp.get("prep_steps")
    if prep_steps is None:
        prep_steps = _saved_prep_steps(study, domain)
    py = python_program(study, domain, [dict(b) for b in blocks], settings, prep_steps=prep_steps)
    sk = resolve_subject_keys(study, _blocks_datasets(blocks))
    sas = sas_program(study, domain, blocks, settings, subject_keys=sk, prep_steps=prep_steps)
    save_programs(study, domain, blocks, settings, by="codegen", prep_steps=prep_steps)
    return {"domain": domain.upper(), "python": py, "sas": sas}
