"""Generated programs: the build, written out as code a programmer can take away.

The build executes named functions (ops.py); nothing here changes that. These emitters
write a STANDALONE reproduction of a domain's build — a Python/pandas program and a
house-style SAS program — from the same structured blocks the build ran, so the handed-off
code says exactly what the tool did.

Honesty rule: an operation this emitter cannot translate is a clearly-marked TODO comment
carrying the spec's own rule and SAS code — never a silent gap, never invented logic.
"""
from __future__ import annotations

import json

from .blocks import Block
from .util import norm_key, s, upper

CT_INLINE_CAP = 600            # same cap the build applies to inline codelist maps
SUBJECT_KEYS = ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID", "SUBJECT_ID", "SUBJ_ID",
                "SCRNID", "SCREENINGNUMBER")


# ── shared analysis ─────────────────────────────────────────────────────────
def _sources_of(spec: dict) -> list[tuple[str, str]]:
    """(dataset, column) pairs a source descriptor reads."""
    spec = spec or {}
    ds, col = s(spec.get("dataset")), s(spec.get("column"))
    return [(ds, col)] if ds and col else []


def _block_datasets(b: Block) -> set[str]:
    """Raw/prepared dataset names one block reads."""
    out = set()
    if b.dataset:
        out.add(norm_key(b.dataset))
    a = b.args or {}
    if a.get("dataset"):
        out.add(norm_key(a["dataset"]))
    for key in ("sources",):
        for src in a.get(key) or []:
            if isinstance(src, dict) and src.get("dataset"):
                out.add(norm_key(src["dataset"]))
    for rule in a.get("rules") or []:
        for c in [rule] + list(rule.get("and") or []):
            src = (c or {}).get("src") or {}
            if src.get("dataset"):
                out.add(norm_key(src["dataset"]))
        then = (rule or {}).get("then") or {}
        if then.get("dataset"):
            out.add(norm_key(then["dataset"]))
    els = a.get("else") or {}
    if isinstance(els, dict) and els.get("dataset"):
        out.add(norm_key(els["dataset"]))
    for st in a.get("steps") or []:
        sub = Block(variable=b.variable, domain=b.domain,
                    dataset=s(st.get("dataset")), args=st.get("args") or {})
        out |= _block_datasets(sub)
    return out


def _step_datasets(step: dict) -> set[str]:
    """Raw dataset names one prep step reads (its own output name excluded)."""
    p = step.get("params") or {}
    out = set()
    for n in p.get("datasets") or []:
        out.add(norm_key(n))
    for spec in p.get("inputs") or []:
        if spec.get("dataset"):
            out.add(norm_key(spec["dataset"]))
    for spec in p.get("sources") or []:
        if spec.get("dataset"):
            out.add(norm_key(spec["dataset"]))
    if p.get("dataset"):
        out.add(norm_key(p["dataset"]))
    return out


def _ct_inline(codelists: dict, cl: str, observed_vals: list[str] | None,
               overrides: dict | None) -> tuple[dict, str]:
    """The codelist map for ONE variable, scoped to the values ITS source column holds.
    A 100-term list with 2 observed values inlines 2 entries. Without observed values
    the full map is kept — never guess what the data holds. Returns (map, comment)."""
    cmap = dict(codelists.get(upper(cl), {}))
    for k, v in (overrides or {}).items():
        cmap[upper(k)] = v
    total = len(cmap)
    if observed_vals is None:
        return cmap, f"  # {upper(cl)}: all {total} term(s) — the source values could not be profiled"
    seen = {upper(x) for x in observed_vals}
    small = {k: v for k, v in cmap.items() if k in seen}
    if not small:
        return {}, (f"  # {upper(cl)}: none of its {total} term(s) occur in the data — "
                    "values pass through and are flagged")
    return small, (f"  # {upper(cl)}: {len(small)} of {total} term(s) — only the values "
                   "observed in this study's data")


# ═════════════════════════════════════════════════════════════════════════════
# Python / pandas
# ═════════════════════════════════════════════════════════════════════════════
_PY_HELPERS = '''
def read_raw(name):
    """First file whose stem is `name` (or starts with `name_`) — csv / sas7bdat / xpt /
    parquet, the same discovery rule the tool uses. Columns are upper-cased."""
    for p in sorted(RAW.rglob("*")):
        stem = p.stem.lower()
        if stem != name and not stem.startswith(name + "_"):
            continue
        ext = p.suffix.lower()
        if ext in (".csv", ".txt", ".tsv"):
            d = pd.read_csv(p, dtype=str, keep_default_na=False, na_values=[""])
        elif ext == ".sas7bdat":
            d = pd.read_sas(p, encoding="latin-1")
        elif ext == ".xpt":
            d = pd.read_sas(p, format="xport", encoding="latin-1")
        elif ext in (".parquet", ".pq"):
            d = pd.read_parquet(p)
        else:
            continue
        d.columns = [str(c).upper() for c in d.columns]
        return d
    raise FileNotFoundError(f"no raw file found for '{name}' under {RAW}")

def TXT(ser):
    """Character view of a column (SAS-style: strip handled per use)."""
    return ser.astype("string")

def subject_map(frame, col, base):
    """One value per subject from another dataset, aligned to the base records —
    a positional copy across different row counts would silently misalign."""
    key = next((k for k in SUBJECT_KEYS if k in frame.columns and k in base.columns), None)
    if key is None:
        raise KeyError(f"no shared subject key to align {col}")
    src = frame.dropna(subset=[key]).drop_duplicates(key)
    return base[key].map(pd.Series(src[col].values, index=src[key].values))

def apply_ct(ser, cmap):
    """Normalise to the codelist's submission values (manual study mappings included);
    unmatched values PASS THROUGH for validation to flag — never dropped, never invented."""
    if not cmap:
        return TXT(ser).str.strip()
    text = TXT(ser).str.strip()
    return text.str.upper().map(cmap).fillna(text)

def iso_parts(y, m=None, d=None, t=None):
    """Partial-aware ISO 8601 from split date parts (2024 / 2024-03 stay as collected)."""
    BAD = {"", "NAN", "NONE", "NAT", "UN", "UNK", "UNKN", "UNKNOWN", ".", "--", "NA", "NULL"}
    def clean(x, width):
        if x is None:
            return None
        v = TXT(x).str.strip()
        v = v.where(~v.str.upper().isin(BAD), "")
        num = v.str.replace(r"\\.0$", "", regex=True)
        return num.str.zfill(width).where(num != "", "")
    Y, M, D = clean(y, 4), clean(m, 2), clean(d, 2)
    res = Y.copy()
    if M is not None:
        res = res.mask((Y != "") & (M != ""), Y + "-" + M)
        if D is not None:
            res = res.mask((Y != "") & (M != "") & (D != ""), Y + "-" + M + "-" + D)
    if t is not None:
        T = TXT(t).str.strip()
        T = T.where(~(T.isna() | T.str.upper().isin(sorted(BAD))), "")
        res = res.mask((res.str.len() >= 10) & (T != ""), res + "T" + T)
    return res.fillna("")

def iso_text(ser):
    """Free-text date -> ISO date; unparseable becomes blank, never a guess."""
    parsed = pd.to_datetime(ser, errors="coerce", format="mixed")
    return parsed.dt.strftime("%Y-%m-%d").astype("string").fillna("")
'''.rstrip()


