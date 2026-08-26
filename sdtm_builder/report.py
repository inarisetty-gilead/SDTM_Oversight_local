"""Human-readable output: console summaries, the comparison workbook, and an HTML report."""
from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd

from .compare import DomainComparison
from .util import s


# ── console ─────────────────────────────────────────────────────────────────
def build_summary(results: dict) -> str:
    lines = [f"{'DOMAIN':<10}{'ROWS':>8}{'BUILT':>8}{'DROPPED':>9}{'NOT BUILT':>11}"
             f"{'GUESSED':>9}{'EDITED':>8}  BASE DATASET"]
    lines.append("-" * 95)
    for dom in sorted(results):
        r = results[dom]
        if not r.ok:
            lines.append(f"{dom:<10}{'—':>8}{'—':>8}{'—':>9}{'—':>11}{'—':>9}{'—':>8}  ERROR: {r.error}")
            continue
        c = r.counts
        nb = c["not_built"] + c["error"]
        lines.append(f"{dom:<10}{len(r.dataset):>8}{c['built']:>8}{c['dropped']:>9}"
                     f"{nb:>11}{c['name_matched'] or '':>9}{c['edited'] or '':>8}  {r.base_dataset}")
    return "\n".join(lines)


def edit_detail(results: dict) -> str:
    rows = [(d, b.variable, b.method or b.describe_source(), b.spec_method)
            for d in sorted(results) for b in results[d].blocks if b.edited]
    if not rows:
        return ""
    out = [f"\n{len(rows)} mapping(s) were changed by hand — for these variables this build is "
           "not independent of your own work:"]
    out += [f"  {d}.{v:<12} built as {how}   (spec: {spec or '—'})" for d, v, how, spec in rows]
    return "\n".join(out)


def guessed_detail(results: dict) -> str:
    rows = [(d, b.variable, f"{b.dataset}.{b.column}", b.confidence)
            for d in sorted(results) for b in results[d].blocks
            if b.method_source == "name_match" and b.status == "built"]
    if not rows:
        return ""
    out = [f"\n{len(rows)} variable(s) have no source in the spec and were matched by name. "
           "Agreement with the vendor on these is not evidence the spec was followed:"]
    out += [f"  {d}.{v:<12} ~ {src}  ({c}% name similarity)" for d, v, src, c in rows]
    return "\n".join(out)


def not_built_detail(results: dict, limit: int = 0) -> str:
    rows = []
    for dom in sorted(results):
        for b in results[dom].blocks:
            if b.status in ("not_built", "error"):
                rows.append((dom, b.variable, b.error or b.reason or "no deterministic rule"))
    if not rows:
        return "Every variable in the spec was either built or explicitly dropped."
    shown = rows[:limit] if limit else rows
    out = [f"{len(rows)} variable(s) could not be built deterministically:"]
    out += [f"  {d}.{v:<12} {why}" for d, v, why in shown]
    if limit and len(rows) > limit:
        out.append(f"  … and {len(rows) - limit} more (see build_manifest.xlsx, 'Not Built')")
    return "\n".join(out)


def compare_summary(comps: dict[str, DomainComparison]) -> str:
    lines = [f"{'DOMAIN':<10}{'BUILT':>8}{'VENDOR':>8}{'MATCH':>8}{'+BUILT':>8}"
             f"{'+VENDOR':>9}{'VALUE DIFF':>12}  STATUS"]
    lines.append("-" * 88)
    for dom in sorted(comps):
        c = comps[dom]
        if c.error:
            lines.append(f"{dom:<10}{c.rows_built:>8}{c.rows_vendor:>8}{'—':>8}{'—':>8}"
                         f"{'—':>9}{'—':>12}  {c.error}")
            continue
        status = "identical" if c.clean else "differences"
        lines.append(f"{dom:<10}{c.rows_built:>8}{c.rows_vendor:>8}{c.matched:>8}"
                     f"{c.only_built:>8}{c.only_vendor:>9}{c.total_differences:>12}  {status}")
    return "\n".join(lines)


