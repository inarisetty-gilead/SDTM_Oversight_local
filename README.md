# SDTM Oversight

A local application that rebuilds SDTM datasets from a mapping spec and raw data, then
checks a vendor's delivered SDTM against them.

    mapping spec  ─┐
                   ├─►  build SDTM  ─►  compare  ─►  report
    raw datasets  ─┘                        ▲
                              vendor SDTM  ─┘

**Fully local.** No network calls, no cloud storage, no AI. Nothing leaves the machine.

## Requirements

Python 3.10 or newer. Everything else installs on first launch into a private virtual
environment inside this folder.

> **Keep this folder out of cloud sync.** It lives in `~/Developer` for that reason. Inside
> an iCloud-, Dropbox- or Synology-synced folder (`~/Documents` and `~/Desktop` are synced by
> default on macOS) the sync daemon fights the virtual environment and `node_modules`: we
> measured `import pandas` taking **12 minutes of wall clock for 0.9 seconds of CPU**, and saw
> a package file corrupted mid-write. More importantly, `runs/` holds built SDTM datasets —
> real subject data — and a synced folder uploads every one of them automatically, which
> defeats the point of a local-only tool.

## Studies

The application opens on a **study card** view. A study is a named piece of work — its mapping
spec, its raw data folder, and every decision made on top of them.

Everything is saved **as you make it**, not when you remember to press a button: a hand-edited
mapping, a preparation pipeline, a record source, a comparison key. Close the application,
reopen the study, and the work is where you left it.

Each study is a folder under `studies/` holding one `study.json` — plain, readable, diffable,
and independent of this application. That split is deliberate:

- **A build is reproducible** from the spec and the raw data, and `provenance.json` proves it.
- **A study is the record of judgement** applied on top of them — what you decided, and where.

The spec and the raw data are never copied into a study; only their paths are recorded, and the
card tells you if either has moved.

## Run the application

Double-click **`run.command`** in Finder. First launch sets up its own Python environment
(a minute or two); after that it opens straight to <http://127.0.0.1:8020>.

From a terminal:

```bash
cd ~/Developer/sdtm-oversight && ./run.command
```

The app binds to loopback only — it is not reachable from the network.

### The four steps

1. **Mapping specification** — pick the SDTM Designer spec workbook. It reports the domains,
   variables and codelists it found, and which sheets it skipped.
2. **Raw datasets** — pick the folder holding the raw extracts. It lists every dataset with
   its row and column counts, and flags every `raw.<dataset>.<column>` the spec references
   that is **not** in the folder. Resolve those before building: they are spec defects or
   missing extracts, and each one costs you a variable.
3. **Build the SDTM datasets** — choose the output format and build. You get per-domain
   record counts, how many variables were built, dropped and not built, a preview of any
   built dataset, and the build manifest. **Click any domain row** for its detail view
   (below).
4. **Compare against the vendor delivery** — pick the folder holding the vendor's SDTM. You
   get, per domain: records only in your build, records only in the vendor's, and per-variable
   agreement with worked examples of every difference.

Each run writes a timestamped folder under `runs/`.

## The domain view

Click a domain in the build table to open it. This is where you check a single domain the
way you would in SDTM Designer's Domain Studio, and it is the fastest way to answer "where
did this value come from?".

**Every variable, and how it was built** — mapping type and recipe, the exact source
`dataset.column` or constant, how many records are populated, a few real values, the
codelist applied, and the spec row it came from. Filter by status (built / dropped / not
built) or search by variable, label or source.

**The resulting dataset stays on screen.** The Variables tab shows the mapping list above and
the built dataset below, permanently — the way SDTM Designer's Domain Studio works. Clicking a
variable highlights its column in that dataset and scrolls it into view, and opens a small
table of just the subject key and that variable's value, record by record. Editing a mapping
rebuilds the domain and both tables refresh. You watch the output change as you work on it.

