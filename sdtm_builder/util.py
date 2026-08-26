"""Small shared helpers — value cleaning and name normalisation."""
from __future__ import annotations

import re
import unicodedata

import pandas as pd

# the many spellings of "no value" that survive a trip through Excel + pandas
NULLISH = {"", "nan", "none", "null", "na", "n/a", "<na>", "nat", "."}


def clean(val):
    """Excel/pandas value -> plain Python value, with missings collapsed to None.
    Deliberately conservative: 0 and False are values, not missings."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, str):
        t = val.strip()
        return None if t.lower() in NULLISH else t
    return val


def s(val) -> str:
    """clean() as a string, never None."""
    v = clean(val)
    return "" if v is None else str(v).strip()


def upper(val) -> str:
    return s(val).upper()


def lower(val) -> str:
    return s(val).lower()


def norm_key(name) -> str:
    """Case- and punctuation-insensitive key for a dataset / column / sheet name:
    'AE Log ' -> 'ae_log', 'AESTDAT-RAW' -> 'aestdat_raw'."""
    t = unicodedata.normalize("NFKD", str(name or ""))
    return re.sub(r"[^0-9A-Za-z]+", "_", t).strip("_").lower()


def as_int(x, default=0) -> int:
    try:
        return int(float(str(x)))
    except (TypeError, ValueError):
        return default


def as_float(x, default=0.0) -> float:
    try:
        return float(str(x))
    except (TypeError, ValueError):
        return default


def str_series(ser: pd.Series) -> pd.Series:
    """Text view of a Series with SAS-style STRIP applied; missings preserved as <NA>."""
    return ser.astype("string").str.strip()


def blank_mask(ser: pd.Series) -> pd.Series:
    """Element-wise 'this cell carries no value' for a text-ish Series."""
    t = ser.astype("string").str.strip()
    return (t.isna() | t.str.lower().isin(sorted(NULLISH))).fillna(True)


def empty_str_series(index) -> pd.Series:
    return pd.Series(pd.NA, index=index, dtype="string")


def is_blank(val) -> bool:
    return s(val) == ""
