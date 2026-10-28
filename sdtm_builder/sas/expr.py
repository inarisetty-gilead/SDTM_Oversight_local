"""SAS expressions — parsed (never eval'd) into a small AST that every part of the build shares.

A spec row's Implemented SAS Code (SUBJID = scan(usubjid,-2,'-')||'-'||scan(usubjid,-1,'-'),
AGE = floor(yrdif(input(brthdtc, yymmdd10.), input(rfstdtc, yymmdd10.), 'AGE'))) and an
expression a reader types into a recipe or a prep step are the same thing: a SAS expression. One
parse feeds three outputs — canonical SAS (to_sas) for the SAS program, pandas source (to_py,
over the runtime helpers) for the Python program, and direct evaluation (evaluate) for the
engine — so all of them compute the value SAS would.

The grammar is the SAS DATA-step expression, with SAS's operator precedence:

    or      :=  and  (('or' | '|') and)*
    and     :=  cmp  (('and' | '&') cmp)*
    cmp     :=  cat  ((CMP cat) | (['not'] 'in' '(' const (',' const)* ')'))*
    cat     :=  add  ('||' add)*                    CMP: = ^= ~= < <= > >= =: eq ne lt le gt ge
    add     :=  mul  (('+' | '-') mul)*
    mul     :=  unary (('*' | '/') unary)*
    unary   :=  ('-' | '+' | 'not' | '^' | '~') unary | power
    power   :=  atom ('**' unary)?
    atom    :=  STRING | DATE-LITERAL | NUMBER | '.' | NAME | FUNC '(' [args] ')' | '(' or ')'

FUNC is any function in sas_runtime._SX_F; its argument count is checked, and an argument
that takes a format (PUT/INPUT) reads a format token like yymmdd10. or $20. or 8.2, which must be
one the runtime implements. Arguments may be omitted (compress(x, , 'kd')). Anything else raises
SasExprError, so an expression qualifies only when the whole of it is understood.
"""
from __future__ import annotations

import re

from . import runtime as _rt
from .runtime import HELPERS  # noqa: F401 — re-exported for the exec engines

FUNCS = {k: (v[3], v[4]) for k, v in _rt._SX_F.items()}     # name -> (min args, max args | None)
_CMP_WORDS = {"eq": "=", "ne": "^=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<cat>\|\||!!)"
    r"|(?P<dlit>'(?:[^']|'')*'(?:dt|d|t)(?![A-Za-z0-9_])|\"(?:[^\"]|\"\")*\"(?:dt|d|t)(?![A-Za-z0-9_]))"
    r"|(?P<str>'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")"
    r"|(?P<fmt>\$[A-Za-z0-9_]*\.\d*|[A-Za-z_][A-Za-z0-9_]*\.\d*(?![A-Za-z_]))"
    r"|(?P<num>\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)"
    r"|(?P<miss>\.(?![A-Za-z0-9_]))"
    r"|(?P<op>\*\*|<=|>=|\^=|~=|¬=|=:|\?\?|[-+*/=<>^~¬&|(),?])"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<other>\S))", re.I)
_OPS = {"~=": "^=", "¬=": "^=", "~": "not", "¬": "not", "^": "not", "&": "and", "|": "or"}


class SasExprError(ValueError):
    pass


def _tokens(src: str) -> list:
    s = str(src or "").strip().rstrip(";").strip()
    out, pos = [], 0
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise SasExprError(f"cannot read {s[pos:pos + 12]!r}")
        pos = m.end()
        k = m.lastgroup
        t = m.group(k)
        if k == "other":
            raise SasExprError(f"operator {t!r} is not supported")
        if k == "str":
            q = t[0]
            out.append(("str", t[1:-1].replace(q + q, q)))
        elif k == "dlit":
            q = t[0]
            end = t.rindex(q)
            out.append(("dlit", (t[end + 1:].lower(), t[1:end].replace(q + q, q))))
        elif k == "num":
            out.append(("num", (float(t) if re.search(r"[.eE]", t) else int(t), t)))
        elif k == "miss":
            out.append(("num", (None, ".")))
        elif k == "op":
            out.append((_OPS.get(t, t), t))
        elif k == "cat":
            out.append(("||", t))
        else:
            out.append((k, t))
    return out


