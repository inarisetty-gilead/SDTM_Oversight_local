"""Compile the spec's `Implemented SAS Code` into this engine's named operations.

A mapping spec carries a great deal of its detail here rather than in Input Variables:

    SITEID   =  scan(usubjid, -2, '-')
    SUBJID   =  scan(usubjid, -2, '-') || '-' || scan(usubjid, -1, '-')
    AETERM   =  strip(upcase(aeterm))

Ignoring that column loses those variables, or — worse — reduces them to a copy of whatever
Input Variables happened to name, which produces confident wrong values.

This module parses a deliberately RESTRICTED grammar: character functions, concatenation and
literals over identifiers. It is not a SAS interpreter and does not try to be. Anything
outside the grammar returns None, and the caller reports the variable as not built rather
than guessing — an unparsed expression must never become a silent approximation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .util import s, upper

# SAS functions with a direct, faithful equivalent in ops.op_fn
FUNCS = {
    "scan", "substr", "strip", "trim", "left", "compress", "upcase", "lowcase", "propcase",
    "reverse", "length", "index", "tranwrd", "catx", "cats", "cat", "coalesce", "compbl",
    "put", "input",
}

TOKEN = re.compile(r"""
    \s*(?:
      (?P<num>-?\d+(?:\.\d+)?)
    | (?P<str>'[^']*'|"[^"]*")
    | (?P<concat>\|\|)
    | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
    | (?P<punct>[(),])
    )
