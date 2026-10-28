"""The post-build action list: what a person has to look at, kept per study.

A build leaves three kinds of item behind — a value the engine GUESSED (a name-similarity
match, or a convention such as a composed USUBJID), a variable it could not build, and a
domain that built no records. Every item has a stable id derived from what it says, so a
decision on it (confirmed / dismissed) survives a rebuild: the item stays decided while the
build keeps producing the same thing, leaves by itself when a later build no longer produces
it, and comes back as a NEW item when what it says changes (a different guessed source, a
different error). A reader never re-reviews what they already decided.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .util import s

GUESS_SOURCES = ("name_match", "convention")


def _id(*parts) -> str:
    return hashlib.sha1("|".join(s(p).upper() for p in parts).encode()).hexdigest()[:16]


def action_items(results: dict) -> list[dict]:
    """Every item the current build results leave for a person, domain by domain."""
    items: list[dict] = []
    for dom in sorted(results):
        r = results[dom]
        rows = 0 if r.dataset is None else len(r.dataset)
        if r.error or not rows:
            why = r.error or f"no records from '{r.base_dataset or '?'}'"
            items.append({"id": _id("domain", dom, why), "kind": "empty", "domain": dom,
                          "variable": "", "title": f"{dom} built no records", "detail": why,
                          "fix": "Open the domain and give it a record source, or prepare one."})
            if r.error:
                continue
        for b in r.blocks:
            if b.supp:
                continue
            if b.status in ("not_built", "error"):
                why = b.error or b.reason or "no deterministic rule in the spec"
                items.append({"id": _id("blocked", dom, b.variable, why), "kind": "blocked",
                              "domain": dom, "variable": b.variable,
                              "title": f"{dom}.{b.variable} could not be built", "detail": why,
                              "fix": "Map it in the domain view, or dismiss it if the domain does "
                                     "not need it; the next build clears it once it builds."})
            elif b.status == "built" and b.method_source in GUESS_SOURCES and not b.edited:
                src = ".".join(x for x in (s(b.dataset), s(b.column)) if x) or b.recipe or "derived"
                items.append({"id": _id("guess", dom, b.variable, b.method_source, src), "kind": "guess",
                              "domain": dom, "variable": b.variable,
                              "title": f"{dom}.{b.variable} ← {src}",
                              "detail": f"{b.reason} (confidence {b.confidence})".strip(),
                              "fix": "The spec does not say this — confirm it, or map the variable "
                                     "by hand in the domain view."})
    return items


def open_actions(results: dict, decisions: dict) -> dict:
    """{open: [undecided items], decided: [decided items, with the decision]} for this build."""
    items = action_items(results)
    return {"open": [i for i in items if i["id"] not in decisions],
            "decided": [{**i, **decisions[i["id"]]} for i in items if i["id"] in decisions]}


def decide(decisions: dict, action_id: str, decision: str, title: str = "") -> dict:
    """Record a decision (confirmed / dismissed) on an item, or `reopen` to remove it."""
    if decision == "reopen":
        decisions.pop(action_id, None)
    elif decision in ("confirmed", "dismissed"):
        decisions[action_id] = {"decision": decision, "title": title,
                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    else:
        raise ValueError("decision must be confirmed, dismissed or reopen")
    return decisions
