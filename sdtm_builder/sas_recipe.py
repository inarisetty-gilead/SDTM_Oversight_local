"""SAS code as a mapping: an expression, an IF/THEN/ELSE chain, RETAIN and LAG.

Three recipes, all evaluated by the SAS runtime (sdtm_builder/sas) so the engine, the Python
program and the SAS program compute the same value SAS would:

  * ``sas``    — {"code": "<SAS>", "sources": {NAME: {"dataset", "column"} | {"var": X}}}.
                 The code is one expression (``floor(yrdif(input(brthdtc, yymmdd10.),
                 input(rfstdtc, yymmdd10.), 'AGE'))``) or assignments to this one variable
                 (``if aesev = '1' then _aesev = 'MILD'; else …``). Each name it reads comes
                 from ``sources`` when given; otherwise from a variable of this domain built
                 before it, else from a column of the domain's base records. A name none of
                 those has is an error — never a silently blank input.
  * ``retain`` — {"source": <source>, "by": [...], "order": [...], "mode": "locf" | "first"}:
                 the last (or first) non-missing value so far within each BY group.
  * ``lag``    — {"source": <source>, "by": [...], "order": [...], "n": 1, "dif": false}:
                 LAGn / DIFn within each BY group.

RETAIN and LAG walk the records in BY + ORDER order and return the values in the domain's own
record order, as the SAS DATA step does after a PROC SORT and a sort back.
"""
from __future__ import annotations

import re

import pandas as pd

from .sas import expr as sx
from .sas import runtime as rt
from .util import s, upper

CARRY_RECIPES = ("retain", "lag")
_STATEMENT = re.compile(r"^\s*(if|else)\b", re.I)
_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)", re.S)


class SasCodeError(ValueError):
    pass


def compile_code(code: str, variable: str = ""):
    """('expr', tree) or ('chains', chains) — or SasCodeError naming what is not SAS.

    Statements (IF/ELSE, or `X = …;` assigning this variable or its `_x` work twin) become an
    IF chain; anything else must be ONE expression."""
    text = s(code).strip()
    if not text:
        raise SasCodeError("no SAS code")
    body = text.rstrip().rstrip(";")
    m = _ASSIGN.match(body)
    v = upper(variable)
    statement = bool(_STATEMENT.match(body)) or ";" in body or bool(
        m and upper(m.group(1)) in {v, "_" + v} and v)
    try:
        if statement:
            _target, chains = sx.parse_if_chain(text)
            return ("chains", chains)
        return ("expr", sx.parse(body))
    except sx.SasExprError as exc:
        raise SasCodeError(str(exc)) from exc


def names(compiled) -> list[str]:
    kind, obj = compiled
    return sx.chain_names(obj) if kind == "chains" else sx.names(obj)


def to_sas(compiled, target: str, ref=None) -> list[str]:
    """DATA-step statements assigning `target`."""
    kind, obj = compiled
    if kind == "chains":
        return sx.chains_to_sas(obj, target, ref)
    return [f"{target} = {sx.to_sas(obj, ref)};"]


def to_py(compiled, ref) -> str:
    kind, obj = compiled
    if kind == "chains":
        return f"_sx_res(_ix, {sx.chains_to_py(obj, ref)})"
    return f"_sx_res(_ix, {sx.to_py(obj, ref)})"


def check(code: str, variable: str = "") -> dict:
    """What the interface shows while a reader types: does it parse, which names does it read,
    and the canonical SAS it will run as."""
    try:
        c = compile_code(code, variable)
    except SasCodeError as exc:
        return {"ok": False, "error": str(exc)}
    if not variable and c[0] == "expr":          # a prep step's expression: no target
        return {"ok": True, "names": names(c), "sas": [sx.to_sas(c[1])]}
    return {"ok": True, "names": names(c), "sas": to_sas(c, upper(variable) or "X")}


# ── the engine ──────────────────────────────────────────────────────────────
def _column(ctx, name: str, spec: dict | None, selfvar: str) -> pd.Series:
    from .ops import OpError, source_series
    if spec:
        if spec.get("var") or spec.get("kind") == "var":
            v = upper(spec.get("var"))
            if v not in ctx.frame.columns:
                raise OpError(f"{v} is not built yet — move {selfvar} after it")
            return ctx.frame[v]
        if spec.get("dataset") and spec.get("column"):
            return source_series(ctx, spec["dataset"], spec["column"])
    if name != selfvar and name in ctx.frame.columns:
        return ctx.frame[name]
    col = next((c for c in ctx.base.columns if upper(c) == name), None)
    if col is not None:
        return ctx.base[col]
    raise OpError(f"{name} is neither a variable built before {selfvar} nor a column of the "
                  "base records — name its source")