class _Py:
    """Emitter state for one Python program."""

    def __init__(self, domain: str, base_name: str, codelists: dict,
                 observed: dict[str, list[str]] | None = None):
        self.dom = domain
        self.base = norm_key(base_name)
        self.codelists = codelists
        self.observed = observed or {}
        self.todos: list[str] = []

    def ct_call(self, expr: str, var: str, cl: str, overrides: dict | None) -> str:
        """apply_ct(<expr>, {…}) with the map scoped to THIS variable's data values."""
        cmap, note = _ct_inline(self.codelists, cl, self.observed.get(var), overrides)
        if len(cmap) > CT_INLINE_CAP:
            return (f'df["{var}"] = ({expr}).str.strip()'
                    f'  # codelist {upper(cl)} too large to inline — values pass through')
        return f'df["{var}"] = apply_ct({expr}, {json.dumps(cmap, ensure_ascii=False)}){note}'

    # a source descriptor -> pandas expression (string cast)
    def src(self, spec: dict, selfvar: str = "") -> str:
        spec = spec or {}
        kind = str(spec.get("kind") or "").lower()
        if kind == "self" and selfvar:
            return f'TXT(df["{upper(selfvar)}"])'
        if kind == "step":
            return f'TXT(_step{int(spec.get("step") or 0)})'
        if kind == "var" or (not kind and spec.get("var")):
            return f'TXT(df["{upper(spec.get("var"))}"])'
        if kind == "text" or (not kind and spec.get("text") is not None and not spec.get("column")):
            return f'pd.Series({s(spec.get("text"))!r}, index=df.index, dtype="string")'
        ds, col = norm_key(spec.get("dataset")), upper(spec.get("column"))
        if ds and col:
            return self.col(ds, col)
        return 'pd.Series(pd.NA, index=df.index, dtype="string")'

    def col(self, ds: str, col: str) -> str:
        """A raw column aligned to the base records (same semantics as the build)."""
        if ds == self.base:
            return f'TXT({self.frame(ds)}["{col}"])'
        return f'TXT(subject_map({self.frame(ds)}, "{col}", base))'

    @staticmethod
    def frame(ds: str) -> str:
        return f"raw_{norm_key(ds)}"

    def todo(self, b: Block, why: str) -> str:
        self.todos.append(b.variable)
        lines = [f'# TODO (hand-code): {b.variable} — {why}']
        if s(b.mapping_rule) and b.mapping_rule.lower() != "nan":
            lines.append(f"#   spec rule: {b.mapping_rule[:200]}")
        if s(b.sas_code) and b.sas_code.lower() != "nan":
            lines.append(f"#   spec SAS:  {b.sas_code[:200]}")
        lines.append(f'df["{b.variable}"] = pd.Series(pd.NA, index=df.index, dtype="string")')
        return "\n".join(lines)


def _py_fn_expr(g: _Py, b: Block, a: dict, selfvar: str) -> str | None:
    """pandas expression for one SAS-style function; None -> not translatable."""
    fn = str(a.get("fn") or "").lower()
    srcs = [g.src(x, selfvar) for x in (a.get("sources") or [])]
    one = srcs[0] if srcs else 'pd.Series(pd.NA, index=df.index, dtype="string")'
    if fn in ("catx", "cats", "cat"):
        if not srcs:
            return None
        if fn == "catx":
            sep = s(a.get("sep"))
            return ("pd.concat([" + ", ".join(f"({x}).str.strip().fillna('')" for x in srcs) + "], axis=1)"
                    f".apply(lambda r: {sep!r}.join(v for v in r if v != ''), axis=1)")
        parts = [f"({x}).str.strip().fillna('')" if fn == "cats" else f"({x}).fillna('')" for x in srcs]
        return " + ".join(parts)
    if fn == "coalesce":
        return ("pd.concat([" + ", ".join(srcs) + "], axis=1).bfill(axis=1).iloc[:, 0]") if srcs else None
    simple = {
        "strip": ".str.strip()", "trim": ".str.rstrip()", "left": ".str.lstrip()",
        "upcase": ".str.upper()", "lowcase": ".str.lower()", "propcase": ".str.title()",
        "reverse": ".str.slice(step=-1)", "length": ".str.len()",
        "compbl": ".str.replace(r'\\s+', ' ', regex=True).str.strip()",
    }
    if fn in simple:
        return f"({one}){simple[fn]}"
    if fn == "substr":
        start = max(int(float(a.get("start") or 1)) - 1, 0)
        ln = s(a.get("len"))
        return (f"({one}).str.slice({start})" if ln in ("", "0")
                else f"({one}).str.slice({start}, {start + int(float(ln))})")
    if fn == "scan":
        word = int(float(a.get("word") or 1))
        idx = word - 1 if word > 0 else word
        return f"({one}).str.split({(s(a.get('delim')) or ' ')!r}).str[{idx}]"
    if fn == "compress":
        chars = s(a.get("chars"))
        if not chars:
            return f"({one}).str.replace(r'\\s+', '', regex=True)"
        expr = one
        for ch in chars:
            expr = f"({expr}).str.replace({ch!r}, '', regex=False)"
        return expr
    if fn == "zeropad":
        return f"({one}).str.strip().str.zfill({int(float(a.get('width') or 0))})"
    if fn == "tranwrd":
        return f"({one}).str.replace({s(a.get('find'))!r}, {s(a.get('replace'))!r}, regex=False)"
    if fn == "index":
        return f"(({one}).str.find({s(a.get('find'))!r}) + 1)"
    if fn == "put":
        return (f"({one}).map(lambda x: '' if pd.isna(x) else (str(int(float(x))) "
                f"if str(x).replace('.', '', 1).lstrip('-').isdigit() and float(x) == int(float(x)) "
                f"else str(x))).astype('string')")
    if fn == "input":
        return f"pd.to_numeric({one}, errors='coerce')"
    return None


def _py_cond_mask(g: _Py, rule: dict, selfvar: str) -> str:
    """Boolean pandas expression for one condition (the rule itself or an AND extra)."""
    op = str((rule or {}).get("op") or "eq").lower()
    src = g.src((rule or {}).get("src"), selfvar)
    val = s((rule or {}).get("value"))
    blank = f"(({src}).isna() | (({src}).str.strip() == ''))"
    if op == "missing":
        return blank
    if op == "notmissing":
        return f"~{blank}"
    if op in ("in", "notin"):
        items = [x.strip() for x in val.split(",") if x.strip()]
        base = f"({src}).isin({items!r})"
        return f"~{base}" if op == "notin" else base
    if op == "contains":
        return f"({src}).str.contains({val!r}, na=False, regex=False)"
    if op == "starts":
        return f"({src}).fillna('').str.startswith({val!r})"
    if op == "ends":
        return f"({src}).fillna('').str.endswith({val!r})"
    if op in ("gt", "lt", "ge", "le"):
        sym = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}[op]
        return f"(pd.to_numeric({src}, errors='coerce') {sym} {float(val or 0)})"
    if op == "between":
        return (f"pd.to_numeric({src}, errors='coerce').between("
                f"{float(val or 0)}, {float(s((rule or {}).get('value2')) or 0)})")
    if op == "ne":
        return f"(({src}) != {val!r})"
    return f"(({src}) == {val!r})"


def _py_cond_mask_all(g: _Py, rule: dict, selfvar: str) -> str:
    parts = [_py_cond_mask(g, rule, selfvar)]
    parts += [_py_cond_mask(g, extra, selfvar) for extra in (rule or {}).get("and") or []]
    if len(parts) == 1:
        return f"({parts[0]}).fillna(False)"
    return " & ".join(f"({p}).fillna(False)" for p in parts)


def _py_result(g: _Py, res: dict, selfvar: str) -> str:
    if (res or {}).get("kind") == "missing":
        return 'pd.Series(pd.NA, index=df.index, dtype="string")'
    return g.src(res, selfvar)


