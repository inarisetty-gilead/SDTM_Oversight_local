"""Copy SDTM Studio's deterministic build engine into sdtm_builder/studio, unchanged.

    python tools/vendor_studio.py /path/to/sdtm-studio/Container

Testers use Oversight in place of the full application, so the build must be the studio's own
code, not a re-implementation. The modules below are copied byte for byte; the only edits are
the import lines (`from src.x` → `from .x`, botocore's ClientError → the local one), so a diff
against the studio shows nothing else. Storage and the database are swapped underneath them by
the hand-written modules in sdtm_builder/studio (aws_clients, db, config and a few one-function
stand-ins), which keep everything on this machine.

Re-run after the studio changes; then run tests/test_studio_equivalence.py.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# the deterministic build: Domain Build, Auto-Build, prep, construction standards, CT, the
# biomedical concepts, the rule library, the safe expression evaluator and program generation
MODULES = ["build_exec", "build_engine", "prep_engine", "sdtm_knowledge", "biomedical_concepts",
           "program_gen", "rule_library", "build_safe", "trial_design_build"]
# definitions lifted out of studio files that are otherwise web routers (FastAPI, auth): only
# these names are copied, each as its exact source text
EXTRACTS = {"routers/trial_design.py": ["_LEGACY_ALIASES", "_LEGACY_DROP", "_migrate_td_row", "_migrate_td"],
            "mapping_engine_spec.py": ["_DOMAIN_LABELS", "_is_dropped_qnam", "_build_toc"]}
# an extract lands in the same relative path, except where a hand-written stand-in owns that name
EXTRACT_DEST = {"mapping_engine_spec.py": "mapping_engine_spec_parts.py"}
# the SAS runtime lives once, in sdtm_builder/sas — the studio package aliases it
SAS_ALIASES = {"sas_runtime": "runtime", "sas_expr": "expr", "sas_functions": "functions"}

HERE = Path(__file__).resolve().parents[1]
SRC_ROOT = Path(".")
DEST = HERE / "sdtm_builder" / "studio"


def rewrite(text: str) -> str:
    text = re.sub(r"^(\s*)from src import ", r"\1from . import ", text, flags=re.M)
    text = re.sub(r"^(\s*)from src\.", r"\1from .", text, flags=re.M)
    text = re.sub(r"^(\s*)from botocore\.exceptions import ClientError",
                  r"\1from .aws_clients import ClientError", text, flags=re.M)
    leftover = re.findall(r"^\s*(?:from|import) (?:src\b|boto3|botocore|sqlalchemy).*$", text, flags=re.M)
    if leftover:
        raise SystemExit(f"unhandled import(s): {leftover}")
    return text


def extract(src_file: Path, dest_file: Path, names: list[str], commit: str) -> None:
    """Copy only the named top-level definitions of a studio file, as their exact source."""
    import ast
    text = src_file.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    found = {}
    for node in tree.body:
        name = (node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else
                node.targets[0].id if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                else None)
        if name in names:
            found[name] = "".join(lines[node.lineno - 1:node.end_lineno])
    missing = [n for n in names if n not in found]
    if missing:
        raise SystemExit(f"{src_file}: not found: {missing}")
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    if dest_file.parent != DEST:
        (dest_file.parent / "__init__.py").write_text("", encoding="utf-8")
    body = "\n\n".join(found[n].rstrip() + "\n" for n in names)
    dest_file.write_text(
        f"# Extracted from sdtm-studio sdtm_api/src/{src_file.relative_to(SRC_ROOT).as_posix()}"
        f" @ {commit} by tools/vendor_studio.py:\n# only {', '.join(names)} — the rest of that file is not part "
        "of the offline build.\n# Do not edit here — change the studio and re-vendor.\n\n" + body, encoding="utf-8")


def main() -> None:
    container = Path(sys.argv[1] if len(sys.argv) > 1 else "../sdtm-studio/Container").resolve()
    global SRC_ROOT
    src = SRC_ROOT = container / "sdtm_api" / "src"
    if not (src / "build_exec.py").exists():
        raise SystemExit(f"not an sdtm-studio Container folder: {container}")
    try:
        commit = subprocess.run(["git", "-C", str(container), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    DEST.mkdir(parents=True, exist_ok=True)
    for name in MODULES:
        body = rewrite((src / f"{name}.py").read_text(encoding="utf-8"))
        head = (f"# Vendored from sdtm-studio sdtm_api/src/{name}.py @ {commit} by tools/vendor_studio.py.\n"
                "# Do not edit here — change the studio and re-vendor.\n")
        (DEST / f"{name}.py").write_text(head + body, encoding="utf-8")
        print(f"  {name}.py")
    for rel, names in EXTRACTS.items():
        extract(src / rel, DEST / EXTRACT_DEST.get(rel, rel), names, commit)
        print(f"  {rel} ({', '.join(names)})")
    schema = container / "sdtm_api" / "db" / "schema" / "schema_consolidated.sql"
    (DEST / "schema").mkdir(exist_ok=True)
    (DEST / "schema" / "schema_consolidated.sql").write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
    rules = container / "sdtm_api" / "reference" / "rule_library"
    (DEST / "rule_library_data").mkdir(exist_ok=True)
    for f in sorted(rules.glob("*.json")):
        (DEST / "rule_library_data" / f.name).write_bytes(f.read_bytes())
    (DEST / "VENDORED_FROM").write_text(f"sdtm-studio {commit}\n", encoding="utf-8")
    print(f"vendored {len(MODULES)} modules from sdtm-studio @ {commit}")


if __name__ == "__main__":
    main()
