# Extracted from sdtm-studio sdtm_api/src/mapping_engine_spec.py @ fad836c by tools/vendor_studio.py:
# only _DOMAIN_LABELS, _is_dropped_qnam, _build_toc — the rest of that file is not part of the offline build.
# Do not edit here — change the studio and re-vendor.

_DOMAIN_LABELS = {
    "DM": "Demographics", "AE": "Adverse Events", "CM": "Concomitant Medications",
    "EX": "Exposure", "EC": "Exposure as Collected", "LB": "Laboratory Test Results",
    "VS": "Vital Signs", "EG": "ECG Test Results", "MH": "Medical History",
    "DS": "Disposition", "DV": "Protocol Deviations", "SV": "Subject Visits",
    "SE": "Subject Elements", "PR": "Procedures", "PE": "Physical Examination",
    "QS": "Questionnaires", "RS": "Disease Response", "TU": "Tumor Identification",
    "TR": "Tumor Results", "FA": "Findings About", "PC": "Pharmacokinetics Concentrations",
    "PP": "Pharmacokinetics Parameters", "IE": "Inclusion/Exclusion Exceptions",
    "CO": "Comments", "SU": "Substance Use", "DA": "Drug Accountability",
    "MB": "Microbiology", "IS": "Immunogenicity Specimen", "DD": "Death Details",
    "CE": "Clinical Events", "HO": "Healthcare Encounters", "ML": "Meal Data",
    "RP": "Reproductive System", "SC": "Subject Characteristics",
}


def _is_dropped_qnam(r) -> bool:
    """A DROP supplemental-qualifier row (Dataset=QNAM) — not part of the study, so it must not
    count toward the domain's coverage denominator."""
    return (str(r.get("Dataset") or "").strip().upper() == "QNAM"
            and str(r.get("Mapping Action") or "").strip().upper() == "DROP")


def _build_toc(specs: dict) -> list:
    toc = []
    for dom, rows in specs.items():
        counted = [r for r in rows if not _is_dropped_qnam(r)]
        mapped = sum(1 for r in counted if r.get("_mapped"))
        toc.append({
            "Dataset": dom, "Label": _DOMAIN_LABELS.get(dom.upper(), ""),
            "in_study": "Y" if mapped else "",
            "mapped_vars": mapped, "total_vars": len(counted),
        })
    toc.sort(key=lambda t: (t["in_study"] != "Y", -t["mapped_vars"]))
    return toc
