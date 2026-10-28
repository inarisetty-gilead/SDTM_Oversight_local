"""The SAS function catalog: what each function does, its syntax, an example and its arguments.

Behaviour lives in runtime._SX_F (one implementation for the whole build); this module is
documentation and form metadata only. tests/test_sas_runtime.py keeps the two in step: every
runtime function is documented here and every documented example is checked against the runtime.

A function picked from the catalog with its arguments filled in becomes a SAS expression AST
(fn_tree), so it builds through exactly the same path as spec SAS.
"""
from __future__ import annotations

from . import runtime as _rt

CATEGORIES = [
    "Character: case & blanks", "Character: pieces & joins", "Character: search & test",
    "Regular expressions", "Missing values & selection", "Numeric & rounding",
    "Descriptive statistics", "Conversion: PUT / INPUT", "Dates & times",
]

INTERVALS = ["day", "week", "month", "qtr", "semiyear", "year", "weekday",
             "dtday", "dtweek", "dtmonth", "dtqtr", "dtyear", "hour", "minute", "second"]


def _p(name, kind, label=None, default=None, optional=False, options=None):
    """A parameter. kind: src (one input) | srcs (one or more inputs) | num | text | mods |
    fmt (format) | infmt (informat) | interval | choice."""
    return {"name": name, "kind": kind, "label": label or name, "default": default,
            "optional": optional, **({"options": options} if options else {})}


S = _p("string", "src", "input")
N = _p("value", "src", "input")
D = _p("date", "src", "SAS date")
SS = _p("items", "srcs", "inputs")


def _f(name, cat, syntax, summary, example, params, note=""):
    return {"name": name, "category": cat, "syntax": syntax, "summary": summary,
            "example": example, "params": params, **({"note": note} if note else {})}


C1, C2, C3, RX, MS, NM, ST, CV, DT = CATEGORIES

