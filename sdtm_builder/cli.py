"""Command line for sdtm_builder.

  sdtmbuild build   --spec SPEC.xlsx --raw RAWDIR --out OUTDIR [--domains AE,DM]
  sdtmbuild compare --spec SPEC.xlsx --raw RAWDIR --vendor VENDORDIR --out OUTDIR
  sdtmbuild inspect --spec SPEC.xlsx [--raw RAWDIR]

Everything is local. The tool opens no network connection and calls no model.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, report
from .build import build_study
from .compare import compare_study
from .rawio import RawStore
from .spec import load_spec
from .util import upper
from .writers import write_dataset, write_manifest


def _kv_list(values, sep=",") -> dict:
    """--base AE=ae_log --base LB=lb_all  ->  {'AE': 'ae_log', 'LB': 'lb_all'}"""
    out = {}
    for item in values or []:
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"expected DOMAIN=VALUE, got '{item}'")
        k, v = item.split("=", 1)
        out[upper(k)] = [x.strip() for x in v.split(sep) if x.strip()] if sep in v else v.strip()
    return out


def _csv_list(value):
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _add_common(p):
    p.add_argument("--spec", required=True, help="Designer-format mapping spec workbook (.xlsx)")
    p.add_argument("--raw", required=True, help="folder holding the raw datasets")
    p.add_argument("--out", default="sdtm_out", help="output folder (default: sdtm_out)")
    p.add_argument("--domains", type=_csv_list, default=None,
                   help="comma-separated domains to build (default: every domain in the spec)")
    p.add_argument("--studyid", default="", help="STUDYID value, when the raw data does not carry one")
    p.add_argument("--base", action="append", default=[], metavar="DOMAIN=DATASET",
                   help="force a domain's record source, e.g. --base DS=disposition")
    p.add_argument("--sort", action="append", default=[], metavar="DOMAIN=VAR1,VAR2",
                   help="sort a domain before --SEQ numbering")
    p.add_argument("--encoding", default="latin-1", help="raw file text encoding (default: latin-1)")
    p.add_argument("--no-recursive", action="store_true", help="do not search the raw folder recursively")
    p.add_argument("--name-match", type=int, default=70, metavar="PCT",
                   help="name-similarity threshold for variables the spec leaves unmapped "
                        "(0 disables it; SDTM Designer uses 45; default 70)")
    p.add_argument("--structure", default="full", choices=("full", "populated"),
                   help="'full' (default) emits every variable the spec defines for the domain, "
                        "empty where it could not be populated — a submission-shaped dataset. "
                        "'populated' emits only the variables that were built.")
    p.add_argument("--format", default="csv", choices=("csv", "xpt", "parquet", "none"),
                   help="output dataset format (default: csv)")


def _progress():
    """Live per-domain progress, but only when a person is watching."""
    if not sys.stdout.isatty():
        return None
    return lambda d: print(f"  building {d} …".ljust(40), end="\r", flush=True)


def _load(args):
    spec = load_spec(args.spec)
    store = RawStore.discover(args.raw, recursive=not args.no_recursive, encoding=args.encoding)
    return spec, store


def _do_build(args, spec, store):
    print(f"spec:  {args.spec}  ({len(spec.domain_names)} domains)")
    print(f"raw:   {store.root}  ({len(store.refs)} datasets)")
    targets = args.domains or spec.domain_names
    missing = [d for d in (upper(x) for x in targets) if d not in spec.domains]
    if missing:
        print(f"error: not in the mapping spec: {', '.join(missing)}", file=sys.stderr)
        return None
    print(f"building {len(targets)} domain(s)…\n")

    results = build_study(
        spec, store, domains=targets, studyid=args.studyid,
        base_overrides=_kv_list(args.base, sep="\x00"),
        sort_overrides=_kv_list(args.sort),
        include_unbuilt=(args.structure == "full"),
        name_match_threshold=args.name_match,
        progress=_progress(),
    )
    if sys.stdout.isatty():
        print(" " * 40, end="\r")
    print(report.build_summary(results))

    warned = [(d, w) for d in sorted(results) for w in results[d].warnings]
    if warned:
        print("\nBuild notes:")
        for d, w in warned:
            print(f"  {d}: {w}")
    print()
    print(report.not_built_detail(results, limit=25))
    gd = report.guessed_detail(results)
    if gd:
        print(gd)
    ed = report.edit_detail(results)
    if ed:
        print(ed)

    out = Path(args.out)
    if args.format != "none":
        data_dir = out / "datasets"
        for dom, res in results.items():
            if not res.ok:
                continue
            labels = {b.variable: b.label for b in res.blocks}
            _, w = write_dataset(res.dataset, data_dir, dom, args.format, labels)
            for msg in w:
                print(f"  note: {msg}")
            if res.supp is not None and len(res.supp):
                write_dataset(res.supp, data_dir, f"SUPP{dom}", args.format)
        print(f"\ndatasets written to {data_dir}")

    from . import provenance
    provenance.record_run(out, __version__, args.spec, str(store.root),
                          {"format": args.format, "structure": args.structure,
                           "name_match": args.name_match, "studyid": args.studyid,
                           "domains": sorted(results)})
    provenance.record_outputs(out)

    meta = {"spec": str(Path(args.spec).resolve()), "raw": str(store.root.resolve()),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "tool": f"sdtm_builder {__version__}", "studyid": args.studyid,
            "domains": sorted(results)}
    jpath, xpath = write_manifest(results, out, meta)
    print(f"manifest:  {xpath}")
    return results, meta


def cmd_build(args):
    spec, store = _load(args)
    got = _do_build(args, spec, store)
    if got is None:
        return 2
    results, meta = got
    report.write_html_report(results, None, Path(args.out) / "build_report.html", meta)
    print(f"report:    {Path(args.out) / 'build_report.html'}")
    return 0 if all(r.ok for r in results.values()) else 1


def cmd_compare(args):
    spec, store = _load(args)
    got = _do_build(args, spec, store)
    if got is None:
        return 2
    results, meta = got

    print(f"\ncomparing against vendor delivery: {args.vendor}\n")
    comps = compare_study(
        results, args.vendor,
        keys=_kv_list(args.key),
        ignore_case=args.ignore_case,
        numeric_tol=args.numeric_tolerance,
        ignore_vars=set(args.ignore_var or []),
        max_examples=args.examples,
    )
    print(report.compare_summary(comps))
    print(report.compare_detail(comps, top=args.top))

    out = Path(args.out)
    wb = report.write_comparison_workbook(comps, out / "vendor_comparison.xlsx")
    meta["vendor"] = str(Path(args.vendor).resolve())
    html_path = report.write_html_report(results, comps, out / "build_report.html", meta)
    print(f"\ncomparison: {wb}")
    print(f"report:     {html_path}")

    dirty = [d for d, c in comps.items() if c.error or not c.clean]
    return 1 if dirty else 0


def cmd_synth(args):
    from .synth import SynthOptions, generate
    spec = load_spec(args.spec)
    res = generate(spec, args.out, SynthOptions(
        subjects=args.subjects, visits=args.visits, events_per_subject=args.events,
        studyid=args.studyid, seed=args.seed, fmt=args.sfmt))
    print(f"spec:  {args.spec}")
    print(f"wrote {len(res['datasets'])} dataset(s), {res['rows']:,} records, "
          f"{res['subjects']} subjects -> {res['out_dir']}\n")
    print(f"{'DATASET':<24}{'ROWS':>8}{'COLS':>7}  GRAIN")
    print("-" * 55)
    for d in res["datasets"]:
        print(f"{d['dataset']:<24}{d['rows']:>8}{d['columns']:>7}  {d['grain']}")
    print("\nThis data is INVENTED. Use it to check that the spec builds — never to judge a\n"
          "vendor delivery, which is only meaningful against the raw data the vendor read.")
    return 0


def cmd_verify(args):
    """Re-run a recorded build and report whether it reproduces bit for bit."""
    import tempfile

    from . import provenance
    from .build import build_study

    rec = provenance.load(args.run)
    if not rec:
        print(f"error: no provenance.json in {args.run}", file=sys.stderr)
        return 2

    spec_path = args.spec or rec.get("spec", {}).get("path", "")
    raw_path = args.raw or rec.get("raw", {}).get("root", "")
    print(f"run recorded {rec.get('created')} by sdtm_builder {rec.get('tool_version')}")
    print(f"  spec: {spec_path}")
    print(f"  raw : {raw_path}\n")

    problems = []
    if spec_path and Path(spec_path).exists():
        now = provenance.file_digest(spec_path)
        was = rec.get("spec", {}).get("digest")
        state = "unchanged" if now == was else "CHANGED since the run"
        print(f"  mapping spec: {state}")
        if now != was:
            problems.append("the mapping spec has changed")
    else:
        problems.append("the mapping spec is not where it was")
        print("  mapping spec: NOT FOUND")

    if raw_path and Path(raw_path).is_dir():
        now = provenance.folder_digest(raw_path)
        was = rec.get("raw", {})
        same = now["digest"] == was.get("digest")
        print(f"  raw data    : {'unchanged' if same else 'CHANGED since the run'} "
              f"({now['count']} files)")
        if not same:
            wf, nf = was.get("files", {}), now["files"]
            for name in sorted(set(wf) | set(nf))[:8]:
                if wf.get(name) != nf.get(name):
                    print(f"      {name}: "
                          f"{'added' if name not in wf else 'removed' if name not in nf else 'edited'}")
            problems.append("the raw data has changed")
    else:
        problems.append("the raw data folder is not where it was")
        print("  raw data    : NOT FOUND")

    for note in provenance.describe_drift(rec):
        print(f"  environment : {note}")

    if problems:
        print("\nCannot reproduce: " + "; ".join(problems))
        print("A build is only reproducible against the inputs it was made from.")
        return 1

    opts = rec.get("options", {})
    print("\nrebuilding…")
    spec = load_spec(spec_path)
    store = RawStore.discover(raw_path)
    with tempfile.TemporaryDirectory() as td:
        results = build_study(spec, store, domains=opts.get("domains"),
                              studyid=opts.get("studyid", ""),
                              include_unbuilt=(opts.get("structure", "full") == "full"),
                              name_match_threshold=int(opts.get("name_match", 70)))
        fmt = opts.get("format", "xpt")
        data_dir = Path(td) / "datasets"
        if fmt != "none":
            for dom, res in results.items():
                if not res.ok:
                    continue
                labels = {b.variable: b.label for b in res.blocks}
                write_dataset(res.dataset, data_dir, dom, fmt, labels)
                if res.supp is not None and len(res.supp):
                    write_dataset(res.supp, data_dir, f"SUPP{dom}", fmt)
        outcome = provenance.compare_to(rec, td)

    if outcome["reproduced"]:
        print(f"\nREPRODUCED — {len(outcome['identical'])} dataset(s) hold exactly the records "
              "the recorded run produced.")
        print("(Compared by dataset content. A transport file stamps its own creation time, so "
              "its bytes differ on every write even when the data does not.)")
        return 0
    print("\nDID NOT REPRODUCE")
    for k in ("changed", "missing", "added"):
        if outcome[k]:
            print(f"  {k}: {', '.join(outcome[k][:10])}")
    return 1


def cmd_inspect(args):
    spec = load_spec(args.spec)
    print(f"spec: {args.spec}")
    print(f"{len(spec.domain_names)} domain(s): {', '.join(spec.domain_names)}")
    if spec.codelists:
        print(f"{len(spec.codelists)} codelist(s) loaded from the spec")
    if spec.skipped_sheets:
        print("\nskipped sheets:")
        for sheet, why in spec.skipped_sheets:
            print(f"  {sheet}: {why}")

    if not args.raw:
        return 0
    store = RawStore.discover(args.raw, recursive=not args.no_recursive, encoding=args.encoding)
    print(f"\nraw: {store.root}  ({len(store.refs)} datasets)")
    for name in sorted(store.refs):
        ref = store.refs[name]
        try:
            df = store.get(name)
            print(f"  {name:<24} {len(df):>8} rows  {len(df.columns):>4} cols   {ref.path.name}")
        except Exception as exc:                                    # noqa: BLE001
            print(f"  {name:<24} {'unreadable':>8}   {ref.path.name}  ({exc})")

    from .translate import raw_refs
    print("\nspec sources that are NOT in the raw folder:")
    missing = {}
    for dom, rows in spec.domains.items():
        for r in rows:
            for ds, col in raw_refs(r.input_variables):
                if not store.has(ds):
                    missing.setdefault(ds, set()).add(f"{dom}.{r.variable}")
                elif col not in store.columns(store.resolve(ds)):
                    missing.setdefault(f"{ds}.{col}", set()).add(f"{dom}.{r.variable}")
    if not missing:
        print("  none — every raw.<dataset>.<column> in the spec resolves")
    else:
        for key in sorted(missing):
            users = sorted(missing[key])
            print(f"  {key:<32} used by {', '.join(users[:5])}"
                  f"{' …' if len(users) > 5 else ''}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="sdtmbuild",
        description="Build SDTM datasets locally from a mapping spec and raw data, and check "
                    "a vendor's delivery against them. No network, no AI.")
    p.add_argument("--version", action="version", version=f"sdtm_builder {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build SDTM datasets from the spec and raw data")
    _add_common(b)
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("compare", help="build, then compare against a vendor SDTM delivery")
    _add_common(c)
    c.add_argument("--vendor", required=True, help="folder holding the vendor's delivered SDTM")
    c.add_argument("--key", action="append", default=[], metavar="DOMAIN=VAR1,VAR2",
                   help="record-matching keys for a domain (default: derived per domain)")
    c.add_argument("--ignore-var", action="append", default=[], metavar="VAR",
                   help="variable to exclude from value comparison (repeatable)")
    c.add_argument("--ignore-case", action="store_true", help="compare text case-insensitively")
    c.add_argument("--numeric-tolerance", type=float, default=1e-9,
                   help="absolute tolerance for numeric comparison (default: 1e-9)")
    c.add_argument("--examples", type=int, default=5, help="difference examples per variable")
    c.add_argument("--top", type=int, default=10, help="variables detailed per domain on the console")
    c.set_defaults(func=cmd_compare)

    g = sub.add_parser("synth", help="generate synthetic raw data from the spec's Input Variables")
    g.add_argument("--spec", required=True)
    g.add_argument("--out", required=True, help="folder to write the synthetic raw data into")
    g.add_argument("--subjects", type=int, default=40)
    g.add_argument("--visits", type=int, default=5)
    g.add_argument("--events", type=int, default=3, help="max event records per subject")
    g.add_argument("--studyid", default="SYNTH-001")
    g.add_argument("--seed", default="sdtm-oversight",
                   help="same seed + same spec gives byte-identical data")
    g.add_argument("--format", dest="sfmt", default="csv", choices=("csv", "parquet"))
    g.set_defaults(func=cmd_synth)

    v = sub.add_parser("verify", help="re-run a past build and prove the output is unchanged")
    v.add_argument("run", help="a run folder containing provenance.json")
    v.add_argument("--spec", default="", help="override the recorded spec path if it has moved")
    v.add_argument("--raw", default="", help="override the recorded raw path if it has moved")
    v.set_defaults(func=cmd_verify)

    i = sub.add_parser("inspect", help="show what the spec asks for and what the raw folder has")
    i.add_argument("--spec", required=True)
    i.add_argument("--raw", default="")
    i.add_argument("--encoding", default="latin-1")
    i.add_argument("--no-recursive", action="store_true")
    i.set_defaults(func=cmd_inspect)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