def _date_literal(kind, text):
    """'01JAN2020'd / '10:30't / '01JAN2020:10:30:00'dt -> the SAS numeric value."""
    if kind == "d":
        v = _rt._sx_parse_date(text)
    elif kind == "t":
        v = _rt._sx_parse_time(text)
    else:
        d, _, t = text.partition(":")
        dd, tt = _rt._sx_parse_date(d), _rt._sx_parse_time(t or "0:00")
        v = None if dd is None or tt is None else dd * 86400 + tt
    if v is None:
        raise SasExprError(f"invalid {kind} literal {text!r}")
    return v


def parse(src: str):
    """SAS text -> AST. Nodes: ("str", s) ("num", n|None) ("var", NAME) ("call", f, [args])
    ("cat", [parts]) ("bin", op, l, r) ("un", op, x) ("in", x, [items], negated) ("fmt", text)
    ("dlit", kind, text, value) ("omit",)."""
    toks = _tokens(src)
    if not toks:
        raise SasExprError("empty expression")
    pos = [0]

    def peek(o=0):
        i = pos[0] + o
        return toks[i] if i < len(toks) else (None, None)

    def kind():
        k, v = peek()
        if k == "name" and v.lower() in ("and", "or", "not", "in", *_CMP_WORDS):
            return v.lower()
        return k

    def take(k):
        if kind() != k:
            raise SasExprError(f"expected {k!r}")
        pos[0] += 1
        return toks[pos[0] - 1][1]

    def fmt_arg(fn):
        k, v = peek()
        if k == "??":
            pos[0] += 1
            k, v = peek()
        if k == "fmt":
            pos[0] += 1
            text = v
        elif k == "num" and re.fullmatch(r"\d+\.\d*", v[1]):
            pos[0] += 1
            text = v[1]
        else:
            raise SasExprError(f"{fn}() needs a format like yymmdd10. or best32.")
        try:
            ok = _rt._sx_known(text, informat=(fn == "input"))
        except ValueError as e:
            raise SasExprError(str(e)) from None
        if not ok:
            raise SasExprError(f"{'informat' if fn == 'input' else 'format'} {text} is not supported")
        return ("fmt", text.upper())

    def call(name):
        f = name.lower()
        types = _rt._SX_F[f][1]
        take("(")
        args = []
        if kind() == ")":
            take(")")
        else:
            while True:
                t = types[min(len(args), len(types) - 1)] if types else "a"
                if kind() in (",", ")"):
                    args.append(("omit",))
                elif t == "f":
                    args.append(fmt_arg(f))
                else:
                    args.append(or_())
                if kind() == ",":
                    take(",")
                    continue
                take(")")
                break
        lo, hi = FUNCS[f]
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise SasExprError(f"{f}() takes {lo}{'' if hi == lo else '+' if hi is None else f'-{hi}'} argument(s)")
        return ("call", f, args)

    def atom():
        k = kind()
        if k == "str":
            return ("str", take("str"))
        if k == "dlit":
            kd, text = take("dlit")
            return ("dlit", kd, text, _date_literal(kd, text))
        if k == "num":
            return ("num", take("num")[0])
        if k == "(":
            take("(")
            e = or_()
            take(")")
            return e
        if k == "name":
            name = take("name")
            if kind() != "(":
                if name.lower() in FUNCS and FUNCS[name.lower()][0] > 0:
                    raise SasExprError(f"{name} needs arguments")
                return ("var", name.upper())
            if name.lower() not in FUNCS:
                raise SasExprError(f"function {name}() is not supported")
            return call(name)
        if k == "fmt":
            raise SasExprError(f"{peek()[1]!r} is a format; use it inside put() or input()")
        raise SasExprError("unexpected end" if k is None else f"unexpected {peek()[1]!r}")

    def power():
        a = atom()
        if kind() == "**":
            take("**")
            return ("bin", "**", a, unary())
        return a

    def unary():
        k = kind()
        if k in ("-", "+", "not"):
            pos[0] += 1
            x = unary()
            if k == "-" and x[0] == "num" and x[1] is not None:
                return ("num", -x[1])
            return x if k == "+" else ("un", k, x)
        return power()

    def mul():
        a = unary()
        while kind() in ("*", "/"):
            op = kind()
            pos[0] += 1
            a = ("bin", op, a, unary())
        return a

    def add():
        a = mul()
        while kind() in ("+", "-"):
            op = kind()
            pos[0] += 1
            a = ("bin", op, a, mul())
        return a

    def cat():
        parts = [add()]
        while kind() == "||":
            take("||")
            parts.append(add())
        return parts[0] if len(parts) == 1 else ("cat", parts)

    def const_item():
        k = kind()
        if k == "-" and peek(1)[0] == "num":
            pos[0] += 1
            return ("num", -take("num")[0])
        if k in ("str", "num"):
            return atom()
        raise SasExprError("IN lists hold constants only")

    def cmp():
        a = cat()
        while True:
            k = kind()
            if k in ("=", "^=", "<", "<=", ">", ">=", "=:") or k in _CMP_WORDS:
                pos[0] += 1
                a = ("bin", _CMP_WORDS.get(k, k), a, cat())
            elif k == "in" or (k == "not" and peek(1)[0] == "name" and str(peek(1)[1]).lower() == "in"):
                neg = k == "not"
                pos[0] += 2 if neg else 1
                take("(")
                items = [const_item()]
                while kind() == ",":
                    take(",")
                    items.append(const_item())
                take(")")
                a = ("in", a, items, neg)
            else:
                return a

    def and_():
        a = cmp()
        while kind() == "and":
            pos[0] += 1
            a = ("bin", "and", a, cmp())
        return a

    def or_():
        a = and_()
        while kind() == "or":
            pos[0] += 1
            a = ("bin", "or", a, and_())
        return a

    tree = or_()
    if pos[0] != len(toks):
        raise SasExprError(f"unexpected {toks[pos[0]][1]!r}")
    return tree


