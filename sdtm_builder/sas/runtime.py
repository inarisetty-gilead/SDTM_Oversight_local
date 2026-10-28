"""SAS DATA-step semantics: the functions, operators, formats and informats a SAS programmer
uses to transform data, implemented once and used by every part of the build.

The engine evaluates a SAS expression through the helpers here, the generated Python program
inlines this module's source and calls the same helpers, and the generated SAS program writes the
expression back as SAS text. So a derivation gives the same value in all three, and the same as
SAS itself:

  - character missing is blank; numeric missing is `.` (None/NaN here);
  - dates are days since 01JAN1960, datetimes and times are seconds — exactly SAS's values;
  - a numeric argument to a character function is converted with BEST12. (right-aligned), and
    `||` does the same, while the CAT family strips it — the well-known SAS behaviour;
  - comparisons treat numeric missing as smaller than every number; `=:` compares prefixes;
  - an invalid argument gives a missing result instead of an error, as SAS does (with a NOTE).

Self-contained (stdlib + pandas/numpy only) because the generated Python program inlines its
source. Every helper is named `_sx_*`. Function metadata for the interface (categories, syntax,
help) lives in `sas.functions`; the behaviour lives only here, in `_SX_F`. Shared with SDTM
Studio, where it was written — keep the two copies in step.
"""

import calendar
import math
import re
import string
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import repeat

import numpy as np
import pandas as pd

_SX_EPOCH = date(1960, 1, 1).toordinal()
_SX_MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_SX_SCAN_DELIMS = " !$%&()*+,-./;<^|"
_SX_PROP_DELIMS = " /-(.\t"


# ── scalar values: char is str ('' missing), numeric is float or None ───────────────────────
def _sx_missing(v):
    if v is None or v is pd.NA or v is pd.NaT:
        return True
    try:
        return bool(isinstance(v, float) and v != v)
    except Exception:  # noqa: BLE001
        return False


def _sx_best(v, w=12):
    """BESTw. text of a number (no padding); '.' for missing."""
    if v is None:
        return "."
    if float(v).is_integer() and abs(v) < 10 ** w:
        return str(int(v))
    for p in range(w - 1, 0, -1):
        t = f"{v:.{p}g}"
        if "e" in t:
            m, e = t.split("e")
            t = f"{m}E{int(e)}"
        if len(t) <= w:
            return t
    return f"{v:.1E}"


def _sx_num(v):
    """Any value -> SAS numeric (float) or None. Character text is read like the BEST informat."""
    if _sx_missing(v):
        return None
    if isinstance(v, (bool, np.bool_)):
        return float(v)
    if isinstance(v, (int, float, np.integer, np.floating)):
        f = float(v)
        return None if f != f or f in (math.inf, -math.inf) else f
    if isinstance(v, (pd.Timestamp, datetime)):
        return _sx_from_dt(v)
    if isinstance(v, date):
        return float(v.toordinal() - _SX_EPOCH)
    s = str(v).strip()
    if s in ("", "."):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if f != f or f in (math.inf, -math.inf) else f


def _sx_chr(v, pad=True):
    """Any value -> SAS character. A number converts with BEST12., right-aligned when `pad`."""
    if isinstance(v, str):
        return v
    if _sx_missing(v):
        return ""
    n = _sx_num(v)
    t = _sx_best(n)
    return t.rjust(12) if pad else t


def _sx_from_dt(v):
    """A pandas/py datetime -> SAS date (midnight) or SAS datetime seconds."""
    ts = pd.Timestamp(v)
    days = ts.toordinal() - _SX_EPOCH
    secs = ts.hour * 3600 + ts.minute * 60 + ts.second + ts.microsecond / 1e6
    return float(days) if not secs else float(days * 86400 + secs)


def _sx_d2py(d):
    return date.fromordinal(int(math.floor(d)) + _SX_EPOCH)


def _sx_py2d(dt):
    return float(dt.toordinal() - _SX_EPOCH)


def _sx_ymd(y, m, d):
    try:
        return _sx_py2d(date(int(y), int(m), int(d)))
    except (ValueError, TypeError, OverflowError):
        return None


def _sx_yr(y):
    """Two-digit years follow YEARCUTOFF=1940, like SAS's default."""
    y = int(y)
    return y if y >= 100 else (1900 + y if y >= 40 else 2000 + y)


# ── vectors ─────────────────────────────────────────────────────────────────────────────────
def _sx_s(ix, x):
    """A value as a string Series on index `ix` (numbers as BEST, unpadded); blank for missing."""
    if isinstance(x, pd.Series):
        s = x.reindex(ix) if not x.index.equals(ix) else x
        if _sx_kind(s) == "n":
            return pd.Series([_sx_chr(v, pad=False) if not _sx_missing(v) else "" for v in _sx_vals(s, "n")],
                             index=ix, dtype="string")
        return s.astype("string").fillna("")
    v = "" if _sx_missing(x) else (x if isinstance(x, str) else _sx_chr(x, pad=False))
    return pd.Series(v, index=ix, dtype="string")


def _sx_kind(x):
    """'n' for a numeric value/Series (dates included), else 'c'."""
    if isinstance(x, pd.Series):
        if pd.api.types.is_bool_dtype(x) or pd.api.types.is_numeric_dtype(x) \
                or pd.api.types.is_datetime64_any_dtype(x):
            return "n"
        if x.dtype == object:
            first = next((v for v in x.tolist() if not _sx_missing(v)), None)
            return "n" if isinstance(first, (int, float, np.integer, np.floating, date)) \
                and not isinstance(first, bool) else "c"
        return "c"
    return "c" if isinstance(x, str) else "n"


def _sx_vals(s, t):
    """A Series' values as SAS scalars of type t ('c', 'C' = char unpadded, 'n', or 'a' = as-is)."""
    if pd.api.types.is_datetime64_any_dtype(s):
        dt = pd.to_datetime(s)
        midnight = bool(((dt.dt.hour == 0) & (dt.dt.minute == 0) & (dt.dt.second == 0)).all())
        vals = [None if pd.isna(v) else (float(v.toordinal() - _SX_EPOCH) if midnight else _sx_from_dt(v))
                for v in dt.tolist()]
        return vals if t != "c" and t != "C" else [_sx_chr(v, pad=(t == "c")) for v in vals]
    raw = s.tolist()
    if t == "n":
        return [_sx_num(v) for v in raw]
    if t in ("c", "C"):
        return [_sx_chr(v, pad=(t == "c")) for v in raw]
    k = _sx_kind(s)
    return [_sx_num(v) for v in raw] if k == "n" else [_sx_chr(v) for v in raw]


def _sx_one(x, t):
    if t == "n":
        return _sx_num(x)
    if t in ("c", "C"):
        return _sx_chr(x, pad=(t == "c"))
    return x if isinstance(x, str) else _sx_num(x)


def _sx_ser(ix, vals, ret):
    if ret == "c":
        return pd.Series(["" if v is None else v for v in vals], index=ix, dtype="string")
    return pd.Series([np.nan if v is None else v for v in vals], index=ix, dtype="float64")


def _sx_n(ix, x):
    """A value as a float Series (NaN = missing)."""
    if isinstance(x, pd.Series):
        s = x.reindex(ix) if not x.index.equals(ix) else x
        return _sx_ser(ix, _sx_vals(s, "n"), "n")
    return pd.Series(np.nan if _sx_num(x) is None else _sx_num(x), index=ix, dtype="float64")