def _py_block_lines(g: _Py, b: Block, var: str | None = None) -> list[str]:
    """Statements that set df[VAR] for one block (or one pipeline sub-block)."""
    v = upper(var or b.variable)
    a = b.args or {}

    if b.mtype == "constant":
        return [f'df["{v}"] = {s(b.value if b.mtype == "constant" else a.get("value"))!r}']
    if b.mtype == "sequence":
        grp = upper(a.get("group") or "USUBJID")
        return [f'df["{v}"] = (df.groupby("{grp}", dropna=False).cumcount() + 1).astype("Int64")'
                f'  # numbered on the FINAL sorted records']
    if b.mtype == "assign":
        expr = g.col(norm_key(b.dataset), upper(b.column))
        if b.codelist:
            return [g.ct_call(expr, v, b.codelist, (b.args or {}).get("ct_overrides"))]
        return [f'df["{v}"] = ({expr}).str.strip()']

    rc = b.recipe
    if rc == "custom_fn":                # the user's library function — its steps were
        rc = "pipeline"                  # resolved into args at build time
    if rc == "ct":
        src = (a.get("sources") or [a])[0]
        cl = upper(a.get("codelist")) or upper(b.codelist)
        return [g.ct_call(g.src(src, v), v, cl, a.get("ct_overrides"))]
    if rc == "fn":
        expr = _py_fn_expr(g, b, a, v)
        return [f'df["{v}"] = {expr}'] if expr else [g.todo(b, f"function '{a.get('fn')}' has no translation")]
    if rc == "cond":
        lines = [f'df["{v}"] = {_py_result(g, a.get("else") or {"kind": "missing"}, v)}']
        for rule in reversed(a.get("rules") or []):       # reverse so the FIRST rule wins
            lines.append(f'df["{v}"] = df["{v}"].mask({_py_cond_mask_all(g, rule, v)}, '
                         f'{_py_result(g, rule.get("then"), v)})')
        return lines
    if rc == "iso_date":
        ds = norm_key(a.get("dataset")) or norm_key(b.dataset)
        y, m, d = upper(a.get("y_col")), upper(a.get("m_col")), upper(a.get("d_col"))
        t = upper(a.get("time_col"))
        date_col = upper(a.get("date_col"))
        if y:
            parts = [f'{g.col(ds, y)}']
            parts.append(f"m={g.col(ds, m)}" if m else "m=None")
            parts.append(f"d={g.col(ds, d)}" if d else "d=None")
            if t:
                parts.append(f"t={g.col(ds, t)}")
            return [f'df["{v}"] = iso_parts({", ".join(parts)})  # partial-aware ISO 8601']
        if not date_col:
            return [g.todo(b, "no raw date column resolved")]
        lines = [f'df["{v}"] = iso_text({g.col(ds, date_col)})']
        if t:
            lines += [f'_t = ({g.col(ds, t)}).str.strip().fillna("")',
                      f'df["{v}"] = df["{v}"].mask((df["{v}"].str.len() >= 10) & (_t != ""), '
                      f'df["{v}"] + "T" + _t)']
        return lines
    if rc == "study_day":
        dtc = upper(a.get("dtc_var"))
        ref = upper(a.get("ref_var")) or "RFSTDTC"
        if not dtc:
            return [g.todo(b, "event --DTC variable unresolved")]
        ref_expr = (f'TXT(df["{ref}"])' if b.domain.upper() == "DM"
                    else f'TXT(df["USUBJID"].map(DM_REF["{ref}"]))')
        return [
            f'_ev = pd.to_datetime(TXT(df["{dtc}"]).str[:10], errors="coerce", format="mixed")',
            f'_rf = pd.to_datetime(({ref_expr}).str[:10], errors="coerce", format="mixed")',
            f'_d = (_ev - _rf).dt.days',
            f'df["{v}"] = _d.where(_d < 0, _d + 1).astype("Int64")'
            f'  # +1 on/after {ref}, no day 0',
        ]
    if rc == "copy_var":
        srcv = upper(a.get("var") or a.get("source_var"))
        if b.codelist:                         # the build applies CT to copies too
            return [g.ct_call(f'TXT(df["{srcv}"])', v, b.codelist,
                              (b.args or {}).get("ct_overrides"))]
        return [f'df["{v}"] = df["{srcv}"]']
    if rc == "concat":
        cols = [c for c in (a.get("columns") or []) if c]
        ds = norm_key(a.get("dataset")) or norm_key(b.dataset)
        if not cols:
            return [g.todo(b, "no columns picked to concatenate")]
        sep = s(a.get("sep")) or " "
        expr = f" + {sep!r} + ".join(f"({g.col(ds, upper(c))}).fillna('')" for c in cols)
        return [f'df["{v}"] = {expr}']
    if rc == "pipeline":
        steps = a.get("steps") or []
        if not steps:
            return [g.todo(b, "pipeline has no steps")]
        lines: list[str] = []
        for i, st in enumerate(steps, start=1):
            op = str(st.get("op") or "fn").lower()
            sub = Block(variable=v, domain=b.domain, codelist=b.codelist)
            if op == "assign":
                sub.mtype, sub.dataset, sub.column = "assign", s(st.get("dataset")), s(st.get("column"))
            elif op == "constant":
                sub.mtype, sub.value = "constant", st.get("value", "")
            else:
                sub.mtype, sub.recipe, sub.args = "derived", op, (st.get("args") or {})
            lines.append(f"# step {i}: {op}")
            lines += _py_block_lines(g, sub, v)
            lines.append(f'_step{i} = df["{v}"]')
        return lines
    return [g.todo(b, f"recipe '{rc or b.mtype}' runs inside the tool but has no standalone "
                      "translation yet")]


def _py_prep_lines(g: _Py, step: dict) -> list[str]:
    """Statements producing one prep step's output frame(s)."""
    op = str(step.get("op") or "").lower()
    name = norm_key(step.get("name") or "prep")
    p = step.get("params") or {}
    L = [f"# prep: {op} -> {name}"]

    if op == "stack":
        parts = []
        for n in p.get("datasets") or []:
            parts.append(f'{g.frame(n)}.assign(__SOURCE_DATASET="{norm_key(n)}")')
        L.append(f'{name} = pd.concat([{", ".join(parts)}], ignore_index=True, sort=False)')
        return L
    if op == "merge":
        specs = p.get("inputs") or []
        how = str(p.get("how", "left")).lower()
        on = [upper(x) for x in (p.get("on") or [])]
        frames = []
        for i, spec_in in enumerate(specs):
            fr = g.frame(spec_in.get("dataset"))
            keep = [upper(c) for c in (spec_in.get("columns") or [])]
            if keep:
                keys = list(dict.fromkeys(keep + on + [k for k in SUBJECT_KEYS + ("STUDYID",)]))
                L.append(f'_m{i} = {fr}[[c for c in {keys!r} if c in {fr}.columns]]')
                frames.append(f"_m{i}")
            else:
                frames.append(fr)
        L.append(f"{name} = {frames[0]}")
        for fr in frames[1:]:
            key_expr = (repr(on) if on
                        else f'[k for k in {list(SUBJECT_KEYS)!r} if k in {name}.columns and k in {fr}.columns][:1]')
            L.append(f'{name} = {name}.merge({fr}, on={key_expr}, how="{how}", suffixes=("", "_r"))')
            L.append(f'for _c in [c for c in {name}.columns if c.endswith("_r") '
                     f'and c[:-2] in {name}.columns]:   # SAS MERGE: the later dataset wins')
            L.append(f'    {name}[_c[:-2]] = {name}[_c].combine_first({name}[_c[:-2]])')
            L.append(f'{name} = {name}.drop(columns=[c for c in {name}.columns '
                     f'if c.endswith("_r") and c[:-2] in {name}.columns])')
        return L
    if op == "select":
        L.append(f'{name} = {g.frame(p.get("dataset"))}'
                 f'[[c for c in {[upper(c) for c in p.get("columns") or []]!r}]].copy()')
        return L
    if op == "drop":
        L.append(f'{name} = {g.frame(p.get("dataset"))}.drop('
                 f'columns={[upper(c) for c in p.get("columns") or []]!r}, errors="ignore")')
        return L
    if op == "rename":
        mapping = {upper(r.get("from")): upper(r.get("to"))
                   for r in p.get("renames") or [] if s(r.get("from")) and s(r.get("to"))}
        L.append(f'{name} = {g.frame(p.get("dataset"))}.rename(columns={mapping!r})')
        return L
    if op == "filter":
        conds = p.get("conds") or [{"column": p.get("column"),
                                    "operator": p.get("operator", "=="), "value": p.get("value", "")}]
        fr = g.frame(p.get("dataset"))
        masks = []
        for c in conds:
            colx = f'{fr}["{upper(c.get("column"))}"].astype("string").str.strip()'
            oper, val = str(c.get("operator") or "==").lower(), s(c.get("value"))
            if oper == "==":
                masks.append(f'({colx}.str.upper() == {val.upper()!r})')
            elif oper == "!=":
                masks.append(f'({colx}.str.upper() != {val.upper()!r})')
            elif oper == "contains":
                masks.append(f'{colx}.str.contains({val!r}, case=False, na=False, regex=False)')
            elif oper in ("in", "notin"):
                items = [x.strip().upper() for x in val.split(",") if x.strip()]
                m = f'{colx}.str.upper().isin({items!r})'
                masks.append(m if oper == "in" else f"~({m})")
            elif oper == "missing":
                masks.append(f'({colx}.isna() | ({colx} == ""))')
            elif oper == "notmissing":
                masks.append(f'~({colx}.isna() | ({colx} == ""))')
            else:
                masks.append("pd.Series(True, index=%s.index)  # TODO: operator %r" % (fr, oper))
        L.append(f'{name} = {fr}[{" & ".join(masks)}].reset_index(drop=True)')
        return L
    if op == "sort":
        cols = [upper(c) for c in p.get("columns") or []]
        asc = [str(x).lower() != "desc" for x in (p.get("directions") or [])]
        asc = (asc + [True] * len(cols))[:len(cols)]
        L.append(f'{name} = {g.frame(p.get("dataset"))}.sort_values(by={cols!r}, ascending={asc!r}, '
                 f'kind="stable", na_position="last").reset_index(drop=True)')
        return L
    if op == "dedup":
        keys = [upper(c) for c in p.get("keys") or []]
        keep = "last" if str(p.get("keep", "first")).lower() == "last" else "first"
        L.append(f'{name} = {g.frame(p.get("dataset"))}.drop_duplicates(subset={keys!r}, '
                 f'keep="{keep}").reset_index(drop=True)')
        return L
    if op == "transpose_findings":
        src = g.frame(p.get("dataset"))
        idv = [upper(c) for c in p.get("id_vars") or []]
        tc, tn = upper(p.get("testcd_col")), upper(p.get("test_col"))
        orres, orresu = upper(p.get("orres_col")), upper(p.get("orresu_col"))
        L.append("_parts = []")
        for order, m in enumerate(p.get("measures") or []):
            vcol, ucol = upper(m.get("value_col")), upper(m.get("unit_col"))
            L += [
                f'_p = {src}[[c for c in {idv!r} if c in {src}.columns]].copy()',
                f'_p["__ROW"] = range(len({src})); _p["__M"] = {order}',
                f'_p["{tc}"] = {upper(m.get("testcd"))!r}; '
                f'_p["{tn}"] = {s(m.get("test")) or upper(m.get("testcd"))!r}',
                f'_p["{orres}"] = {src}["{vcol}"] if "{vcol}" in {src}.columns else ""',
                f'_p["{orresu}"] = {src}["{ucol}"] if "{ucol}" in {src}.columns else ""'
                if ucol else f'_p["{orresu}"] = ""',
                '_parts.append(_p)',
            ]
        L += [
            f'{name} = pd.concat(_parts, ignore_index=True, sort=False)',
            f'{name} = {name}[~({name}["{orres}"].astype("string").str.strip().replace("nan", "")'
            f'.fillna("") == "")]   # a measurement not taken is not a record',
            f'{name} = {name}.sort_values(["__ROW", "__M"], kind="stable")'
            f'.drop(columns=["__ROW", "__M"]).reset_index(drop=True)',
        ]
        return L
    # aggregate / date_extreme / split / compute / transpose_long — run in the tool
    L += [f"# TODO (hand-code): prep step '{op}' has no standalone translation yet.",
          f"#   its parameters: {json.dumps(p)[:400]}",
          f'{name} = pd.DataFrame()']
    return L


