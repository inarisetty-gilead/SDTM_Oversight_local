# Vendored from sdtm-studio sdtm_api/src/build_safe.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""Sandboxed expression evaluator for build derivations.

SDTM Designer compiles every mapping — including USER free-text and LLM output — into
pandas and runs `exec(code, ns)` with a full builtins namespace and no restrictions; its
own code comments admit "exec() — trusted users only until sandboxed" and note that anyone
who can edit a block gets arbitrary server code execution.

This is the 10x fix: derivations are expressed as a restricted, vectorized expression
language evaluated over an AST **allow-list** — no imports, no attribute access, no dunders,
no statements, no arbitrary calls. Only whitelisted helper functions and the row's source
columns (as pandas Series) are in scope. Anything outside the grammar raises before eval.
"""
from __future__ import annotations

import ast

import numpy as np
import pandas as pd

# AST nodes permitted in a derivation expression. Anything else → rejected.
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp,
    ast.Call, ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Dict,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Subscript, ast.Index if hasattr(ast, "Index") else ast.Load,
)


def _series(x, n):
    return x if isinstance(x, pd.Series) else pd.Series([x] * n)


def _build_funcs(nrows: int):
    """Whitelisted, vectorized helpers available inside a derivation expression."""
    def s(x):
        return _series(x, nrows)

    def _str(x):
        return s(x).astype("string")

    return {
        "upper":  lambda x: _str(x).str.upper(),
        "lower":  lambda x: _str(x).str.lower(),
        "strip":  lambda x: _str(x).str.strip(),
        "title":  lambda x: _str(x).str.title(),
        "substr": lambda x, a, b=None: _str(x).str.slice(int(a), None if b is None else int(a) + int(b)),
        "left":   lambda x, n: _str(x).str.slice(0, int(n)),
        "len":    lambda x: _str(x).str.len(),
        "concat": lambda *xs: pd.concat([_str(x) for x in xs], axis=1).agg("".join, axis=1),
        "coalesce": lambda *xs: _first_nonnull([s(x) for x in xs]),
        "when":   lambda cond, a, b: s(a).where(_series(cond, nrows).astype("boolean").fillna(False), s(b)),
        "isnull": lambda x: s(x).isna(),
        "notnull": lambda x: s(x).notna(),
        "to_num": lambda x: pd.to_numeric(s(x), errors="coerce"),
        "map_ct": lambda x, m: _str(x).str.strip().str.upper().map({str(k).upper(): v for k, v in dict(m).items()}).fillna(s(x)),
        "const":  lambda v: pd.Series([v] * nrows),
        "year":   lambda x: pd.to_datetime(s(x), errors="coerce").dt.year,
        # regex=False deliberately: a derivation's `contains('Grade 3.5')` must
        # not also match 'Grade 345', and a crafted pattern against a 31-char
        # value burned 45s of CPU in-process under pandas' regex default.
        "contains": lambda x, sub: _str(x).str.contains(str(sub), case=False, na=False,
                                                        regex=False),
    }


# The helper names a derivation may call. Derived from _build_funcs rather than
# written out, so the allow-list cannot drift from what is actually bound.
_FUNC_NAMES = frozenset(_build_funcs(0))


def _first_nonnull(series_list):
    out = series_list[0].copy()
    for s in series_list[1:]:
        out = out.where(out.notna(), s)
    return out


def validate(expr: str) -> tuple[bool, str]:
    """Return (ok, reason) — reject anything outside the allow-list before evaluation."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return False, f"syntax error: {e.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            return False, "attribute access is not allowed"
        if isinstance(node, (ast.Lambda, ast.comprehension, ast.ListComp, ast.DictComp,
                             ast.SetComp, ast.GeneratorExp, ast.Await, ast.Starred)):
            return False, f"{type(node).__name__} is not allowed"
        if not isinstance(node, _ALLOWED_NODES):
            return False, f"{type(node).__name__} is not allowed"
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                return False, "only direct function calls are allowed"
            # The empty eval namespace already stops `open(...)` reaching
            # anything, but validate() is also the whole of
            # POST /validate-expr — without this it answered "valid" for an
            # expression the build then rejects as 'Series' not callable.
            if node.func.id not in _FUNC_NAMES:
                return False, f"unknown function '{node.func.id}' is not allowed"
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return False, "dunder names are not allowed"
    return True, ""


def safe_eval(expr: str, columns: dict, nrows: int):
    """Evaluate a whitelisted derivation expression. `columns` maps NAME → pandas Series.
    Raises ValueError with a friendly message on any rejected/failed expression."""
    ok, reason = validate(expr)
    if not ok:
        raise ValueError(f"Blocked expression ({reason})")
    funcs = _build_funcs(nrows)
    env = {"__builtins__": {}, **funcs, **columns}
    # names referenced but absent → provided as all-null Series so the expr still runs
    for node in ast.walk(ast.parse(expr, mode="eval")):
        if isinstance(node, ast.Name) and node.id not in env:
            env[node.id] = pd.Series([pd.NA] * nrows)
    try:
        result = eval(compile(ast.parse(expr, mode="eval"), "<derivation>", "eval"), env)  # noqa: S307 — AST allow-listed above
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Derivation failed: {e}") from e
    return _series(result, nrows)