FUNCTIONS = [
    # ── character: case & blanks ────────────────────────────────────────────────────────────
    _f("upcase", C1, "UPCASE(string)", "Uppercase.", "upcase('Severe') = 'SEVERE'", [S]),
    _f("lowcase", C1, "LOWCASE(string)", "Lowercase.", "lowcase('MG') = 'mg'", [S]),
    _f("propcase", C1, "PROPCASE(string <, delimiters>)",
       "First letter of each word up, the rest down.", "propcase('JOHN o''NEIL') = 'John O''neil'",
       [S, _p("delimiters", "text", optional=True)]),
    _f("strip", C1, "STRIP(string)", "Remove leading and trailing blanks.", "strip('  A  ') = 'A'", [S]),
    _f("trim", C1, "TRIM(string)", "Remove trailing blanks.", "trim('A  ') || 'B' = 'AB'", [S]),
    _f("trimn", C1, "TRIMN(string)", "Remove trailing blanks; a blank string becomes empty.",
       "trimn('A  ') || 'B' = 'AB'", [S]),
    _f("left", C1, "LEFT(string)", "Left-align (leading blanks move to the end).", "strip(left('  A')) = 'A'", [S]),
    _f("right", C1, "RIGHT(string)", "Right-align (trailing blanks move to the front).", "right('A  ') = '  A'", [S]),
    _f("compbl", C1, "COMPBL(string)", "Squeeze runs of blanks to one blank.", "compbl('A   B') = 'A B'", [S]),
    _f("compress", C1, "COMPRESS(string <, chars> <, modifiers>)",
       "Remove characters (blanks by default); modifier K keeps them instead. Modifiers: a letters, "
       "d digits, s spaces, p punctuation, u/l upper/lower, i ignore case, k keep.",
       "compress('A-1 B/2', , 'kd') = '12'",
       [S, _p("chars", "text", "characters", optional=True), _p("mods", "mods", "modifiers", optional=True)]),
    _f("translate", C1, "TRANSLATE(string, to, from <, to, from …>)", "Replace single characters, position by position.",
       "translate('1/2/3', '-', '/') = '1-2-3'", [S, _p("to", "text"), _p("from", "text")]),
    _f("tranwrd", C1, "TRANWRD(string, target, replacement)", "Replace every occurrence of a word or substring.",
       "tranwrd('MILLIGRAM', 'MILLI', 'M') = 'MGRAM'",
       [S, _p("find", "text", "target"), _p("replace", "text", "replacement")],
       note="An empty replacement inserts one blank in SAS; use TRANSTRN to delete."),
    _f("transtrn", C1, "TRANSTRN(string, target, replacement)",
       "Like TRANWRD, but the replacement may be empty (deletes the target).",
       "transtrn('10 MG/KG', ' ', trimn('')) = '10MG/KG'",
       [S, _p("find", "text", "target"), _p("replace", "text", "replacement")]),
    _f("dequote", C1, "DEQUOTE(string)", "Remove enclosing quotation marks.", "dequote('\"ABC\"') = 'ABC'", [S]),
    _f("quote", C1, "QUOTE(string <, mark>)", "Add enclosing quotation marks.", "quote('AB') = '\"AB\"'",
       [S, _p("mark", "text", optional=True)]),
    _f("reverse", C1, "REVERSE(string)", "Reverse the characters.", "reverse('ABC') = 'CBA'", [S]),
    _f("repeat", C1, "REPEAT(string, n)", "The string repeated n more times.", "repeat('-', 2) = '---'",
       [S, _p("n", "num", default=1)]),
    # ── character: pieces & joins ───────────────────────────────────────────────────────────
    _f("substr", C2, "SUBSTR(string, position <, length>)", "Extract part of a string (1-based).",
       "substr('2020-01-05', 1, 7) = '2020-01'", [S, _p("start", "num", "position", 1), _p("len", "num", "length", optional=True)]),
    _f("substrn", C2, "SUBSTRN(string, position <, length>)",
       "SUBSTR that never errors: positions outside the string are just dropped.",
       "substrn('ABC', 0, 2) = 'A'", [S, _p("start", "num", "position", 1), _p("len", "num", "length", optional=True)]),
    _f("scan", C2, "SCAN(string, n <, delimiters> <, modifiers>)",
       "The n-th word; negative n counts from the right. A run of delimiters counts once (modifier M: each one).",
       "scan('GS-101-0001', -2, '-') = '101'",
       [S, _p("word", "num", "n", 1), _p("delim", "text", "delimiters", optional=True),
        _p("mods", "mods", "modifiers", optional=True)]),
    _f("countw", C2, "COUNTW(string <, delimiters> <, modifiers>)", "Number of words.", "countw('A B  C') = 3",
       [S, _p("delim", "text", "delimiters", optional=True), _p("mods", "mods", "modifiers", optional=True)]),
    _f("char", C2, "CHAR(string, position)", "The single character at a position.", "char('ABC', 2) = 'B'",
       [S, _p("pos", "num", "position", 1)]),
    _f("first", C2, "FIRST(string)", "The first character.", "first('Yes') = 'Y'", [S]),
    _f("cat", C2, "CAT(item, …)", "Concatenate as-is (blanks kept).", "cat('A ', 'B') = 'A B'", [SS]),
    _f("cats", C2, "CATS(item, …)", "Concatenate, stripping each item.", "cats(' A ', ' B ') = 'AB'", [SS]),
    _f("catt", C2, "CATT(item, …)", "Concatenate, trimming trailing blanks of each item.", "catt('A ', 'B') = 'AB'", [SS]),
    _f("catx", C2, "CATX(delimiter, item, …)", "Strip each item, drop blank ones, join with the delimiter.",
       "catx('-', '2020', ' ', '05') = '2020-05'", [_p("sep", "text", "delimiter", " "), SS]),
    _f("length", C2, "LENGTH(string)", "Length without trailing blanks (1 for a blank string).",
       "length('AB  ') = 2", [S]),
    _f("lengthn", C2, "LENGTHN(string)", "Length without trailing blanks (0 for a blank string).", "lengthn('') = 0", [S]),
    _f("lengthc", C2, "LENGTHC(string)", "Length including trailing blanks.", "lengthc('AB  ') = 4", [S]),
    # ── character: search & test ────────────────────────────────────────────────────────────
    _f("index", C3, "INDEX(string, excerpt)", "Position of a substring (0 if absent).", "index('ABCDE', 'CD') = 3",
       [S, _p("find", "text", "excerpt")]),
    _f("indexc", C3, "INDEXC(string, chars, …)", "Position of the first of any of the characters.",
       "indexc('AB-1', '0123456789') = 4", [S, _p("chars", "text", "characters")]),
    _f("indexw", C3, "INDEXW(string, word <, delimiters>)", "Position of a whole word.",
       "indexw('NOT DONE', 'DONE') = 5", [S, _p("word", "text"), _p("delim", "text", "delimiters", optional=True)]),
    _f("find", C3, "FIND(string, substring <, modifiers> <, start>)",
       "Position of a substring; modifier I ignores case, T trims; a negative start searches leftward.",
       "find('Not Done', 'done', 'i') = 5",
       [S, _p("find", "text", "substring"), _p("mods", "mods", "modifiers", optional=True),
        _p("start", "num", optional=True)]),
    _f("findc", C3, "FINDC(string, chars <, modifiers> <, start>)",
       "Position of the first character from a list or class (modifiers a d s p u l, K = not in list).",
       "findc('AB12', , 'd') = 3",
       [S, _p("chars", "text", "characters", optional=True), _p("mods", "mods", "modifiers", optional=True),
        _p("start", "num", optional=True)]),
    _f("findw", C3, "FINDW(string, word <, delimiters> <, modifiers>)",
       "Position of a whole word (modifier E returns the word number).", "findw('GRADE 3 AE', 'AE') = 9",
       [S, _p("word", "text"), _p("delim", "text", "delimiters", optional=True),
        _p("mods", "mods", "modifiers", optional=True)]),
    _f("count", C3, "COUNT(string, substring <, modifiers>)", "How many times a substring occurs.",
       "count('A,B,C', ',') = 2", [S, _p("find", "text", "substring"), _p("mods", "mods", "modifiers", optional=True)]),
    _f("countc", C3, "COUNTC(string, chars <, modifiers>)", "How many characters are from a list or class.",
       "countc('A1B22', , 'd') = 3",
       [S, _p("chars", "text", "characters", optional=True), _p("mods", "mods", "modifiers", optional=True)]),
    _f("verify", C3, "VERIFY(string, chars, …)", "Position of the first character NOT in the list (0 if all are).",
       "verify('12A4', '0123456789') = 3", [S, _p("chars", "text", "characters")]),
    *[_f(f"any{k}", C3, f"ANY{k.upper()}(string <, start>)", f"Position of the first {w} character (0 if none).",
         ex, [S, _p("start", "num", optional=True)])
      for k, w, ex in [("alpha", "letter", "anyalpha('12AB') = 3"), ("digit", "digit", "anydigit('AB1') = 3"),
                       ("alnum", "letter or digit", "anyalnum('--A') = 3"),
                       ("punct", "punctuation", "anypunct('AB-C') = 3"), ("space", "blank/whitespace", "anyspace('A B') = 2"),
                       ("upper", "uppercase", "anyupper('abC') = 3"), ("lower", "lowercase", "anylower('ABc') = 3"),
                       ("cntrl", "control", "anycntrl('AB') = 0")]],
    *[_f(f"not{k}", C3, f"NOT{k.upper()}(string <, start>)", f"Position of the first character that is NOT {w} (0 if none).",
         ex, [S, _p("start", "num", optional=True)])
      for k, w, ex in [("alpha", "a letter", "notalpha('AB1') = 3"), ("digit", "a digit", "notdigit('12.5') = 3"),
                       ("alnum", "a letter or digit", "notalnum('AB-1') = 3"),
                       ("punct", "punctuation", "notpunct('-A') = 2"), ("space", "blank/whitespace", "notspace('  A') = 3"),
                       ("upper", "uppercase", "notupper('ABc') = 3"), ("lower", "lowercase", "notlower('abC') = 3"),
                       ("cntrl", "a control character", "notcntrl('AB') = 1")]],
    # ── regular expressions ─────────────────────────────────────────────────────────────────
    _f("prxmatch", RX, "PRXMATCH('/regex/flags', string)", "Position where a Perl regular expression matches (0 if none).",
       "prxmatch('/\\d{4}-\\d{2}/', 'ON 2020-01') = 4", [_p("regex", "text", "/regex/"), S]),
    _f("prxchange", RX, "PRXCHANGE('s/regex/replacement/flags', times, string)",
       "Regex find-and-replace; times = -1 replaces every match. $1… refer to groups.",
       "prxchange('s/(\\d+)\\s*MG/$1 mg/i', -1, '10MG') = '10 mg'",
       [_p("regex", "text", "s/regex/replacement/"), _p("times", "num", default=-1), S]),
    # ── missing values & selection ──────────────────────────────────────────────────────────
    _f("coalescec", MS, "COALESCEC(string, …)", "First non-blank character value.", "coalescec('', 'UNK') = 'UNK'", [SS]),
    _f("coalesce", MS, "COALESCE(number, …)", "First non-missing numeric value.", "coalesce(., 5) = 5", [SS]),
    _f("ifc", MS, "IFC(condition, true, false <, missing>)",
       "Character result chosen by a condition (non-zero, non-missing = true).", "ifc(3 > 2, 'Y', 'N') = 'Y'",
       [_p("cond", "src", "condition"), _p("true", "text"), _p("false", "text"), _p("missing", "text", optional=True)]),
    _f("ifn", MS, "IFN(condition, true, false <, missing>)", "Numeric result chosen by a condition.",
       "ifn(. > 2, 1, 0) = 0",
       [_p("cond", "src", "condition"), _p("true", "num"), _p("false", "num"), _p("missing", "num", optional=True)]),
    _f("choosec", MS, "CHOOSEC(n, value, …)", "The n-th of the listed character values.",
       "choosec(2, 'MILD', 'MODERATE', 'SEVERE') = 'MODERATE'", [_p("n", "src", "index"), SS]),
    _f("choosen", MS, "CHOOSEN(n, value, …)", "The n-th of the listed numeric values.", "choosen(3, 10, 20, 30) = 30",
       [_p("n", "src", "index"), SS]),
    _f("whichc", MS, "WHICHC(value, candidate, …)", "Which candidate equals the value (0 if none).",
       "whichc('Y', 'N', 'Y') = 2", [S, SS]),
    _f("whichn", MS, "WHICHN(value, candidate, …)", "Which numeric candidate equals the value (0 if none).",
       "whichn(7, 1, 7) = 2", [N, SS]),
    _f("missing", MS, "MISSING(value)", "1 when the value is missing (blank or .), else 0.", "missing('') = 1", [N]),
    _f("cmiss", MS, "CMISS(value, …)", "How many values are missing (any type).", "cmiss('', 1, .) = 2", [SS]),
    _f("nmiss", MS, "NMISS(number, …)", "How many numeric values are missing.", "nmiss(1, ., 3) = 1", [SS]),
    _f("n", MS, "N(number, …)", "How many numeric values are present.", "n(1, ., 3) = 2", [SS]),
    # ── numeric & rounding ──────────────────────────────────────────────────────────────────
    _f("abs", NM, "ABS(number)", "Absolute value.", "abs(-2.5) = 2.5", [N]),
    _f("ceil", NM, "CEIL(number)", "Smallest integer ≥ the number.", "ceil(2.1) = 3", [N]),
    _f("floor", NM, "FLOOR(number)", "Largest integer ≤ the number.", "floor(-2.1) = -3", [N]),
    _f("int", NM, "INT(number)", "Integer part (toward zero).", "int(-2.7) = -2", [N]),
    _f("round", NM, "ROUND(number <, unit>)", "Round to the nearest multiple of unit (halves away from zero).",
       "round(2.345, 0.01) = 2.35", [N, _p("unit", "num", optional=True)]),
    _f("mod", NM, "MOD(number, divisor)", "Remainder (sign of the number).", "mod(10, 3) = 1",
       [N, _p("divisor", "num", default=2)]),
    _f("sign", NM, "SIGN(number)", "-1, 0 or 1.", "sign(-4) = -1", [N]),
    _f("sqrt", NM, "SQRT(number)", "Square root.", "sqrt(16) = 4", [N]),
    _f("exp", NM, "EXP(number)", "e raised to the number.", "round(exp(1), 0.001) = 2.718", [N]),
    _f("log", NM, "LOG(number)", "Natural logarithm.", "round(log(10), 0.0001) = 2.3026", [N]),
    _f("log10", NM, "LOG10(number)", "Base-10 logarithm.", "log10(1000) = 3", [N]),
    _f("log2", NM, "LOG2(number)", "Base-2 logarithm.", "log2(8) = 3", [N]),
    # ── descriptive statistics (across arguments of one row) ────────────────────────────────
    _f("sum", ST, "SUM(number, …)", "Sum of the non-missing values.", "sum(1, ., 3) = 4", [SS]),
    _f("min", ST, "MIN(number, …)", "Smallest non-missing value.", "min(4, ., 2) = 2", [SS]),
    _f("max", ST, "MAX(number, …)", "Largest non-missing value.", "max(4, ., 2) = 4", [SS]),
    _f("mean", ST, "MEAN(number, …)", "Mean of the non-missing values.", "mean(1, ., 3) = 2", [SS]),
    _f("median", ST, "MEDIAN(number, …)", "Median of the non-missing values.", "median(1, 9, 3) = 3", [SS]),
    _f("std", ST, "STD(number, …)", "Standard deviation of the non-missing values.", "std(2, 4) = 1.4142135623730951", [SS]),
    _f("var", ST, "VAR(number, …)", "Variance of the non-missing values.", "var(2, 4) = 2", [SS]),
    _f("range", ST, "RANGE(number, …)", "Largest minus smallest.", "range(3, 9, 5) = 6", [SS]),
    _f("largest", ST, "LARGEST(k, number, …)", "The k-th largest value.", "largest(2, 5, 9, 1) = 5",
       [_p("k", "num", default=1), SS]),
    _f("smallest", ST, "SMALLEST(k, number, …)", "The k-th smallest value.", "smallest(1, 5, ., 1) = 1",
       [_p("k", "num", default=1), SS]),
    # ── conversion ──────────────────────────────────────────────────────────────────────────
    _f("put", CV, "PUT(value, format.)",
       "Value → text with a format (best12., z3., 8.2, yymmdd10., e8601da., date9., e8601dt., time5., $20.). "
       "Numeric formats right-align; wrap in STRIP() to left-align.",
       "put(3, z3.) = '003'", [N, _p("fmt", "fmt", "format", "best12.")]),
    _f("input", CV, "INPUT(text, informat.)",
       "Text → value with an informat (best32., 8., comma12., yymmdd10., date9., e8601da., anydtdte., "
       "e8601dt., time5., $20.). Dates become SAS dates (days since 01JAN1960).",
       "input('2020-01-05', yymmdd10.) = 21919", [S, _p("fmt", "infmt", "informat", "best32.")]),
    _f("putc", CV, "PUTC(text, 'format.')", "PUT with a character format given as text.", "putc('ab', '$upcase.') = 'AB'",
       [S, _p("fmt", "text", "format")]),
    _f("putn", CV, "PUTN(number, 'format.')", "PUT with a numeric format given as text.", "putn(5, 'z2.') = '05'",
       [N, _p("fmt", "text", "format")]),
    _f("inputc", CV, "INPUTC(text, 'informat.')", "INPUT with a character informat given as text.",
       "inputc('  ab', '$5.') = 'ab'", [S, _p("fmt", "text", "informat")]),
    _f("inputn", CV, "INPUTN(text, 'informat.')", "INPUT with a numeric informat given as text.",
       "inputn('1,250', 'comma8.') = 1250", [S, _p("fmt", "text", "informat")]),
    # ── dates & times ───────────────────────────────────────────────────────────────────────
    _f("today", DT, "TODAY()", "Today's date (SAS date).", "today() > '01JAN2020'd = 1", []),
    _f("date", DT, "DATE()", "Today's date (same as TODAY).", "date() > '01JAN2020'd = 1", []),
    _f("datetime", DT, "DATETIME()", "The current date and time (SAS datetime).", "datetime() > 0 = 1", []),
    _f("time", DT, "TIME()", "The current time of day (seconds).", "time() >= 0 = 1", []),
    _f("mdy", DT, "MDY(month, day, year)", "A SAS date from its parts.", "put(mdy(1, 5, 2020), yymmdd10.) = '2020-01-05'",
       [_p("month", "src"), _p("day", "src"), _p("year", "src")]),
    _f("yyq", DT, "YYQ(year, quarter)", "First day of a quarter.", "put(yyq(2020, 2), date9.) = '01APR2020'",
       [_p("year", "src"), _p("quarter", "num", default=1)]),
    _f("dhms", DT, "DHMS(date, hour, minute, second)", "A SAS datetime from a date and a time.",
       "put(dhms('05JAN2020'd, 9, 30, 0), e8601dt.) = '2020-01-05T09:30:00'",
       [D, _p("hour", "num", default=0), _p("minute", "num", default=0), _p("second", "num", default=0)]),
    _f("hms", DT, "HMS(hour, minute, second)", "A SAS time from its parts.", "put(hms(9, 5, 0), time5.) = ' 9:05'",
       [_p("hour", "src"), _p("minute", "num", default=0), _p("second", "num", default=0)]),
    _f("year", DT, "YEAR(date)", "The year of a SAS date.", "year('05JAN2020'd) = 2020", [D]),
    _f("month", DT, "MONTH(date)", "The month (1–12).", "month('05JAN2020'd) = 1", [D]),
    _f("day", DT, "DAY(date)", "The day of the month.", "day('05JAN2020'd) = 5", [D]),
    _f("qtr", DT, "QTR(date)", "The quarter (1–4).", "qtr('05MAY2020'd) = 2", [D]),
    _f("weekday", DT, "WEEKDAY(date)", "Day of the week, 1 = Sunday … 7 = Saturday.", "weekday('05JAN2020'd) = 1", [D]),
    _f("hour", DT, "HOUR(time or datetime)", "The hour.", "hour('05JAN2020:14:30:00'dt) = 14", [_p("time", "src")]),
    _f("minute", DT, "MINUTE(time or datetime)", "The minute.", "minute('14:30't) = 30", [_p("time", "src")]),
    _f("second", DT, "SECOND(time or datetime)", "The second.", "second('14:30:15't) = 15", [_p("time", "src")]),
    _f("datepart", DT, "DATEPART(datetime)", "The date of a datetime.",
       "put(datepart('05JAN2020:14:30:00'dt), yymmdd10.) = '2020-01-05'", [_p("datetime", "src")]),
    _f("timepart", DT, "TIMEPART(datetime)", "The time of a datetime.",
       "put(timepart('05JAN2020:14:30:00'dt), time5.) = '14:30'", [_p("datetime", "src")]),
    _f("intck", DT, "INTCK('interval', from, to <, 'method'>)",
       "How many interval boundaries lie between two dates ('c' = continuous: whole intervals elapsed).",
       "intck('month', '31JAN2020'd, '01FEB2020'd) = 1",
       [_p("interval", "interval", default="day", options=INTERVALS), _p("from", "src"), _p("to", "src"),
        _p("method", "choice", optional=True, options=["discrete", "continuous"])]),
    _f("intnx", DT, "INTNX('interval', start, n <, 'alignment'>)",
       "Move a date by n intervals; alignment b(eginning) / m(iddle) / e(nd) / s(ame day).",
       "put(intnx('month', '31JAN2020'd, 1, 'e'), date9.) = '29FEB2020'",
       [_p("interval", "interval", default="month", options=INTERVALS), D, _p("n", "num", default=1),
        _p("align", "choice", "alignment", optional=True, options=["b", "m", "e", "s"])]),
    _f("yrdif", DT, "YRDIF(start, end <, 'basis'>)",
       "Years between two dates; basis 'AGE' gives age in years (floor it for AGE), or ACT/ACT, 30/360, ACT/360, ACT/365.",
       "floor(yrdif('15JUN1980'd, '14JUN2020'd, 'AGE')) = 39",
       [_p("start", "src"), _p("end", "src"),
        _p("basis", "choice", default="AGE", optional=True, options=["AGE", "ACT/ACT", "30/360", "ACT/360", "ACT/365"])]),
    _f("datdif", DT, "DATDIF(start, end, 'basis')", "Days between two dates (ACT/ACT or 30/360).",
       "datdif('01JAN2020'd, '01MAR2020'd, 'ACT/ACT') = 60",
       [_p("start", "src"), _p("end", "src"), _p("basis", "choice", default="ACT/ACT", options=["ACT/ACT", "30/360"])]),
]

