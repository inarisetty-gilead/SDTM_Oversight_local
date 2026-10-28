"""The findings transpose a mapping spec spells out in its --TESTCD Mapping Rule.

Gilead specs describe a wide form to transpose in prose rather than as a list of columns:

    Transpose the raw dataset vsperf …
    if height_raw ne ' ' then _vstestcd = "HEIGHT";
    if weight_raw ne ' ' then _vstestcd = "WEIGHT";
    if vsperf_std = 'N' then _vstestcd = "VSALL";

with the test names in the --TEST rule (`if vstestcd is "HEIGHT" then _vstest = "Height"`), the
value in the --ORRES rule (`strip(height_raw)`, or `%m_sdtm_isodate(srcdt=…)` for a date), and the
not-done status in the --STAT rule. Read literally, every `if <column> ne ' '` is one test, taken
from whichever of this study's forms holds that column; a visit whose form is marked not done
gives ONE `<DOM>ALL` record (TEST, STAT, REASND, date) instead of its tests.

This is the transpose that column names alone cannot give: the test CODE comes from the rule,
not the column name (PREGDAT_RAW is PCONFDTC), the test NAME is the spec's, not a copy of the
code, and the not-done visit is a record rather than a silent gap. Fewer than two tests found in
the study's forms means the rule does not describe a transpose here, and nothing is proposed.
"""
from __future__ import annotations

import re

import pandas as pd

from .translate import raw_refs
from .util import blank_mask, norm_key, s, upper

_Q = r"""['"]"""
_NE_BLANK = rf"(?:ne|\^=|~=|!=|<>)\s*(?:{_Q}\s*{_Q}|\.)"        # ne ' ' (character) or ne . (numeric)
_EXTRA = r"(?:\s+(?:or|and)\s+[^;]*?)?"                         # "… or daretyn_std = 'N'"
SUBJECT_KEYS = ("USUBJID", "X_SUBJID", "SUBJID", "SUBJECTID", "SUBJECT_ID")
VISIT_KEYS = ("FOLDER", "FOLDERNAME", "INSTANCENAME", "VISIT", "VISITNUM", "FOLDERSEQ")
UNIT_SUFFIXES = ("U", "_UN", "_UNIT", "U_STD", "_UN_STD")
STRESC_SUFFIXES = ("", "_STD", "_STD_RAW")
STRESU_SUFFIXES = ("_STD_UN", "U", "_UN", "_STD_UNIT")


def rule_text(b) -> str:
    if b is None:
        return ""
    return " ".join(x for x in (s(b.mapping_rule), s(b.sas_code)) if x.lower() != "nan")


def _is_date(col: str) -> bool:
    return bool(re.search(r"DAT|DTC|DATE", col)) and not re.search(r"TIM", col)


def _raw_forms(store) -> list[str]:
    from .prep import is_raw_file
    return sorted(k for k in store.refs if is_raw_file(store, k))


