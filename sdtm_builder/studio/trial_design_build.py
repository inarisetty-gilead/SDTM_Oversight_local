# Vendored from sdtm-studio sdtm_api/src/trial_design_build.py @ fad836c by tools/vendor_studio.py.
# Do not edit here — change the studio and re-vendor.
"""Bridge the trial-design store into the SDTM build.

Trial-design content (arms / elements / eligibility / visits / summary) is captured separately in
the `trial_design` table by protocol extraction — it never flows through the raw→SDTM build engine
because it has no raw source. This module turns that content into real built SDTM trial-design
datasets (TA/TE/TI/TV/TS) in `sdtm_output`, and marks the matching mapping-spec rows as
trial-design-sourced, so the domains appear in SDTM Output, count as in-study in the Mapping Spec
TOC (Active=Y via the always-active rule), and are carried into the delivery/export bundle — exactly
like a raw-sourced domain.
"""
import json
import logging

import pandas as pd

from .db import get_db

logger = logging.getLogger(__name__)

# trial_design.design_type → SDTM trial-design domain. (soa/schedule has no submission domain.)
TD_DESIGN_DOMAIN = {"arms": "TA", "elements": "TE", "eligibility": "TI",
                    "visits": "TV", "summary": "TS", "td": "TD", "tm": "TM"}
# --SEQ variable to auto-number when the source rows don't carry it.
_SEQ_VAR = {"TS": "TSSEQ"}


def _latest_td_rows(db, study_id: str) -> dict:
    """Latest-version rows per design_type, migrated to SDTM variable keys."""
    rows = db.execute(
        "SELECT design_type, version, data FROM trial_design WHERE study_id=%s "
        "ORDER BY design_type, version DESC", (study_id,)) or []
    from .routers.trial_design import _migrate_td
    out: dict = {}
    for r in rows:
        dt = r.get("design_type")
        if dt in out:                       # ORDER BY version DESC → first seen is newest
            continue
        data = r.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = []
        out[dt] = _migrate_td(dt, data) if isinstance(data, list) else []
    return out


def _domain_frame(domain: str, spec_rows: list, td_rows: list, sid: str):
    """Build the domain DataFrame from trial-design rows, columns ordered by the spec-shell
    variables (STUDYID/DOMAIN first) and limited to variables that actually carry data.
    Returns (df, populated_vars)."""
    order = [str(r.get("Variable")).upper() for r in (spec_rows or []) if r.get("Variable")]
    if not order:
        order = ["STUDYID", "DOMAIN"] + sorted(
            {str(k).upper() for r in td_rows for k in r if not str(k).startswith("_")})
    present = {str(k).upper() for r in td_rows for k, v in r.items()
               if not str(k).startswith("_") and v not in (None, "")}
    seq = _SEQ_VAR.get(domain)
    # The --SEQ variable is exempt from the `present` filter: it is Required,
    # and it is auto-numbered below precisely BECAUSE the source rows carry no
    # value for it. Filtering it out first meant the numbering only ever fired
    # for rows that already had a sequence, and TS shipped without TSSEQ.
    cols = [v for v in order if v in ("STUDYID", "DOMAIN") or v == seq or v in present]
    if seq and seq not in cols:
        cols.insert(cols.index("DOMAIN") + 1 if "DOMAIN" in cols else 0, seq)
    recs = []
    for i, r in enumerate(td_rows, start=1):
        up = {str(k).upper(): v for k, v in r.items() if not str(k).startswith("_")}
        rec = {}
        for c in cols:
            if c == "STUDYID":
                rec[c] = sid
            elif c == "DOMAIN":
                rec[c] = domain
            else:
                rec[c] = up.get(c)
        if seq and seq in cols and rec.get(seq) in (None, ""):
            rec[seq] = i
        recs.append(rec)
    df = pd.DataFrame(recs, columns=cols)
    # The auto-numbered --SEQ carries data in the built dataset even though no
    # source row supplied it, so its spec row counts as trial-design-sourced.
    populated = [c for c in cols if c not in ("STUDYID", "DOMAIN")
                 and (c in present or c == seq)]
    return df, populated


def build_trial_design(study_id: str, spec: dict | None = None, persist: bool = True) -> dict:
    """Build TA/TE/TI/TV/TS datasets from the trial_design store into sdtm_output.
    Returns {domain: {"rows": n, "populated_vars": [...]}}. Does not mutate the spec."""
    from .build_engine import _persist, _studyid
    db = get_db()
    td = _latest_td_rows(db, study_id)
    specs = (spec or {}).get("specs") or {}
    sid = _studyid(study_id)
    out: dict = {}
    for design_type, domain in TD_DESIGN_DOMAIN.items():
        rows = td.get(design_type)
        if not rows:
            continue
        spec_rows = specs.get(domain) or specs.get(domain.upper()) or []
        df, populated = _domain_frame(domain, spec_rows, rows, sid)
        if not len(df):
            continue
        if persist:
            try:
                _persist(study_id, domain, df)
            except Exception as exc:  # noqa: BLE001
                logger.warning("trial-design persist failed for %s: %s", domain, exc)
                continue
        out[domain] = {"rows": int(len(df)), "populated_vars": populated}
    return out


def mark_trial_design_specs(specs: dict, built: dict) -> int:
    """Mark spec rows for built trial-design domains as trial-design-sourced so they count as
    in-study (TOC) and export their spec sheet. Returns the number of rows newly marked."""
    changed = 0
    for domain, info in (built or {}).items():
        rows = specs.get(domain) or specs.get(domain.upper())
        if not rows:
            continue
        populated = {str(v).upper() for v in (info.get("populated_vars") or [])}
        for r in rows:
            var = str(r.get("Variable") or "").upper()
            if var in ("STUDYID", "DOMAIN") or var in populated:
                if not r.get("_mapped"):
                    r["_mapped"] = True
                    r["_method"] = "TRIAL_DESIGN"
                    if not str(r.get("Input Variables") or "").strip():
                        r["Input Variables"] = f"td.{domain.lower()}"
                    changed += 1
    return changed


def sync_trial_design(study_id: str, user_email: str = "system", persist: bool = True) -> dict:
    """Build the trial-design datasets AND mark their spec rows in one shot. Safe to call after
    trial-design generation or as part of Auto-Build. Best-effort on the spec-mark step."""
    from .build_engine import load_spec
    spec = load_spec(study_id)
    built = build_trial_design(study_id, spec, persist)
    if persist and built:
        try:
            from .mapping_engine_spec import _build_toc
            from .routers.mapping_run import _save_delta

            def _mut(doc):
                specs = doc.get("specs") or {}
                if mark_trial_design_specs(specs, built):
                    doc["toc"] = _build_toc(specs)

            _save_delta(get_db(), study_id, _mut, user_email)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trial-design spec mark skipped: %s", exc)
    return built
