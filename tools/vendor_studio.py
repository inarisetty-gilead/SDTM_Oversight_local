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
           "program_gen", "rule_library", "build_safe"]
# the SAS runtime lives once, in sdtm_builder/sas — the studio package aliases it
SAS_ALIASES = {"sas_runtime": "runtime", "sas_expr": "expr", "sas_functions": "functions"}

HERE = Path(__file__).resolve().parents[1]
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


def main() -> None:
    container = Path(sys.argv[1] if len(sys.argv) > 1 else "../sdtm-studio/Container").resolve()
    src = container / "sdtm_api" / "src"
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