**A Data tab** shows the records themselves — the built dataset, its `SUPP--`, or any raw
input. The **whole dataset** is loaded and rendered with TanStack Table over a virtualiser, so
a domain of tens of thousands of records scrolls smoothly with only the visible rows in the
page. Click a header to sort, drag a column edge to resize, and filter per column in the row
beneath the headers — a dropdown of the actual values where a column has forty or fewer, a
search box where it does not. Because every row is present, the count reads `15 of 600
records`: you are filtering the dataset, never just what happens to be on screen. Above
100,000 records it loads the first 100,000 and says so; the written file has all of it. Switching between the output and the
input it came from is the fastest way to check a mapping: you see what went in and what came
out, side by side in the same grid. It reloads after every rebuild, so what you are looking at
is always the current build, and it names how many columns are present-but-empty rather than
letting you wonder.

**Per-domain overrides**, applied by rebuilding just that domain:

| Control | What it does |
| --- | --- |
| Record source | force which raw dataset supplies the domain's records |
| Data preparation | `auto` detects stacking and transposing; `off` uses one raw form as-is |
| Sort before `--SEQ` | order the records before sequence numbers are assigned |
| Keep one record per group | drop duplicates on chosen keys, first or last |
| Comparison keys | how records are matched against the vendor in step 4 |

Rebuilding one domain leaves the others alone, rewrites that dataset and the manifest, and
invalidates the comparison so you never read a report against data that has since changed.

## Sessions

A build is kept alive across the things that would otherwise throw it away:

- **Re-loading the same spec or re-scanning the same raw folder keeps the build.** Only a spec
  file or raw folder that has actually changed invalidates it — and when that happens the app
  says so and clears the stale tables rather than leaving results on screen that no longer
  match what it holds.
- **Restarting the application resumes the last run**, build and comparison both, including
  the in-memory prepared datasets behind stacked and transposed domains. Start with
  `--fresh` to begin empty instead.

Each run is cached beside its outputs in `runs/<run>/.session.pkl`. Very large studies are
not cached — the written datasets are the record — and the app says so at build time.

## Editing a mapping

Click any variable in the domain view to open its editor. You can set it to a constant, a
raw column with a codelist, a `--SEQ`, a drop, or any of the derivations the engine
implements — ISO dates, study day, LOBXFL, concatenation, per-subject date extremes, a SAS
function, if/else-if/else logic, a copy of another variable, a value from another built
domain, or a **pipeline** that chains several of those on one variable.

**Preview before you commit.** The preview builds the domain with the candidate mapping and
shows what the variable would contain — how many records populate and real values — without
changing anything. Nothing is saved until you apply.

**Every edit is recorded as a deviation from the spec.** This is the important part. An
edited variable is marked `hand edit` in the domain view, counted in the build table, listed
on a **Hand Edits** sheet in `build_manifest.xlsx` alongside what the spec would have
produced, and called out in the HTML report:

> These variables were **not** built from the mapping spec. For those variables this build is
> not an independent rebuild, and the vendor comparison below should be read with that in
> mind.

That warning is the whole point. The value of this tool is that its build is derived from
the spec and not from the vendor's output — the moment you edit a mapping to match what the
vendor did, agreement on that variable stops being evidence. Editing is still the right tool
when the spec is wrong, ambiguous, or silent, and you want to see the consequence; the
record simply has to say so. **Revert to the spec** per variable, or for the whole domain,
puts it back.

Edits apply *after* the automatic repair passes, so a deliberate mapping is never
overwritten by the ISO-date or study-day pass.

## Dates

**A `--DTC` from what the form collected.** Name the date column. That is the whole job.

Forms often split a date into `BRTHDAT_YYYY` / `_MM` / `_DD`, sometimes with a `_RAW` variant
and a separate time column. You do not have to know or name any of that: pick any one of those
columns and the rest are found beside it. The parts are what keep a partial date partial — a
known year and month with an unknown day becomes `1962-11`, never padded out to a day nobody
recorded. Time is appended as `Thh:mm`, and only when the date is complete.

If the automatic match picks the wrong column, *Show the columns it matched automatically*
reveals the individual overrides. Most of the time you will never open it.