BY_NAME = {f["name"]: f for f in FUNCTIONS}

# Older Transform-function recipes saved before the library existed: same keys, mapped onto SAS.
LEGACY = {
    "zeropad": {"name": "zeropad", "category": CV, "syntax": "PUT(INPUT(value, best32.), Zw.)",
                "summary": "Left-pad a number with zeros to a width (SAS: the Zw. format).",
                "example": "put(input('7', best32.), z3.) = '007'",
                "params": [S, _p("width", "num", "width", 2)]},
}

# SAS statements & procedures a SAS programmer uses to transform data, and where the app does
# each one. status: built (a recipe/prep step does it), expression (write it as a SAS
# expression), gap (not available yet).
def _st(sas, status, what, example, app):
    return {"sas": sas, "status": status, "what": what, "example": example, "app": app}


STATEMENTS = [
    _st("Assignment  var = expression;", "built",
        "Sets a variable to the value of an expression for every record.",
        "AGE = floor(yrdif(input(BRTHDTC, yymmdd10.), input(RFSTDTC, yymmdd10.), 'AGE'));",
        "Variable: SAS function / Multiple steps, or the spec's own SAS. Prepare data: Compute (SAS)."),
    _st("IF / THEN / ELSE IF / ELSE", "built",
        "Chooses a value by testing conditions in order; the first true one wins.",
        "if AETOXGR in ('3','4','5') then AESEV = 'SEVERE';\nelse if AETOXGR = '2' then AESEV = 'MODERATE';\nelse AESEV = 'MILD';",
        "Variable: IF / THEN / ELSE. Prepare data: Derive."),
    _st("SELECT / WHEN / OTHERWISE", "built",
        "The same choice written as a list of cases — SELECT(x); WHEN(value) …; OTHERWISE …; END;",
        "select (VSTESTCD);\n  when ('SYSBP', 'DIABP') VSCAT = 'BLOOD PRESSURE';\n  otherwise VSCAT = 'OTHER';\nend;",
        "Variable: IF / THEN / ELSE — each WHEN is a rule, OTHERWISE is the else."),
    _st("WHERE / subsetting IF", "built",
        "Keeps only the records for which a condition is true.",
        "data ae1; set raw.ae; if not missing(AETERM) and AESTDAT >= '01JAN2020'd; run;",
        "Prepare data: Filter — a column test, or any SAS expression (subsetting IF)."),
    _st("SET (stack datasets)", "built",
        "Reads datasets one after another, so their records are stacked into one dataset.",
        "data ae_all; set raw.ae raw.ae_sae; run;",
        "Prepare data: Stack."),
    _st("MERGE … BY, IN= flags", "built",
        "Joins datasets side by side on BY variables; IN= flags tell which dataset a record came from.",
        "data dm1; merge raw.dm(in=a) raw.enroll(in=b); by SUBJID; if a; run;",
        "Prepare data: Merge (left/inner/outer/right); xx-merge lookup recipe on a variable."),
    _st("PROC SQL joins", "built",
        "Joins tables with SQL, including on keys whose names differ.",
        "proc sql; create table x as select a.*, b.INVNAME from dm a left join inv b on a.SITEID = b.SITEID; quit;",
        "Prepare data: Merge with a cross-key map (left column = right column)."),
    _st("PROC SORT NODUPKEY / FIRST. & LAST.", "built",
        "Sorts records; NODUPKEY or FIRST./LAST. keep one record per BY group.",
        "proc sort data=ex; by USUBJID EXSTDTC; run;\ndata ex1; set ex; by USUBJID; if first.USUBJID; run;",
        "Domain settings: sort + dedup (keep first/last); lookup merge 'IF LAST.' option; Aggregate."),
    _st("PROC TRANSPOSE (wide ↔ long)", "built",
        "Turns columns into rows (one record per test) or rows into columns.",
        "proc transpose data=vs_wide out=vs_long(rename=(_NAME_=VSTESTCD COL1=VSORRES));\n  by USUBJID VISIT; var SYSBP DIABP PULSE;\nrun;",
        "Prepare data: Transpose → findings / long / wide; sequence pivot recipe on a variable."),
    _st("PROC SUMMARY / MEANS (min, max, …)", "built",
        "Computes statistics per group (count, min, max, mean …) into a new dataset.",
        "proc summary data=ex nway; class USUBJID; var EXSTDT; output out=rf min=RFXSTDT; run;",
        "Prepare data: Aggregate and Earliest/Latest date; Earliest/Latest date recipe on a variable."),
    _st("KEEP / DROP / RENAME", "built",
        "Chooses which variables are kept, removed or renamed.",
        "data dm1; set raw.dm(keep=SUBJID SEX BRTHDAT rename=(BRTHDAT=BRTHDTC)); run;",
        "Prepare data: Keep / Delete / Rename columns; Exclude (drop) on a variable."),
    _st("OUTPUT to several datasets", "built",
        "Writes each record to one or more output datasets from the same DATA step.",
        "data ae_ser ae_non; set ae; if AESER = 'Y' then output ae_ser; else output ae_non; run;",
        "Prepare data: Split rows."),
    _st("--SEQ counter (RETAIN + BY group)", "built",
        "Numbers records 1, 2, 3 … within each subject (a retained counter reset at FIRST.).",
        "data ae; set ae; by USUBJID; if first.USUBJID then AESEQ = 0; AESEQ + 1; run;",
        "Variable: Sequence number (numbered on the final sort order)."),
    _st("--DY study day, ISO 8601 --DTC", "built",
        "Turns collected dates into ISO 8601 text and counts the study day from the reference start date.",
        "AESTDTC = put(AESTDAT, e8601da.);\nAESTDY = AESTDT - RFSTDT + (AESTDT >= RFSTDT);",
        "Variable: ISO date and Study day recipes; PUT(…, E8601DA.) / E8601DT. in SAS functions."),
    _st("FORMAT / INFORMAT (PUT, INPUT)", "expression",
        "PUT writes a value as text with a format; INPUT reads text into a value with an informat.",
        "BRTHDT = input(BRTHDAT, date9.);   BRTHDTC = put(BRTHDT, yymmdd10.);",
        "Variable: SAS function PUT / INPUT (and PUTC/PUTN/INPUTC/INPUTN) with the formats in this list."),
    _st("LENGTH / ATTRIB", "built",
        "Declares a variable's type, length and label before it is used.",
        "attrib AETERM length=$200 label='Reported Term for the Adverse Event';",
        "Taken from the spec (length, label, type) when the dataset is written."),
    _st("PROC FORMAT value decodes", "built",
        "Defines a lookup that turns codes into decoded text (and back).",
        "proc format; value $sex 'M'='MALE' 'F'='FEMALE'; run;\nSEX = put(SEX_STD, $sex.);",
        "Controlled-terminology mapping (codelists) on the variable."),
    _st("RETAIN carry-forward (non-SEQ)", "built",
        "Keeps a value from one record to the next, e.g. the last non-missing result carried forward (LOCF).",
        "data vs; set vs; by USUBJID VSTESTCD; retain _rt;\n  if first.VSTESTCD then call missing(_rt);\n  if not missing(VSORRES) then _rt = VSORRES;\n  VSLOCF = _rt;\nrun;",
        "Variable: Carry forward within a group (RETAIN). Prepare data: Carry / LAG."),
    _st("LAG / DIF (previous record)", "built",
        "LAGn returns the value from n records earlier; DIFn is the change from it.",
        "data vs; set vs; by USUBJID VSTESTCD;\n  _lg = lag(VSSTRESN); if first.VSTESTCD then call missing(_lg);\n  VSCHG = VSSTRESN - _lg;\nrun;",
        "Variable: Previous record within a group (LAG / DIF). Prepare data: Carry / LAG."),
    _st("ARRAY / DO loops over variables", "gap",
        "Repeats the same statements over a list of variables (an array) or a range of values.",
        "array d{*} SYSBP DIABP PULSE; do i = 1 to dim(d); if d{i} < 0 then d{i} = .; end;",
        "Not yet as a recipe — each variable is its own block; Transpose → findings covers the common wide-to-long use."),
    _st("CALL routines (CALL SYMPUT, CALL MISSING …)", "gap",
        "Run actions rather than return a value — set macro variables, blank several variables at once …",
        "call missing(AESTDTC, AEENDTC);   call symputx('nsubj', n);",
        "Macro-time routines don't apply to a per-variable build; blanking is a Fixed value or IF/THEN."),
]


