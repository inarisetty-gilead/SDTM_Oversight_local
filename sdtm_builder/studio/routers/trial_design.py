# Extracted from sdtm-studio sdtm_api/src/routers/trial_design.py @ fad836c by tools/vendor_studio.py:
# only _LEGACY_ALIASES, _LEGACY_DROP, _migrate_td_row, _migrate_td — the rest of that file is not part of the offline build.
# Do not edit here — change the studio and re-vendor.

_LEGACY_ALIASES = {
    "arms": {"arm_id": "ARMCD", "arm_code": "ARMCD", "arm_label": "ARM", "arm_name": "ARM"},
    "elements": {"element_code": "ETCD", "element_name": "ELEMENT", "start_rule": "TESTRL", "end_rule": "TEENRL"},
    "visits": {"visit_id": "VISITNUM", "visit_num": "VISITNUM", "visit_name": "VISIT", "visit_day": "VISITDY", "day": "VISITDY", "study_day": "VISITDY", "arm_code": "ARMCD", "arm_name": "ARM"},
    "schedule": {"visit_id": "VISITNUM", "visit_num": "VISITNUM", "visit_name": "VISIT", "visit_day": "VISITDY", "day": "VISITDY", "study_day": "VISITDY", "arm_code": "ARMCD", "arm_name": "ARM"},
    "eligibility": {"criterion_id": "IETESTCD", "criterion_type": "IECAT", "type": "IECAT", "text": "IETEST", "description": "IETEST", "criterion_text": "IETEST", "category": "IESCAT"},
    "summary": {"parameter": "TSPARMCD", "value": "TSVAL", "description": "TSPARM"},
}


_LEGACY_DROP = {"arm_type", "intervention_name", "dose", "route", "frequency", "duration", "visit_window", "window", "phase", "procedures", "continuous"}


def _migrate_td_row(design_type, row):
    if not isinstance(row, dict):
        return row
    alias = _LEGACY_ALIASES.get(design_type, {})
    out = {}

    def set_pref(k, v):
        if v in (None, ""):
            out.setdefault(k, v)
        else:
            out[k] = v

    for k, v in row.items():
        if str(k).startswith("_"):
            out[k] = v
        elif k in alias:
            set_pref(alias[k], v)
        elif k in _LEGACY_DROP:
            continue
        else:
            set_pref(k, v)
    return out


def _migrate_td(design_type, data):
    """Map legacy keys → SDTM for a design's data (list of row dicts)."""
    if isinstance(data, list):
        return [_migrate_td_row(design_type, r) for r in data]
    return data