**The earliest or latest date across several datasets.** `RFSTDTC` is the first dose across
every exposure form; `RFENDTC` is the last contact across every visit form. The *Earliest /
latest date across datasets* derivation takes any number of dataset + date-column pairs, pools
every date it finds per subject, and takes the minimum or maximum:

| | |
| --- | --- |
| Take the | `min` for earliest, `max` for latest |
| Datasets and their date columns | one row per raw form — add as many as you need |
| Per | `USUBJID` by default |

**It reads this from the spec.** Picking a derivation fills the form from the variable's own
Input Variables — the datasets and columns are already there, with `min` or `max` chosen from
the variable name and from what the Mapping Rule says ("keep the most recent…" gives `max`).
You adjust rather than transcribe. Add or remove rows in the editor; there is no JSON to write.

The **reference dates build themselves**: `RFSTDTC`, `RFENDTC`, `RFXSTDTC`, `RFXENDTC`,
`RFICDTC` and `RFPENDTC` are wired to this derivation automatically from their spec sources —
whether that is fifteen date columns across a dozen forms, or one form holding many visits per
subject. Only those six; an ordinary `--DTC` is the date *of a record* and is left alone, which
matters for a domain like DS whose records come from the very forms its date is drawn from. The same operation exists at
dataset level as the `date_extreme` pipeline step, when you want the result as a column of a
prepared dataset rather than as one SDTM variable.

## Data preparation

### The pipeline

Each domain has an ordered pipeline of dataset operations, edited in the domain view. Every
step names its output, and any later step — or any variable mapping — can read that output by
name. The pipeline **runs as you edit it**. Each step reports its record and column counts as soon
as it changes, and the final dataset is sampled underneath — no preview button to remember,
because working blind between presses is where a wrong step survives long enough to be
trusted. Nothing is saved to the build until you apply it.

| Operation | |
| --- | --- |
| `stack` | append the records of several datasets (each record keeps `__SOURCE_DATASET`) |
| `merge` | join datasets on key columns; subject keys are retained automatically |
| `filter` | keep only the records matching a set of conditions |
| `select` / `drop` / `rename` | choose, remove or rename columns |
| `derive` | set a column with sequential if/then rules |
| `aggregate` | group and summarise (`min` / `max` are date-aware) |
| `date_extreme` | earliest or latest date per group across several datasets |
| `sort` / `dedup` | order records; keep the first or last per group |
| `split` | route records into separate outputs, first match wins |
| `transpose_long` | melt chosen columns into name/value records |
| `transpose_findings` | value+unit columns into one record per test |

Conditions accept `==`, `!=`, `contains`, `startswith`, `endswith`, `in`, `notin`,
`missing`, `notmissing`, `>`, `<`, `>=`, `<=`. Text comparison is trimmed and
case-insensitive, matching SAS character comparison.

By default the last step's output becomes the domain's record source; **Record source**
overrides that. A step that fails names the step, the operation and what was wrong — it never
half-runs.

### What is detected automatically

Two raw-data shapes cannot be handled by mapping columns one at a time. Both are detected
from the spec, both are reported in plain language, and both can be overridden or switched
off per domain. **Start from the detected step** turns whatever was detected into an editable
pipeline, so it is a starting point rather than a black box.

**Stack** — a domain with no raw dataset of its own, whose records come from several forms
(DS from consent, enrolment and completion). A form referenced by two or more of the
domain's variables is a record source; one referenced by a single variable is a per-subject
lookup. Two or more record sources means the forms union.

**Transpose** — a wide findings form where one raw record holds several measurements in
separate columns. `--TESTCD` and `--ORRES` listing the same set of two or more raw result
columns is the signature; those columns melt into one row per test, carrying the identifier,
visit and date columns through. The form is found by which dataset actually holds the
measurement columns, so a spec that says `raw.eg.*` still finds a form collected as
`egperf`.

> The transposed test codes are taken from the **column names** (`EGVR` → `VR`). If your
> codelist uses different submission values, set them explicitly — the tool says so in the
> build note rather than pretending the codes are authoritative.

Measurement order follows the order the spec lists the columns, so `--SEQ` is reproducible.

## Command line