def op_sas(ctx, b) -> pd.Series:
    from .ops import OpError
    a = b.args or {}
    try:
        compiled = compile_code(a.get("code"), b.variable)
    except SasCodeError as exc:
        raise OpError(f"the SAS code is not understood: {exc}") from exc
    srcs = {upper(k): v for k, v in (a.get("sources") or {}).items()}
    cols = {n: _column(ctx, n, srcs.get(n), b.variable) for n in names(compiled)}
    kind, obj = compiled
    if kind == "chains":
        out = sx.evaluate_chains(obj, cols, ctx.index)
    else:
        out = sx.evaluate(obj, cols, ctx.index)
    return _as_column(out)


def _as_column(out: pd.Series) -> pd.Series:
    """A numeric result stays numeric; text is stripped, with missing as <NA>."""
    if pd.api.types.is_numeric_dtype(out):
        return out
    t = out.astype("string").str.strip()
    return t.where(t != "", pd.NA)


def _carry_frame(ctx, cols: list[str], selfvar: str) -> pd.DataFrame:
    return pd.DataFrame({upper(c): _column(ctx, upper(c), None, selfvar) for c in cols},
                        index=ctx.index)


def carry_source(a: dict) -> dict:
    """The value a RETAIN / LAG carries: {'source': {...}}, or the editor's `source_var`."""
    src = a.get("source") or {}
    if not src and s(a.get("source_var")):
        src = {"var": upper(a["source_var"])}
    return src


def _is_dif(a: dict) -> bool:
    return bool(a.get("dif")) or s(a.get("fn")).lower() == "dif"


def op_retain(ctx, b) -> pd.Series:
    from .ops import _series_for
    a = b.args or {}
    by, order = [upper(c) for c in a.get("by") or []], [upper(c) for c in a.get("order") or []]
    src = _series_for(ctx, carry_source(a), b.variable, "raw")
    frame = _carry_frame(ctx, by + order, b.variable)
    return _as_column(rt._sx_retain(frame, src, by, order, s(a.get("mode")) or "locf"))


def op_lag(ctx, b) -> pd.Series:
    from .ops import _series_for
    a = b.args or {}
    by, order = [upper(c) for c in a.get("by") or []], [upper(c) for c in a.get("order") or []]
    src = _series_for(ctx, carry_source(a), b.variable, "raw")
    frame = _carry_frame(ctx, by + order, b.variable)
    n = max(int(float(a.get("n") or 1)), 1)
    return _as_column(rt._sx_lag(frame, src, by, order, n, _is_dif(a)))


def describe(b) -> str:
    """One line of SAS for a carry recipe, for reports and the editor."""
    a = b.args or {}
    src = carry_source(a)
    col = upper(src.get("var") or src.get("column") or "?")
    by = " ".join(upper(c) for c in a.get("by") or []) or "the whole dataset"
    if b.recipe == "retain":
        what = f"RETAIN the {'first' if s(a.get('mode')) == 'first' else 'last'} non-missing {col}"
    else:
        n = max(int(float(a.get("n") or 1)), 1)
        what = f"{'DIF' if _is_dif(a) else 'LAG'}{n}({col})"
    order = " ".join(upper(c) for c in a.get("order") or [])
    return f"{what} within {by}" + (f", ordered by {order}" if order else "")


def sas_carry_step(work: str, target: str, col: str, by: list[str], order: list[str],
                   mode: str, n: int = 1) -> list[str]:
    """RETAIN / LAG / DIF of `col` into `target` within BY groups, in BY + ORDER order, as SAS:
    tag the input order, sort, one BY-group DATA step, sort back. mode: locf | first | lag | dif."""
    last = by[-1] if by else None
    L = [f"data _cf; set {work}; _ord_ = _n_; run;",
         f"proc sort data=_cf; by {' '.join([*by, *order, '_ord_'])}; run;",
         f"data {work};", "  set _cf;"] + ([f"  by {' '.join(by)};"] if by else [])
    if mode in ("locf", "first"):
        L += ["  retain _rt;", f"  if 0 then _rt = {col};          /* _rt takes {col}'s type and length */"]
        if last:
            L.append(f"  if first.{last} then call missing(_rt);")
        L += [f"  if missing(_rt) then _rt = {col};" if mode == "first" else
              f"  if not missing({col}) then _rt = {col};", f"  {target} = _rt;", "  drop _rt;"]
    else:
        L += [f"  _lg = lag{n}({col});                /* always executed, so the queue stays in step */"]
        L += [f"  if first.{last} then _gn = 0;", "  _gn + 1;"] if last else ["  _gn = _n_;"]
        L += [f"  if _gn <= {n} then call missing(_lg);   /* no earlier record in this group */",
              f"  {target} = {col} - _lg;" if mode == "dif" else f"  {target} = _lg;", "  drop _lg _gn;"]
    return L + ["run;", f"proc sort data={work} out={work}(drop=_ord_); by _ord_; run;"]


def carry_mode(b) -> str:
    a = b.args or {}
    if b.recipe == "retain":
        return "first" if s(a.get("mode")) == "first" else "locf"
    return "dif" if _is_dif(a) else "lag"