# ── character helpers ───────────────────────────────────────────────────────────────────────
def _sx_class(mods, chars=""):
    """The character set a COMPRESS/FINDC/COUNTC/SCAN modifier list selects (plus `chars`)."""
    m = str(mods or "").lower()
    s = set(chars or "")
    if "a" in m:
        s |= set(string.ascii_letters)
    if "d" in m:
        s |= set(string.digits)
    if "s" in m:
        s |= set(" \t\n\r\f\v")
    if "p" in m:
        s |= set(string.punctuation)
    if "u" in m:
        s |= set(string.ascii_uppercase)
    if "l" in m:
        s |= set(string.ascii_lowercase)
    if "n" in m:
        s |= set(string.ascii_letters + string.digits + "_")
    if "f" in m:
        s |= set(string.ascii_letters + "_")
    if "h" in m:
        s |= {"\t"}
    if "c" in m:
        s |= {chr(i) for i in range(32)} | {chr(127)}
    if "x" in m:
        s |= set(string.hexdigits)
    if "w" in m or "g" in m:
        s |= {chr(i) for i in range(33, 127)} | ({" "} if "w" in m else set())
    if "i" in m:
        s |= {c.upper() for c in s} | {c.lower() for c in s}
    return s


def _sx_words(s, d, mods=""):
    """SAS word splitting: consecutive delimiters are one (unless the M modifier), leading and
    trailing delimiters are ignored."""
    m = str(mods or "").lower()
    dl = _SX_SCAN_DELIMS if d is None else str(d)
    dset = _sx_class(m, dl)
    if "k" in m:
        dset = {chr(i) for i in range(256)} - dset
    if "i" in m:
        dset |= {c.upper() for c in dset} | {c.lower() for c in dset}
    words, cur, out = [], [], s
    for ch in out:
        if ch in dset:
            words.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    words.append("".join(cur))
    if "m" not in m:
        words = [w for w in words if w != ""]
    if "r" in m:
        words = [w.strip() for w in words]
    return words


def _sx_regex(pat):
    """A SAS/Perl regex literal ('/x/i', 's/a/b/', 'm{x}') -> (compiled, replacement or None)."""
    p = str(pat).strip()
    sub = p[:1] == "s" and len(p) > 2 and not p[1].isalnum()
    if p[:1] == "m" and len(p) > 2 and not p[1].isalnum():
        p = p[1:]
    if sub:
        p = p[1:]
    if not p or p[0].isalnum() or p[0] == "\\":
        return re.compile(p), None
    o = p[0]
    c = {"{": "}", "(": ")", "[": "]", "<": ">"}.get(o, o)
    parts, buf, i = [], [], 1
    while i < len(p):
        ch = p[i]
        if ch == "\\" and i + 1 < len(p):
            buf.append(p[i:i + 2])
            i += 2
            continue
        if ch == c:
            parts.append("".join(buf))
            buf = []
            if not sub or len(parts) == 2:
                i += 1
                break
            i += 1
            if c != o and i < len(p) and p[i] == o:
                i += 1
            continue
        buf.append(ch)
        i += 1
    flags = p[i:]
    f = 0
    for ch in flags:
        f |= {"i": re.I, "x": re.X, "s": re.S, "m": re.M}.get(ch, 0)
    rx = re.compile(parts[0] if parts else "", f)
    rep = None
    if sub:
        rep = re.sub(r"\$(\d+)", r"\\g<\1>", parts[1] if len(parts) > 1 else "")
    return rx, rep


# ── character functions ─────────────────────────────────────────────────────────────────────
def _f_propcase(s, d=None):
    dl = _SX_PROP_DELIMS if d is None else str(d)
    out, up = [], True
    for ch in s.lower():
        out.append(ch.upper() if up else ch)
        up = ch in dl
    return "".join(out)


def _f_compress(s, chars=None, mods=None):
    m = str(mods or "").lower()
    if chars is None and not m:
        return s.replace(" ", "")
    cs = _sx_class(m, (chars or "").rstrip() if "t" in m else (chars or ""))
    if "k" in m:
        return "".join(ch for ch in s if ch in cs)
    return "".join(ch for ch in s if ch not in cs)


def _f_substr(s, p, n=None):
    if p is None:
        return ""
    p = int(p)
    if p < 1 or p > len(s):
        return ""
    if n is None:
        return s[p - 1:]
    n = int(n)
    return "" if n < 1 else s[p - 1:p - 1 + n]


def _f_substrn(s, p, n=None):
    if p is None:
        return ""
    p = int(p)
    end = len(s) + 1 if n is None else p + int(n)
    a, b = max(p, 1), min(end, len(s) + 1)
    return s[a - 1:b - 1] if b > a else ""


def _f_scan(s, n, d=None, mods=None):
    if n is None or int(n) == 0:
        return ""
    w = _sx_words(s, d, mods)
    n = int(n)
    if abs(n) > len(w):
        return ""
    return w[n - 1] if n > 0 else w[n]


def _f_countw(s, d=None, mods=None):
    return float(len(_sx_words(s, d, mods)))


def _f_translate(s, *pairs):
    out = s
    table = {}
    for i in range(0, len(pairs) - 1, 2):
        to, frm = pairs[i] or "", pairs[i + 1] or ""
        for k, ch in enumerate(frm):
            table.setdefault(ch, to[k] if k < len(to) else " ")
    return "".join(table.get(ch, ch) for ch in out)


def _f_tranwrd(s, t, r):
    if t in ("", None):
        return s
    return s.replace(t, " " if r == "" else r)


def _f_transtrn(s, t, r):
    if t in ("", None):
        return s
    return s.replace(t, r or "")


def _f_find(s, sub, a=None, b=None):
    mods, start = "", None
    for x in (a, b):
        if isinstance(x, str) and x.strip() and not re.fullmatch(r"\s*-?\d+\s*", x):
            mods = x
        elif x is not None and _sx_num(x) is not None:
            start = int(_sx_num(x))
    m = mods.lower()
    if "t" in m:
        s, sub = s.rstrip(), (sub or "").rstrip()
    if "i" in m:
        s, sub = s.lower(), (sub or "").lower()
    if not sub:
        return 0.0
    if start is None or start > 0:
        st = (start or 1) - 1
        return float(s.find(sub, st) + 1)
    end = min(-start, len(s))
    return float(s.rfind(sub, 0, end) + 1) if end > 0 else 0.0


def _f_findc(s, chars=None, a=None, b=None):
    mods, start = "", None
    for x in (a, b):
        if isinstance(x, str) and x.strip() and not re.fullmatch(r"\s*-?\d+\s*", x):
            mods = x
        elif x is not None and _sx_num(x) is not None:
            start = int(_sx_num(x))
    m = mods.lower()
    cs = _sx_class(m.replace("k", ""), chars or "")
    if "i" in m:
        s = s.lower()
        cs = {c.lower() for c in cs}
    hit = (lambda ch: ch not in cs) if "k" in m else (lambda ch: ch in cs)
    rng = range((start or 1) - 1, len(s)) if (start is None or start > 0) else range(min(-start, len(s)) - 1, -1, -1)
    return float(next((i + 1 for i in rng if hit(s[i])), 0))


def _f_findw(s, w, d=None, mods=None, start=None):
    m = str(mods or "").lower()
    if not w:
        return 0.0
    src, ww = (s.lower(), w.lower()) if "i" in m else (s, w)
    dset = _sx_class(m, " " if d is None else d)
    pos, k, n = 0, 0, len(src)
    while pos < n:
        while pos < n and src[pos] in dset:
            pos += 1
        e = pos
        while e < n and src[e] not in dset:
            e += 1
        if e > pos:
            k += 1
            if src[pos:e] == ww and (start is None or pos + 1 >= int(start)):
                return float(k if "e" in m else pos + 1)
        pos = e
    return 0.0


def _f_indexw(s, w, d=None):
    return _f_findw(s, (w or "").strip(), " " if d is None else d)


def _f_count(s, sub, mods=None):
    m = str(mods or "").lower()
    if "t" in m:
        s, sub = s.rstrip(), (sub or "").rstrip()
    if "i" in m:
        s, sub = s.lower(), (sub or "").lower()
    return float(s.count(sub)) if sub else 0.0


def _f_countc(s, chars=None, mods=None):
    m = str(mods or "").lower()
    cs = _sx_class(m.replace("k", ""), chars or "")
    if "i" in m:
        s, cs = s.lower(), {c.lower() for c in cs}
    return float(sum((ch not in cs) if "k" in m else (ch in cs) for ch in s))


