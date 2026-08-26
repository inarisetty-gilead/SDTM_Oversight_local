"""Write built datasets and the build manifest to disk."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .blocks import Block
from .util import s, upper

# SAS transport v5 limits — exceeded names/labels are truncated, and the truncation reported
XPT_NAME_LEN = 8
XPT_LABEL_LEN = 40


def _for_export(df: pd.DataFrame, numeric_as_int: bool = False) -> pd.DataFrame:
    """Pandas nullable dtypes -> the plain object/float types the writers accept."""
    out = pd.DataFrame(index=df.index)
    for c in df.columns:
        ser = df[c]
        if isinstance(ser.dtype, pd.StringDtype) or ser.dtype == object:
            out[c] = ser.astype(object).where(ser.notna(), "")
        elif pd.api.types.is_integer_dtype(ser):
            # keep whole numbers whole — a --SEQ of 1 must not be written as 1.0
            num = pd.to_numeric(ser, errors="coerce")
            out[c] = num.astype("Int64") if numeric_as_int else num.astype(float)
        elif pd.api.types.is_float_dtype(ser):
            out[c] = pd.to_numeric(ser, errors="coerce").astype(float)
        elif pd.api.types.is_bool_dtype(ser):
            out[c] = ser.astype(float)
        else:
            out[c] = ser.astype(str).where(ser.notna(), "")
    return out


def write_dataset(df: pd.DataFrame, out_dir: str | Path, name: str, fmt: str = "csv",
                  labels: dict[str, str] | None = None,
                  dataset_label: str = "") -> tuple[Path, list[str]]:
    """Write one dataset. Returns (path, warnings)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    base = upper(name)

    if fmt == "csv":
        p = out_dir / f"{base}.csv"
        _for_export(df, numeric_as_int=True).to_csv(p, index=False, na_rep="")
        return p, warnings
    if fmt == "parquet":
        p = out_dir / f"{base}.parquet"
        df.to_parquet(p, index=False)
        return p, warnings
    if fmt == "xpt":
        import pyreadstat
        p = out_dir / f"{base.lower()}.xpt"
        ex = _for_export(df)
        renames = {}
        for c in ex.columns:
            if len(c) > XPT_NAME_LEN:
                renames[c] = c[:XPT_NAME_LEN]
                warnings.append(f"{base}: variable {c} truncated to {c[:XPT_NAME_LEN]} for XPT v5")
        if renames:
            ex = ex.rename(columns=renames)
        col_labels = None
        if labels:
            col_labels = []
            for c in ex.columns:
                lab = s(labels.get(c, ""))[:XPT_LABEL_LEN]
                col_labels.append(lab or None)
        if len(base) > XPT_NAME_LEN:
            warnings.append(f"dataset name {base} exceeds {XPT_NAME_LEN} characters for XPT v5")
        pyreadstat.write_xport(ex, str(p), file_format_version=5,
                               table_name=base[:XPT_NAME_LEN],
                               file_label=s(dataset_label)[:XPT_LABEL_LEN] or base,
                               column_labels=col_labels)
        return p, warnings
    raise ValueError(f"unsupported output format: {fmt}")


def block_records(domain: str, blocks: list[Block]) -> list[dict]:
    """One manifest record per spec variable — the audit trail for the build."""
    out = []
    for b in blocks:
        out.append({
            "domain": domain,
            "variable": b.variable,
            "label": b.label,
            "status": b.status,
            "mapped_by": {"edit": "hand edit", "name_match": "name match (guess)"}
                         .get(b.method_source, "mapping spec"),
            "confidence": b.confidence,
            "edit_note": b.edit_note,
            "spec_would_have_been": b.spec_method,
            "target": f"SUPP{domain}" if b.supp else domain,
            "method": b.method or b.describe_source(),
            "mapping_type": b.mtype,
            "recipe": b.recipe,
            "source_dataset": b.dataset,
            "source_column": b.column,
            "constant_value": b.value,
            "spec_action": b.action,
            "spec_input_variables": b.input_variables,
            "spec_mapping_rule": b.mapping_rule,
            "spec_sas_code": b.sas_code,
            "codelist": b.codelist,
            "origin": b.origin,
            "role": b.role,
            "reason": b.reason,
            "error": b.error,
            "spec_sheet_row": b.sheet_row,
        })
    return out


def write_manifest(results: dict, out_dir: str | Path, meta: dict) -> tuple[Path, Path]:
    """Write build_manifest.json and build_manifest.xlsx."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    domains: list[dict] = []
    for dom, res in results.items():
        records.extend(block_records(dom, res.blocks))
        domains.append({
            "domain": dom, "built": res.ok, "error": res.error,
            "base_dataset": res.base_dataset,
            "rows": 0 if res.dataset is None else len(res.dataset),
            "supp_rows": 0 if res.supp is None else len(res.supp),
            **res.counts, "warnings": res.warnings,
        })

    payload = {"meta": meta, "domains": domains, "variables": records}
    jpath = out_dir / "build_manifest.json"
    jpath.write_text(json.dumps(payload, indent=2, default=str))

    xpath = out_dir / "build_manifest.xlsx"
    with pd.ExcelWriter(xpath, engine="openpyxl") as xw:
        pd.DataFrame([{k: v for k, v in d.items() if k != "warnings"} for d in domains]) \
            .to_excel(xw, sheet_name="Domains", index=False)
        pd.DataFrame(records).to_excel(xw, sheet_name="Variables", index=False)
        unbuilt = [r for r in records if r["status"] in ("not_built", "error")]
        if unbuilt:
            pd.DataFrame(unbuilt).to_excel(xw, sheet_name="Not Built", index=False)
        # Hand edits get their own sheet: a reviewer must be able to see, at a glance, every
        # place this build departed from the mapping spec.
        edited = [r for r in records if r["mapped_by"] == "hand edit"]
        if edited:
            pd.DataFrame(edited).to_excel(xw, sheet_name="Hand Edits", index=False)
        # Name-matched variables are guesses. They get their own sheet so a reviewer can
        # separate "the vendor followed the spec" from "the vendor agrees with our guess".
        guessed = [r for r in records if r["mapped_by"] == "name match (guess)"]
        if guessed:
            pd.DataFrame(guessed).to_excel(xw, sheet_name="Name Matched", index=False)
    return jpath, xpath