def compare_detail(comps: dict[str, DomainComparison], top: int = 10) -> str:
    out = []
    for dom in sorted(comps):
        c = comps[dom]
        if c.error or c.clean:
            continue
        out.append(f"\n{dom} — matched on {', '.join(c.keys) or '(none)'}")
        if c.key_note:
            out.append(f"  note: {c.key_note}")
        for note in c.notes:
            out.append(f"  note: {note}")
        if c.vars_only_vendor:
            out.append(f"  variables only in the vendor dataset: {', '.join(c.vars_only_vendor)}")
        if c.vars_only_built:
            out.append(f"  variables only in the built dataset:  {', '.join(c.vars_only_built)}")
        if c.not_built:
            out.append(f"  not built here, so not compared: {', '.join(c.not_built)}")
        if c.only_built:
            out.append(f"  {c.only_built} record(s) built here are not in the vendor delivery")
        if c.only_vendor:
            out.append(f"  {c.only_vendor} vendor record(s) were not produced by this build")
        diffs = [d for d in c.diffs if d.differing][:top]
        for d in diffs:
            out.append(f"    {d.variable:<12} {d.differing:>7} of {d.compared} differ "
                       f"({d.agreement:5.1f}% agree)")
            for ex in d.examples[:2]:
                keys = " ".join(f"{k}={v}" for k, v in ex.items() if k not in ("built", "vendor"))
                out.append(f"        {keys}  built={ex['built']!r}  vendor={ex['vendor']!r}")
        more = len([d for d in c.diffs if d.differing]) - len(diffs)
        if more > 0:
            out.append(f"    … and {more} more variable(s) with differences")
    return "\n".join(out) if out else "\nNo differences found in any compared domain."


