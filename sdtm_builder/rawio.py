"""Local raw-dataset discovery and loading. Filesystem only — never the network.

A RawStore indexes a folder of datasets by normalised name so the mapping spec's
`raw.<dataset>.<column>` tokens resolve regardless of file extension, letter case, or
punctuation. Column names are upper-cased on load, which is the convention the Designer
spec's Input Variables are written against.
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .util import norm_key, upper

READERS = {
    ".sas7bdat": "sas7bdat",
    ".xpt": "xport",
    ".csv": "csv",
    ".txt": "csv",
    ".tsv": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".xls": "excel",
    ".json": "json",
}


@dataclass
class DatasetRef:
    """One loadable dataset on disk."""
    name: str            # normalised lookup key, e.g. 'ae_log'
    path: Path
    kind: str            # reader key
    sheet: str | None = None
    label: str = ""


# Extract files rarely carry the bare domain name. These patterns strip the decoration an
# EDC export adds, so a spec that says `raw.ae` still finds `ae_raw_20260522_163947.csv`,
# and `raw.lb` finds `rawlb1`, `rawlb2`, `rawlb3`.
ALIAS_STRIP = (
    re.compile(r"_raw(?:_\d+)*$"),        # ae_raw_20260522_163947 -> ae
    re.compile(r"_\d{6,}(?:_\d+)*$"),     # ae_20260522_163947     -> ae
    re.compile(r"^raw_?"),                # rawlb1                 -> lb1
    re.compile(r"_?(?:extract|export|dataset|data|final|prod|snapshot)$"),
)
TRAILING_DIGITS = re.compile(r"\d+$")


def _aliases(key: str) -> list[str]:
    """Every name a spec might plausibly use for this dataset, most specific first."""
    out, seen = [], set()

    def add(v: str) -> None:
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    add(key)
    cur = key
    for _ in range(4):                     # peel the decorations, in any order
        nxt = cur
        for pat in ALIAS_STRIP:
            nxt = pat.sub("", nxt).strip("_")
        if nxt == cur or not nxt:
            break
        add(nxt)
        cur = nxt
    stem = TRAILING_DIGITS.sub("", cur).strip("_")   # lb1 -> lb (a split form)
    add(stem)
    return out


@dataclass
class RawStore:
    """Index of a raw-data folder, with a small load cache."""
    root: Path
    refs: dict[str, DatasetRef] = field(default_factory=dict)
    alias: dict[str, list[str]] = field(default_factory=dict)
    _cache: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)
    encoding: str = "latin-1"

    # ── discovery ───────────────────────────────────────────────────────────
    @classmethod
    def discover(cls, root: str | Path, recursive: bool = True,
                 encoding: str = "latin-1") -> "RawStore":
        p = Path(root)
        if not p.is_dir():
            raise NotADirectoryError(f"raw data folder not found: {p}")
        store = cls(root=p, encoding=encoding)
        files = sorted(p.rglob("*") if recursive else p.glob("*"))
        for f in files:
            if not f.is_file() or f.name.startswith((".", "~$")):
                continue
            kind = READERS.get(f.suffix.lower())
            if not kind:
                continue
            if kind == "excel":
                for sheet in _excel_sheets(f):
                    key = norm_key(sheet) if len(_excel_sheets(f)) > 1 else norm_key(f.stem)
                    store._add(DatasetRef(name=key, path=f, kind=kind, sheet=sheet))
            else:
                store._add(DatasetRef(name=norm_key(f.stem), path=f, kind=kind))
        return store

    def _add(self, ref: DatasetRef) -> None:
        # first file wins; a duplicate name in a second folder is reported, not merged
        if ref.name not in self.refs:
            self.refs[ref.name] = ref
            for a in _aliases(ref.name):
                bucket = self.alias.setdefault(a, [])
                if ref.name not in bucket:
                    bucket.append(ref.name)

    # ── lookup ──────────────────────────────────────────────────────────────
    def resolve(self, name: str) -> str | None:
        """A spec dataset name -> the actual store key. Tolerates case and punctuation, the
        decoration an EDC export adds to file names, and the 'ex_iv' vs 'ex_iv_onc' variant
        pattern. When several datasets share the name (lb1/lb2/lb3), the first is returned;
        use resolve_all() to get them all."""
        found = self.resolve_all(name)
        return found[0] if found else None

    def resolve_all(self, name: str) -> list[str]:
        """Every store key a spec name could mean, in a stable order. Split forms of one
        collection (rawlb1, rawlb2, rawlb3 for `raw.lb`) all come back, so the caller can
        stack them into a single record source."""
        key = norm_key(name)
        if not key:
            return []
        if key in self.refs:
            return [key]
        if key in self.alias:
            return sorted(self.alias[key])
        starts = sorted(k for k in self.refs if k.startswith(key + "_"))
        if starts:
            return starts
        ends = sorted(k for k in self.refs if k.endswith("_" + key))
        if ends:
            return ends
        # last resort: the alias index the other way round (spec name is the decorated one)
        for alias, keys in self.alias.items():
            if key.startswith(alias + "_") or key == alias:
                return sorted(keys)
        return []

    def has(self, name: str) -> bool:
        return self.resolve(name) is not None

    def get(self, name: str) -> pd.DataFrame:
        """Load a dataset by (possibly approximate) name. Raises KeyError if absent."""
        key = self.resolve(name)
        if key is None:
            raise KeyError(f"raw dataset '{name}' not found in {self.root}")
        if key not in self._cache:
            self._cache[key] = _read(self.refs[key], self.encoding)
        return self._cache[key]

    def columns(self, name: str) -> set[str]:
        try:
            return set(self.get(name).columns)
        except (KeyError, OSError, ValueError):
            return set()

    def schema(self) -> dict[str, list[str]]:
        """{dataset: [columns]} for every dataset that loads. Used by the spec audit."""
        out: dict[str, list[str]] = {}
        for key in sorted(self.refs):
            try:
                out[key] = list(self.get(key).columns)
            except Exception:                        # noqa: BLE001 - reported by audit
                out[key] = []
        return out

    def put(self, name: str, df: pd.DataFrame) -> None:
        """Register an in-memory frame (a prep-step output) under a lookup name."""
        key = norm_key(name)
        self._cache[key] = df
        self.refs.setdefault(key, DatasetRef(name=key, path=self.root / f"<memory:{key}>",
                                             kind="memory"))


def _excel_sheets(path: Path) -> list[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return list(pd.ExcelFile(path).sheet_names)
    except Exception:                                # noqa: BLE001
        return []


def _read(ref: DatasetRef, encoding: str) -> pd.DataFrame:
    """Read one dataset off disk and normalise it for the build engine."""
    p = ref.path
    if ref.kind == "sas7bdat":
        import pyreadstat
        df, _meta = pyreadstat.read_sas7bdat(str(p), encoding=encoding)
    elif ref.kind == "xport":
        import pyreadstat
        df, _meta = pyreadstat.read_xport(str(p))
    elif ref.kind == "parquet":
        df = pd.read_parquet(p)
    elif ref.kind == "excel":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = pd.read_excel(p, sheet_name=ref.sheet or 0, dtype=object)
    elif ref.kind == "json":
        df = pd.read_json(p)
    else:
        sep = "\t" if p.suffix.lower() == ".tsv" else None
        df = pd.read_csv(p, sep=sep, engine="python", dtype=object,
                         encoding=encoding, keep_default_na=True)
    df = df.copy()
    df.columns = [upper(c) for c in df.columns]
    return df.reset_index(drop=True)


def read_dataset(path: str | Path, encoding: str = "latin-1",
                 sheet: str | None = None) -> pd.DataFrame:
    """Read a single dataset file directly (used for vendor SDTM comparison)."""
    p = Path(path)
    kind = READERS.get(p.suffix.lower())
    if not kind:
        raise ValueError(f"unsupported dataset format: {p.suffix} ({p})")
    return _read(DatasetRef(name=norm_key(p.stem), path=p, kind=kind, sheet=sheet), encoding)