def python_program(domain: str, blocks: list[Block], base_dataset: str,
                   prep_step: dict | None, pipeline: list[dict],
                   sort_by: list[str], dedup: dict, codelists: dict,
                   raw_path: str, studyid: str = "", version: str = "",
                   observed: dict[str, list[str]] | None = None) -> str:
    """A standalone pandas program reproducing this domain's build. `observed` maps a
    variable to the distinct raw values its source column holds — when given, each
    inlined codelist is scoped to those values (5 entries, not 100)."""
    dom = upper(domain)
    steps = list(pipeline or [])
    if prep_step and not steps:                     # the automatic stack / transpose
        steps = [prep_step]
    prep_outputs = {norm_key(st.get("name")) for st in steps if st.get("name")}
    g = _Py(dom, base_dataset, codelists, observed)

    live = [b for b in blocks if not b.supp and b.mtype not in ("drop", "unmapped")]
    seq_blocks = [b for b in live if b.mtype == "sequence"]
    late = [b for b in live if b.recipe in ("lobxfl", "age")]
    main = [b for b in live if b not in seq_blocks and b not in late]

    used: set[str] = {norm_key(base_dataset)}
    for b in live:
        used |= _block_datasets(b)
    for st in steps:
        used |= _step_datasets(st)
    raw_used = sorted(d for d in used if d and d not in prep_outputs)

    needs_dm_ref = dom != "DM" and any(b.recipe in ("study_day", "lobxfl") for b in live)

    L: list[str] = []
    L.append('#!/usr/bin/env python3')
    L.append(f'"""{dom} — generated by SDTM Oversight{(" " + version) if version else ""} '
             f'from the current build.')
    L.append('')
    L.append('Reproduces the build outside the tool: raw loads -> data preparation -> one')
    L.append('variable at a time in spec order -> sort / de-duplicate -> --SEQ numbering.')
    L.append('Anything the tool ran that has no standalone translation is a clearly-marked')
    L.append('TODO carrying the spec rule — never a silent gap.')
    L.append('')
    L.append('Needs: pandas' + (', pyreadstat (for .sas7bdat raw files)' if raw_used else '') + '.')
    if needs_dm_ref:
        L.append(f'Run the generated DM program first — {dom} reads reference dates from DM.csv.')
    L.append('"""')
    L.append('import pandas as pd')
    L.append('from pathlib import Path')
    L.append('')
    L.append(f'RAW = Path(r"{raw_path}")        # the study raw data folder')
    L.append(f'SUBJECT_KEYS = {list(SUBJECT_KEYS)!r}')
    L.append('')
    L.append(_PY_HELPERS)
    L.append('')
    L.append('# ── raw datasets this domain reads ──────────────────────────────────────────')
    for ds in raw_used:
        L.append(f'{g.frame(ds)} = read_raw("{ds}")')
    if needs_dm_ref:
        L.append('')
        L.append('# reference dates per subject, from the already-built DM (run DM first)')
        L.append('_dm = pd.read_csv("DM.csv", dtype=str)')
        L.append('DM_REF = {c: pd.Series(_dm[c].values, index=_dm["USUBJID"].values)')
        L.append('          for c in _dm.columns if c.startswith("RF")}')
    if steps:
        L.append('')
        L.append('# ── prepare the data ────────────────────────────────────────────────────────')
        for st in steps:
            L += _py_prep_lines(g, st)
            out_name = norm_key(st.get("name") or "prep")
            L.append(f'{g.frame(out_name)} = {out_name}   # readable as a source below')
    L.append('')
    L.append('# ── the domain frame, one record per base-dataset record ────────────────────')
    L.append(f'base = {g.frame(norm_key(base_dataset))}')
    L.append('df = pd.DataFrame(index=base.index)')
    L.append('')
    L.append('# ── variables, in spec order ────────────────────────────────────────────────')
    for b in main + late:
        head = f'# {b.variable} — {b.label}' if s(b.label) else f'# {b.variable}'
        if b.edited:
            head += '   [hand edit — deviates from the spec]'
        L.append(head)
        L += _py_block_lines(g, b)
        L.append('')
    L.append('# ── finalize: sort, de-duplicate, then number --SEQ on the final order ──────')
    srt = [upper(x) for x in (sort_by or [])]
    if srt:
        L.append(f'df = df.sort_values(by=[c for c in {srt!r} if c in df.columns], '
                 'kind="stable", na_position="last")')
    dd = dedup or {}
    if dd.get("enabled") and dd.get("keys"):
        keys = [upper(k) for k in dd["keys"]]
        L.append(f'df = df.drop_duplicates(subset=[c for c in {keys!r} if c in df.columns], '
                 f'keep="{dd.get("keep") or "first"}")')
    for b in seq_blocks:
        L += _py_block_lines(g, b)
    order = [b.variable for b in blocks
             if not b.supp and b.mtype != "drop" and b.status in ("built", "empty", "not_built", "error")]
    order = list(dict.fromkeys(order))
    L.append('')
    L.append('# every spec variable, in spec order; an unpopulated one is an empty column')
    L.append(f'for v in {order!r}:')
    L.append('    if v not in df.columns:')
    L.append('        df[v] = pd.Series(pd.NA, index=df.index, dtype="string")')
    L.append(f'df = df[{order!r}].reset_index(drop=True)')
    L.append('')
    L.append(f'df.to_csv("{dom}.csv", index=False)')
    L.append(f'print(f"{dom}: {{len(df)}} record(s), {{len(df.columns)}} variable(s) -> {dom}.csv")')
    if g.todos:
        L.append('')
        L.append(f'# {len(g.todos)} variable(s) above are marked TODO and need a hand-written')
        L.append(f'# derivation: {", ".join(dict.fromkeys(g.todos))}')
    return "\n".join(L) + "\n"


# ═════════════════════════════════════════════════════════════════════════════
# SAS
# ═════════════════════════════════════════════════════════════════════════════
def _sas_quote(v: str) -> str:
    return '"' + s(v).replace('"', '""') + '"'