def catalog() -> dict:
    """Everything the UI needs: functions by category, formats, informats, intervals, statements."""
    return {"categories": CATEGORIES, "functions": FUNCTIONS, "legacy": list(LEGACY.values()),
            "formats": _rt.SX_FORMATS, "informats": _rt.SX_INFORMATS, "intervals": INTERVALS,
            "operators": ["+ - * / **", "|| (concatenate)", "= ^= < <= > >= (eq ne lt le gt ge)",
                          "=: (begins with)", "in (…) / not in (…)", "and & / or | / not ^"],
            "statements": STATEMENTS}


# ── recipe → SAS expression ─────────────────────────────────────────────────────────────────
def _const(kind, v):
    if kind in ("fmt", "infmt"):
        t = str(v).strip()
        return ("fmt", (t if t.endswith(".") or "." in t else t + ".").upper())
    if kind == "num":
        try:
            f = float(str(v).strip())
        except ValueError:
            return ("num", None)
        return ("num", int(f) if f.is_integer() else f)
    if kind == "choice" and v == "continuous":
        return ("str", "c")
    if kind == "choice" and v == "discrete":
        return ("str", "d")
    return ("str", str(v))


def fn_tree(args: dict):
    """A Transform-function recipe -> (sas_expr AST, n_sources). Inputs are placeholders
    ("var", "__S<i>") the caller binds to its own source references."""
    a = args or {}
    fn = str(a.get("fn") or "").strip().lower()
    srcs = list(a.get("sources") or [])
    if fn == "zeropad":
        w = int(float(a.get("width") or 0) or 1)
        inner = ("call", "input", [("var", "__S0"), ("fmt", "BEST32.")])
        return ("call", "strip", [("call", "put", [inner, ("fmt", f"Z{w}.")])]), 1
    if fn == "coalesce" and not a.get("numeric"):
        # the recipe's coalesce is the character one (the SDTM case): first non-blank stripped input
        return ("call", "coalescec", [("call", "strip", [("var", f"__S{i}")]) for i in range(max(len(srcs), 1))]), len(srcs)
    spec = BY_NAME.get(fn)
    if spec is None:
        raise ValueError(f"unknown function {fn!r}")
    nodes, si = [], 0
    for p in spec["params"]:
        if p["kind"] == "srcs":
            more = max(len(srcs) - si, 1)
            nodes += [("var", f"__S{si + j}") for j in range(more)]
            si += more
        elif p["kind"] == "src":
            nodes.append(("var", f"__S{si}"))
            si += 1
        else:
            v = a.get(p["name"])
            if v is None or str(v) == "":
                v = p.get("default")
            nodes.append(("omit",) if v is None or str(v) == "" else _const(p["kind"], v))
    if fn == "tranwrd" and not str(a.get("replace") or ""):
        # no replacement = delete the target, as the recipe always did (SAS TRANWRD would leave a blank)
        fn, nodes[2] = "transtrn", ("call", "trimn", [("str", "")])
    while nodes and nodes[-1] == ("omit",):
        nodes.pop()
    tree = ("call", fn, nodes)
    if fn == "put":
        tree = ("call", "strip", [tree])        # a value, not a right-aligned print field
    return tree, si