The same engine runs headless, for scripted or scheduled checks:

```bash
.venv/bin/sdtmbuild inspect --spec MAPPING_SPEC.xlsx --raw /path/to/rawdata
```
```bash
.venv/bin/sdtmbuild compare --spec MAPPING_SPEC.xlsx --raw /path/to/rawdata \
    --vendor /path/to/vendor_sdtm --out ./sdtm_out
```

`compare` exits 1 when anything differs, so it drops straight into a scripted check.

## Output of a run


| File | What it is |
| --- | --- |
| `datasets/` | the built SDTM datasets (`csv`, `xpt` v5, or `parquet`) |
| `build_manifest.xlsx` | every spec variable: how it was built, from which source, or why it was not |
| `vendor_comparison.xlsx` | overview, per-variable agreement, difference examples, unmatched records |
| `build_report.html` | the same, readable in a browser |

## The interface

React with [shadcn/ui](https://ui.shadcn.com) components, built by Vite into
`app/static_dist` and served by the same local server — no CDN, no external fonts, nothing
fetched at runtime. It follows the operating system's light/dark appearance.

The layout is an application shell rather than a form: a left rail carrying the workflow
(Setup → Build → Compare) and every built domain with its record count and status, and a
work area that opens a domain in place. Tables are a proper data grid — row numbers, a field
icon per column, a frozen first column, collapsible grouping (by status, role or source
dataset), and coloured chips for the things you scan for: **built**, **name match**,
**hand edit**, **dropped**, **not built**. Coverage appears as a segmented bar so the shape
of a build reads at a glance.

To work on it:

```bash
cd ui && npm install && npm run dev     # port 5173, proxies /api to 8020
```
```bash
cd ui && npx vite build                 # rebuild what the app serves
```

`npm install` is the one step that needs the network. Once built, the application is
entirely offline.

### Command-line options

| Option | Why |
| --- | --- |
| `--domains AE,DM,LB` | build a subset |
| `--studyid GS-US-XXX-NNNN` | when the raw data carries no STUDYID |
| `--base DS=disposition` | force which raw dataset supplies a domain's records |
| `--sort LB=USUBJID,LBTESTCD,LBDTC` | sort before `--SEQ` numbering |
| `--key AE=USUBJID,AETERM,AESTDTC` | control how records are matched in the comparison |
| `--ignore-var AESEQ` | exclude a variable from value comparison |
| `--ignore-case`, `--numeric-tolerance 0.001` | loosen the comparison |
| `--include-unbuilt` | emit unbuildable variables as empty columns |

## The mapping spec

SDTM Designer's core-spec export: one worksheet per domain, header on row 1, one row per
SDTM variable. These columns are read (header names are matched case-insensitively, with
common aliases):

`Variable`, `Label`, `Mapping Action`, `Input Variables`, `Mapping Rule`,
`Implemented SAS Code`, `Codelist`, `Role`, `Origin`, `Dataset`, `Type`, `Length`, `Order`

`Input Variables` drives the build. `raw.<dataset>.<column>` names a raw source;
`sdtm.<domain>.<variable>` names another SDTM variable. A row whose `Dataset` cell is
`QNAM` is a supplemental qualifier and is transposed into `SUPP<DOMAIN>`.

An optional `Codelist` sheet (`Codelist` / `Submission Value` / `Decode` / `Synonyms`)
normalises raw entries to CT submission values.

## What it builds

Assignments (with CT normalisation), constants, `--SEQ` numbering, and these derivations:

- **`--DTC`** — partial-aware ISO 8601, assembled from the raw year/month/day component
  columns plus a time column when the study collected them. An unknown day yields
  `1962-11`, never a fabricated `1962-11-01`.
- **`--DY`** — study day against the DM reference date, `+1` on or after it, no day zero.
- **`--LOBXFL`** — last observation on or before first exposure.
- **SAS character functions** — `substr`, `scan`, `strip`, `upcase`, `catx`, `coalesce`,
  `tranwrd`, `compress`, `zeropad`, `put`, `input` and the rest.
- **conditional logic**, **multi-step pipelines**, cross-domain references, and
  earliest/latest date per subject.

`SUPP--` datasets are generated from the QNAM rows, with `IDVAR`/`IDVARVAL` linking back.

## No raw data yet?

A spec already describes the extract it expects: every `raw.<dataset>.<column>` in Input
Variables names a column some EDC extract is meant to supply. That is enough to generate a
raw folder of the right shape and exercise the spec end to end before any extract exists.

In step 2: **No raw data yet? Generate it from the spec**. Or:

```bash
.venv/bin/sdtmbuild synth --spec MAPPING_SPEC.xlsx --out ./synthetic_raw --subjects 40 --visits 5
```

The shape is realistic — consistent subjects across datasets, visits spaced from enrolment,
dates that follow it, units matched to their measurement, controlled terms where the spec
names a codelist, and split date parts (`_YYYY`/`_MM`/`_DD`) that agree with the record date.
Generation is deterministic: the same spec and seed give byte-identical files.

**The values are invented.** The folder carries a `.synthetic.json` marker, and every stage
downstream says so — the raw panel, the build, and most emphatically the comparison:

> The build is from synthetic data, so this comparison is not evidence about the vendor. It
> tests the mapping logic only.

Use it to find out whether your spec builds, and where it does not. Point step 2 at the real
extract before drawing any conclusion about a delivery.

## The dataset it builds

A built domain is **submission-shaped**: every variable the spec defines for that domain, in
spec order, with its label — empty where it could not be populated. Only an explicit spec
`DROP` removes a variable from the structure. That is what an SDTM dataset looks like, and it
is what the vendor's delivery is compared against.

Switch **Dataset structure** to *populated only* if you want just the variables that were
built (`--structure populated` on the command line).

## Reading the spec

The spec's `Implemented SAS Code` column is compiled, not ignored. A great deal of a real
spec's detail lives there rather than in Input Variables:

```
SITEID  =  scan(usubjid, -2, '-')
SUBJID  =  scan(usubjid, -2, '-') || '-' || scan(usubjid, -1, '-')
AETERM  =  strip(upcase(aeterm))
```

Reading only Input Variables turns the first two into a copy of `USUBJID` — a confident wrong
value. A restricted grammar is supported: the character functions, concatenation and literals
over identifiers. Anything outside it — formats, `datepart`, procedural code — is **refused**,
and the variable is reported as not built rather than approximated.

Two more things the loader handles that specs commonly do:

- **A title banner above the headings.** Many workbooks open a sheet with
  `DM: Subject demographics…` and put `Variable | Label | …` on the row below. The header row
  is located, not assumed — assuming row 1 made whole workbooks look empty.
- **Decorated file names.** `raw.ae` finds `ae_raw_20260522_163947.csv`, and `raw.lb` finds
  `rawlb1`, `rawlb2` and `rawlb3` and stacks them into one record source.

## Built, but empty

A mapping that runs without error and populates nothing is the quietest kind of wrong: the
variable reads as built, the column is blank, and nobody looks again. Those are marked
**empty** rather than built — a chip in the domain view, a filter of their own, their own
section in the report, and a line in the manifest.

The most common cause is a join that matched no subject. `date_extreme` pools dates from
several forms, and each form is joined to the domain on whichever subject key the two share:
one form keyed on `SUBJID`, another on `USUBJID`, the domain on either. Aggregating on one key
and joining on another looks right at both steps and matches nothing — so when a source cannot
be joined at all, it is named and skipped, and if none can be joined the variable reports the
reason instead of returning a blank column.

## Why a variable is not built — and how to raise coverage

Coverage is set by what the mapping spec actually states, not by the engine. After a build,
**Why N variables were not built** groups every gap by cause, with examples. The usual causes,
and what to do:

| Cause | What helps |
| --- | --- |
| The spec states the rule in prose, with no machine-readable Input Variables | map it by hand in the domain view |
| The spec row is blank — no source and no rule | name matching, or map it by hand |
| The source the spec names is not in the raw data | a spec defect or a missing extract — see step 2 |
| It depends on another variable that was not built | fix the dependency first |

### Name matching

For a variable the spec leaves unmapped, the build can look for a raw column of a similar
name in the domain's own records and in DM, using a synonym table for the cases where EDC
and SDTM names diverge (`AEBODSYS` ← `MDRSOC`, `USUBJID` ← `X_SUBJID`, and so on). This is
how SDTM Designer reaches high coverage on specs that do not spell every source out, and it
is pure name comparison — no model, no network.

Set it on the build step: **off**, **strict (85%)**, **on (70%, the default)**, or
**loose (45%)** — 45% is Designer's own setting. This tool defaults higher, because for
oversight a wrong mapping is worse than an absent one: it produces a confident value to
compare against.

A name-matched variable is **a guess, and is labelled as one** — `name match 80%` in the
domain view, a separate column in the build table, its own **Name Matched** sheet in the
manifest, and its own section in the report:

> The mapping spec names no source for these variables. Agreement with the vendor here shows
> the two guesses coincide — it is **not** evidence that the spec was followed.

A variable with no similar column is never invented, at any threshold.

## What it will not do

This is the part that makes the output trustworthy.

- **It never guesses.** A spec row whose rule is narrative prose ("Set to Y if the subject
  died on study") has no deterministic reading. It is reported as `not_built` with the
  reason, left out of the dataset, and excluded from the comparison — so it can never be
  mistaken for a vendor discrepancy.
- **It never executes generated code.** Every mapping runs through a named function in
  `ops.py`. You can read the function that produced any column in the output.
- **It never invents identifiers.** If `USUBJID` is not in the raw data and cannot be
  carried from DM, the build says so rather than constructing one.
- **It is reproducible.** The same spec and the same raw data give byte-identical datasets
  every run, on every machine. Record order is part of that, because `--SEQ` numbers the
  records — a test asserts it.

Read `build_manifest.xlsx` ("Not Built" sheet) before relying on a comparison: those
variables are the part of the delivery this tool cannot independently verify.

## Comparison method

Records are matched on **natural keys**, not row position — two correct implementations can
order records differently. Keys are derived per domain (`USUBJID` plus the topic and timing
variables present in both datasets), verified for uniqueness, and reported in the output.
Override with `--key`.

Key variables are not value-compared: a difference in a key shows up as an unmatched
record instead. Values are compared with numbers normalised (`1`, `1.0` and `1.00` agree)
and every spelling of missing treated as absent.

## Proving a run, years later

An oversight finding may be questioned long after it was made. "The tool said so" is not an
answer if the tool cannot be made to say it again, so every run writes `provenance.json`: the
inputs by **content hash**, the tool version, the interpreter, and the versions of the
libraries that did the arithmetic.

```bash
.venv/bin/sdtmbuild verify runs/run_20260820_180115
```

It reports whether the spec and the raw data are still what they were, names any file that
changed, notes any drift in the environment, then rebuilds and compares:

```
mapping spec: unchanged
raw data    : CHANGED since the run (7 files)
    ae.csv: edited
Cannot reproduce: the raw data has changed
```

Outputs are compared by **dataset content, not file bytes**. A SAS transport file stamps its
own creation time into the header, so writing the same data twice gives different bytes.
Comparing bytes would report every re-run as a difference and train the reader to ignore the
check — so the comparison is on the records, which is what reproducibility means for a dataset.

Dependencies carry upper bounds, and `requirements.lock.txt` records the exact versions a run
was made with. A library upgrade can change a rounding edge; `verify` will tell you the
environment drifted rather than letting it drift silently.

## Tests

```bash
./run_tests.sh
```

49 tests across five suites — the build engine, the preparation pipeline, the HTTP API,
synthetic data generation, and reproducibility. The core of it generates a synthetic study
with planted vendor discrepancies and asserts the comparison finds exactly those and nothing
else.

## Provenance

The transform semantics are ported from SDTM Designer's mapping engine
(`sdtm_pred/backend/main.py`) so a spec that builds correctly there builds the same here,
with two deliberate changes: generated-code recipes became named operations, and the
hardcoded sponsor STUDYID became a resolved value.