def _sas_name(prefix: str, *parts: str) -> str:
    """A deterministic SAS-safe helper name (<=32 chars)."""
    body = "_".join(norm_key(p) for p in parts if p)
    return (prefix + body)[:32].upper()


class _Sas:
    """Emitter state for one SAS program.

    The build engine aligns a column from another dataset to the base records with a
    per-subject lookup. SAS gets the same result deterministically: every cross-dataset
    column is pre-merged onto the base BY the subject key the two datasets share, under
    a __-prefixed name, and the DATA-step statements then read plain columns."""

    def __init__(self, domain: str, base: str, codelists: dict,
                 source_columns: dict[str, list[str]] | None, studyid: str = ""):
        self.dom = upper(domain)
        self.base = norm_key(base)
        self.codelists = codelists
        self.studyid = s(studyid)
        self.cols = {norm_key(k): {upper(c) for c in v}
                     for k, v in (source_columns or {}).items()}
        self.xmerges: dict[str, dict] = {}     # ds -> {"key": KEY, "cols": {COL: NAME}}
        self.dx: dict[str, dict] = {}          # variable -> date_extreme plan
        self.ref_vars: set[str] = set()        # DM reference vars needed (RFSTDTC, …)
        self.ref_doms: dict[str, dict] = {}    # built domain -> {VAR: NAME} (sdtm_ref)
        self.todos: list[str] = []
        self.uses_dates = False                # __ev/__rf scratch vars used

    def key_between(self, ds: str) -> str | None:
        a, b = self.cols.get(norm_key(ds)), self.cols.get(self.base)
        if not a or not b:
            return None
        return next((k for k in SUBJECT_KEYS if k in a and k in b), None)

    def xref(self, ds, col) -> str | None:
        """Register a cross-dataset column for the pre-merge; returns its merged name.
        The base's own columns come back unchanged. None -> cannot be aligned."""
        dsn, c = norm_key(ds), upper(col)
        if not dsn or not c:
            return None
        if dsn == self.base:
            return c
        key = self.key_between(dsn)
        if key is None:
            return None
        name = _sas_name("__", dsn[:10], c[:16])
        self.xmerges.setdefault(dsn, {"key": key, "cols": {}})["cols"][c] = name
        return name

    def src(self, spec: dict, selfvar: str) -> str | None:
        """SAS token for a source descriptor; None when it cannot be resolved."""
        spec = spec or {}
        kind = str(spec.get("kind") or "").lower()
        if kind == "self" and selfvar:
            return upper(selfvar)
        if kind == "step":
            n = int(float(spec.get("step") or 0))
            return f"__STEP{n}" if n > 0 else None   # saved by the pipeline emitter
        if kind == "var" or (not kind and spec.get("var")):
            return upper(spec.get("var"))
        if kind == "text" or (not kind and spec.get("text") is not None and not spec.get("column")):
            return _sas_quote(spec.get("text"))
        if spec.get("dataset") and spec.get("column"):
            return self.xref(spec.get("dataset"), spec.get("column"))
        if spec.get("column"):
            return upper(spec.get("column"))
        return None

    def todo(self, b: Block, why: str) -> list[str]:
        self.todos.append(upper(b.variable))
        out = [f"  /* TODO (hand-code): {upper(b.variable)} — {why}"]
        if s(b.mapping_rule) and b.mapping_rule.lower() != "nan":
            out.append(f"     spec rule: {b.mapping_rule[:180]}")
        out.append("  */")
        if s(b.sas_code) and b.sas_code.lower() != "nan":
            out.append(f"  /* the spec's own Implemented SAS Code for {upper(b.variable)}: */")
            out += [f"  {line}" for line in b.sas_code.splitlines()]
        else:
            out.append(f'  {upper(b.variable)} = "";')
        return out


def _sas_cond(g: _Sas, rule: dict, selfvar: str) -> str | None:
    op = str((rule or {}).get("op") or "eq").lower()
    tok = g.src((rule or {}).get("src"), selfvar)
    if tok is None:
        return None
    val = s((rule or {}).get("value"))
    strip = tok if tok.startswith('"') else f"strip({tok})"
    if op == "missing":
        return f"missing({tok})"
    if op == "notmissing":
        return f"not missing({tok})"
    if op in ("in", "notin"):
        items = ", ".join(_sas_quote(x.strip()) for x in val.split(",") if x.strip())
        return f"{strip} {'not in' if op == 'notin' else 'in'} ({items})"
    if op == "contains":
        return f"index({tok}, {_sas_quote(val)}) > 0"
    if op == "starts":
        return f"{strip} =: {_sas_quote(val)}"
    if op == "ends":
        return f"find(strip({tok}), {_sas_quote(val)}, -length(strip({tok}))) > 0"
    if op in ("gt", "lt", "ge", "le"):
        sym = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}[op]
        return f"input({tok}, ?? best32.) {sym} {float(val or 0)}"
    if op == "between":
        return (f"{float(val or 0)} <= input({tok}, ?? best32.) <= "
                f"{float(s((rule or {}).get('value2')) or 0)}")
    if op == "ne":
        return f"{strip} ne {_sas_quote(val)}"
    return f"{strip} = {_sas_quote(val)}"


def _sas_cond_all(g: _Sas, rule: dict, selfvar: str) -> str | None:
    parts = [_sas_cond(g, rule, selfvar)]
    parts += [_sas_cond(g, extra, selfvar) for extra in (rule or {}).get("and") or []]
    if any(p is None for p in parts):
        return None
    return parts[0] if len(parts) == 1 else " and ".join(f"({p})" for p in parts)


def _sas_ct_lines(v: str, codelist: str, codelists: dict,
                  observed_vals: set | None = None,
                  overrides: dict | None = None) -> list[str]:
    cmap = dict(codelists.get(upper(codelist)) or {})
    for k, val in (overrides or {}).items():
        cmap[upper(k)] = val
    total = len(cmap)
    if observed_vals:                      # only the terms this study's data holds
        cmap = {k: val for k, val in cmap.items() if k in observed_vals}
    if not cmap or len(cmap) > CT_INLINE_CAP:
        return ([f"  /* codelist {upper(codelist)}: none of its terms occur in the data "
                 "— values kept as collected */"] if observed_vals and total else [])
    bysub: dict[str, list[str]] = {}
    for term, sub in cmap.items():
        bysub.setdefault(str(sub), []).append(str(term))
    q = lambda x: "'" + x.replace("'", "''") + "'"   # noqa: E731
    scope = (f"   /* {len(cmap)} of {total} terms — only the values observed in the data */"
             if observed_vals and len(cmap) < total else "")
    out = [f"  select (upcase(strip({v})));{scope}"]
    for sub, terms in bysub.items():
        out.append(f"    when ({', '.join(q(t) for t in sorted(set(terms)))}) {v} = {q(sub)};")
    out += ["    otherwise;                     /* not in CT — keep as collected */", "  end;"]
    return out


def _sas_fn_stmt(g: _Sas, v: str, a: dict, selfvar: str) -> str | None:
    fn = str(a.get("fn") or "").lower()
    toks = [g.src(x, selfvar) for x in (a.get("sources") or [])]
    if any(t is None for t in toks):
        return None
    one = toks[0] if toks else '""'
    simple = {"strip": "strip", "trim": "trimn", "left": "left", "upcase": "upcase",
              "lowcase": "lowcase", "propcase": "propcase", "compbl": "compbl",
              "reverse": "reverse", "length": "length"}
    if fn in simple:
        return f"  {v} = {simple[fn]}({one});"
    if fn == "substr":
        start = int(float(a.get("start") or 1))
        ln = s(a.get("len"))
        return (f"  {v} = substr({one}, {start});" if ln in ("", "0")
                else f"  {v} = substr({one}, {start}, {int(float(ln))});")
    if fn == "scan":
        return f"  {v} = scan({one}, {int(float(a.get('word') or 1))}, {_sas_quote(s(a.get('delim')) or ' ')});"
    if fn == "catx":
        return f"  {v} = catx({_sas_quote(s(a.get('sep')))}, {', '.join(toks)});"
    if fn in ("cats", "cat"):
        return f"  {v} = {fn}({', '.join(toks)});"
    if fn == "coalesce":
        return f"  {v} = coalescec({', '.join(toks)});"
    if fn == "tranwrd":
        return f"  {v} = tranwrd({one}, {_sas_quote(a.get('find'))}, {_sas_quote(a.get('replace'))});"
    if fn == "compress":
        chars = s(a.get("chars"))
        return f"  {v} = compress({one}{', ' + _sas_quote(chars) if chars else ''});"
    if fn == "zeropad":
        return f"  {v} = put(input({one}, ?? best32.), z{int(float(a.get('width') or 0))}.);"
    if fn == "put":
        return f"  {v} = strip(put(input({one}, ?? best32.), best32.));"
    if fn == "input":
        return f"  {v} = input({one}, ?? best32.);"
    if fn == "index":
        return f"  {v} = index({one}, {_sas_quote(a.get('find'))});"
    return None