def detect(blocks, store, domain: str):
    """A PrepStep('transpose_rule') when the --TESTCD rule describes a transpose this study's
    forms can carry out, else None."""
    from .prep import PrepStep, output_name
    dom = upper(domain)
    p = dom.lower()
    row = {b.variable: b for b in blocks}
    tc = row.get(f"{dom}TESTCD")
    text = rule_text(tc)
    if not text or "transpose" not in text.lower():
        return None
    # "… else _pctestcd = pctestcd": the raw is already one record per test with extra tests
    # added — transposing only those would drop every other record, so it is not this pattern
    if re.search(rf"else\s+_?{p}testcd\s*=\s*[a-z_]", text, re.I):
        return None
    pairs = re.findall(rf"if\s+([a-z_][a-z0-9_]*)\s+{_NE_BLANK}{_EXTRA}\s*then\s+_?{p}testcd\s*=\s*"
                       rf"{_Q}\s*([A-Za-z0-9_]+)\s*{_Q}", text, re.I)
    pairs += re.findall(rf"if\s+not\s+missing\(\s*([a-z_][a-z0-9_]*)\s*\)\s*then\s+_?{p}testcd\s*=\s*"
                        rf"{_Q}\s*([A-Za-z0-9_]+)\s*{_Q}", text, re.I)
    if len(pairs) < 2:
        return None

    is_code = rf"if\s+_?{p}testcd\s+(?:is|=|eq)\s*{_Q}\s*([A-Za-z0-9_]+)\s*{_Q}\s*then\s+_?{p}"
    names = {c.upper(): n.strip().strip("'\"").strip() for c, n in
             re.findall(is_code + rf"test\s*=\s*{_Q}(.+?){_Q}\s*;", rule_text(row.get(f"{dom}TEST")), re.I)}
    orres_rules = {c.upper(): e for c, e in
                   re.findall(is_code + r"orres\s*=\s*([^;]+);", rule_text(row.get(f"{dom}ORRES")), re.I)}

    forms = _raw_forms(store)
    cols = {f: {upper(c): c for c in store.columns(f)} for f in forms}
    spec_forms = [store.resolve(ds) for b in blocks for ds, _c in raw_refs(b.input_variables)]
    order = list(dict.fromkeys([f for f in spec_forms if f in cols] + forms))

    by_form: dict[str, list[dict]] = {}
    for col, code in pairs:
        code = code.upper()
        expr = orres_rules.get(code, "")
        m = re.search(r"srcdt\s*=\s*([a-z_][a-z0-9_]*)", expr, re.I) \
            or re.search(r"\(\s*([a-z_][a-z0-9_]*)\s*\)", expr, re.I)
        src_col = upper(m.group(1) if m else col)
        # the column as named, else without its _RAW suffix (DANUMDP_RAW is DANUMDP in some studies)
        cands = list(dict.fromkeys(x for n in (src_col, upper(col)) for x in (n, re.sub(r"_RAW$", "", n))))
        hit = next(((f, cols[f][n]) for n in cands for f in order if n in cols[f]), None)
        if hit is None:
            continue                      # a test this study did not collect is left out
        f, c = hit
        if any(upper(x["value_col"]) == upper(c) for x in by_form.get(f, [])):
            continue
        by_form.setdefault(f, []).append({"value_col": c, "testcd": code, "test": names.get(code) or code,
                                          "iso": "isodate" in expr.lower()})
    if sum(len(v) for v in by_form.values()) < 2:
        return None

    def spec_cols(var: str) -> set[str]:
        return {c for _ds, c in raw_refs(getattr(row.get(var), "input_variables", ""))}

    unit_src = spec_cols(f"{dom}ORRESU")
    dtc_src = [(store.resolve(ds) or ds, c) for ds, c in raw_refs(getattr(row.get(f"{dom}DTC"), "input_variables", ""))]
    reas_src = [(store.resolve(ds) or ds, c) for ds, c in raw_refs(getattr(row.get(f"{dom}REASND"), "input_variables", ""))]
    not_done = re.findall(rf"if\s+([a-z_][a-z0-9_]*)\s*=\s*{_Q}([^'\"]*){_Q}\s*then\s+_?{p}testcd\s*=\s*"
                          rf"{_Q}\s*{dom}ALL\s*{_Q}", text, re.I)
    stat_value = (re.findall(rf"then\s+_?{p}stat\s*=\s*{_Q}([^'\"]+){_Q}",
                             rule_text(row.get(f"{dom}STAT")), re.I) or ["NOT DONE"])[0].strip().upper()

    form_params, notes = [], []
    for f, ms in by_form.items():
        fc = cols[f]
        for m in ms:
            stem = re.sub(r"_RAW$", "", upper(m["value_col"]))
            units = [stem + sfx for sfx in UNIT_SUFFIXES]
            u = next((x for x in units if x in fc and x in unit_src), None) or next((x for x in units if x in fc), None)
            m["unit_col"] = fc[u] if u else ""
        fp = {"dataset": f, "measures": ms, "date_col": "", "date_from": None, "time_col": "",
              "not_done": [], "reasnd_col": ""}
        # the --DTC rule's date (a date, never its time) — on this form, or merged in from the
        # form that holds it by subject AND visit; never by subject alone
        d = next((fc[c] for ff, c in dtc_src if ff == f and c in fc and _is_date(c)), "")
        if not d:
            other = next(((ff, c) for ff, c in dtc_src if ff != f and _is_date(c) and c in cols.get(ff, {})), None)
            if other:
                ff, c = other
                subj = [k for k in SUBJECT_KEYS if k in fc and k in cols[ff]][:1]
                visit = [k for k in VISIT_KEYS if k in fc and k in cols[ff]]
                if subj and visit:
                    fp["date_from"] = {"dataset": ff, "column": cols[ff][c],
                                       "keys": [fc[k] for k in subj + visit]}
                    notes.append(f"the date comes from {ff}.{cols[ff][c]}, merged by "
                                 f"{' + '.join(subj + visit)}")
        fp["date_col"] = d
        tim = next((fc[c] for ff, c in dtc_src if ff == f and c in fc and re.search(r"TIM", c)), "")
        fp["time_col"] = tim
        fp["not_done"] = [{"column": fc.get(upper(c)) or fc.get(re.sub(r"_STD$", "", upper(c))), "value": v}
                          for c, v in not_done]
        fp["not_done"] = [x for x in fp["not_done"] if x["column"]]
        fp["reasnd_col"] = next((fc[c] for ff, c in reas_src if ff == f and c in fc), "")
        form_params.append(fp)

    # --STRESC / --STRESU: each test's standardised column, named in that variable's spec inputs
    stresc, stresu = {}, {}
    for target, prefer, out in ((f"{dom}STRESC", STRESC_SUFFIXES, stresc),
                                (f"{dom}STRESU", STRESU_SUFFIXES, stresu)):
        named = spec_cols(target)
        for fp in form_params:
            fc = cols[fp["dataset"]]
            for m in fp["measures"]:
                stem = re.sub(r"_RAW$", "", upper(m["value_col"]))
                c = next((stem + sfx for sfx in prefer if stem + sfx in named and stem + sfx in fc), None)
                if c:
                    out[m["testcd"]] = fc[c]

    tests = [m["testcd"] for fp in form_params for m in fp["measures"]]
    note = (f"the {dom}TESTCD Mapping Rule describes a transpose: {len(tests)} tests "
            f"({', '.join(tests)}) from {', '.join(by_form)}, with the test names from the "
            f"{dom}TEST rule")
    if any(fp["not_done"] for fp in form_params):
        note += f"; a visit marked not done gives one {dom}ALL record ({dom}STAT = {stat_value})"
    if notes:
        note += "; " + "; ".join(notes)
    return PrepStep(op="transpose_rule", name=output_name(store, dom), note=note + ".", params={
        "forms": form_params, "testcd_col": f"{dom}TESTCD", "test_col": f"{dom}TEST",
        "orres_col": f"{dom}ORRES", "orresu_col": f"{dom}ORRESU", "dtc_col": f"{dom}DTC",
        "stat_col": f"{dom}STAT", "reasnd_target": f"{dom}REASND", "stat_value": stat_value,
        "all_code": f"{dom}ALL", "all_test": names.get(f"{dom}ALL") or f"{dom}ALL",
        "stresc": stresc, "stresu": stresu, "stresc_col": f"{dom}STRESC", "stresu_col": f"{dom}STRESU"})