def _f_verify(s, *lists):
    ok = set("".join(x or "" for x in lists))
    return float(next((i + 1 for i, ch in enumerate(s.rstrip()) if ch not in ok), 0))


def _sx_anyfn(pred, want):
    def f(s, start=None):
        if start is not None and int(start) < 0:
            rng = range(min(-int(start), len(s)) - 1, -1, -1)
        else:
            rng = range((int(start) if start else 1) - 1, len(s))
        return float(next((i + 1 for i in rng if bool(pred(s[i])) == want), 0))
    return f


_SX_PRED = {
    "alpha": lambda c: c in string.ascii_letters, "digit": lambda c: c in string.digits,
    "alnum": lambda c: c in string.ascii_letters + string.digits, "punct": lambda c: c in string.punctuation,
    "space": lambda c: c in " \t\n\r\f\v", "upper": lambda c: c in string.ascii_uppercase,
    "lower": lambda c: c in string.ascii_lowercase, "cntrl": lambda c: ord(c) < 32 or ord(c) == 127,
}


def _f_cat(*a):
    return "".join(a)


def _f_catx(sep, *a):
    return (sep or "").join(x.strip() for x in a if x.strip() != "")


def _f_length(s):
    t = s.rstrip()
    return float(len(t) if t else 1)


def _f_dequote(s):
    t = s.lstrip()
    if t[:1] in ("'", '"'):
        q = t[0]
        out, i = [], 1
        while i < len(t):
            if t[i] == q:
                if t[i + 1:i + 2] == q:
                    out.append(q)
                    i += 2
                    continue
                return "".join(out) + t[i + 1:]
            out.append(t[i])
            i += 1
        return "".join(out)
    return s


def _f_prxmatch(rx, s):
    r, _ = _sx_regex(rx)
    m = r.search(s)
    return float(m.start() + 1) if m else 0.0


def _f_prxchange(rx, times, s):
    r, rep = _sx_regex(rx)
    n = int(times) if times is not None else -1
    return r.sub(rep or "", s, count=0 if n < 0 else n)


# ── selection / missing ─────────────────────────────────────────────────────────────────────
def _sx_true(v):
    n = _sx_num(v)
    return n is not None and n != 0


def _f_ifx(c, t, f, m=None, *, _given=False):
    if _sx_num(c) is None:
        return m if _given else f
    return t if _sx_num(c) != 0 else f


def _f_choose(n, *a):
    if n is None:
        return None
    k = int(n)
    if k == 0 or abs(k) > len(a):
        return None
    return a[k - 1] if k > 0 else a[k]


def _f_which(x, *a):
    for i, v in enumerate(a, 1):
        if (x.rstrip() == v.rstrip()) if isinstance(x, str) and isinstance(v, str) else (x == v and x is not None):
            return float(i)
    return 0.0


def _f_cmiss(*a):
    return float(sum(1 for v in a if (v.strip() == "" if isinstance(v, str) else v is None)))


def _sx_stat(fn):
    def f(*a):
        xs = [v for v in a if v is not None]
        return fn(xs) if xs else None
    return f


