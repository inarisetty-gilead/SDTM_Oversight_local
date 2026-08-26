"""sdtm_builder — deterministic, fully-local SDTM dataset builder and vendor-delivery checker.

No network. No AI. No cloud storage. Given a Designer-format mapping spec and a folder of
raw datasets, it rebuilds SDTM domains and diffs them against a vendor's delivered SDTM.

Design rules (these are why the output can be trusted):
  * Every mapping is executed by a NAMED, typed operation in `ops.py`. Nothing is
    exec()'d from a generated string.
  * Any spec row whose rule cannot be interpreted deterministically is reported as
    NOT_BUILT with a reason. It is never silently guessed at, and never left blank as
    if it had been built.
  * Every built variable carries provenance (method + source) into the build manifest.
"""

__version__ = "0.1.0"