def _sas_stmts(g: _Sas, b: Block, observed_vals: set | None,
               var: str | None = None) -> list[str]:
    """DATA-step statement(s) for one block (or one pipeline sub-block)."""
    v = upper(var or b.variable)
    a = b.args or {}

    def with_ct(lines: list[str], codelist: str, overrides=None) -> list[str]:
        if not codelist:
            return lines
        ct = _sas_ct_lines(v, codelist, g.codelists, observed_vals, overrides)
        return lines + (ct or [f"  /* codelist {upper(codelist)} too large to inline */"])

    if b.mtype == "constant":
        return [f"  {v} = {_sas_quote(b.value)};"]
    if b.mtype == "assign":
        tok = g.xref(b.dataset, b.column)
        if tok is None:
            return g.todo(b, f"{b.dataset}.{b.column} shares no subject key with the base "
                             f"'{g.base}' — align it by hand")
        return with_ct([f"  {v} = strip({tok});"], b.codelist,
                       (b.args or {}).get("ct_overrides"))

    rc = b.recipe
    if rc == "custom_fn":                # the user's library function — its steps were
        rc = "pipeline"                  # resolved into args at build time
    if rc == "ct":
        tok = g.src((a.get("sources") or [a])[0], v)
        cl = upper(a.get("codelist")) or upper(b.codelist)
        if tok is None:
            return g.todo(b, f"assign_ct input could not be aligned; codelist {cl}")
        return with_ct([f"  {v} = strip({tok});"], cl, a.get("ct_overrides"))
    if rc == "fn":
        stmt = _sas_fn_stmt(g, v, a, v)
        return [stmt] if stmt else g.todo(b, f"function '{a.get('fn')}' has no SAS translation here")
    if rc == "cond":
        rules = a.get("rules") or []
        if not rules:
            return g.todo(b, "empty if/then rule set")
        conds = [_sas_cond_all(g, r, v) for r in rules]
        thens = [g.src(r.get("then"), v) if (r.get("then") or {}).get("kind") != "missing"
                 else '""' for r in rules]
        els = a.get("else") or {"kind": "missing"}
        etok = '""' if els.get("kind") == "missing" else g.src(els, v)
        if any(c is None for c in conds) or any(t is None for t in thens) or etok is None:
            return g.todo(b, "an if/then input could not be aligned to the base records")
        out = []
        for i, r in enumerate(rules):
            kw = "if" if i == 0 else "else if"
            out.append(f"  {kw} {conds[i]} then {v} = {thens[i]};")
        out.append(f"  else {v} = {etok};")
        return out
    if rc == "iso_date":
        ds = s(a.get("dataset")) or b.dataset
        y, m, d = upper(a.get("y_col")), upper(a.get("m_col")), upper(a.get("d_col"))
        t = upper(a.get("time_col"))
        date_col = upper(a.get("date_col"))
        if y:
            ty = g.xref(ds, y)
            tm = g.xref(ds, m) if m else ""
            td = g.xref(ds, d) if d else ""
            if ty is None or (m and tm is None) or (d and td is None):
                return g.todo(b, "the split date parts could not be aligned to the base")
            expr = f"put(input({ty}, ?? best32.), z4.)"
            if tm:
                expr += f", put(input({tm}, ?? best32.), z2.)"
            if td:
                expr += f", put(input({td}, ?? best32.), z2.)"
            lines = ["  /* partial-aware ISO 8601 from split parts */",
                     f"  {v} = catx('-', {expr});"]
            if t:
                tt = g.xref(ds, t)
                if tt:
                    lines.append(f"  if length({v}) >= 10 and not missing({tt}) "
                                 f"then {v} = catx('T', {v}, strip({tt}));")
            return lines
        if not date_col:
            return g.todo(b, "no raw date column resolved for this --DTC")
        tok = g.xref(ds, date_col)
        if tok is None:
            return g.todo(b, f"{ds}.{date_col} shares no subject key with the base")
        return [f"  {v} = put(input(strip({tok}), ?? anydtdte32.), yymmdd10.);"
                f"   /* partials: keep as collected */"]
    if rc == "study_day":
        dtc = upper(a.get("dtc_var"))
        ref = upper(a.get("ref_var")) or "RFSTDTC"
        if not dtc:
            return g.todo(b, "event --DTC unresolved")
        g.uses_dates = True
        if g.dom != "DM":
            g.ref_vars.add(ref)
            ref_tok = _sas_name("__DMREF_", ref)
        else:
            ref_tok = ref                      # built earlier in this same domain
        return [
            f"  /* {v}: {dtc} minus DM.{ref}, +1 on/after, no day 0 */",
            f"  __ev = input(substr(strip({dtc}), 1, 10), ?? yymmdd10.);",
            f"  __rf = input(substr(strip({ref_tok}), 1, 10), ?? yymmdd10.);",
            f"  if n(__ev, __rf) = 2 then {v} = __ev - __rf + (__ev >= __rf);",
        ]
    if rc == "sdtm_ref":
        sdom, svar = upper(a.get("source_domain")), upper(a.get("source_var"))
        if not (sdom and svar):
            return g.todo(b, "the source domain/variable is unresolved")
        name = _sas_name("__", sdom, svar)
        g.ref_doms.setdefault(sdom, {})[svar] = name
        return with_ct([f"  {v} = strip({name});   /* from the built {sdom} — run its "
                        "program first */"], b.codelist)
    if rc == "studyid":
        if g.studyid:
            return [f"  {v} = {_sas_quote(g.studyid)};   /* the study identifier override */"]
        return g.todo(b, "STUDYID resolves from the raw data at run time — pass --studyid "
                         "or hand-code the constant")
    if rc == "age":
        birth = upper(a.get("birth_var")) or "BRTHDTC"
        ref = upper(a.get("ref_var")) or "RFSTDTC"
        g.uses_dates = True
        lines = [f"  /* {v}: the reported age where collected, otherwise whole years from "
                 f"{birth} to {ref} on the anniversary rule (never a fraction). The tool "
                 "additionally imputes partial birth dates — full dates match exactly. */"]
        age_col = upper(a.get("age_col"))
        tok = None
        if age_col:
            tok = (g.xref(a.get("age_dataset"), age_col) if s(a.get("age_dataset"))
                   else g.xref(g.base, age_col))
        lines.append(f"  {v} = input(strip(vvalue({tok})), ?? best32.);" if tok
                     else f"  {v} = .;")
        lines += [
            f"  __ev = input(substr(strip({birth}), 1, 10), ?? yymmdd10.);",
            f"  __rf = input(substr(strip({ref}), 1, 10), ?? yymmdd10.);",
            f"  if missing({v}) and n(__ev, __rf) = 2 then",
            f"    {v} = year(__rf) - year(__ev) - (month(__rf) < month(__ev) or",
            f"          (month(__rf) = month(__ev) and day(__rf) < day(__ev)));",
        ]
        return lines
    if rc == "copy_var":
        srcv = upper(a.get("source_var") or a.get("var"))
        return with_ct([f"  {v} = {srcv};"], b.codelist)
    if rc == "concat":
        cols = [c for c in (a.get("columns") or []) if c]
        ds = s(a.get("dataset")) or b.dataset
        if not cols:
            return g.todo(b, "no columns picked to concatenate")
        toks = [g.xref(ds, upper(c)) for c in cols]
        if any(t is None for t in toks):
            return g.todo(b, "a concatenation input could not be aligned to the base")
        return [f"  {v} = catx({_sas_quote(s(a.get('sep')) or ' ')}, {', '.join(toks)});"]
    if rc == "date_extreme":
        func = str(a.get("func", "min")).lower()
        srcs = []
        for x in a.get("sources") or []:
            ds, dc = s(x.get("dataset")), upper(x.get("date_col"))
            if not (ds and dc):
                continue
            key = g.key_between(ds)
            if key is None:
                continue
            srcs.append((norm_key(ds), dc, key))
        if not srcs:
            return g.todo(b, "none of the date sources share a subject key with the base")
        g.dx[v] = {"func": func, "sources": srcs}
        parts = f"of {_sas_name('__DN_', v)}_:"
        return [
            f"  /* {v}: the {'latest' if func == 'max' else 'earliest'} date per subject "
            f"across {', '.join(d for d, _c, _k in srcs)} (pre-merged above) */",
            f"  if n({parts}) then {v} = put({func}({parts}), yymmdd10.);",
            f"  else {v} = '';",
        ]
    if rc == "pipeline":
        steps = a.get("steps") or []
        if not steps:
            return g.todo(b, "pipeline has no steps")
        out: list[str] = []
        for i, st in enumerate(steps, start=1):
            op = str(st.get("op") or "fn").lower()
            sub = Block(variable=v, domain=b.domain, codelist=b.codelist)
            if op == "assign":
                sub.mtype, sub.dataset, sub.column = "assign", s(st.get("dataset")), s(st.get("column"))
                sub.codelist = ""              # a bare copy step never applies CT itself
            elif op == "constant":
                sub.mtype, sub.value = "constant", st.get("value", "")
            elif op in ("ct", "fn", "cond", "iso_date"):
                sub.mtype, sub.recipe, sub.args = "derived", op, (st.get("args") or {})
            else:
                return g.todo(b, f"pipeline step '{op}' has no SAS translation here")
            out.append(f"  /* step {i}: {op} */")
            stmts = _sas_stmts(g, sub, observed_vals, v)
            if stmts and stmts[0].lstrip().startswith("/* TODO"):
                return g.todo(b, f"pipeline step {i} ({op}) could not be translated")
            out += stmts
            out.append(f"  __STEP{i} = {v};   /* this step's value, for later steps */")
        return out
    return g.todo(b, f"recipe '{rc or b.mtype}' runs inside the tool")


