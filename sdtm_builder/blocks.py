"""The structured mapping unit the build engine executes.

A Block is a *decision*, not code. Every block is executed by a named function in
`ops.py`; nothing is compiled or exec()'d. A block that cannot be resolved
deterministically carries mtype='unmapped' plus a human-readable `reason`, and the
variable is reported as NOT_BUILT rather than silently emitted empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# how a variable's value is produced
MTYPES = ("constant", "assign", "sequence", "derived", "drop", "unmapped")

# derived recipes this engine implements deterministically
RECIPES = (
    "iso_date",       # partial-aware ISO 8601 from raw date/time (or Y/M/D component) columns
    "study_day",      # --DY = event --DTC vs a DM reference date
    "lobxfl",         # last observation before exposure flag
    "concat",         # join columns with a separator
    "date_extreme",   # earliest/latest date per subject across datasets
    "constant",       # literal value
    "copy_var",       # copy a sibling SDTM variable in the same domain
    "sdtm_ref",       # pull a variable from another already-built SDTM domain, by USUBJID
    "studyid",        # STUDYID from the raw data or the --studyid option
    "fn",             # one SAS-style character/numeric function
    "cond",           # if / else-if / else
    "pipeline",       # chain of the above on one variable
)


@dataclass
class Block:
    """One SDTM variable's mapping decision."""
    variable: str
    domain: str
    mtype: str = "unmapped"
    dataset: str = ""
    column: str = ""
    value: str = ""
    recipe: str = ""
    args: dict = field(default_factory=dict)

    # spec provenance, carried through to the manifest
    label: str = ""
    action: str = ""
    input_variables: str = ""
    mapping_rule: str = ""
    sas_code: str = ""
    codelist: str = ""
    role: str = ""
    origin: str = ""
    type: str = ""
    length: str = ""
    order: int = 0
    sheet_row: int = 0

    # supplemental qualifier (Dataset == 'QNAM' in the spec)
    supp: bool = False
    qlabel: str = ""
    qorig: str = ""

    # user edit provenance — an edit is a deliberate deviation from the mapping spec and is
    # recorded as such, so a comparison run against edited mappings is never mistaken for an
    # independent one
    edited: bool = False
    edit_note: str = ""
    # how the source was decided: the spec's own text, a name-similarity guess, or a hand edit
    method_source: str = "spec"
    confidence: int = 100
    spec_method: str = ""       # what the spec alone would have produced

    # build outcome, filled in by the engine
    status: str = "pending"     # built | dropped | not_built | error
    reason: str = ""
    method: str = ""            # short provenance string, e.g. "ASSIGN ae.AETERM"
    error: str = ""

    def describe_source(self) -> str:
        if self.mtype == "constant":
            return f'constant "{self.value}"'
        if self.mtype == "assign":
            return f"{self.dataset}.{self.column}"
        if self.mtype == "sequence":
            return "sequence"
        if self.mtype == "derived":
            return f"derived:{self.recipe}" if self.recipe else "derived"
        return self.mtype