""", re.X)


@dataclass
class Node:
    kind: str                 # call | ident | text | number | concat
    value: str = ""
    args: list | None = None


def _tokenize(src: str) -> list[tuple[str, str]] | None:
    out, pos = [], 0
    src = src.strip().rstrip(";")
    while pos < len(src):
        m = TOKEN.match(src, pos)
        if not m or m.end() == pos:
            return None
        pos = m.end()
        for k in ("num", "str", "concat", "name", "punct"):
            v = m.group(k)
            if v is not None:
                out.append((k, v))
                break
    return out


class _Parser:
    def __init__(self, toks): self.t, self.i = toks, 0
    def peek(self): return self.t[self.i] if self.i < len(self.t) else ("end", "")
    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expr(self) -> Node | None:
        left = self.term()
        if left is None:
            return None
        parts = [left]
        while self.peek()[0] == "concat":
            self.take()
            nxt = self.term()
            if nxt is None:
                return None
            parts.append(nxt)
        return parts[0] if len(parts) == 1 else Node("concat", args=parts)

    def term(self) -> Node | None:
        kind, val = self.take()
        if kind == "str":
            return Node("text", val[1:-1])
        if kind == "num":
            return Node("number", val)
        if kind == "punct" and val == "(":
            inner = self.expr()
            if inner is None or self.take() != ("punct", ")"):
                return None
            return inner
        if kind == "name":
            if self.peek() == ("punct", "("):
                self.take()
                if val.lower() not in FUNCS:
                    return None                     # an unsupported function is a hard stop
                args = []
                if self.peek() != ("punct", ")"):
                    while True:
                        a = self.expr()
                        if a is None:
                            return None
                        args.append(a)
                        nxt = self.take()
                        if nxt == ("punct", ")"):
                            break
                        if nxt != ("punct", ","):
                            return None
                else:
                    self.take()
                return Node("call", val.lower(), args)
            return Node("ident", val)
        return None


def parse(sas: str) -> Node | None:
    """A restricted SAS expression -> an AST, or None when it is outside the grammar."""
    text = s(sas).replace("\n", " ").replace("\r", " ")
    if not text or "=" in text.split("(")[0] and text.count("=") and not text.strip().startswith("("):
        # tolerate a leading "VAR =" assignment
        head, _, tail = text.partition("=")
        if re.fullmatch(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*", head):
            text = tail
    toks = _tokenize(text)
    if not toks:
        return None
    p = _Parser(toks)
    node = p.expr()
    return node if node is not None and p.i == len(p.t) else None


class Unresolved(Exception):
    """An identifier in the expression is neither an SDTM variable nor a raw column."""


def compile_node(node: Node, resolve, steps: list) -> dict:
    """Compile one node, appending pipeline steps, and return a source descriptor for it."""
    if node.kind == "text":
        return {"kind": "text", "text": node.value}
    if node.kind == "number":
        return {"kind": "text", "text": node.value}
    if node.kind == "ident":
        return resolve(node.value)
    if node.kind == "concat":
        srcs = [compile_node(a, resolve, steps) for a in node.args]
        steps.append({"op": "fn", "args": {"fn": "cat", "sources": srcs}})
        return {"kind": "step", "step": len(steps)}
    if node.kind != "call":
        raise Unresolved(node.kind)

    fn, args = node.value, node.args or []

    def lit(i, default=""):
        return args[i].value if i < len(args) and args[i].kind in ("text", "number") else default

    if fn in ("catx", "cats", "cat", "coalesce"):
        rest = args[1:] if fn == "catx" else args
        srcs = [compile_node(a, resolve, steps) for a in rest]
        fargs = {"fn": fn, "sources": srcs}
        if fn == "catx":
            fargs["sep"] = lit(0)
        steps.append({"op": "fn", "args": fargs})
        return {"kind": "step", "step": len(steps)}

    if not args:
        raise Unresolved(fn)
    src = compile_node(args[0], resolve, steps)
    fargs: dict = {"fn": fn, "sources": [src]}
    if fn == "scan":
        fargs["word"] = lit(1, "1")
        fargs["delim"] = lit(2, " ")
    elif fn == "substr":
        fargs["start"] = lit(1, "1")
        fargs["len"] = lit(2, "")
    elif fn == "tranwrd":
        fargs["find"], fargs["replace"] = lit(1), lit(2)
    elif fn == "index":
        fargs["find"] = lit(1)
    elif fn == "compress":
        fargs["chars"] = lit(1)
    steps.append({"op": "fn", "args": fargs})
    return {"kind": "step", "step": len(steps)}


def to_block_args(sas: str, resolve) -> dict | None:
    """`Implemented SAS Code` -> {mtype, recipe, args} for the build engine, or None when the
    expression is outside the supported grammar.

    `resolve(name)` maps an identifier to a source descriptor, and raises Unresolved when the
    name is neither an SDTM variable in this domain nor a column in the referenced raw data.
    """
    node = parse(sas)
    if node is None:
        return None
    steps: list = []
    try:
        final = compile_node(node, resolve, steps)
    except Unresolved:
        return None
    if not steps:
        # a bare identifier or literal — a plain assign or constant, handled by the caller
        if node.kind == "ident":
            return {"mtype": "passthrough", "source": final}
        if node.kind in ("text", "number"):
            return {"mtype": "constant", "value": node.value}
        return None
    # the last step already writes the variable; the registers carry the intermediates
    if final.get("kind") == "step" and final["step"] != len(steps):
        steps.append({"op": "fn", "args": {"fn": "cat", "sources": [final]}})
    return {"mtype": "derived", "recipe": "pipeline", "args": {"steps": steps}}


# ── conditional statements: IF ... THEN x = ...; ELSE IF ...; ELSE x = ...; ────────────
# A second, separate grammar from the expression parser above: SAS recodes are usually
# written as IF-THEN-ELSE statements, not expressions, and need their own keyword-aware
# tokenizer (THEN, ELSE, AND, IN, MISSING, comparison operators) rather than the function/
# concatenation grammar `parse()` handles.

_COND_KEYWORDS = {"IF", "THEN", "ELSE", "AND", "OR", "NOT", "IN", "MISSING"}
_COMPARE_OPS = {"=": "eq", "eq": "eq", "^=": "ne", "~=": "ne", "<>": "ne", "ne": "ne",
                ">": "gt", "gt": "gt", "<": "lt", "lt": "lt",
                ">=": "ge", "ge": "ge", "<=": "le", "le": "le"}

_COND_TOKEN = re.compile(r"""
    \s*(?:
      (?P<num>-?\d+(?:\.\d+)?)
    | (?P<str>'[^']*'|"[^"]*")
    | (?P<op><=|>=|\^=|~=|<>|=|<|>)
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<punct>[(),;])
    )