def _sas_where(conds: list[dict]) -> str | None:
    """A WHERE clause for the prep filter's conditions (all must hold)."""
    parts = []
    for c in conds or []:
        col = upper((c or {}).get("column"))
        op = str((c or {}).get("operator") or "==").lower()
        val = s((c or {}).get("value"))
        base = f"upcase(strip({col}))"
        if op == "==":
            parts.append(f"{base} = {_sas_quote(val.upper())}")
        elif op == "!=":
            parts.append(f"{base} ne {_sas_quote(val.upper())}")
        elif op == "contains":
            parts.append(f"index({base}, {_sas_quote(val.upper())}) > 0")
        elif op == "startswith":
            parts.append(f"{base} =: {_sas_quote(val.upper())}")
        elif op == "endswith":
            parts.append(f"find({base}, {_sas_quote(val.upper())}, "
                         f"-length({base})) > 0")
        elif op in ("in", "notin"):
            items = ", ".join(_sas_quote(x.strip().upper()) for x in val.split(",") if x.strip())
            parts.append(f"{base} {'not in' if op == 'notin' else 'in'} ({items})")
        elif op == "between":
            lo, hi = (val.split(",") + ["", ""])[:2]
            parts.append(f"{float(lo or 0)} <= input({col}, ?? best32.) <= {float(hi or 0)}")
        else:
            return None
    return " and ".join(parts) if parts else None


def _sas_prep(step: dict) -> list[str]:
    """Program-level SAS for one prep step (a data/proc step producing its output)."""
    op = str(step.get("op") or "").lower()
    name = norm_key(step.get("name") or "prep")
    p = step.get("params") or {}

    if op == "stack":
        members = [norm_key(n) for n in p.get("datasets") or []]
        return [f"data {name};",
                f"  set {' '.join(members)} indsname=__src;",
                "  __SOURCE_DATASET = scan(__src, 2, '.');",
                "run;"]
    if op == "merge":
        on = [upper(x) for x in (p.get("on") or [])] or ["USUBJID"]
        ins = [norm_key(i.get("dataset")) for i in (p.get("inputs") or [])]
        L = [f"proc sort data={d}; by {' '.join(on)}; run;" for d in ins]
        return L + [f"data {name};", f"  merge {' '.join(ins)};",
                    f"  by {' '.join(on)};", "run;"]
    if op == "select":
        cols = " ".join(upper(c) for c in p.get("columns") or [])
        return [f"data {name}; set {norm_key(p.get('dataset'))}(keep={cols}); run;"]
    if op == "drop":
        cols = " ".join(upper(c) for c in p.get("columns") or [])
        return [f"data {name}; set {norm_key(p.get('dataset'))}(drop={cols}); run;"]
    if op == "rename":
        pairs = " ".join(f"{upper(r.get('from'))}={upper(r.get('to'))}"
                         for r in p.get("renames") or []
                         if s(r.get("from")) and s(r.get("to")))
        return [f"data {name}; set {norm_key(p.get('dataset'))}(rename=({pairs})); run;"]
    if op == "filter":
        conds = p.get("conds") or [{"column": p.get("column"),
                                    "operator": p.get("operator", "=="),
                                    "value": p.get("value", "")}]
        clause = _sas_where(conds)
        if clause is None:
            return [f"/* TODO (hand-code): filter -> {name} — an operator has no SAS "
                    f"translation. Parameters: {json.dumps(p)[:300]} */"]
        return [f"data {name}; set {norm_key(p.get('dataset'))}; where {clause}; run;"]
    if op == "sort":
        cols = [upper(c) for c in p.get("columns") or []]
        dirs = [str(x).lower() for x in (p.get("directions") or [])]
        by = " ".join(("descending " if (dirs[i:i + 1] or ["asc"])[0] == "desc" else "") + c
                      for i, c in enumerate(cols))
        return [f"proc sort data={norm_key(p.get('dataset'))} out={name}; by {by}; run;"]
    if op == "dedup":
        keys = [upper(c) for c in p.get("keys") or []]
        keep = "last" if str(p.get("keep", "first")).lower() == "last" else "first"
        return [f"proc sort data={norm_key(p.get('dataset'))} out={name}; by {' '.join(keys)}; run;",
                f"data {name}; set {name}; by {' '.join(keys)};",
                f"  if {keep}.{keys[-1]};", "run;"]
    if op == "transpose_findings":
        src = norm_key(p.get("dataset"))
        idv = " ".join(upper(c) for c in p.get("id_vars") or [])
        tc, tn = upper(p.get("testcd_col")), upper(p.get("test_col"))
        orres, orresu = upper(p.get("orres_col")), upper(p.get("orresu_col"))
        L = [f"data {name};", f"  set {src};",
             f"  length {tc} $40 {tn} $200 {orres} $200 {orresu} $40;",
             f"  keep {idv} {tc} {tn} {orres} {orresu};"]
        for m in p.get("measures") or []:
            vcol, ucol = upper(m.get("value_col")), upper(m.get("unit_col"))
            if not vcol:
                continue
            L += [f"  {tc} = {_sas_quote(upper(m.get('testcd')))}; "
                  f"{tn} = {_sas_quote(s(m.get('test')) or upper(m.get('testcd')))};",
                  f"  {orres} = strip(vvalue({vcol})); "
                  + (f"{orresu} = strip(vvalue({ucol}));" if ucol else f"{orresu} = '';"),
                  f"  if not missing({orres}) then output;   "
                  "/* a measurement not taken is not a record */"]
        return L + ["run;"]
    # aggregate / split / compute / transpose_long — run in the tool
    return [f"/* TODO (hand-code): prep step '{op}' -> {name}",
            f"   parameters: {json.dumps(p)[:300]} */"]