def _f_round(x, unit=None):
    if x is None:
        return None
    u = 1.0 if unit is None else unit
    if u == 0:
        return x
    q = (Decimal(repr(x)) / Decimal(repr(u))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(q * Decimal(repr(u)))


def _f_largest(k, *a):
    xs = sorted((v for v in a if v is not None), reverse=True)
    return xs[int(k) - 1] if k is not None and 1 <= int(k) <= len(xs) else None


def _f_smallest(k, *a):
    xs = sorted(v for v in a if v is not None)
    return xs[int(k) - 1] if k is not None and 1 <= int(k) <= len(xs) else None


def _f_median(*a):
    xs = sorted(v for v in a if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _f_var(*a):
    xs = [v for v in a if v is not None]
    if len(xs) < 2:
        return None
    mu = sum(xs) / len(xs)
    return sum((v - mu) ** 2 for v in xs) / (len(xs) - 1)


# ── dates, times, datetimes ─────────────────────────────────────────────────────────────────
def _f_today():
    return _sx_py2d(date.today())


def _f_datetime():
    n = datetime.now()
    return _sx_py2d(n.date()) * 86400 + n.hour * 3600 + n.minute * 60 + n.second


def _f_time():
    n = datetime.now()
    return float(n.hour * 3600 + n.minute * 60 + n.second)


def _f_mdy(m, d, y):
    if None in (m, d, y):
        return None
    return _sx_ymd(_sx_yr(y), m, d)


def _f_yyq(y, q):
    if None in (y, q) or not 1 <= int(q) <= 4:
        return None
    return _sx_ymd(_sx_yr(y), 3 * int(q) - 2, 1)


def _f_dhms(d, h, m, s):
    if None in (d, h, m, s):
        return None
    return d * 86400 + h * 3600 + m * 60 + s


def _f_hms(h, m, s):
    if None in (h, m, s):
        return None
    return h * 3600 + m * 60 + s


def _sx_dpart(fn):
    def f(d):
        if d is None:
            return None
        try:
            return float(fn(_sx_d2py(d)))
        except (ValueError, OverflowError):
            return None
    return f


def _sx_tpart(div, mod):
    def f(t):
        return None if t is None else float(int((t % 86400) // div) % mod)
    return f


def _sx_interval(name):
    n = str(name or "").strip().lower()
    m = re.fullmatch(r"(dt)?([a-z]+?)(\d*)(?:\.(\d+))?", n)
    if not m:
        return None
    dt, base, mult = bool(m.group(1)), m.group(2), int(m.group(3) or 1)
    alias = {"days": "day", "weeks": "week", "months": "month", "qtrs": "qtr", "years": "year",
             "hours": "hour", "minutes": "minute", "seconds": "second", "semiyear": "semiyear",
             "weekday": "weekday", "tenday": "tenday", "semimonth": "semimonth"}
    base = alias.get(base, base)
    if base in ("hour", "minute", "second"):
        dt = True
    if base not in ("day", "week", "month", "qtr", "year", "semiyear", "hour", "minute", "second", "weekday"):
        return None
    return base, mult, dt


def _sx_idx(base, v, dt):
    """The interval number containing value v (date days, or datetime seconds when dt)."""
    if base in ("hour", "minute", "second"):
        return math.floor(v / {"hour": 3600, "minute": 60, "second": 1}[base])
    d = math.floor(v / 86400) if dt else math.floor(v)
    if base == "day":
        return d
    if base == "week":
        return (d + 5) // 7
    py = _sx_d2py(d)
    if base == "month":
        return py.year * 12 + py.month - 1
    if base == "qtr":
        return py.year * 4 + (py.month - 1) // 3
    if base == "semiyear":
        return py.year * 2 + (py.month - 1) // 6
    if base == "year":
        return py.year
    return d


def _f_intck(interval, a, b, method=None):
    iv = _sx_interval(interval)
    if iv is None or a is None or b is None:
        return None
    base, mult, dt = iv
    if base == "weekday":
        da, db = int(a // 86400 if dt else a), int(b // 86400 if dt else b)
        sgn = 1 if db >= da else -1
        return float(sgn * sum(1 for d in range(min(da, db) + 1, max(da, db) + 1) if _sx_d2py(d).weekday() < 5))
    if str(method or "d").lower().startswith("c"):
        if base in ("month", "qtr", "year", "semiyear"):
            pa, pb = _sx_d2py(math.floor(a / 86400) if dt else a), _sx_d2py(math.floor(b / 86400) if dt else b)
            months = (pb.year - pa.year) * 12 + pb.month - pa.month
            if months > 0 and pb.day < pa.day:
                months -= 1
            elif months < 0 and pb.day > pa.day:
                months += 1
            per = {"month": 1, "qtr": 3, "semiyear": 6, "year": 12}[base] * mult
            return float(int(months / per))
        span = {"day": 86400 if dt else 1, "week": 7 * (86400 if dt else 1), "hour": 3600, "minute": 60, "second": 1}[base]
        return float(int((b - a) / (span * mult)))
    ia, ib = _sx_idx(base, a, dt), _sx_idx(base, b, dt)
    return float(ib // mult - ia // mult)


def _f_intnx(interval, start, n, align=None):
    iv = _sx_interval(interval)
    if iv is None or start is None or n is None:
        return None
    base, mult, dt = iv
    al = str(align or "b").strip().lower()[:1]
    k = int(n) * mult
    if base in ("hour", "minute", "second"):
        span = {"hour": 3600, "minute": 60, "second": 1}[base] * mult
        b0 = math.floor(start / span) * span + int(n) * span
        return float({"b": b0, "e": b0 + span - 1, "m": b0 + span / 2, "s": start + int(n) * span}.get(al, b0))
    d = math.floor(start / 86400) if dt else math.floor(start)
    py = _sx_d2py(d)
    if base == "day":
        lo = hi = d + k
    elif base == "week":
        lo = d - ((d + 5) % 7) + 7 * k
        hi = lo + 6
        if al == "s":
            lo = hi = d + 7 * k
    else:
        per = {"month": 1, "qtr": 3, "semiyear": 6, "year": 12}[base] * mult
        if base == "month" and mult == 1:
            first = py.year * 12 + py.month - 1
        else:
            first = py.year * 12 + ((py.month - 1) // per) * per if base != "year" else py.year * 12
        tgt = first + int(n) * per
        y, mo = divmod(tgt, 12)
        lo = _sx_ymd(y, mo + 1, 1)
        ey, em = divmod(tgt + per - 1, 12)
        hi = _sx_ymd(ey, em + 1, calendar.monthrange(ey, em + 1)[1])
        if al == "s":
            sy, sm = divmod(py.year * 12 + py.month - 1 + int(n) * per, 12)
            lo = hi = _sx_ymd(sy, sm + 1, min(py.day, calendar.monthrange(sy, sm + 1)[1]))
    out = {"b": lo, "e": hi, "m": math.floor((lo + hi) / 2), "s": lo}.get(al, lo)
    return float(out * 86400) if dt else float(out)


def _sx_anniv(py, y):
    try:
        return date(y, py.month, py.day)
    except ValueError:
        return date(y, 2, 28)


def _f_yrdif(a, b, basis=None):
    if a is None or b is None:
        return None
    bs = str(basis or "ACT/ACT").strip().upper()
    pa, pb = _sx_d2py(a), _sx_d2py(b)
    if bs == "AGE":
        sgn = 1
        if pb < pa:
            pa, pb, sgn = pb, pa, -1
        yrs = pb.year - pa.year
        if _sx_anniv(pa, pb.year) > pb:
            yrs -= 1
        last, nxt = _sx_anniv(pa, pa.year + yrs), _sx_anniv(pa, pa.year + yrs + 1)
        return sgn * (yrs + (pb - last).days / (nxt - last).days)
    if bs in ("ACT/ACT", "ACTUAL"):
        if pb < pa:
            return -_f_yrdif(b, a, basis)
        tot, cur = 0.0, pa
        while cur.year < pb.year:
            ny = date(cur.year + 1, 1, 1)
            tot += (ny - cur).days / (366 if calendar.isleap(cur.year) else 365)
            cur = ny
        return tot + (pb - cur).days / (366 if calendar.isleap(cur.year) else 365)
    if bs == "30/360":
        return _f_datdif(a, b, "30/360") / 360
    if bs == "ACT/360":
        return (b - a) / 360
    if bs == "ACT/365":
        return (b - a) / 365
    return None


def _f_datdif(a, b, basis=None):
    if a is None or b is None:
        return None
    bs = str(basis or "ACT/ACT").strip().upper()
    if bs == "30/360":
        pa, pb = _sx_d2py(a), _sx_d2py(b)
        d1, d2 = min(pa.day, 30), pb.day
        if d1 == 30 and d2 == 31:
            d2 = 30
        return float((pb.year - pa.year) * 360 + (pb.month - pa.month) * 30 + (d2 - d1))
    return float(b - a)


# ── formats (PUT) and informats (INPUT) ─────────────────────────────────────────────────────
_SX_FMT_RE = re.compile(r"^\s*(\$?)([A-Za-z_](?:[A-Za-z0-9_]*[A-Za-z_])?)?(\d*)\.(\d*)\s*$")


def _sx_fmt(text):
    """'yymmdd10.' -> (char?, NAME, w or None, d or None)."""
    m = _SX_FMT_RE.match(str(text or ""))
    if not m:
        raise ValueError(f"not a SAS format: {text!r}")
    return bool(m.group(1)), (m.group(2) or "").upper(), (int(m.group(3)) if m.group(3) else None), \
        (int(m.group(4)) if m.group(4) else None)


def _sx_hms(t, sep=":", pad_h=False, secs=True):
    t = int(round(t))
    h, r = divmod(t, 3600)
    mi, s = divmod(r, 60)
    hh = f"{h:02d}" if pad_h else str(h)
    return f"{hh}{sep}{mi:02d}" + (f"{sep}{s:02d}" if secs else "")


def _sx_put_date(name, w, v):
    py = _sx_d2py(v)
    y4, y2, mo, dd = f"{py.year:04d}", f"{py.year % 100:02d}", f"{py.month:02d}", f"{py.day:02d}"
    mon = _SX_MON[py.month - 1]
    if name == "DATE":
        w = w or 7
        return f"{dd}{mon}{y4}" if w >= 9 and w < 11 else (f"{dd}-{mon}-{y4}" if w >= 11 else f"{dd}{mon}{y2}")
    if name in ("E8601DA", "IS8601DA"):
        return f"{y4}-{mo}-{dd}"
    m = re.fullmatch(r"(YYMMDD|MMDDYY|DDMMYY)([BCDNPS]?)", name)
    if m:
        w = w or 8
        sep = {"": "-" if m.group(1) == "YYMMDD" else "/", "B": " ", "C": ":", "D": "-", "N": "",
               "P": ".", "S": "/"}[m.group(2)]
        if w < 8:
            sep = ""                                 # YYMMDD6. is packed: 200102
        yy = y4 if (w >= 10 or (sep == "" and w >= 8)) else y2
        parts = {"YYMMDD": (yy, mo, dd), "MMDDYY": (mo, dd, yy), "DDMMYY": (dd, mo, yy)}[m.group(1)]
        return sep.join(parts)
    if name == "MONYY":
        return f"{mon}{y4}" if (w or 5) >= 7 else f"{mon}{y2}"
    if name == "YEAR":
        return y4 if (w or 4) >= 4 else y2
    if name == "MONTH":
        return str(py.month)
    if name == "DAY":
        return str(py.day)
    if name == "MONNAME":
        t = calendar.month_name[py.month]
        return t[:w] if w else t
    if name == "DOWNAME":
        t = calendar.day_name[py.weekday()]
        return t[:w] if w else t
    if name == "WEEKDAY":
        return str((py.weekday() + 1) % 7 + 1)
    if name == "QTR":
        return str((py.month - 1) // 3 + 1)
    if name == "YYMMN":
        return f"{y4}{mo}"
    if name == "YYMM":
        return f"{y4}M{mo}"
    if name == "YYQ":
        return f"{y4}Q{(py.month - 1) // 3 + 1}"
    if name == "JULIAN":
        return f"{y2 if (w or 5) < 7 else y4}{py.timetuple().tm_yday:03d}"
    return None


# the formats and informats understood here (validated when an expression is parsed)
SX_FORMATS = {
    "char": ["$", "$CHAR", "$UPCASE", "$QUOTE"],
    "numeric": ["w.d", "F", "BEST", "Z", "COMMA", "COMMAX", "DOLLAR", "PERCENT", "E"],
    "date": ["DATE", "E8601DA", "IS8601DA", "YYMMDD", "YYMMDDB", "YYMMDDC", "YYMMDDD", "YYMMDDN",
             "YYMMDDP", "YYMMDDS", "MMDDYY", "MMDDYYB", "MMDDYYC", "MMDDYYD", "MMDDYYN", "MMDDYYP",
             "MMDDYYS", "DDMMYY", "DDMMYYB", "DDMMYYC", "DDMMYYD", "DDMMYYN", "DDMMYYP", "DDMMYYS",
             "MONYY", "YEAR", "MONTH", "DAY", "QTR", "WEEKDAY", "MONNAME", "DOWNAME", "YYMMN", "YYMM",
             "YYQ", "JULIAN"],
    "datetime": ["DATETIME", "E8601DT", "IS8601DT", "E8601DN", "IS8601DN", "DTDATE", "DTYYMMDD"],
    "time": ["TIME", "HHMM", "TOD", "E8601TM", "IS8601TM", "HOUR"],
}
SX_INFORMATS = {
    "char": ["$", "$CHAR"],
    "numeric": ["w.d", "F", "BEST", "COMMA", "COMMAX", "DOLLAR", "PERCENT", "E"],
    "date": ["DATE", "E8601DA", "IS8601DA", "YYMMDD", "YYMMDDN", "MMDDYY", "DDMMYY", "MONYY", "YYMMN",
             "ANYDTDTE"],
    "datetime": ["DATETIME", "E8601DT", "IS8601DT", "ANYDTDTM"],
    "time": ["TIME", "E8601TM", "IS8601TM", "ANYDTTME"],
}


def _sx_known(text, informat=False):
    """True when `text` is a format (or informat) this runtime implements."""
    ch, name, _w, _d = _sx_fmt(str(text).replace("?", ""))
    reg = SX_INFORMATS if informat else SX_FORMATS
    names = {n.lstrip("$") for v in reg.values() for n in v} - {"w.d"}
    return (ch and name in {"", *{n.lstrip("$") for n in reg["char"]}}) or (not ch and (name == "" or name in names))


_SX_DEFAULT_W = {"": 12, "F": 12, "BEST": 12, "DATE": 7, "E8601DA": 10, "IS8601DA": 10, "YYMMDD": 8,
                 "MMDDYY": 8, "DDMMYY": 8, "YYMMDDN": 8, "MONYY": 5, "DATETIME": 16, "E8601DT": 19,
                 "IS8601DT": 19, "TIME": 8, "HHMM": 5, "TOD": 8, "E8601TM": 8, "IS8601TM": 8}


def _sx_put(v, text):
    ch, name, w, d = _sx_fmt(text)
    if ch:
        s = _sx_chr(v)
        if name == "UPCASE":
            s = s.upper()
        elif name == "QUOTE":
            s = '"' + s.replace('"', '""') + '"'
        elif name not in ("", "CHAR", "F"):
            return s if w is None else s[:w]      # a user format (PROC FORMAT) is not known here
        return s if w is None else s[:w]
    x = _sx_num(v)
    if x is None:                                 # missing prints '.', right-aligned in the width
        return ".".rjust(w or _SX_DEFAULT_W.get(name, 1))
    if name in ("", "F", "BEST", "BESTD", "Z", "COMMA", "COMMAX", "DOLLAR", "PERCENT", "E"):
        if name == "BEST" or (name == "" and d is None and w is None):
            out = _sx_best(x, w or 12)
        elif name == "Z":
            dd = d or 0
            out = f"{x:0{w or 1}.{dd}f}" if x >= 0 else "-" + f"{-x:0{max((w or 1) - 1, 1)}.{dd}f}"
        elif name in ("COMMA", "DOLLAR"):
            out = ("$" if name == "DOLLAR" else "") + f"{x:,.{d or 0}f}"
        elif name == "COMMAX":
            out = f"{x:,.{d or 0}f}".replace(",", "_").replace(".", ",").replace("_", ".")
        elif name == "PERCENT":
            out = f"{x * 100:.{d or 0}f}%"
        elif name == "E":
            out = f"{x:.{max((w or 12) - 7, 1)}E}"
        else:
            out = f"{float(_f_round(x, 10 ** -(d or 0))):.{d or 0}f}"
        return out.rjust(w) if w and len(out) <= w else (out if not w else "*" * w)
    if name in ("DATETIME", "E8601DT", "IS8601DT", "E8601DN", "IS8601DN", "DTDATE", "DTYYMMDD"):
        days, secs = divmod(x, 86400)
        py = _sx_d2py(days)
        if name in ("E8601DN", "IS8601DN", "DTYYMMDD"):
            return f"{py.year:04d}-{py.month:02d}-{py.day:02d}"
        if name == "DTDATE":
            return _sx_put_date("DATE", w or 9, days)
        if name == "DATETIME":
            w = w or 16
            yy = f"{py.year:04d}" if w >= 18 else f"{py.year % 100:02d}"
            dd = f"{py.day:02d}{_SX_MON[py.month - 1]}{yy}"
            return (dd + ":" + _sx_hms(secs, pad_h=True)).rjust(w)
        w = w or 19
        out = f"{py.year:04d}-{py.month:02d}-{py.day:02d}T{_sx_hms(secs, pad_h=True)}"
        if w > 19 and d:
            out += f"{secs % 1:.{d}f}"[1:]
        return out[:w]
    if name in ("TIME", "HHMM", "TOD", "E8601TM", "IS8601TM", "HOUR"):
        t = x % 86400 if name in ("TOD", "E8601TM", "IS8601TM") else x
        if name == "HOUR":
            return str(int(t // 3600))
        if name == "HHMM":
            return _sx_hms(t, secs=False).rjust(w or 5)
        if name == "TIME":
            w = w or 8
            return _sx_hms(t, secs=w >= 7).rjust(w)
        return _sx_hms(t, pad_h=True, secs=(w or 8) >= 8)
    out = _sx_put_date(name, w, x)
    if out is None:
        raise ValueError(f"format {name}. is not supported")
    return out.rjust(w) if w and name in ("DATE", "MONYY", "YEAR", "MONTH", "DAY", "QTR", "WEEKDAY") else out


def _sx_parse_date(s, order="YMD"):
    """Read a date in YMD / MDY / DMY order (separated, or 6/8 packed digits), or ddMONyy(yy)."""
    t = s.strip().upper()
    if not t:
        return None
    m = re.fullmatch(r"(\d{1,2})[-\s/.]?([A-Z]{3})[-\s/.]?(\d{2}|\d{4})", t)
    if m:
        return _sx_ymd(_sx_yr(m.group(3)), _SX_MON.index(m.group(2)) + 1, m.group(1)) if m.group(2) in _SX_MON else None
    digits = re.findall(r"\d+", t)
    if len(digits) == 1 and len(digits[0]) in (6, 8):
        g = digits[0]
        k = len(g) - 4                              # year width: 4 when 8 digits, 2 when 6
        if order == "YMD":
            y, mo, d = g[:k], g[k:k + 2], g[k + 2:]
        elif order == "MDY":
            mo, d, y = g[:2], g[2:4], g[4:]
        else:
            d, mo, y = g[:2], g[2:4], g[4:]
        return _sx_ymd(_sx_yr(y), mo, d)
    if len(digits) == 3:
        a, b, c = digits
        y, mo, d = {"YMD": (a, b, c), "MDY": (c, a, b), "DMY": (c, b, a)}[order]
        return _sx_ymd(_sx_yr(y), mo, d)
    return None


def _sx_parse_time(s):
    m = re.fullmatch(r"\s*(\d{1,2}):?(\d{2})(?::?(\d{2}(?:\.\d+)?))?\s*", s or "")
    if not m:
        return None
    h, mi, sec = int(m.group(1)), int(m.group(2)), float(m.group(3) or 0)
    return float(h * 3600 + mi * 60 + sec) if mi < 60 and sec < 61 else None


def _sx_any_date(s):
    t = s.strip().upper()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", t)
    if m:
        return _sx_ymd(*m.groups())
    m = re.fullmatch(r"([A-Z]{3})[-\s]?(\d{2}|\d{4})", t)
    if m and m.group(1) in _SX_MON:
        return _sx_ymd(_sx_yr(m.group(2)), _SX_MON.index(m.group(1)) + 1, 1)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return _sx_ymd(_sx_yr(m.group(3)), *((b, a) if a > 12 else (a, b)))
    m = re.fullmatch(r"(\d{2}[A-Z]{3}(?:\d{2}|\d{4})):.*", t)
    if m:
        return _sx_parse_date(m.group(1))
    return _sx_parse_date(t, "YMD")


def _sx_input(v, text):
    s = _sx_chr(v)
    t = str(text or "").replace("??", "").replace("?", "").strip()
    ch, name, w, d = _sx_fmt(t)
    if w:
        s = s[:w]
    if ch:
        return s if name == "CHAR" else s.lstrip()
    s = s.strip()
    if s in ("", "."):
        return None
    if name in ("", "F", "BEST", "COMMA", "COMMAX", "DOLLAR", "PERCENT", "NUMX", "E", "BESTD"):
        u = s
        if name in ("COMMA", "DOLLAR"):
            u = re.sub(r"[,$\s]", "", u)
        elif name == "COMMAX":
            u = u.replace(".", "").replace(",", ".")
        elif name == "PERCENT":
            u = u.rstrip("%")
        neg = u.startswith("(") and u.endswith(")")
        u = u.strip("()")
        try:
            f = float(u)
        except ValueError:
            return None
        if d and "." not in u and name in ("", "F", "COMMA"):
            f /= 10 ** d
        f = -f if neg else f
        return f / 100 if name == "PERCENT" else f
    if name in ("E8601DA", "IS8601DA", "YYMMDD", "YYMMDDN"):
        if name in ("E8601DA", "IS8601DA") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return None
        return _sx_parse_date(s, "YMD")
    if name == "MMDDYY":
        return _sx_parse_date(s, "MDY")
    if name == "DDMMYY":
        return _sx_parse_date(s, "DMY")
    if name in ("DATE", "DDMMMYY"):
        return _sx_parse_date(s)
    if name in ("MONYY",):
        return _sx_any_date(s)
    if name in ("ANYDTDTE",):
        return _sx_any_date(s)
    if name in ("YYMMN",):
        m = re.fullmatch(r"(\d{4})(\d{2})", s)
        return _sx_ymd(m.group(1), m.group(2), 1) if m else None
    if name in ("E8601DT", "IS8601DT", "ANYDTDTM", "DATETIME"):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)", s)
        if m:
            dd, tt = _sx_parse_date(m.group(1)), _sx_parse_time(m.group(2))
            return None if dd is None or tt is None else dd * 86400 + tt
        m = re.fullmatch(r"(\d{1,2}[A-Za-z]{3}\d{2,4}):(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)", s)
        if m and name in ("DATETIME", "ANYDTDTM"):
            dd, tt = _sx_parse_date(m.group(1)), _sx_parse_time(m.group(2))
            return None if dd is None or tt is None else dd * 86400 + tt
        if name == "ANYDTDTM":
            dd = _sx_any_date(s)
            return None if dd is None else dd * 86400
        return None
    if name in ("TIME", "E8601TM", "IS8601TM", "HHMMSS", "ANYDTTME"):
        return _sx_parse_time(s)
    raise ValueError(f"informat {name}. is not supported")


def _sx_input_kind(args):
    t = str(args[1] if len(args) > 1 else "").replace("?", "").strip()
    return "c" if t.startswith("$") else "n"


# ── the function table: name -> (impl, arg types, return type, min args, max args) ──────────
# arg types: c char (numbers BEST12.-padded), C char (numbers unpadded, CAT family), n numeric,
# a any (kept as-is), f format text; the last type repeats for variadic arguments.
def _sx_c1(fn):
    return lambda s: fn(s)


_SX_F = {
    # character: case, blanks, pieces
    "upcase": (_sx_c1(str.upper), "c", "c", 1, 1),
    "lowcase": (_sx_c1(str.lower), "c", "c", 1, 1),
    "propcase": (_f_propcase, "cc", "c", 1, 2),
    "strip": (_sx_c1(str.strip), "c", "c", 1, 1),
    "trim": (_sx_c1(str.rstrip), "c", "c", 1, 1),
    "trimn": (_sx_c1(str.rstrip), "c", "c", 1, 1),
    "left": (lambda s: s.lstrip() + " " * (len(s) - len(s.lstrip())), "c", "c", 1, 1),
    "right": (lambda s: " " * (len(s) - len(s.rstrip())) + s.rstrip(), "c", "c", 1, 1),
    "compbl": (lambda s: re.sub(r" {2,}", " ", s), "c", "c", 1, 1),
    "compress": (_f_compress, "ccc", "c", 1, 3),
    "reverse": (lambda s: s[::-1], "c", "c", 1, 1),
    "repeat": (lambda s, n: None if n is None else s * (int(n) + 1), "cn", "c", 2, 2),
    "substr": (_f_substr, "cnn", "c", 2, 3),
    "substrn": (_f_substrn, "cnn", "c", 2, 3),
    "char": (lambda s, p: s[int(p) - 1] if p is not None and 1 <= int(p) <= len(s) else "", "cn", "c", 2, 2),
    "first": (lambda s: s[:1], "c", "c", 1, 1),
    "scan": (_f_scan, "cncc", "c", 2, 4),
    "countw": (_f_countw, "ccc", "n", 1, 3),
    "translate": (_f_translate, "cc", "c", 3, None),
    "tranwrd": (_f_tranwrd, "ccc", "c", 3, 3),
    "transtrn": (_f_transtrn, "ccc", "c", 3, 3),
    "dequote": (_f_dequote, "c", "c", 1, 1),
    "quote": (lambda s, q=None: (q or '"')[:1] + s.rstrip().replace((q or '"')[:1], (q or '"')[:1] * 2) + (q or '"')[:1],
              "cc", "c", 1, 2),
    "cat": (_f_cat, "C", "c", 1, None),
    "cats": (lambda *a: "".join(x.strip() for x in a), "C", "c", 1, None),
    "catt": (lambda *a: "".join(x.rstrip() for x in a), "C", "c", 1, None),
    "catx": (_f_catx, "CC", "c", 2, None),
    "length": (_f_length, "c", "n", 1, 1),
    "lengthn": (lambda s: float(len(s.rstrip())), "c", "n", 1, 1),
    "lengthc": (lambda s: float(len(s)), "c", "n", 1, 1),
    # character: search & test
    "index": (lambda s, t: float(s.find(t) + 1) if t else 0.0, "cc", "n", 2, 2),
    "indexc": (lambda s, *cs: float(next((i + 1 for i, ch in enumerate(s) if any(ch in c for c in cs)), 0)),
               "cc", "n", 2, None),
    "indexw": (_f_indexw, "ccc", "n", 2, 3),
    "find": (_f_find, "ccaa", "n", 2, 4),
    "findc": (_f_findc, "ccaa", "n", 1, 4),
    "findw": (_f_findw, "cccn", "n", 2, 5),
    "count": (_f_count, "ccc", "n", 2, 3),
    "countc": (_f_countc, "ccc", "n", 2, 3),
    "verify": (_f_verify, "cc", "n", 2, None),
    **{f"any{k}": (_sx_anyfn(p, True), "cn", "n", 1, 2) for k, p in _SX_PRED.items()},
    **{f"not{k}": (_sx_anyfn(p, False), "cn", "n", 1, 2) for k, p in _SX_PRED.items()},
    "prxmatch": (_f_prxmatch, "cc", "n", 2, 2),
    "prxchange": (_f_prxchange, "cnc", "c", 3, 3),
    # selection & missing values
    "coalescec": (lambda *a: next((x for x in a if x.strip() != ""), ""), "C", "c", 1, None),
    "coalesce": (lambda *a: next((x for x in a if x is not None), None), "n", "n", 1, None),
    "ifc": (lambda c, t, f, *m: _f_ifx(c, t, f, *(m or [None]), _given=bool(m)), "nccc", "c", 3, 4),
    "ifn": (lambda c, t, f, *m: _f_ifx(c, t, f, *(m or [None]), _given=bool(m)), "nnnn", "n", 3, 4),
    "choosec": (_f_choose, "nc", "c", 2, None),
    "choosen": (_f_choose, "nn", "n", 2, None),
    "whichc": (_f_which, "cc", "n", 2, None),
    "whichn": (_f_which, "nn", "n", 2, None),
    "missing": (lambda x: 1.0 if (x.strip() == "" if isinstance(x, str) else x is None) else 0.0, "a", "n", 1, 1),
    "cmiss": (_f_cmiss, "a", "n", 1, None),
    "nmiss": (lambda *a: float(sum(1 for v in a if v is None)), "n", "n", 1, None),
    "n": (lambda *a: float(sum(1 for v in a if v is not None)), "n", "n", 1, None),
    # numeric
    "abs": (lambda x: None if x is None else abs(x), "n", "n", 1, 1),
    "ceil": (lambda x: None if x is None else float(math.ceil(round(x, 9))), "n", "n", 1, 1),
    "floor": (lambda x: None if x is None else float(math.floor(round(x, 9))), "n", "n", 1, 1),
    "int": (lambda x: None if x is None else float(math.trunc(round(x, 9))), "n", "n", 1, 1),
    "round": (_f_round, "nn", "n", 1, 2),
    "mod": (lambda a, b: None if a is None or not b else math.fmod(a, b), "nn", "n", 2, 2),
    "sign": (lambda x: None if x is None else float((x > 0) - (x < 0)), "n", "n", 1, 1),
    "sqrt": (lambda x: None if x is None or x < 0 else math.sqrt(x), "n", "n", 1, 1),
    "exp": (lambda x: None if x is None else math.exp(x), "n", "n", 1, 1),
    "log": (lambda x: None if x is None or x <= 0 else math.log(x), "n", "n", 1, 1),
    "log10": (lambda x: None if x is None or x <= 0 else math.log10(x), "n", "n", 1, 1),
    "log2": (lambda x: None if x is None or x <= 0 else math.log2(x), "n", "n", 1, 1),
    "min": (_sx_stat(min), "n", "n", 1, None),
    "max": (_sx_stat(max), "n", "n", 1, None),
    "sum": (_sx_stat(sum), "n", "n", 1, None),
    "mean": (_sx_stat(lambda xs: sum(xs) / len(xs)), "n", "n", 1, None),
    "median": (_f_median, "n", "n", 1, None),
    "var": (_f_var, "n", "n", 2, None),
    "std": (lambda *a: None if _f_var(*a) is None else math.sqrt(_f_var(*a)), "n", "n", 2, None),
    "range": (_sx_stat(lambda xs: max(xs) - min(xs)), "n", "n", 1, None),
    "largest": (_f_largest, "nn", "n", 2, None),
    "smallest": (_f_smallest, "nn", "n", 2, None),
    # conversion
    "put": (_sx_put, "af", "c", 2, 2),
    "putc": (_sx_put, "cc", "c", 2, 2),
    "putn": (_sx_put, "nc", "c", 2, 2),
    "input": (_sx_input, "cf", _sx_input_kind, 2, 2),
    "inputc": (_sx_input, "cc", "c", 2, 2),
    "inputn": (_sx_input, "cc", "n", 2, 2),
    # dates & times
    "today": (_f_today, "", "n", 0, 0),
    "date": (_f_today, "", "n", 0, 0),
    "datetime": (_f_datetime, "", "n", 0, 0),
    "time": (_f_time, "", "n", 0, 0),
    "mdy": (_f_mdy, "nnn", "n", 3, 3),
    "yyq": (_f_yyq, "nn", "n", 2, 2),
    "dhms": (_f_dhms, "nnnn", "n", 4, 4),
    "hms": (_f_hms, "nnn", "n", 3, 3),
    "year": (_sx_dpart(lambda d: d.year), "n", "n", 1, 1),
    "month": (_sx_dpart(lambda d: d.month), "n", "n", 1, 1),
    "day": (_sx_dpart(lambda d: d.day), "n", "n", 1, 1),
    "qtr": (_sx_dpart(lambda d: (d.month - 1) // 3 + 1), "n", "n", 1, 1),
    "weekday": (_sx_dpart(lambda d: (d.weekday() + 1) % 7 + 1), "n", "n", 1, 1),
    "hour": (_sx_tpart(3600, 24), "n", "n", 1, 1),
    "minute": (_sx_tpart(60, 60), "n", "n", 1, 1),
    "second": (lambda t: None if t is None else (t % 86400) % 60, "n", "n", 1, 1),
    "datepart": (lambda t: None if t is None else float(math.floor(t / 86400)), "n", "n", 1, 1),
    "timepart": (lambda t: None if t is None else t % 86400, "n", "n", 1, 1),
    "intck": (_f_intck, "cnnc", "n", 3, 4),
    "intnx": (_f_intnx, "cnnc", "n", 3, 4),
    "yrdif": (_f_yrdif, "nnc", "n", 2, 3),
    "datdif": (_f_datdif, "nnc", "n", 3, 3),
}


# ── evaluation entry points (what to_py emits) ──────────────────────────────────────────────
def _sx_fn(ix, f, *a):
    """Call SAS function `f` over Series/scalar arguments, row-wise with SAS semantics."""
    impl, types, ret, _lo, _hi = _SX_F[f]
    cols, vec = [], False
    for i, x in enumerate(a):
        t = types[min(i, len(types) - 1)] if types else "a"
        if t == "f":
            cols.append(x)
            continue
        if x is None:
            cols.append(None)
            continue
        if isinstance(x, pd.Series):
            s = x.reindex(ix) if not x.index.equals(ix) else x
            cols.append(_sx_vals(s, t))
            vec = True
        else:
            cols.append(_sx_one(x, t))
    rt = ret(a) if callable(ret) else ret

    def call(args):
        try:
            v = impl(*args)
        except (ValueError, TypeError, ZeroDivisionError, OverflowError, IndexError, re.error):
            v = None
        if rt == "c":
            return "" if v is None else (v if isinstance(v, str) else _sx_chr(v))
        return _sx_num(v)

    if not vec:
        return call(cols)
    n = len(ix)
    it = [c if isinstance(c, list) else repeat(c, n) for c in cols]
    return _sx_ser(ix, [call(r) for r in zip(*it)], rt)


def _sx_cat(ix, *xs):
    """SAS `||`: plain concatenation; a number converts with BEST12. (right-aligned)."""
    parts = []
    for x in xs:
        if isinstance(x, pd.Series):
            s = x.reindex(ix) if not x.index.equals(ix) else x
            parts.append(pd.Series(_sx_vals(s, "c"), index=ix, dtype="string").fillna(""))
        else:
            parts.append(pd.Series(_sx_chr(x), index=ix, dtype="string"))
    out = parts[0]
    for p in parts[1:]:
        out = out + p
    return out


def _sx_cmpv(ix, x):
    """Comparison operand: ('c', rstripped str Series) or ('n', float Series)."""
    if _sx_kind(x) == "c":
        return "c", _sx_s(ix, x).str.rstrip()
    return "n", _sx_n(ix, x)


def _sx_bool(ix, s):
    return pd.Series(np.where(s.fillna(False).to_numpy(dtype=bool), 1.0, 0.0), index=ix)


def _sx_op(ix, op, a, b):
    """A SAS binary operator. Arithmetic propagates missing; comparisons return 1/0 with missing
    lower than every number; character comparisons ignore trailing blanks; `=:` compares the
    shorter length (prefix)."""
    if op in ("+", "-", "*", "/", "**"):
        x, y = _sx_n(ix, a), _sx_n(ix, b)
        with np.errstate(all="ignore"):
            r = {"+": x + y, "-": x - y, "*": x * y, "/": x / y, "**": x ** y}[op]
        return r.where(np.isfinite(r))
    if op in ("and", "or"):
        x = _sx_n(ix, a).map(lambda v: v == v and v != 0)
        y = _sx_n(ix, b).map(lambda v: v == v and v != 0)
        return _sx_bool(ix, (x & y) if op == "and" else (x | y))
    ka, x = _sx_cmpv(ix, a)
    kb, y = _sx_cmpv(ix, b)
    if ka == "c" and kb == "c":
        if op == "=:":
            n = np.minimum(x.str.len(), y.str.len())
            r = pd.Series([u[:k] == v[:k] for u, v, k in zip(x, y, n)], index=ix)
        else:
            r = {"=": x == y, "^=": x != y, "<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]
        return _sx_bool(ix, r)
    if ka == "c":
        x = _sx_n(ix, x)
    if kb == "c":
        y = _sx_n(ix, y)
    if op == "=:":
        op = "="
    xf, yf = x.fillna(-np.inf), y.fillna(-np.inf)
    r = {"=": xf == yf, "^=": xf != yf, "<": xf < yf, "<=": xf <= yf, ">": xf > yf, ">=": xf >= yf}[op]
    return _sx_bool(ix, r)


def _sx_un(ix, op, x):
    if op == "not":
        return _sx_bool(ix, ~_sx_n(ix, x).map(lambda v: v == v and v != 0).astype(bool))
    v = _sx_n(ix, x)
    return -v if op == "-" else v


def _sx_in(ix, x, items, neg=False):
    """`x in (a, b, …)` — character values compare without trailing blanks."""
    k, v = _sx_cmpv(ix, x)
    if k == "c":
        r = v.isin([_sx_chr(i).rstrip() for i in items])
    else:
        nums = [_sx_num(i) for i in items]
        r = v.isin([n for n in nums if n is not None]) | (v.isna() if any(n is None for n in nums) else False)
    return _sx_bool(ix, ~r if neg else r)


def _sx_out(x):
    """Final value: a character result keeps its text (blank becomes NA, trailing blanks drop);
    a numeric result stays numeric (missing is NaN)."""
    if isinstance(x, pd.Series) and _sx_kind(x) == "n":
        return x.astype("float64")
    if not isinstance(x, pd.Series):
        return x
    x = x.astype("string").str.rstrip()
    return x.where(x != "", pd.NA)


def _sx_res(ix, x):
    """A scalar or Series expression value as the variable's final Series."""
    if not isinstance(x, pd.Series):
        x = _sx_s(ix, x) if isinstance(x, str) else _sx_n(ix, x)
    return _sx_out(x)


# ── BY-group carry-forward: RETAIN and LAG / DIF ────────────────────────────────────────────
def _sx_sorted(frame, by, order):
    """(row labels in PROC SORT order — BY then ORDER variables, missing first, ties kept in
    input order —, the BY-group key of each of those rows). A column whose values are all
    numbers sorts as numbers, like a SAS numeric variable; otherwise as text."""
    keys = [c for c in [*by, *order] if c in frame.columns]
    work = pd.DataFrame(index=frame.index)
    cols = []
    for i, c in enumerate(keys):
        col = frame[c]
        txt = col.astype("string").str.strip().fillna("")
        num = pd.to_numeric(txt.where(txt != ""), errors="coerce")
        present = txt != ""
        numeric = bool(present.any()) and bool(num[present].notna().all())
        work[f"m{i}"] = present.astype(int)                       # missing sorts first
        work[f"v{i}"] = num.fillna(-np.inf) if numeric else txt
        cols += [f"m{i}", f"v{i}"]
    ordx = work.sort_values(cols, kind="mergesort").index if cols else frame.index
    byk = [c for c in by if c in frame.columns]
    if not byk:
        return ordx, pd.Series("", index=ordx)
    g = frame.loc[ordx, byk].astype("string").fillna("").agg("\x1f".join, axis=1)
    return ordx, g


def _sx_blank(s):
    """True where a value is SAS-missing (NA, NaN or an all-blank string)."""
    return s.isna() | s.astype("string").str.strip().fillna("").eq("")


def _sx_typed(s, like):
    """Carry `like`'s SAS type: numeric stays float, character stays text (missing = NA)."""
    if _sx_kind(like) == "n":
        return pd.to_numeric(s, errors="coerce").astype("float64")
    out = s.astype("string")
    return out.where(~_sx_blank(out), pd.NA)


def _sx_retain(frame, src, by=(), order=(), mode="locf"):
    """RETAIN carry-forward of `src` within BY groups, in BY + ORDER order:
    mode 'locf' — the last non-missing value so far (if not missing(x) then _r = x);
    mode 'first' — the first non-missing value of the group (if missing(_r) then _r = x).
    The result is returned in the frame's own row order."""
    src = src.reindex(frame.index) if isinstance(src, pd.Series) else pd.Series(src, index=frame.index)
    ordx, g = _sx_sorted(frame, list(by or []), list(order or []))
    v = src.loc[ordx].astype(object).where(~_sx_blank(src.loc[ordx]), None)
    if str(mode or "locf").lower() == "first":
        seen = v.notna().groupby(g.values).cumsum()
        v = v.where(v.notna() & (seen == 1), None)
    out = v.groupby(g.values).ffill()
    return _sx_typed(out.reindex(frame.index), src)


def _sx_lag(frame, src, by=(), order=(), n=1, dif=False):
    """LAGn of `src` within BY groups (missing for a group's first n records), in BY + ORDER
    order; with `dif`, DIFn = x - LAGn(x) (numeric, missing propagates)."""
    src = src.reindex(frame.index) if isinstance(src, pd.Series) else pd.Series(src, index=frame.index)
    ordx, g = _sx_sorted(frame, list(by or []), list(order or []))
    v = src.loc[ordx].astype(object).where(~_sx_blank(src.loc[ordx]), None)
    lag = v.groupby(g.values).shift(max(int(n or 1), 1))
    if dif:
        cur = _sx_n(ordx, v)
        prev = _sx_n(ordx, lag)
        return (cur - prev).reindex(frame.index).astype("float64")
    return _sx_typed(lag.reindex(frame.index), src)


def _sx_if(ix, chains):
    """IF / ELSE IF / ELSE chains assigning one variable, in DATA-step order: in each chain the
    first true condition (non-zero, non-missing) sets the value, ELSE covers the rest, and a chain
    without ELSE leaves unmatched rows as an earlier chain left them (initially missing)."""
    def obj(v):
        if isinstance(v, pd.Series):
            s = v.reindex(ix) if not v.index.equals(ix) else v
            return pd.Series(_sx_vals(s, "a"), index=ix, dtype=object)
        return pd.Series([v if isinstance(v, str) else _sx_num(v)] * len(ix), index=ix, dtype=object)
    out = pd.Series([None] * len(ix), index=ix, dtype=object)
    for branches, els in chains:
        done = pd.Series(False, index=ix)
        for c, v in branches:
            hit = _sx_n(ix, c).fillna(0).ne(0) & ~done
            out = out.where(~hit, obj(v))
            done = done | hit
        if els is not None:
            out = out.where(done, obj(els))
    vals = [v for v in out.tolist() if v is not None and not (isinstance(v, str) and v.strip() == "")]
    if vals and all(not isinstance(v, str) for v in vals):
        return pd.to_numeric(out, errors="coerce").astype("float64")
    return pd.Series(["" if v is None else (v if isinstance(v, str) else _sx_chr(v, pad=False)) for v in out],
                     index=ix, dtype="string")


HELPERS = (_sx_fn, _sx_cat, _sx_op, _sx_un, _sx_in, _sx_out, _sx_res, _sx_s, _sx_n,
           _sx_retain, _sx_lag, _sx_if)