""", re.X)


def looks_conditional(sas: str) -> bool:
    """True when the spec's SAS code is visibly an IF-THEN(-ELSE) statement — a shape a
    naive raw assign can never stand in for, unlike a plain character-function transform."""
    text = s(sas)
    return bool(re.match(r"(?i)^\s*if\b", text) and re.search(r"(?i)\bthen\b", text))


def _cond_tokenize(src: str) -> list[tuple[str, str]] | None:
    out, pos = [], 0
    src = src.strip()
    while pos < len(src):
        m = _COND_TOKEN.match(src, pos)
        if not m or m.end() == pos:
            return None
        pos = m.end()
        for k in ("num", "str", "op", "name", "punct"):
            v = m.group(k)
            if v is not None:
                out.append((k, v))
                break
    return out


class _CondParser:
    def __init__(self, toks: list[tuple[str, str]]):
        self.t, self.i = toks, 0

    def peek(self) -> tuple[str, str]:
        return self.t[self.i] if self.i < len(self.t) else ("end", "")

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        self.i += 1
        return tok

    def kw(self, *words: str) -> str | None:
        """Consume a keyword name token (case-insensitive), else leave the position alone."""
        k, v = self.peek()
        if k == "name" and v.upper() in words:
            self.take()
            return v.upper()
        return None

    def take_punct(self, ch: str) -> bool:
        if self.peek() == ("punct", ch):
            self.take()
            return True
        return False

    def take_ident(self) -> str | None:
        k, v = self.peek()
        if k == "name" and v.upper() not in _COND_KEYWORDS:
            self.take()
            return v
        return None

    def take_value(self) -> str | None:
        k, v = self.peek()
        if k == "str":
            self.take()
            return v[1:-1]
        if k == "num":
            self.take()
            return v
        return None


def _value_list(p: _CondParser) -> list[str] | None:
    if not p.take_punct("("):
        return None
    vals = []
    while True:
        v = p.take_value()
        if v is None:
            return None
        vals.append(v)
        if p.take_punct(")"):
            return vals
        if not p.take_punct(","):
            return None


def _condition(p: _CondParser, resolve) -> dict | None:
    if p.kw("NOT"):
        if not p.kw("MISSING") or not p.take_punct("("):
            return None
        ident = p.take_ident()
        if ident is None or not p.take_punct(")"):
            return None
        try:
            src = resolve(ident)
        except Unresolved:
            return None
        return {"src": src, "op": "notmissing", "value": ""}
    if p.kw("MISSING"):
        if not p.take_punct("("):
            return None
        ident = p.take_ident()
        if ident is None or not p.take_punct(")"):
            return None
        try:
            src = resolve(ident)
        except Unresolved:
            return None
        return {"src": src, "op": "missing", "value": ""}

    ident = p.take_ident()
    if ident is None:
        return None
    try:
        src = resolve(ident)
    except Unresolved:
        return None

    negate = bool(p.kw("NOT"))
    if p.kw("IN"):
        vals = _value_list(p)
        if vals is None:
            return None
        return {"src": src, "op": "notin" if negate else "in", "value": ",".join(vals)}
    if negate:
        return None                                   # 'NOT' only makes sense before IN/MISSING

    k, v = p.peek()
    if k == "op":
        op = v
    elif k == "name" and v.lower() in _COMPARE_OPS:
        op = v
    else:
        return None
    p.take()
    val = p.take_value()
    if val is None:
        return None
    return {"src": src, "op": _COMPARE_OPS.get(op.lower(), "eq"), "value": val}


def _and_cond(p: _CondParser, resolve) -> dict | None:
    first = _condition(p, resolve)
    if first is None:
        return None
    extra = []
    while p.kw("AND"):
        c = _condition(p, resolve)
        if c is None:
            return None
        extra.append(c)
    if extra:
        first = {**first, "and": extra}
    return first


def _assign_rhs(p: _CondParser, target: str) -> dict | None:
    name = p.take_ident()
    if name is None or upper(name) != upper(target):
        return None                                   # a branch must set the SAME variable
    if p.peek() != ("op", "="):
        return None
    p.take()
    val = p.take_value()
    if val is None:
        return None                                   # only literal branches are supported
    return {"kind": "text", "text": val}


def _eat_semi(p: _CondParser) -> None:
    while p.take_punct(";"):
        pass


def parse_cond(sas: str, target: str, resolve) -> dict | None:
    """'IF x = 1 THEN y = "A"; ELSE IF ... THEN y = "B"; ELSE y = "C";' -> {rules, else} for
    the 'cond' recipe, or None outside the supported grammar (an OR, a non-literal branch, a
    branch that sets a different variable, ...) — never a guess at what an unsupported
    statement might have meant."""
    toks = _cond_tokenize(sas)
    if not toks:
        return None
    p = _CondParser(toks)
    if not p.kw("IF"):
        return None
    rules: list[dict] = []
    while True:
        cond = _and_cond(p, resolve)
        if cond is None or not p.kw("THEN"):
            return None
        then = _assign_rhs(p, target)
        if then is None:
            return None
        rules.append({**cond, "then": then})
        _eat_semi(p)
        if not p.kw("ELSE"):
            return {"rules": rules, "else": {"kind": "missing"}} if p.i == len(p.t) else None
        if p.kw("IF"):
            continue
        else_val = _assign_rhs(p, target)
        if else_val is None:
            return None
        _eat_semi(p)
        return {"rules": rules, "else": else_val} if p.i == len(p.t) else None



def describe(sas: str) -> str:
    node = parse(sas)
    return "" if node is None else upper(s(sas))