def iso_keep_partial(ser: pd.Series) -> pd.Series:
    """A date column as ISO 8601: a full date converted, a partial one (2024, 2024-05) and
    anything unparseable kept as written — never a day invented."""
    t = ser.astype("string").str.strip().fillna("")
    partial = t.str.fullmatch(r"\d{4}(-\d{2})?") | (t == "")
    parsed = pd.to_datetime(t.where(~partial), errors="coerce", format="mixed")
    iso = parsed.dt.strftime("%Y-%m-%d").astype("string")
    return iso.where(iso.notna() & ~partial, t).fillna("")


def _not_done_mask(src: pd.DataFrame, conds: list[dict]) -> pd.Series:
    m = pd.Series(False, index=src.index)
    for c in conds or []:
        col = c.get("column")
        if col not in src.columns:
            continue
        v = upper(c.get("value"))
        alts = {"N": ["N", "NO"], "Y": ["Y", "YES"]}.get(v, [v])
        m |= src[col].astype("string").str.strip().str.upper().isin(alts).fillna(False)
    return m


def apply(step, store) -> pd.DataFrame:
    p = step.params
    tc, tn, orres, orresu = p["testcd_col"], p["test_col"], p["orres_col"], p["orresu_col"]
    dtc, stat, reas = p["dtc_col"], p["stat_col"], p["reasnd_target"]
    parts = []
    for fi, f in enumerate(p.get("forms") or []):
        src = store.get(f["dataset"]).copy().reset_index(drop=True)
        date = None
        if f.get("date_col") and f["date_col"] in src.columns:
            date = src[f["date_col"]]
        elif f.get("date_from"):
            df_ = f["date_from"]
            other = store.get(df_["dataset"])
            keys = [k for k in df_["keys"] if k in src.columns and k in other.columns]
            if keys and df_["column"] in other.columns:
                look = other[keys + [df_["column"]]].copy()
                look = look[~blank_mask(look[df_["column"]])].drop_duplicates(keys)
                look = look.rename(columns={df_["column"]: "__DATE"})
                src = src.merge(look, on=keys, how="left")       # left merge keeps src order
                date = src["__DATE"]
        if date is not None:
            day = iso_keep_partial(date)
            t = f.get("time_col")
            if t and t in src.columns:
                tm = src[t].astype("string").str.strip().fillna("")
                day = day.where(~((day.str.len() == 10) & (tm != "")), day + "T" + tm)
            src["__DTC"] = day
        # a test row still being filled in (no code or no column yet) is not a test
        measures = [m for m in f.get("measures") or [] if s(m.get("value_col")) and s(m.get("testcd"))]
        measure_cols = {m["value_col"] for m in measures} | {m["unit_col"] for m in measures if m.get("unit_col")}
        id_cols = [c for c in src.columns if c not in measure_cols and c != "__DATE" and c != "__DTC"]
        nd = _not_done_mask(src, f.get("not_done"))
        src["__ROW"] = range(len(src))
        done = src[~nd]
        for mi, m in enumerate(measures):
            vcol = m["value_col"]
            if vcol not in done.columns:
                continue
            part = done[id_cols + ["__ROW"]].copy()
            part["__F"], part["__M"] = fi, mi
            part[tc], part[tn] = upper(m["testcd"]), s(m.get("test")) or upper(m["testcd"])
            val = done[vcol].astype("string").str.strip()
            if m.get("iso"):
                val = iso_keep_partial(val)
            part[orres] = val
            u = m.get("unit_col")
            part[orresu] = done[u].astype("string").str.strip() if u and u in done.columns else ""
            for key, out in (("stresc", p.get("stresc_col")), ("stresu", p.get("stresu_col"))):
                col = (p.get(key) or {}).get(upper(m["testcd"]))
                if col and col in done.columns:
                    part[out] = done[col].astype("string").str.strip()
            if "__DTC" in done.columns:
                part[dtc] = done["__DTC"]
            part = part[~blank_mask(part[orres])]                 # a test not taken is not a record
            parts.append(part)
        if nd.any():
            rec = src[nd][id_cols + ["__ROW"]].copy()
            rec["__F"], rec["__M"] = fi, -1
            rec[tc], rec[tn], rec[orres], rec[orresu] = p["all_code"], p["all_test"], "", ""
            rec[stat] = p["stat_value"]
            rc = f.get("reasnd_col")
            if rc and rc in src.columns:
                rec[reas] = src.loc[nd, rc].astype("string").str.strip().str.upper()
            if "__DTC" in src.columns:
                rec[dtc] = src.loc[nd, "__DTC"]
            parts.append(rec)
    if not parts:
        raise ValueError("the transpose found no results in its forms")
    out = pd.concat(parts, ignore_index=True, sort=False)
    out = out.sort_values(["__F", "__ROW", "__M"], kind="stable").drop(columns=["__F", "__ROW", "__M"])
    for c in (stat, reas, dtc, p.get("stresc_col"), p.get("stresu_col")):
        if c in out.columns:
            out[c] = out[c].astype("string").fillna("")
    return out.reset_index(drop=True)


def produced(step) -> list[str]:
    """The SDTM variables this step's output carries, so the domain reads them from it."""
    p = step.params
    out = [p["testcd_col"], p["test_col"], p["orres_col"], p["orresu_col"]]
    forms = p.get("forms") or []
    if any(f.get("date_col") or f.get("date_from") for f in forms):
        out.append(p["dtc_col"])
    if any(f.get("not_done") for f in forms):
        out.append(p["stat_col"])
        if any(f.get("reasnd_col") for f in forms):
            out.append(p["reasnd_target"])
    if p.get("stresc"):
        out.append(p["stresc_col"])
    if p.get("stresu"):
        out.append(p["stresu_col"])
    return out


def sources(step) -> set[str]:
    return {norm_key(f["dataset"]) for f in step.params.get("forms") or []}