def sas_program(domain: str, blocks: list[Block], base_dataset: str,
                prep_step: dict | None, pipeline: list[dict],
                sort_by: list[str], dedup: dict, codelists: dict,
                raw_path: str, studyid: str = "", version: str = "",
                observed: dict[str, list[str]] | None = None,
                source_columns: dict[str, list[str]] | None = None) -> str:
    """A house-style SAS program reproducing this domain's build, deterministically:
    cross-dataset inputs are pre-merged onto the base by the subject key (the same
    one-value-per-subject alignment the build performs), so the DATA step reads plain
    columns. Raw data is expected as CSV exports; the few recipes with no SAS
    translation are clearly-marked TODOs carrying the spec's own rule."""
    dom = upper(domain)
    steps = list(pipeline or [])
    if prep_step and not steps:
        steps = [prep_step]
    prep_outputs = {norm_key(st.get("name")) for st in steps if st.get("name")}

    live = [b for b in blocks if not b.supp and b.mtype not in ("drop", "unmapped")]
    supp_vars = [upper(b.variable) for b in blocks if b.supp]
    seq_blocks = [b for b in live if b.mtype == "sequence"]
    pass2_recipes = {"sdtm_ref"} | ({"study_day"} if dom != "DM" else set())
    pass2 = [b for b in live if b.recipe in pass2_recipes]
    main = [b for b in live if b not in seq_blocks and b not in pass2
            and b.recipe not in ("lobxfl", "age")]
    late_todo = [b for b in live if b.recipe in ("lobxfl", "age")]

    used: set[str] = {norm_key(base_dataset)}
    for b in live:
        used |= _block_datasets(b)
    for st in steps:
        used |= _step_datasets(st)
    raw_used = sorted(d for d in used if d and d not in prep_outputs)
    base = norm_key(base_dataset)
    g = _Sas(dom, base, codelists, source_columns, studyid)

    # generate every statement FIRST — this registers the cross-dataset merges,
    # the date_extreme pre-tables and the built-domain references the header needs
    def stmts_for(bs: list[Block]) -> list[str]:
        out = []
        for b in bs:
            label = f" — {b.label}" if s(b.label) else ""
            edited = "   [hand edit]" if b.edited else ""
            out.append(f"  /* {b.variable}{label}{edited} */")
            obs = {upper(x) for x in (observed or {}).get(b.variable, [])} or None
            out += _sas_stmts(g, b, obs)
        return out
    main_stmts = stmts_for(main + late_todo)
    pass2_stmts = stmts_for(pass2)

    L: list[str] = []
    L.append(f"/* {dom} — generated by SDTM Oversight{(' ' + version) if version else ''} "
             f"from the current build.")
    L.append("   Raw data: CSV exports of the study raw folder (one file per dataset).")
    L.append("   Columns read from OTHER datasets are pre-merged onto the base records by")
    L.append("   the shared subject key — the same one-value-per-subject alignment the")
    L.append("   build performs. Anything with no SAS translation is a clearly-marked")
    L.append("   TODO carrying the spec's own rule — never a silent gap. */")
    L.append("")
    L.append(f'%let rawdir = {raw_path};      /* point at the CSV exports */')
    L.append("")
    L.append("%macro fetch(ds);")
    L.append('  proc import datafile="&rawdir./&ds..csv" out=&ds dbms=csv replace;')
    L.append("    guessingrows=max;")
    L.append("  run;")
    L.append("%mend;")
    for ds in raw_used:
        L.append(f"%fetch({ds});")
    L.append("")
    if steps:
        L.append("/* ── prepare the data ─────────────────────────────────────────────── */")
        for st in steps:
            L += _sas_prep(st)
        L.append("")

    src_name = base
    if g.xmerges or g.dx:
        src_name = "__base"
        L.append("/* ── cross-dataset inputs: one value per subject, merged onto the base ── */")
        L.append(f"data __base; set {base}; run;")
        for ds in sorted(g.xmerges):
            m = g.xmerges[ds]
            key, cols = m["key"], m["cols"]
            renames = " ".join(f"{c}={n}" for c, n in sorted(cols.items()))
            keep = " ".join([key] + sorted(cols))
            L.append(f"proc sort data={ds}(keep={keep} rename=({renames})) "
                     f"out=__lk_{ds[:20]} nodupkey; by {key}; run;")
            L.append(f"proc sort data=__base; by {key}; run;")
            L.append(f"data __base; merge __base(in=__b) __lk_{ds[:20]}; by {key}; "
                     "if __b; run;")
        for var, plan in sorted(g.dx.items()):
            func = plan["func"]
            for i, (ds, dcol, key) in enumerate(plan["sources"], start=1):
                t = f"{_sas_name('__DN_', var)}_{i}"
                L.append("proc sql;")
                L.append(f"  create table __dxt_{i} as")
                L.append(f"  select {key}, {func}(input(substr(strip({dcol}), 1, 10), "
                         f"?? yymmdd10.)) as {t}")
                L.append(f"  from {ds} group by {key};")
                L.append("quit;")
                L.append(f"proc sort data=__base; by {key}; run;")
                L.append(f"data __base; merge __base(in=__b) __dxt_{i}; by {key}; "
                         "if __b; run;")
        L.append("")

    L.append(f"/* ── build {dom}: one statement per spec variable, in spec order ──── */")
    L.append(f"data {dom.lower()}_work;")
    L.append(f"  set {src_name};")
    if g.uses_dates or g.dx:
        L.append("  length __ev __rf 8;")
    L += main_stmts
    if g.uses_dates or g.dx:
        L.append("  drop __ev __rf;")
    L.append("run;")
    L.append("")
    srt = [upper(x) for x in (sort_by or [])] or ["USUBJID"]
    L.append(f"proc sort data={dom.lower()}_work; by {' '.join(srt)}; run;")
    dd = dedup or {}
    if dd.get("enabled") and dd.get("keys"):
        keys = [upper(k) for k in dd["keys"]]
        keep = str(dd.get("keep") or "first").lower()
        L.append("")
        L.append(f"/* keep the {keep} record per {' '.join(keys)} */")
        L.append(f"proc sort data={dom.lower()}_work; by {' '.join(keys)}; run;")
        L.append(f"data {dom.lower()}_work; set {dom.lower()}_work; by {' '.join(keys)};")
        L.append(f"  if {keep}.{keys[-1]};")
        L.append("run;")

    if pass2:
        L.append("")
        L.append("/* ── values from already-built domains (run their programs first) ─── */")
        needed: dict[str, list[str]] = {}
        if g.ref_vars:
            needed["DM"] = sorted(g.ref_vars)
        for sdom, mapping in sorted(g.ref_doms.items()):
            needed.setdefault(upper(sdom), [])
            needed[upper(sdom)] += sorted(mapping)
        for sdom, vars_ in sorted(needed.items()):
            renames = " ".join(
                (f"{rv}={_sas_name('__DMREF_', rv)}" if sdom == "DM" and rv in g.ref_vars
                 else f"{rv}={g.ref_doms.get(sdom, {}).get(rv, rv)}")
                for rv in dict.fromkeys(vars_))
            L.append(f'proc import datafile="{sdom}.csv" out=__blt_{sdom.lower()} '
                     "dbms=csv replace; guessingrows=max; run;")
            L.append(f"proc sort data=__blt_{sdom.lower()}"
                     f"(keep=USUBJID {' '.join(dict.fromkeys(vars_))} rename=({renames})) "
                     f"out=__ref_{sdom.lower()} nodupkey; by USUBJID; run;")
        L.append(f"proc sort data={dom.lower()}_work; by USUBJID; run;")
        L.append(f"data {dom.lower()}_work;")
        L.append(f"  merge {dom.lower()}_work(in=__b) "
                 + " ".join(f"__ref_{sd.lower()}" for sd in sorted(needed)) + ";")
        L.append("  by USUBJID; if __b;")
        if any(b.recipe == "study_day" for b in pass2):
            L.append("  length __ev __rf 8;")
        L += pass2_stmts
        L.append("  drop __:;   /* every merged helper and scratch variable */")
        L.append("run;")
        L.append(f"proc sort data={dom.lower()}_work; by {' '.join(srt)}; run;")

    L.append("")
    L.append(f"data {dom};")
    L.append(f"  set {dom.lower()}_work;")
    if seq_blocks:
        grp = upper((seq_blocks[0].args or {}).get("group") or "USUBJID")
        seqv = upper(seq_blocks[0].variable)
        L.append(f"  by {grp};")
        L.append(f"  retain {seqv};")
        L.append(f"  if first.{grp} then {seqv} = 0;")
        L.append(f"  {seqv} + 1;")
    drops = ["__:"] + supp_vars
    L.append(f"  drop {' '.join(dict.fromkeys(drops))};" if supp_vars
             else "  drop __:;")
    L.append("run;")
    if g.todos:
        L.append("")
        L.append(f"/* {len(g.todos)} variable(s) above are marked TODO and need a hand-written")
        L.append(f"   derivation: {', '.join(dict.fromkeys(g.todos))} */")
    return "\n".join(L) + "\n"