def _kids(n):
    k = n[0]
    if k == "call":
        return n[2]
    if k == "cat":
        return n[1]
    if k == "bin":
        return [n[2], n[3]]
    if k == "un":
        return [n[2]]
    if k == "in":
        return [n[1], *n[2]]
    return []


def names(tree) -> list:
    """Variable names the expression reads, upper-case, in first-use order."""
    out = []

    def walk(n):
        if n[0] == "var" and n[1] not in out:
            out.append(n[1])
        for c in _kids(n):
            walk(c)
    walk(tree)
    return out


def functions(tree) -> list:
    """SAS functions the expression calls, in first-use order."""
    out = []

    def walk(n):
        if n[0] == "call" and n[1] not in out:
            out.append(n[1])
        for c in _kids(n):
            walk(c)
    walk(tree)
    return out


def to_sas(tree, ref=None) -> str:
    """Canonical SAS for the parsed expression; `ref(NAME)` may stand in for a variable."""
    def go(n, top=False):
        k = n[0]
        if k == "str":
            return "'" + n[1].replace("'", "''") + "'"
        if k == "num":
            return "." if n[1] is None else (str(int(n[1])) if float(n[1]).is_integer() else repr(n[1]))
        if k == "dlit":
            return "'" + n[2].replace("'", "''") + "'" + n[1]
        if k == "var":
            return ref(n[1]) if ref else n[1]
        if k == "fmt":
            return n[1].lower()
        if k == "omit":
            return ""
        if k == "cat":
            s = " || ".join(go(p) for p in n[1])
            return s if top else f"({s})"
        if k == "bin":
            s = f"{go(n[2])} {n[1]} {go(n[3])}"
            return s if top else f"({s})"
        if k == "un":
            return f"not {go(n[2])}" if n[1] == "not" else f"-{go(n[2])}"
        if k == "in":
            s = f"{go(n[1])} {'not in' if n[3] else 'in'} ({', '.join(go(i) for i in n[2])})"
            return s if top else f"({s})"
        return f"{n[1]}({', '.join(go(a, True) for a in n[2])})"
    return go(tree, True)


