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


def describe(sas: str) -> str:
    node = parse(sas)
    return "" if node is None else upper(s(sas))