# ── comparison workbook ─────────────────────────────────────────────────────
def write_comparison_workbook(comps: dict[str, DomainComparison], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    overview, variables, examples, records = [], [], [], []
    for dom in sorted(comps):
        c = comps[dom]
        overview.append({
            "domain": dom, "status": "error" if c.error else ("identical" if c.clean else "differences"),
            "detail": c.error, "keys": ", ".join(c.keys),
            "rows_built": c.rows_built, "rows_vendor": c.rows_vendor, "records_matched": c.matched,
            "records_only_built": c.only_built, "records_only_vendor": c.only_vendor,
            "value_differences": c.total_differences,
            "vars_only_built": ", ".join(c.vars_only_built),
            "vars_only_vendor": ", ".join(c.vars_only_vendor),
            "vars_not_built_here": ", ".join(c.not_built),
            "notes": " | ".join(([c.key_note] if c.key_note else []) + c.notes),
        })
        for d in c.diffs:
            variables.append({
                "domain": dom, "variable": d.variable, "records_compared": d.compared,
                "records_differing": d.differing, "agreement_pct": round(d.agreement, 2),
                "populated_only_built": d.only_built_nonblank,
                "populated_only_vendor": d.only_vendor_nonblank,
            })
            for ex in d.examples:
                rec = {"domain": dom, "variable": d.variable}
                rec.update({k: v for k, v in ex.items() if k not in ("built", "vendor")})
                rec["built_value"] = ex["built"]
                rec["vendor_value"] = ex["vendor"]
                examples.append(rec)
        for label, frame in (("only in built", c.only_built_rows),
                             ("only in vendor", c.only_vendor_rows)):
            if frame is not None and len(frame):
                tmp = frame.copy()
                tmp.insert(0, "side", label)
                tmp.insert(0, "domain", dom)
                records.append(tmp)

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(overview).to_excel(xw, sheet_name="Overview", index=False)
        pd.DataFrame(variables or [{"note": "no variables compared"}]) \
            .to_excel(xw, sheet_name="Variable Agreement", index=False)
        pd.DataFrame(examples or [{"note": "no value differences"}]) \
            .to_excel(xw, sheet_name="Difference Examples", index=False)
        unmatched = pd.concat(records, ignore_index=True) if records else \
            pd.DataFrame([{"note": "every record matched"}])
        unmatched.to_excel(xw, sheet_name="Unmatched Records", index=False)
    return path


# ── HTML report ─────────────────────────────────────────────────────────────
_CSS = """
:root{--bg:#fff;--fg:#1a1d21;--mut:#5b6470;--line:#e3e6ea;--ok:#0a7d33;--warn:#a05a00;--bad:#b3261e;--head:#f6f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e8eaed;--mut:#9aa4b0;--line:#2a2f36;--ok:#4ec26f;--warn:#e0a44a;--bad:#f2776b;--head:#1c2126}}
*{box-sizing:border-box}body{margin:0;padding:2rem 1.5rem;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:1200px;margin-inline:auto}
h1{font-size:1.5rem;margin:0 0 .25rem}h2{font-size:1.1rem;margin:2rem 0 .5rem;padding-top:1rem;border-top:1px solid var(--line)}
h3{font-size:.95rem;margin:1.25rem 0 .4rem}
.sub{color:var(--mut);font-size:.85rem;margin-bottom:1.5rem}
table{border-collapse:collapse;width:100%;font-size:.85rem;margin:.5rem 0}
th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--head);font-weight:600;position:sticky;top:0}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:6px}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--head);padding:.1rem .3rem;border-radius:3px}
.note{color:var(--mut);font-size:.82rem;margin:.3rem 0}
"""


def _tbl(rows: list[dict], cols: list[tuple[str, str]], numeric: set[str] = frozenset()) -> str:
    if not rows:
        return '<p class="note">nothing to show</p>'
    head = "".join(f'<th class="{"n" if k in numeric else ""}">{html.escape(lab)}</th>'
                   for k, lab in cols)
    body = []
    for r in rows:
        cells = "".join(
            f'<td class="{"n" if k in numeric else ""}">{r.get(k, "")}</td>' for k, _ in cols)
        body.append(f"<tr>{cells}</tr>")
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def write_html_report(results: dict, comps: dict[str, DomainComparison] | None,
                      path: str | Path, meta: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    e = html.escape

    parts = [f'<!doctype html><meta charset="utf-8"><title>SDTM build check</title>'
             f"<style>{_CSS}</style>",
             "<h1>SDTM build and vendor comparison</h1>",
             f'<p class="sub">spec <code>{e(str(meta.get("spec","")))}</code> · '
             f'raw <code>{e(str(meta.get("raw","")))}</code> · '
             f'built {e(str(meta.get("timestamp","")))}</p>']

    brows = []
    for dom in sorted(results):
        r = results[dom]
        if not r.ok:
            brows.append({"domain": dom, "rows": "—", "built": "—", "dropped": "—",
                          "guessed": "—",
                          "not_built": f'<span class="bad">{e(r.error)}</span>', "base": "—"})
            continue
        c = r.counts
        nb = c["not_built"] + c["error"]
        brows.append({"domain": dom, "rows": len(r.dataset), "built": c["built"],
                      "dropped": c["dropped"],
                      "guessed": (f'<span class="warn">{c["name_matched"]}</span>'
                                  if c["name_matched"] else "0"),
                      "not_built": f'<span class="{"warn" if nb else "ok"}">{nb}</span>',
                      "base": e(r.base_dataset)})
    parts.append("<h2>Build</h2>")
    parts.append(_tbl(brows, [("domain", "Domain"), ("rows", "Records"), ("built", "Variables built"),
                              ("dropped", "Dropped by spec"), ("not_built", "Not built"),
                              ("guessed", "Name matched"), ("base", "Base raw dataset")],
                      {"rows", "built", "dropped", "not_built", "guessed"}))

    edits = [{"domain": d, "variable": b.variable, "how": e(b.method or b.describe_source()),
              "spec": e(b.spec_method or "—"), "note": e(b.edit_note)}
             for d in sorted(results) for b in results[d].blocks if b.edited]
    if edits:
        parts.append("<h3>Mappings changed by hand</h3>")
        parts.append('<p class="note">These variables were <b>not</b> built from the mapping '
                     'spec. For those variables this build is not an independent rebuild, and '
                     'the vendor comparison below should be read with that in mind.</p>')
        parts.append(_tbl(edits, [("domain", "Domain"), ("variable", "Variable"),
                                  ("how", "Built as"), ("spec", "Spec would have given"),
                                  ("note", "Note")]))

    guessed = [{"domain": d, "variable": b.variable, "label": e(b.label),
                "src": f"<code>{e(b.dataset)}.{e(b.column)}</code>", "conf": f"{b.confidence}%"}
               for d in sorted(results) for b in results[d].blocks
               if b.method_source == "name_match" and b.status == "built"]
    if guessed:
        parts.append("<h3>Sources guessed from the variable name</h3>")
        parts.append('<p class="note">The mapping spec names no source for these variables. '
                     'They were matched to a raw column by name similarity. Agreement with the '
                     'vendor here shows the two guesses coincide — it is <b>not</b> evidence '
                     'that the spec was followed.</p>')
        parts.append(_tbl(guessed, [("domain", "Domain"), ("variable", "Variable"),
                                    ("label", "Label"), ("src", "Matched to"),
                                    ("conf", "Name similarity")], {"conf"}))

    nb_rows = [{"domain": d, "variable": b.variable,
                "why": e(b.error or b.reason or "no deterministic rule in the spec")}
               for d in sorted(results) for b in results[d].blocks
               if b.status in ("not_built", "error")]
    empties = [{"domain": d, "variable": b.variable, "how": e(b.method or ""),
                "why": e(b.reason or "")}
               for d in sorted(results) for b in results[d].blocks if b.status == "empty"]
    if empties:
        parts.append("<h3>Built, but empty</h3>")
        parts.append('<p class="note">These mappings ran without error and produced no values '
                     'at all. That is usually a source that holds nothing for these records, or '
                     'a join that matched nothing — worth checking before the comparison is '
                     'read.</p>')
        parts.append(_tbl(empties, [("domain", "Domain"), ("variable", "Variable"),
                                    ("how", "Built as"), ("why", "Note")]))
    if nb_rows:
        parts.append("<h3>Variables not built</h3>")
        parts.append('<p class="note">These were not produced, so they are excluded from the '
                     'comparison rather than reported as vendor differences.</p>')
        parts.append(_tbl(nb_rows, [("domain", "Domain"), ("variable", "Variable"), ("why", "Reason")]))

    if comps:
        parts.append("<h2>Comparison against the vendor delivery</h2>")
        crows = []
        for dom in sorted(comps):
            c = comps[dom]
            if c.error:
                st = f'<span class="warn">{e(c.error)}</span>'
                crows.append({"domain": dom, "status": st, "rb": c.rows_built, "rv": c.rows_vendor,
                              "m": "—", "ob": "—", "ov": "—", "vd": "—"})
                continue
            st = ('<span class="ok">identical</span>' if c.clean
                  else f'<span class="bad">{c.total_differences + c.only_built + c.only_vendor} '
                       "difference(s)</span>")
            crows.append({"domain": dom, "status": st, "rb": c.rows_built, "rv": c.rows_vendor,
                          "m": c.matched, "ob": c.only_built, "ov": c.only_vendor,
                          "vd": c.total_differences})
        parts.append(_tbl(crows, [("domain", "Domain"), ("status", "Status"), ("rb", "Built rows"),
                                  ("rv", "Vendor rows"), ("m", "Matched"), ("ob", "Only built"),
                                  ("ov", "Only vendor"), ("vd", "Value differences")],
                          {"rb", "rv", "m", "ob", "ov", "vd"}))

        for dom in sorted(comps):
            c = comps[dom]
            if c.error or c.clean:
                continue
            parts.append(f"<h3>{e(dom)}</h3>")
            parts.append(f'<p class="note">matched on <code>{e(", ".join(c.keys))}</code></p>')
            for n in ([c.key_note] if c.key_note else []) + c.notes:
                parts.append(f'<p class="note">{e(n)}</p>')
            vrows = [{"variable": d.variable, "n": d.compared, "diff": d.differing,
                      "agree": f"{d.agreement:.1f}%",
                      "ob": d.only_built_nonblank, "ov": d.only_vendor_nonblank}
                     for d in c.diffs if d.differing]
            parts.append(_tbl(vrows, [("variable", "Variable"), ("n", "Compared"),
                                      ("diff", "Differing"), ("agree", "Agreement"),
                                      ("ob", "Populated only in build"),
                                      ("ov", "Populated only in vendor")],
                              {"n", "diff", "ob", "ov"}))
            ex_rows = []
            for d in c.diffs:
                for ex in d.examples[:3]:
                    ex_rows.append({
                        "variable": d.variable,
                        "key": e(" · ".join(f"{k}={v}" for k, v in ex.items()
                                            if k not in ("built", "vendor"))),
                        "b": f"<code>{e(s(ex['built']))}</code>",
                        "v": f"<code>{e(s(ex['vendor']))}</code>"})
            if ex_rows:
                parts.append(_tbl(ex_rows, [("variable", "Variable"), ("key", "Record"),
                                            ("b", "Built"), ("v", "Vendor")]))

    parts.append('<p class="note" style="margin-top:2rem">Built locally by sdtm_builder — '
                 'no network, no AI, no cloud storage. Every value traces to a named operation '
                 'recorded in build_manifest.xlsx.</p>')
    path.write_text("".join(parts), encoding="utf-8")
    return path


def write_json(obj, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))
    return path