def to_py(tree, ref) -> str:
    """pandas source for the expression; `ref(NAME)` gives the source for a variable's Series.
    The result is evaluated with `_ix` (the frame index) and the sas_runtime helpers in scope."""
    k = tree[0]
    if k in ("str", "num", "fmt"):
        return repr(tree[1])
    if k == "dlit":
        return repr(tree[3])
    if k == "omit":
        return "None"
    if k == "var":
        return ref(tree[1])
    if k == "cat":
        return f"_sx_cat(_ix, {', '.join(to_py(p, ref) for p in tree[1])})"
    if k == "bin":
        return f"_sx_op(_ix, {tree[1]!r}, {to_py(tree[2], ref)}, {to_py(tree[3], ref)})"
    if k == "un":
        return f"_sx_un(_ix, {tree[1]!r}, {to_py(tree[2], ref)})"
    if k == "in":
        return f"_sx_in(_ix, {to_py(tree[1], ref)}, [{', '.join(to_py(i, ref) for i in tree[2])}], {tree[3]!r})"
    args = "".join(", " + to_py(a, ref) for a in tree[2])
    return f"_sx_fn(_ix, {tree[1]!r}{args})"


def evaluate(tree, columns: dict, ix):
    """Evaluate the AST directly (no code generation) — for the sandboxed Auto-Build engine.
    `columns` maps NAME -> Series; a name it lacks reads as blank."""
    return _rt._sx_res(ix, evaluate_raw(tree, columns, ix))


def evaluate_raw(tree, columns: dict, ix):
    """The expression's value before it becomes a variable (a Series or a scalar)."""
    def ev(n):
        k = n[0]
        if k in ("str", "num", "fmt"):
            return n[1]
        if k == "dlit":
            return n[3]
        if k == "omit":
            return None
        if k == "var":
            v = columns.get(n[1])
            return "" if v is None else v
        if k == "cat":
            return _rt._sx_cat(ix, *[ev(p) for p in n[1]])
        if k == "bin":
            return _rt._sx_op(ix, n[1], ev(n[2]), ev(n[3]))
        if k == "un":
            return _rt._sx_un(ix, n[1], ev(n[2]))
        if k == "in":
            return _rt._sx_in(ix, ev(n[1]), [ev(i) for i in n[2]], n[3])
        return _rt._sx_fn(ix, n[1], *[ev(a) for a in n[2]])
    return ev(tree)


def own_domain_expr(sas: str, allowed: set, var: str):
    """(tree, names) when `sas` parses and reads at least one variable, all of them in `allowed`
    (the domain's other variables) — else None."""
    try:
        tree = parse(sas)
    except SasExprError:
        return None
    ns = names(tree)
    if not ns or str(var).upper() in ns or not set(ns) <= {str(a).upper() for a in allowed}:
        return None
    return tree, ns


# ── IF / THEN / ELSE statement chains ───────────────────────────────────────────────────────
_IF_RE = re.compile(r"^(else\s+)?if\s+(.+?)\s+then\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", re.I | re.S)
_ELSE_RE = re.compile(r"^else\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", re.I | re.S)
_SET_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", re.S)


def _statements(src: str) -> list:
    """Split SAS statements on ';' outside quotes."""
    out, cur, q = [], [], None
    for ch in str(src or ""):
        if q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif ch == ";":
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur).strip())
    return [x for x in out if x]


def parse_if_chain(src: str):
    """Implemented SAS made of assignments to ONE variable — `if C then V = E;`, `else if …`,
    `else V = E;`, `V = E;` — as (target, chains): each chain is {"if": [(cond, value)…],
    "else": value | None}, executed in order like the DATA step (a later chain overrides an earlier
    one; a chain without ELSE leaves unmatched rows as they were). Anything else (DO groups,
    subsetting IF, two target variables, an expression the grammar rejects) raises SasExprError."""
    chains, target = [], None

    def tgt(name):
        nonlocal target
        n = name.upper()
        if target is not None and n != target:
            raise SasExprError(f"assigns both {target} and {n}")
        target = n

    for st in _statements(src):
        if re.match(r"^(do|end)\b", st, re.I) or re.search(r"\bthen\s+do\b", st, re.I):
            raise SasExprError("DO groups are not supported")
        m = _IF_RE.match(st)
        if m:
            tgt(m.group(3))
            br = (parse(m.group(2)), parse(m.group(4)))
            if m.group(1):
                if not chains or chains[-1]["else"] is not None or not chains[-1]["if"]:
                    raise SasExprError("ELSE IF without an IF")
                chains[-1]["if"].append(br)
            else:
                chains.append({"if": [br], "else": None})
            continue
        m = _ELSE_RE.match(st)
        if m:
            tgt(m.group(1))
            if not chains or chains[-1]["else"] is not None or not chains[-1]["if"]:
                raise SasExprError("ELSE without an IF")
            chains[-1]["else"] = parse(m.group(2))
            continue
        m = _SET_RE.match(st)
        if m and not re.match(r"^(if|else)\b", st, re.I):
            tgt(m.group(1))
            chains.append({"if": [], "else": parse(m.group(2))})
            continue
        raise SasExprError(f"statement not understood: {st[:60]!r}")
    if target is None:
        raise SasExprError("no assignment")
    return target, chains


def chain_names(chains) -> list:
    out = []
    for c in chains:
        for t in [x for br in c["if"] for x in br] + ([c["else"]] if c["else"] is not None else []):
            out += [n for n in names(t) if n not in out]
    return out


def chains_to_json(chains) -> list:
    """JSON-able form (each part as canonical SAS) for a saved recipe."""
    return [{"if": [[to_sas(c), to_sas(v)] for c, v in ch["if"]],
             "else": None if ch["else"] is None else to_sas(ch["else"])} for ch in chains]


def chains_from_json(js) -> list:
    return [{"if": [(parse(c), parse(v)) for c, v in (ch.get("if") or [])],
             "else": None if ch.get("else") in (None, "") else parse(ch["else"])} for ch in js or []]


def chains_to_sas(chains, target, ref=None) -> list:
    """SAS statements for the chains, one per line."""
    out = []
    for ch in chains:
        for k, (c, v) in enumerate(ch["if"]):
            out.append(f"{'else if' if k else 'if'} {to_sas(c, ref)} then {target} = {to_sas(v, ref)};")
        if ch["else"] is not None:
            out.append(f"{'else ' if ch['if'] else ''}{target} = {to_sas(ch['else'], ref)};")
    return out


def chains_to_py(chains, ref) -> str:
    """pandas source: _sx_if(_ix, [([(cond, value), …], else | None), …])."""
    parts = []
    for ch in chains:
        brs = ", ".join(f"({to_py(c, ref)}, {to_py(v, ref)})" for c, v in ch["if"])
        parts.append(f"([{brs}], {'None' if ch['else'] is None else to_py(ch['else'], ref)})")
    return f"_sx_if(_ix, [{', '.join(parts)}])"


def evaluate_chains(chains, columns: dict, ix):
    def ev(t):
        return evaluate_raw(t, columns, ix)
    return _rt._sx_res(ix, _rt._sx_if(ix, [([(ev(c), ev(v)) for c, v in ch["if"]],
                                           None if ch["else"] is None else ev(ch["else"])) for ch in chains]))

