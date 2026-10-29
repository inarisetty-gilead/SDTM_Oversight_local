import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import {
  ArrowDown, ArrowUp, ArrowUpDown, CalendarClock, Calculator, ChevronDown, ChevronRight,
  CaseUpper, ChevronsDownUp, ChevronsUpDown, Copy, CopyPlus, Eraser, Eye, EyeOff, Filter, GitMerge, Hash,
  Layers, ListChecks, Loader2, Maximize2, PenLine, Plus, Redo2, RotateCcw, Rows3, Sigma, Split,
  SplitSquareHorizontal, Table2, Trash2, Undo2, Wand2, type LucideIcon,
} from "lucide-react"
import { api } from "@/api"
import type { DataPage, DomainDetail } from "@/api"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "@/components/ui/select"
import { cn } from "@/lib/utils"
import { FN_LABELS } from "./fnLabels"
import { RecordTable } from "./DataView"
import { Chip, DataGrid, Mono, type ChipTone } from "./grid"
import { SasCodeField, SasForm } from "./sas"
import { sasSentence, useSasLines } from "./sasHooks"
import { Callout, Panel } from "./shell"

/** Every step reads the same at a glance, in any order, after any amount of time away:
 *  one operation \u2192 one icon \u2192 one colour, everywhere the op appears (the step header,
 *  the op picker itself, and the left edge of the step's whole card). */
const OP_META: Record<string, { icon: LucideIcon; tone: ChipTone }> = {
  stack: { icon: Layers, tone: "blue" },
  merge: { icon: GitMerge, tone: "green" },
  filter: { icon: Filter, tone: "amber" },
  select: { icon: ListChecks, tone: "teal" },
  drop: { icon: Eraser, tone: "red" },
  rename: { icon: PenLine, tone: "violet" },
  constant: { icon: Hash, tone: "slate" },
  derive: { icon: Wand2, tone: "fuchsia" },
  compute: { icon: Calculator, tone: "blue" },
  aggregate: { icon: Sigma, tone: "violet" },
  date_extreme: { icon: CalendarClock, tone: "amber" },
  sort: { icon: ArrowUpDown, tone: "slate" },
  dedup: { icon: Copy, tone: "red" },
  split: { icon: Split, tone: "green" },
  transpose_long: { icon: Rows3, tone: "teal" },
  transpose_findings: { icon: Table2, tone: "teal" },
  transpose_rule: { icon: Table2, tone: "teal" },
  uppercase: { icon: CaseUpper, tone: "teal" },
}
const DEFAULT_META = { icon: SplitSquareHorizontal, tone: "slate" as ChipTone }

/** A short operator wording for step summaries — mirrors COND_OPS but compressed. */
const OP_SHORT: Record<string, string> = {
  "==": "=", "!=": "≠", in: "in", notin: "not in", contains: "contains",
  startswith: "starts with", endswith: "ends with", ">": ">", "<": "<",
  ">=": "≥", "<=": "≤", between: "between", missing: "is missing", notmissing: "is not missing",
}

/** One plain sentence per step — what it does, read without opening it. With twenty steps
 *  in a pipeline, "which one filtered the consents?" must be answerable from the headers. */
function stepSummary(st: Step): string {
  const p = st.params as Record<string, unknown>
  const str = (k: string) => (typeof p[k] === "string" ? (p[k] as string) : "")
  const arr = (k: string) => (Array.isArray(p[k]) ? (p[k] as unknown[]) : [])
  const names = (k: string) => arr(k).filter((x): x is string => typeof x === "string" && !!x)
  const condText = (c: { column?: string; operator?: string; value?: string } | undefined) =>
    c?.column ? `${c.column} ${OP_SHORT[c.operator ?? "=="] ?? c.operator ?? "="}${
      c.value && !COND_NO_VALUE.has(c.operator ?? "") ? ` ${c.value}` : ""}` : ""
  const more = (nAll: number, nShown: number) => (nAll > nShown ? ` +${nAll - nShown} more` : "")
  switch (st.op) {
    case "stack": {
      const d = names("datasets")
      return d.length ? `append ${d.slice(0, 3).join(" + ")}${more(d.length, 3)}` : "append datasets"
    }
    case "merge": {
      const ins = arr("inputs").map((x) => (x as { dataset?: string }).dataset).filter(Boolean) as string[]
      const on = names("on")
      return ins.length
        ? `join ${ins.slice(0, 3).join(" + ")}${more(ins.length, 3)}${on.length ? ` on ${on.join(", ")}` : ""}`
        : "join datasets"
    }
    case "filter": {
      const conds = arr("conds") as Array<{ column?: string; operator?: string; value?: string }>
      const first = condText(conds[0])
      return first ? `keep ${str("dataset") || "records"} where ${first}${more(conds.length, 1)}`
                   : `filter ${str("dataset") || "…"}`
    }
    case "select": return `keep ${names("columns").length || "…"} column(s) of ${str("dataset") || "…"}`
    case "drop": return `drop ${names("columns").length || "…"} column(s) from ${str("dataset") || "…"}`
    case "rename": {
      const r = arr("renames") as Array<{ from?: string; to?: string }>
      const shown = r.filter((x) => x.from).slice(0, 2).map((x) => `${x.from}→${x.to ?? "?"}`)
      return shown.length ? `rename ${shown.join(", ")}${more(r.length, 2)} in ${str("dataset") || "…"}`
                          : `rename columns in ${str("dataset") || "…"}`
    }
    case "constant": {
      const c = arr("columns") as Array<{ column?: string; value?: string }>
      const shown = c.filter((x) => x.column).slice(0, 2).map((x) => `${x.column}="${x.value ?? ""}"`)
      return shown.length ? `set ${shown.join(", ")}${more(c.length, 2)} on ${str("dataset") || "…"}`
                          : `add constants to ${str("dataset") || "…"}`
    }
    case "derive": {
      const t = names("targets").length ? names("targets") : (str("target") ? [str("target")] : [])
      return t.length ? `derive ${t.slice(0, 3).join(", ")}${more(t.length, 3)} on ${str("dataset") || "…"}`
                      : `derive column(s) on ${str("dataset") || "…"}`
    }
    case "uppercase":
      return `uppercase ${names("columns").join(", ") || "\u2026"} on ${str("dataset") || "\u2026"}`
    case "compute": {
      if (!str("func") && str("target") && str("expression"))
        return `${str("target")} = ${str("expression")} on ${str("dataset") || "\u2026"}`
      const fn = str("func"); const cols = names("columns")
      return `${FN_LABELS[fn] ?? fn ?? "compute"}(${cols.slice(0, 3).join(", ")}${more(cols.length, 3)})${
        str("out_col") ? ` → ${str("out_col")}` : ""} on ${str("dataset") || "…"}`
    }
    case "aggregate":
      return `${str("func") || "summarise"} ${str("column") || "…"} per ${names("group_by").join(", ") || "…"}${
        str("out_col") ? ` → ${str("out_col")}` : ""}`
    case "date_extreme": {
      const srcs = arr("sources").length
      return `${str("func") === "max" ? "latest" : "earliest"} date across ${srcs || "…"} source(s)${
        str("out_col") ? ` → ${str("out_col")}` : ""}`
    }
    case "sort": return `sort ${str("dataset") || "…"} by ${names("columns").join(", ") || "…"}`
    case "dedup":
      return `keep ${str("keep") || "first"} record per ${names("keys").join(", ") || "…"} of ${str("dataset") || "…"}`
    case "split": {
      const b = (arr("branches") as Array<{ name?: string }>).map((x) => x.name).filter(Boolean)
      return b.length ? `split ${str("dataset") || "…"} into ${b.join(", ")}` : `split ${str("dataset") || "…"}`
    }
    case "transpose_long":
      return `pivot ${names("value_vars").length || "…"} column(s) of ${str("dataset") || "…"} into rows`
    case "transpose_findings":
      return `reshape ${str("dataset") || "…"} into findings (${arr("measures").length || "…"} measurement(s))`
    case "transpose_rule": {
      const forms = arr("forms") as Array<{ dataset?: string; measures?: unknown[]; not_done?: unknown[] }>
      const tests = forms.reduce((n, f) => n + (f.measures?.length ?? 0), 0)
      const nd = forms.some((f) => (f.not_done?.length ?? 0) > 0)
      return `transpose ${forms.map((f) => f.dataset).join(", ") || "…"} as the spec's ${str("testcd_col") || "--TESTCD"} rule says (${tests} test(s)${nd ? `, one ${str("all_code")} record per visit not done` : ""})`
    }
    default: return str("dataset") ? `on ${str("dataset")}` : ""
  }
}

/** Header band tint per op tone — the step's colour, but as a wash the header sits on. */
const HEAD_TINT: Record<ChipTone, string> = {
  violet: "bg-indigo-500/[0.07]", green: "bg-emerald-500/[0.07]", amber: "bg-amber-500/[0.08]",
  red: "bg-rose-500/[0.07]", blue: "bg-sky-500/[0.07]", teal: "bg-teal-500/[0.07]",
  fuchsia: "bg-purple-500/[0.07]", slate: "bg-muted/60",
}

/** A left-edge colour stripe on the step's whole card, matching its op's chip \u2014 findable by
 *  colour while scrolling, not just by reading every header. */
const ACCENT_BORDER: Record<ChipTone, string> = {
  violet: "border-l-indigo-500", green: "border-l-emerald-500", amber: "border-l-amber-500",
  red: "border-l-rose-500", blue: "border-l-sky-500", teal: "border-l-teal-500",
  fuchsia: "border-l-purple-500", slate: "border-l-muted-foreground/30",
}

/** Every field box gets a colour BY WHAT KIND OF THING IT PICKS, not by which op it belongs
 *  to \u2014 "choose a dataset" reads the same blue whether it is in a filter, a merge or a sort,
 *  so a plain white wall of boxes stops being "which one is the dataset again?". Kept to a
 *  handful of tones, one per concept, so it stays scannable instead of a rainbow. */
const FIELD_TONE: Partial<Record<string, ChipTone>> = {
  ds: "blue", dslist: "blue",                  // which dataset(s)
  mergeinputs: "green", joinkeys: "green",     // how datasets join
  conds: "amber",                              // what records to keep
  collist: "teal", colseq: "teal",             // which columns
  rules: "fuchsia", branches: "fuchsia", targets: "fuchsia", elsevalues: "fuchsia",  // conditional logic
  renames: "violet", constants: "violet",
  measures: "teal", datesources: "teal",
}
const FIELD_TONE_BOX: Record<ChipTone, string> = {
  violet: "border-indigo-500/30 bg-indigo-500/[0.06]",
  green: "border-emerald-500/30 bg-emerald-500/[0.06]",
  amber: "border-amber-500/30 bg-amber-500/[0.06]",
  red: "border-rose-500/30 bg-rose-500/[0.06]",
  blue: "border-sky-500/30 bg-sky-500/[0.06]",
  teal: "border-teal-500/30 bg-teal-500/[0.06]",
  fuchsia: "border-purple-500/30 bg-purple-500/[0.06]",
  slate: "border-transparent",
}
const FIELD_TONE_TEXT: Record<ChipTone, string> = {
  violet: "text-indigo-700 dark:text-indigo-300", green: "text-emerald-700 dark:text-emerald-300",
  amber: "text-amber-700 dark:text-amber-300", red: "text-rose-700 dark:text-rose-300",
  blue: "text-sky-700 dark:text-sky-300", teal: "text-teal-700 dark:text-teal-300",
  fuchsia: "text-purple-700 dark:text-purple-300", slate: "text-muted-foreground",
}

const FULL_ROWS = 100_000

/** A step's complete output, on demand \u2014 the inline preview under each step is capped to 8
 *  rows for readability, but the step itself always ran on the whole dataset. This is that
 *  whole dataset, browsable the same way the Raw Data view shows any other dataset. */
function FullDataDialog({ dataset, onClose }: { dataset: string; onClose: () => void }) {
  const [page, setPage] = useState<DataPage | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState("")
  useEffect(() => {
    let live = true
    setBusy(true); setError("")
    api.rawData(dataset, { offset: 0, limit: FULL_ROWS })
      .then((p) => { if (live) setPage(p) })
      .catch((e) => { if (live) setError((e as Error).message) })
      .finally(() => { if (live) setBusy(false) })
    return () => { live = false }
  }, [dataset])

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="flex h-[88vh] w-full max-w-[95vw] flex-col overflow-hidden sm:max-w-[95vw]">
        <DialogHeader>
          <DialogTitle className="text-[14px]">
            <Mono>{dataset}</Mono>
            {page && <span className="ml-2 text-[12px] font-normal text-muted-foreground">
              {(page.total ?? page.nrows).toLocaleString()} record(s), {page.columns.length} column(s)</span>}
          </DialogTitle>
        </DialogHeader>
        {error && <Callout tone="bad">{error}</Callout>}
        <div className="min-h-0 flex-1 overflow-hidden">
          {/* RecordTable's own wrapper is a plain block div, so "100%" has nothing definite
              to resolve against and the table spills past the dialog instead of scrolling —
              a fixed viewport-relative height, same trick RawDataView already uses, fixes it */}
          <RecordTable page={page} busy={busy} height="calc(88vh - 10rem)" />
        </div>
      </DialogContent>
    </Dialog>
  )
}

type Step = { op: string; name: string; params: Record<string, unknown> }
type Report = { step: number; name: string; op: string; ok: boolean; rows?: number; columns?: string[]; error?: string; extra_outputs?: string[] }

/** A dataset name as it should read in a picker — 'built_<domain>' is a distinct entry
 *  for an already-built domain's own output, never to be confused with its raw dataset
 *  of the same name (often the same letters, e.g. 'dm'), so it gets a label that says so. */
const datasetLabel = (d: string) =>
  d.startsWith("built_") ? `${d.slice(6).toUpperCase()} (built)` : d

/** A precise, hover-revealed "insert exactly here" slot between two steps (and before the
 *  first) — so putting a step in the middle never means adding it at the end and dragging
 *  it up past everything else with the reorder arrows. */
function InsertRow({ onInsert }: { onInsert: () => void }) {
  return (
    <div className="group relative h-3">
      <div className="absolute inset-x-3 top-1/2 h-px -translate-y-1/2 bg-border opacity-0 transition-opacity group-hover:opacity-100" />
      <button type="button" onClick={onInsert} title="insert a new step here"
              className="absolute left-1/2 top-1/2 flex -translate-x-1/2 -translate-y-1/2 items-center gap-1
                         rounded-full border bg-background px-2 py-0.5 text-[10px] text-muted-foreground opacity-0
                         shadow-sm transition-opacity hover:border-primary hover:text-primary group-hover:opacity-100">
        <Plus className="h-3 w-3" />insert step here
      </button>
    </div>
  )
}

/** A checkbox list (datasets to stack, columns to carry through or melt, ...) that can pop
 *  into a full-size dialog — ticking a dozen items in a cramped, scrolling 7-row box is
 *  where a reader stops trusting they got them all. The small box stays for a quick glance;
 *  "maximise" opens the same list full-size, with a filter box, for checking many at once. */
function MaximizableCheckList({ title, options, chosen, labelFor, onChange }: {
  title: string; options: string[]; chosen: string[]
  labelFor?: (opt: string) => string; onChange: (v: string[]) => void
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState("")
  const label = labelFor ?? ((x: string) => x)
  const toggle = (opt: string, checked: boolean) =>
    onChange(checked ? [...chosen, opt] : chosen.filter((x) => x !== opt))
  const shown = q ? options.filter((o) => label(o).toLowerCase().includes(q.toLowerCase())) : options
  return (
    <div className="space-y-1">
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-1 flex-wrap gap-1">
          {chosen.map((o) => <Mono key={o}>{label(o)}</Mono>)}
        </div>
        <Button type="button" size="icon" variant="ghost" className="h-6 w-6 shrink-0"
                title={`open a bigger view to check ${title.toLowerCase()} more easily`}
                onClick={() => setOpen(true)}>
          <Maximize2 className="h-3.5 w-3.5" /></Button>
      </div>
      <div className="max-h-28 space-y-1 overflow-auto rounded-md border p-2">
        {options.map((o) => (
          <label key={o} className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={chosen.includes(o)}
                   onChange={(e) => toggle(o, e.target.checked)} />{label(o)}
          </label>))}
        {!options.length && <p className="text-[11px] text-muted-foreground">nothing to pick from</p>}
      </div>
      {open && (
        <Dialog open onOpenChange={(o) => { if (!o) { setOpen(false); setQ("") } }}>
          <DialogContent className="flex h-[80vh] w-full max-w-lg flex-col overflow-hidden">
            <DialogHeader>
              <DialogTitle className="text-[14px]">{title}
                <span className="ml-2 text-[12px] font-normal text-muted-foreground">
                  {chosen.length} of {options.length} selected</span>
              </DialogTitle>
            </DialogHeader>
            <Input className="h-8 shrink-0 text-xs" placeholder="filter…" value={q}
                   onChange={(e) => setQ(e.target.value)} />
            <div className="min-h-0 flex-1 space-y-0.5 overflow-auto rounded-md border p-2">
              {shown.map((o) => (
                <label key={o} className="flex items-center gap-2 rounded px-1.5 py-1.5 text-sm hover:bg-accent">
                  <input type="checkbox" checked={chosen.includes(o)}
                         onChange={(e) => toggle(o, e.target.checked)} />{label(o)}
                </label>))}
              {!shown.length && <p className="text-[11px] text-muted-foreground">no match</p>}
            </div>
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}

/** Which earlier step's output THIS step reads from, if any — so a single step (1) feeding
 *  several later ones (2, 3, 4), each doing something different to its output, can be drawn
 *  as one workflow (a trunk branching into siblings) instead of reading as four unrelated
 *  boxes stacked on top of each other. */
function stepParent(steps: Step[], idx: number): number | null {
  const st = steps[idx]
  const p = st.params as Record<string, unknown>
  const ref = st.op === "stack" ? (p.datasets as string[] | undefined)?.[0]
            : st.op === "merge" ? (p.inputs as Array<{ dataset?: string }> | undefined)?.[0]?.dataset
            : (p.dataset as string | undefined)
  if (!ref) return null
  const j = steps.findIndex((s, k) => k < idx && (s.name || `prep${k + 1}`) === ref)
  return j >= 0 ? j : null
}
const stepName = (steps: Step[], k: number) => steps[k]?.name || `prep${k + 1}`

/** A git-graph-style connector in the gutter to the left of a step: a line in from a parent
 *  above, a line out to children below, and a dot marking the step itself — so a group of
 *  steps built off the same earlier output reads as a branch, not a coincidence. Bold and
 *  in the app's accent colour on purpose — a hairline in the same grey as every other border
 *  reads as decoration, not as "this is the thing that shows you the routing". */
function GraphGutter({ hasParent, hasChildren, isLastChild }: {
  hasParent: boolean; hasChildren: boolean; isLastChild: boolean
}) {
  if (!hasParent && !hasChildren) return null
  return (
    <div className="relative w-6 shrink-0">
      {hasParent && <div className="absolute left-1/2 top-0 h-6 w-1 -translate-x-1/2 rounded-full bg-primary" />}
      {hasParent && <div className="absolute left-3 top-6 h-1 w-3 rounded-full bg-primary" />}
      {(hasChildren || (hasParent && !isLastChild)) &&
        <div className="absolute left-1/2 top-6 bottom-0 w-1 -translate-x-1/2 rounded-full bg-primary" />}
      <div className="absolute left-3 top-6 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-primary bg-background" />
    </div>
  )
}


type Field = { k: string; t: string; ph?: string; label?: string
               opts?: [string, string][]; funcs?: string[]; notFuncs?: string[] }

const COMPUTE_FUNCS: [string, string][] = [
  ["sas", "SAS expression — any expression, on SAS's own rules"],
  ["complete_date", "Complete a partial date — fill missing month/day with 01"],
  ["year", "Take the year"],
  ...Object.entries(FN_LABELS),
]

const FIELDS: Record<string, Field[]> = {
  stack: [{ k: "datasets", t: "dslist", label: "Datasets to append" }],
  merge: [{ k: "inputs", t: "mergeinputs", label: "Datasets to join" }, { k: "on", t: "joinkeys", label: "Join on" },
          { k: "how", t: "choice:left,inner,outer,right", label: "Keep records from" }],
  filter: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "conds", t: "conds", label: "Keep records where" },
           { k: "expression", t: "sasexpr", label: "…or a SAS condition (used instead of the rows above)",
             ph: "e.g. vsstat ne 'NOT DONE' and input(vsorres, ?? best.) > 0" }],
  select: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "collist", label: "Columns" }],
  drop: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "collist", label: "Columns" }],
  rename: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "renames", t: "renames", label: "Rename" }],
  constant: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "constants", label: "Columns to add" }],
  derive: [{ k: "dataset", t: "ds", label: "Dataset" },
           { k: "targets", t: "list", ph: "one column, or several — e.g. TRSTAT, TRREASND", label: "Column(s) to set" },
           { k: "rules", t: "rules", label: "If / else if" },
           { k: "else_values", t: "elsevalues", label: "Otherwise (else — when nothing above matched)" }],
  compute: [{ k: "dataset", t: "ds", label: "Dataset" },
            { k: "func", t: "choicel", label: "Calculation", opts: COMPUTE_FUNCS },
            { k: "expression", t: "sasexpr", label: "SAS expression", funcs: ["sas"],
              ph: "e.g. put(input(brthdat, date9.), yymmdd10.)" },
            { k: "columns", t: "colseq", label: "From column(s)", notFuncs: ["sas"] },
            { k: "out_col", t: "text", ph: "blank overwrites the source column", label: "Save as" },
            { k: "start", t: "text", ph: "1", label: "From position", funcs: ["substr"] },
            { k: "len", t: "text", ph: "to the end", label: "Length", funcs: ["substr"] },
            { k: "word", t: "text", ph: "-1 = last", label: "Word number", funcs: ["scan"] },
            { k: "delim", t: "text", ph: "space if empty", label: "Separated by", funcs: ["scan"] },
            { k: "find", t: "text", label: "Find", funcs: ["tranwrd", "index"] },
            { k: "replace", t: "text", label: "Replace with", funcs: ["tranwrd"] },
            { k: "chars", t: "text", ph: "blanks if empty", label: "Characters to remove", funcs: ["compress"] },
            { k: "sep", t: "text", ph: "e.g. - (blanks are skipped)", label: "Joined with", funcs: ["catx", "concat"] },
            { k: "width", t: "text", ph: "3", label: "Width", funcs: ["zeropad"] }],
  aggregate: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "group_by", t: "collist", label: "Group by" }, { k: "column", t: "text", label: "Column to summarise" },
              { k: "func", t: "choice:min,max,first,last,count,sum,mean", label: "Summarise with" }, { k: "out_col", t: "text", label: "Result column" }],
  date_extreme: [{ k: "sources", t: "datesources", label: "Datasets and date columns" }, { k: "group_by", t: "list", ph: "USUBJID", label: "Per" },
                 { k: "func", t: "choice:min,max", label: "Take the" }, { k: "out_col", t: "text", label: "Result column" }],
  sort: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "collist", label: "Columns" }, { k: "directions", t: "list", ph: "asc, desc", label: "Direction" }],
  dedup: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "keys", t: "collist", label: "Group by" }, { k: "keep", t: "choice:first,last", label: "Keep" }],
  split: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "branches", t: "branches", label: "Branches" }, { k: "other_name", t: "text", label: "Name for the rest" }],
  transpose_long: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "id_vars", t: "collist", label: "Carry through" }, { k: "value_vars", t: "collist", label: "Columns to melt" },
                   { k: "var_name", t: "text", ph: "TESTCD", label: "Name column" }, { k: "value_name", t: "text", ph: "ORRES", label: "Value column" }],
  transpose_rule: [{ k: "forms", t: "ruleforms", label: "Tests, by form (from the spec's --TESTCD rule)" },
                   { k: "stat_value", t: "text", label: "Status of a visit not done" },
                   { k: "all_test", t: "text", label: "Test name of the not-done record" }],
  transpose_findings: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "id_vars", t: "collist", label: "Carry through" }, { k: "measures", t: "measures", label: "Measurements" },
                       { k: "testcd_col", t: "text", label: "Test code column" }, { k: "test_col", t: "text", label: "Test name column" },
                       { k: "orres_col", t: "text", label: "Result column" }, { k: "orresu_col", t: "text", label: "Unit column" }],
}


type MergeInput = { dataset?: string; columns?: string[]; key?: string }

/** Merge inputs as pickers: one row per dataset, with the columns to keep and, when this
 *  dataset calls the first join key something else (AE's X_SUBJID for DM's USUBJID), what
 *  its own column for that key is called. */
function MergeInputs({ rows, datasets, domain, on, onChange }: {
  rows: MergeInput[]; datasets: string[]; domain: string; on: string[]
  onChange: (rows: MergeInput[]) => void
}) {
  const [cols, setCols] = useState<Record<string, string[]>>({})
  const [open, setOpen] = useState<number | null>(null)

  const loadCols = useCallback(async (ds: string) => {
    if (!ds || cols[ds]) return
    try {
      const r = await api.columns(domain, ds)
      setCols((c) => ({ ...c, [ds]: r.columns }))
    } catch { setCols((c) => ({ ...c, [ds]: [] })) }
  }, [cols, domain])

  useEffect(() => { rows.forEach((r) => r.dataset && void loadCols(r.dataset)) },
            // eslint-disable-next-line react-hooks/exhaustive-deps
            [rows.map((r) => r.dataset).join("|")])

  const set = (i: number, patch: MergeInput) =>
    onChange(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)))
  const keyName = on[0] ?? ""

  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => {
        const available = cols[r.dataset ?? ""] ?? []
        const chosen = r.columns ?? []
        return (
          <div key={i} className="rounded-md border p-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="w-5 text-[11px] text-muted-foreground">{i + 1}</span>
              <Select value={r.dataset || "__none"}
                      onValueChange={(v) => { set(i, { dataset: v === "__none" ? "" : v, columns: [] })
                                              void loadCols(v) }}>
                <SelectTrigger className="h-8 w-52 text-xs"><SelectValue placeholder="dataset" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none" className="text-xs">—</SelectItem>
                  {datasets.map((d) => <SelectItem key={d} value={d} className="text-xs">{datasetLabel(d)}</SelectItem>)}
                </SelectContent>
              </Select>
              {!!keyName && (
                <>
                  <span className="text-[11px] text-muted-foreground">{keyName} is called</span>
                  <Select value={r.key || "__same"}
                          onValueChange={(v) => set(i, { key: v === "__same" ? "" : v })}>
                    <SelectTrigger className="h-8 w-36 text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent className="max-h-64">
                      <SelectItem value="__same" className="text-xs">{keyName} (same name)</SelectItem>
                      {available.filter((c) => c !== keyName).map((c) =>
                        <SelectItem key={c} value={c} className="text-xs">{c}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </>
              )}
              <Button type="button" size="sm" variant="outline" className="h-8 text-xs"
                      disabled={!r.dataset}
                      onClick={() => setOpen(open === i ? null : i)}>
                {chosen.length ? `${chosen.length} column(s)` : "all columns"}
              </Button>
              <Button type="button" size="sm" variant="ghost" className="ml-auto h-8 px-2 text-xs"
                      onClick={() => onChange(rows.filter((_, k) => k !== i))}>Remove</Button>
            </div>
            {open === i && (
              <div className="mt-2 max-h-40 overflow-auto rounded border p-2">
                <p className="mb-1 text-[10px] text-muted-foreground">
                  Leave all unticked to keep every column. Join and subject keys are always kept.
                </p>
                {available.map((c) => (
                  <label key={c} className="flex items-center gap-2 text-[11px]">
                    <input type="checkbox" checked={chosen.includes(c)}
                           onChange={(e) => set(i, { columns: e.target.checked
                             ? [...chosen, c] : chosen.filter((x) => x !== c) })} />{c}
                  </label>
                ))}
                {!available.length && <p className="text-[11px] text-muted-foreground">no columns</p>}
              </div>
            )}
          </div>
        )
      })}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...rows, { dataset: "", columns: [] }])}>
        Add a dataset
      </Button>
      {rows.length < 2 && (
        <p className="text-[11px] text-muted-foreground">A merge needs at least two datasets.</p>
      )}
    </div>
  )
}

/** Join keys: the columns every chosen dataset shares by name, plus a place to type one
 *  by hand when they don't share a name at all — a dataset that calls it something else
 *  says so on its own row above ("USUBJID is called ..."). */
function JoinKeys({ value, inputs, domain, onChange }: {
  value: string[]; inputs: MergeInput[]; domain: string; onChange: (keys: string[]) => void
}) {
  const [byDataset, setByDataset] = useState<Record<string, string[]>>({})
  const names = inputs.map((i) => i.dataset).filter(Boolean).join("|")

  useEffect(() => { void (async () => {
    const next: Record<string, string[]> = {}
    for (const i of inputs) {
      if (!i.dataset) continue
      try { next[i.dataset] = (await api.columns(domain, i.dataset)).columns } catch { next[i.dataset] = [] }
    }
    setByDataset(next)
  })() }, [names, domain, inputs])

  const sets = Object.values(byDataset)
  const shared = sets.length < 2 ? []
    : sets.reduce((a, b) => a.filter((c) => b.includes(c))).sort()
  const extra = value.filter((v) => !shared.includes(v))
  // one group per dataset, not one flat list — five datasets' worth of columns run
  // together is unreadable, and the group name is exactly what a picker needs anyway:
  // which dataset actually has the column being added
  const datasetNames = Array.from(new Set(inputs.map((i) => i.dataset).filter((d): d is string => !!d)))
  const groups = datasetNames
    .map((ds) => ({ ds, opts: (byDataset[ds] ?? []).filter((c) => !shared.includes(c) && !value.includes(c)) }))
    .filter((g) => g.opts.length)

  return (
    <div className="space-y-2">
      {!!shared.length && (
        <div className="max-h-32 overflow-auto rounded-md border p-2">
          {shared.map((c) => (
            <label key={c} className="flex items-center gap-2 text-[11px]">
              <input type="checkbox" checked={value.includes(c)}
                     onChange={(e) => onChange(e.target.checked
                       ? [...value, c] : value.filter((x) => x !== c))} />{c}
            </label>
          ))}
        </div>
      )}
      {!!extra.length && (
        <div className="space-y-1">
          {extra.map((c) => (
            <div key={c} className="flex items-center gap-2 text-[11px]">
              <span className="rounded bg-muted px-1.5 py-0.5 font-mono">{c}</span>
              <Button type="button" size="sm" variant="ghost" className="h-6 px-2 text-[11px]"
                      onClick={() => onChange(value.filter((x) => x !== c))}>Remove</Button>
            </div>
          ))}
        </div>
      )}
      {!!groups.length && (
        <Select value="__none"
                onValueChange={(v) => { if (v !== "__none") onChange([...value, v]) }}>
          <SelectTrigger className="h-7 w-56 text-[11px]"><SelectValue placeholder="add a join key" /></SelectTrigger>
          <SelectContent className="max-h-72">
            <SelectItem value="__none" className="text-xs">add a join key…</SelectItem>
            {groups.map((g) => (
              <SelectGroup key={g.ds}>
                <SelectLabel className="text-[10px] uppercase text-muted-foreground">
                  {datasetLabel(g.ds)}
                </SelectLabel>
                {g.opts.map((c) => (
                  <SelectItem key={`${g.ds}-${c}`} value={c} className="text-xs">{c}</SelectItem>
                ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
      )}
      <p className="text-[11px] text-muted-foreground">
        {shared.length
          ? "If a dataset calls this key something else (e.g. AE's X_SUBJID for USUBJID), " +
            "set that in \u201c… is called\u201d on that dataset above."
          : groups.length
          ? "No column name is shared by every dataset chosen above — pick one below (grouped " +
            "by the dataset that has it), then set what it is called in each other dataset above."
          : "Choose two or more datasets above first."}
      </p>
    </div>
  )
}

// ── condition and row builders — no JSON in sight ──────────────────────────
const COND_OPS: [string, string][] = [
  ["==", "equals"], ["!=", "does not equal"],
  ["in", "is one of (comma separated)"], ["notin", "is not one of"],
  ["contains", "contains"], ["startswith", "starts with"], ["endswith", "ends with"],
  [">", "greater than"], ["<", "less than"], [">=", "at least"], ["<=", "at most"],
  ["between", "between (low, high)"],
  ["missing", "is missing"], ["notmissing", "is not missing"],
]
export const COND_NO_VALUE = new Set(["missing", "notmissing"])

export type Cond = { column?: string; operator?: string; value?: string }

/** Columns of a dataset (raw or a previous prep step), loaded once per name. */
function useColumns(domain: string, dataset?: string) {
  const [cols, setCols] = useState<string[]>([])
  useEffect(() => {
    let alive = true
    if (!dataset) { setCols([]); return }
    void api.columns(domain, dataset)
      .then((r) => { if (alive) setCols(r.columns) })
      .catch(() => { if (alive) setCols([]) })
    return () => { alive = false }
  }, [domain, dataset])
  return cols
}

function ColSelect({ value, columns, onChange, placeholder = "column", width = "w-44", label }: {
  value?: string; columns: string[]; onChange: (v: string) => void
  placeholder?: string; width?: string; label?: (c: string) => string
}) {
  // a hand-set or stale name stays selectable so an existing pipeline never renders blank
  const all = value && !columns.includes(value) ? [value, ...columns] : columns
  const lab = label ?? ((c: string) => c)
  return (
    <Select value={value || "__none"} onValueChange={(v) => onChange(v === "__none" ? "" : v)}>
      <SelectTrigger className={`h-8 ${width} text-xs`}><SelectValue placeholder={placeholder} /></SelectTrigger>
      <SelectContent className="max-h-64">
        <SelectItem value="__none" className="text-xs">—</SelectItem>
        {all.map((c) => <SelectItem key={c} value={c} className="text-xs">{lab(c)}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

/** A condition's comparison value — offered from the column's own distinct values (so a
 *  typo can't silently match nothing), with a plain text box as the fallback for a
 *  high-cardinality column or one a value list expects that today's sample never held.
 *  "in" / "notin" get a checklist instead of one pick, since they compare against several. */
export function ValuePicker({ domain, dataset, column, operator, value, onChange }: {
  domain?: string; dataset?: string; column?: string; operator: string
  value: string; onChange: (v: string) => void
}) {
  const [values, setValues] = useState<string[] | null>(null)
  useEffect(() => {
    setValues(null)
    if (!domain || !dataset || !column) return
    let live = true
    api.columnValues(domain, dataset, column).then((r) => { if (live) setValues(r.values) })
      .catch(() => { if (live) setValues(null) })
    return () => { live = false }
  }, [domain, dataset, column])

  if (!values || !values.length) {
    return <Input className="h-8 w-44 text-xs" placeholder="value" value={value}
                  onChange={(e) => onChange(e.target.value)} />
  }
  if (operator === "in" || operator === "notin") {
    const chosen = value ? value.split(",").map((x) => x.trim()).filter(Boolean) : []
    return (
      <div className="flex max-h-28 max-w-64 flex-wrap gap-x-2 gap-y-1 overflow-auto rounded-md border px-2 py-1.5">
        {values.map((v) => (
          <label key={v} className="flex items-center gap-1 text-[11px]">
            <input type="checkbox" checked={chosen.includes(v)}
                   onChange={(e) => onChange((e.target.checked ? [...chosen, v]
                     : chosen.filter((x) => x !== v)).join(", "))} />{v}
          </label>))}
      </div>
    )
  }
  const known = values.includes(value)
  return (
    <span className="inline-flex items-center gap-1.5">
      <Select value={known ? value : "__custom"}
              onValueChange={(v) => { if (v !== "__custom") onChange(v) }}>
        <SelectTrigger className="h-8 w-40 text-xs"><SelectValue placeholder="value" /></SelectTrigger>
        <SelectContent className="max-h-64">
          {values.map((v) => <SelectItem key={v} value={v} className="text-xs">{v}</SelectItem>)}
          <SelectItem value="__custom" className="text-xs">custom value…</SelectItem>
        </SelectContent>
      </Select>
      {!known && <Input className="h-8 w-32 text-xs" placeholder="type it" value={value}
                        onChange={(e) => onChange(e.target.value)} />}
    </span>
  )
}

/** ANDed conditions: column · comparison · value rows, offered from the dataset itself. */
export function CondsEditor({ conds, columns, onChange, firstWord = "where", domain, dataset }: {
  conds: Cond[]; columns: string[]; onChange: (c: Cond[]) => void; firstWord?: string
  domain?: string; dataset?: string
}) {
  const set = (i: number, patch: Cond) =>
    onChange(conds.map((c, k) => (k === i ? { ...c, ...patch } : c)))
  return (
    <div className="space-y-1.5">
      {conds.map((c, i) => (
        <div key={i} className="flex flex-wrap items-center gap-1.5">
          <span className="w-10 text-right text-[11px] text-muted-foreground">
            {i === 0 ? firstWord : "and"}</span>
          <ColSelect value={c.column} columns={columns}
                     onChange={(v) => set(i, { column: v })} />
          <Select value={c.operator || "=="} onValueChange={(v) => set(i, { operator: v })}>
            <SelectTrigger className="h-8 w-44 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>{COND_OPS.map(([v, l]) =>
              <SelectItem key={v} value={v} className="text-xs">{l}</SelectItem>)}</SelectContent>
          </Select>
          {!COND_NO_VALUE.has(c.operator || "==") && (
            <ValuePicker domain={domain} dataset={dataset} column={c.column} operator={c.operator || "=="}
                        value={c.value ?? ""} onChange={(v) => set(i, { value: v })} />)}
          <Button type="button" size="icon" variant="ghost" className="h-7 w-7"
                  onClick={() => onChange(conds.filter((_, k) => k !== i))}>
            <Trash2 className="h-3 w-3" /></Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...conds, { operator: "==" }])}>
        <Plus className="mr-1 h-3 w-3" />{conds.length ? "And also" : "Add a condition"}
      </Button>
    </div>
  )
}

type Rule = { conds?: Cond[]; value?: string; values?: Record<string, string> }

/** A comma-separated free-text list (new column names, sort directions, ...) — kept as its
 *  OWN local text while the reader is typing. Re-deriving the input's value from the parsed
 *  array on every keystroke (the naive way) erases a trailing ", " the instant it is typed —
 *  the array has no empty last element to show, so the comma the reader just pressed vanishes
 *  and typing a second item is impossible. Committing on blur (or the array changing from
 *  OUTSIDE, e.g. undo) avoids that fight entirely. */
function ListInput({ value, onChange, placeholder }: {
  value: string[]; onChange: (v: string[]) => void; placeholder?: string
}) {
  const [text, setText] = useState(() => value.join(", "))
  const known = useRef<string[]>(value)
  useEffect(() => {
    if (JSON.stringify(known.current) !== JSON.stringify(value)) {
      known.current = value
      setText(value.join(", "))
    }
  }, [value])
  const commit = () => {
    const parsed = text.split(",").map((x) => x.trim()).filter(Boolean)
    known.current = parsed
    onChange(parsed)
  }
  return (
    <Input className="h-8 text-xs" placeholder={placeholder ?? "comma separated"} value={text}
           onChange={(e) => setText(e.target.value)} onBlur={commit}
           onKeyDown={(e) => { if (e.key === "Enter") { commit(); (e.target as HTMLInputElement).blur() } }} />
  )
}

// zero-argument character functions only — 'substr(start,len)'/'scan(word,delim)'/... need
// more than "which column", which this one-column reference has no room to ask for
const DERIVE_REF_FUNCS = ["upcase", "lowcase", "propcase", "strip", "trim", "left", "reverse", "compbl", "length"]

/** A rule's (or "otherwise"'s) value: usually a fixed literal, but SAS as often sets one
 *  variable FROM another — `_trstresc=_trorres`, `_trorres=upcase(LSSTATUS)` — which typing
 *  can only express as syntax ("=COLUMN"). This offers that as pickers instead: a toggle
 *  into "read a column" mode, then the column and an optional single function on it. */
function ValueOrColumn({ value, columns, onChange }: {
  value: string; columns: string[]; onChange: (v: string) => void
}) {
  const m = /^=\s*(?:([A-Za-z_]\w*)\s*\(\s*)?([A-Za-z_][\w.]*)\s*\)?\s*$/.exec(value)
  const isRef = value.startsWith("=")
  const func = m?.[1]?.toLowerCase() ?? ""
  const col = m?.[2] ?? ""

  if (!isRef) {
    return (
      <span className="inline-flex items-center gap-1">
        <Input className="h-8 w-48 text-xs" placeholder="value" value={value}
               onChange={(e) => onChange(e.target.value)} />
        <Button type="button" size="sm" variant="ghost" className="h-8 px-1.5 text-[11px] text-muted-foreground"
                title="set this from another column instead of a fixed value"
                onClick={() => onChange(`=${columns[0] ?? ""}`)}>
          or a column…</Button>
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1">
      <Select value={func || "__asis"}
              onValueChange={(f) => onChange(f === "__asis" ? `=${col}` : `=${f}(${col})`)}>
        <SelectTrigger className="h-8 w-24 text-xs"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="__asis" className="text-xs">as is</SelectItem>
          {DERIVE_REF_FUNCS.map((f) => <SelectItem key={f} value={f} className="text-xs">{f.toUpperCase()}</SelectItem>)}
        </SelectContent>
      </Select>
      <Select value={col || "__none"}
              onValueChange={(c) => onChange(c === "__none" ? "=" : (func ? `=${func}(${c})` : `=${c}`))}>
        <SelectTrigger className="h-8 w-40 text-xs"><SelectValue placeholder="column" /></SelectTrigger>
        <SelectContent><SelectItem value="__none" className="text-xs">—</SelectItem>
          {columns.map((c) => <SelectItem key={c} value={c} className="text-xs">{c}</SelectItem>)}
        </SelectContent>
      </Select>
      <Button type="button" size="sm" variant="ghost" className="h-8 px-1.5 text-[11px] text-muted-foreground"
              title="type a fixed value instead" onClick={() => onChange("")}>
        or a value…</Button>
    </span>
  )
}

/** The "otherwise" (else) value for every column a derive step sets, one row each — a bare
 *  single input when there is only the one legacy target, so a simple derive still looks
 *  simple. */
function ElseValuesEditor({ targets, columns, value, onChange }: {
  targets: string[]; columns: string[]; value: Record<string, string>
  onChange: (v: Record<string, string>) => void
}) {
  if (!targets.length) {
    return <p className="text-[11px] text-muted-foreground">name the column(s) to set first</p>
  }
  return (
    <div className="space-y-1">
      {targets.map((t) => (
        <div key={t} className="flex items-center gap-1.5">
          <Mono>{t}</Mono>
          <ValueOrColumn value={value[t] ?? ""} columns={columns}
                        onChange={(v) => onChange({ ...value, [t]: v })} />
        </div>))}
    </div>
  )
}

/** Derive rules: IF conditions THEN set the column(s) to a value; rules run in order. One
 *  condition setting SEVERAL columns together — a SAS IF/DO/END block assigning _TRSTAT
 *  and _TRREASND under the same IF — is one rule with several "then set" rows, not several
 *  separate rules that happen to repeat the same condition. */
function RulesEditor({ rules, columns, targets, onChange, domain, dataset }: {
  rules: Rule[]; columns: string[]; targets: string[]; onChange: (r: Rule[]) => void
  domain?: string; dataset?: string
}) {
  const set = (i: number, patch: Rule) =>
    onChange(rules.map((r, k) => (k === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-2">
      {rules.map((r, i) => (
        <div key={i} className="space-y-1.5 rounded-md border p-2">
          <div className="flex items-center">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
              {i === 0 ? "If" : "Else if"}</span>
            <Button type="button" size="sm" variant="ghost" className="ml-auto h-6 px-2 text-xs"
                    onClick={() => onChange(rules.filter((_, k) => k !== i))}>Remove</Button>
          </div>
          <CondsEditor conds={r.conds ?? []} columns={columns} firstWord="when" domain={domain} dataset={dataset}
                       onChange={(c) => set(i, { conds: c })} />
          <div className="space-y-1">
            {targets.map((t, ti) => (
              <div key={t} className="flex items-center gap-1.5">
                <span className="w-10 shrink-0 text-right text-[11px] text-muted-foreground">
                  {ti === 0 ? "then" : ""}</span>
                <span className="text-[11px] text-muted-foreground">set</span>
                <Mono>{t}</Mono>
                <span className="text-[11px] text-muted-foreground">to</span>
                <ValueOrColumn columns={columns}
                       value={targets.length > 1 ? ((r.values ?? {})[t] ?? "") : (r.value ?? "")}
                       onChange={(v) => (targets.length > 1
                         ? set(i, { values: { ...(r.values ?? {}), [t]: v } })
                         : set(i, { value: v }))} />
              </div>))}
            {!targets.length && <p className="text-[11px] text-muted-foreground">name the column(s) to set first</p>}
          </div>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...rules, targets.length > 1
                ? { conds: [{ operator: "==" }], values: {} }
                : { conds: [{ operator: "==" }], value: "" }])}>
        <Plus className="mr-1 h-3 w-3" />Add a rule
      </Button>
    </div>
  )
}

type Branch = { name?: string; conds?: Cond[] }

function BranchesEditor({ branches, columns, onChange, domain, dataset }: {
  branches: Branch[]; columns: string[]; onChange: (b: Branch[]) => void
  domain?: string; dataset?: string
}) {
  const set = (i: number, patch: Branch) =>
    onChange(branches.map((b, k) => (k === i ? { ...b, ...patch } : b)))
  return (
    <div className="space-y-2">
      {branches.map((b, i) => (
        <div key={i} className="space-y-1.5 rounded-md border p-2">
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground">dataset named</span>
            <Input className="h-8 w-44 font-mono text-xs" placeholder={`branch${i + 1}`}
                   value={b.name ?? ""} onChange={(e) => set(i, { name: e.target.value })} />
            <span className="text-[11px] text-muted-foreground">takes the records</span>
            <Button type="button" size="sm" variant="ghost" className="ml-auto h-6 px-2 text-xs"
                    onClick={() => onChange(branches.filter((_, k) => k !== i))}>Remove</Button>
          </div>
          <CondsEditor conds={b.conds ?? []} columns={columns} domain={domain} dataset={dataset}
                       onChange={(c) => set(i, { conds: c })} />
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...branches, { name: "", conds: [{ operator: "==" }] }])}>
        <Plus className="mr-1 h-3 w-3" />Add a branch
      </Button>
      <p className="text-[10px] text-muted-foreground">
        A record goes to the first branch it matches; the rest can be kept with a name below.</p>
    </div>
  )
}

function RenamesEditor({ rows, columns, onChange }: {
  rows: Array<{ from?: string; to?: string }>; columns: string[]
  onChange: (r: Array<{ from?: string; to?: string }>) => void
}) {
  const set = (i: number, patch: { from?: string; to?: string }) =>
    onChange(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <ColSelect value={r.from} columns={columns} onChange={(v) => set(i, { from: v })} />
          <span className="text-[11px] text-muted-foreground">becomes</span>
          <Input className="h-8 w-44 font-mono text-xs" placeholder="new name"
                 value={r.to ?? ""} onChange={(e) => set(i, { to: e.target.value })} />
          <Button type="button" size="icon" variant="ghost" className="h-7 w-7"
                  onClick={() => onChange(rows.filter((_, k) => k !== i))}>
            <Trash2 className="h-3 w-3" /></Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...rows, {}])}>
        <Plus className="mr-1 h-3 w-3" />Rename a column
      </Button>
    </div>
  )
}

/** One or more new columns, each set to a fixed value on every record — no condition,
 *  no source column, for when "derive" and its if/then rules are more than the job needs. */
function ConstantsEditor({ rows, onChange }: {
  rows: Array<{ name?: string; value?: string }>
  onChange: (r: Array<{ name?: string; value?: string }>) => void
}) {
  const set = (i: number, patch: { name?: string; value?: string }) =>
    onChange(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Input className="h-8 w-44 font-mono text-xs" placeholder="new column, e.g. EPOCH"
                 value={r.name ?? ""} onChange={(e) => set(i, { name: e.target.value })} />
          <span className="text-[11px] text-muted-foreground">=</span>
          <Input className="h-8 w-56 text-xs" placeholder="value"
                 value={r.value ?? ""} onChange={(e) => set(i, { value: e.target.value })} />
          <Button type="button" size="icon" variant="ghost" className="h-7 w-7"
                  onClick={() => onChange(rows.filter((_, k) => k !== i))}>
            <Trash2 className="h-3 w-3" /></Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...rows, {}])}>
        <Plus className="mr-1 h-3 w-3" />Add a column
      </Button>
      {!rows.length && (
        <p className="text-[11px] text-muted-foreground">
          One row per new column — every record gets the same value, no condition needed.
        </p>
      )}
    </div>
  )
}

/** One dataset + its date column; the column list follows the chosen dataset. */
function DateSourceRow({ row, datasets, domain, onChange, onRemove }: {
  row: { dataset?: string; date_col?: string }; datasets: string[]; domain: string
  onChange: (r: { dataset?: string; date_col?: string }) => void; onRemove: () => void
}) {
  const columns = useColumns(domain, row.dataset)
  return (
    <div className="flex items-center gap-1.5">
      <ColSelect value={row.dataset} columns={datasets} placeholder="dataset" label={datasetLabel}
                 onChange={(v) => onChange({ dataset: v, date_col: "" })} />
      <span className="text-[11px] text-muted-foreground">date in</span>
      <ColSelect value={row.date_col} columns={columns}
                 onChange={(v) => onChange({ ...row, date_col: v })} />
      <Button type="button" size="icon" variant="ghost" className="h-7 w-7" onClick={onRemove}>
        <Trash2 className="h-3 w-3" /></Button>
    </div>
  )
}

function DateSourcesEditor({ rows, datasets, domain, onChange }: {
  rows: Array<{ dataset?: string; date_col?: string }>; datasets: string[]; domain: string
  onChange: (r: Array<{ dataset?: string; date_col?: string }>) => void
}) {
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <DateSourceRow key={i} row={r} datasets={datasets} domain={domain}
                       onChange={(nr) => onChange(rows.map((x, k) => (k === i ? nr : x)))}
                       onRemove={() => onChange(rows.filter((_, k) => k !== i))} />
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...rows, {}])}>
        <Plus className="mr-1 h-3 w-3" />Add a dataset
      </Button>
    </div>
  )
}

type Measure = { testcd?: string; test?: string; value_col?: string; unit_col?: string; iso?: boolean }

function MeasuresEditor({ rows, columns, onChange, withName = false }: {
  rows: Measure[]; columns: string[]; onChange: (r: Measure[]) => void; withName?: boolean
}) {
  const set = (i: number, patch: Measure) =>
    onChange(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex flex-wrap items-center gap-1.5">
          <Input className="h-8 w-32 font-mono text-xs" placeholder="TESTCD, e.g. SYSBP"
                 value={r.testcd ?? ""} onChange={(e) => set(i, { testcd: e.target.value })} />
          {withName && (
            <Input className="h-8 w-48 text-xs" placeholder="test name, e.g. Systolic Blood Pressure"
                   value={r.test ?? ""} onChange={(e) => set(i, { test: e.target.value })} />
          )}
          <span className="text-[11px] text-muted-foreground">value from</span>
          <ColSelect value={r.value_col} columns={columns} width="w-40"
                     onChange={(v) => set(i, { value_col: v })} />
          <span className="text-[11px] text-muted-foreground">unit from</span>
          <ColSelect value={r.unit_col} columns={columns} width="w-40" placeholder="optional"
                     onChange={(v) => set(i, { unit_col: v })} />
          <Button type="button" size="icon" variant="ghost" className="h-7 w-7"
                  onClick={() => onChange(rows.filter((_, k) => k !== i))}>
            <Trash2 className="h-3 w-3" /></Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...rows, {}])}>
        <Plus className="mr-1 h-3 w-3" />Add a measurement
      </Button>
    </div>
  )
}

type RuleForm = {
  dataset: string; measures: Measure[]; date_col?: string; time_col?: string; reasnd_col?: string
  date_from?: { dataset: string; column: string; keys: string[] } | null
  not_done?: Array<{ column: string; value: string }>
}

/** The spec-rule transpose, form by form: its tests (code, name, value and unit columns), the
 *  not-done condition that gives the one --ALL record, and where the date, time and reason
 *  come from — every part the build read from the spec, now the reader's to change. */
function RuleFormsEditor({ forms, domain, onChange }: {
  forms: RuleForm[]; domain: string; onChange: (f: RuleForm[]) => void
}) {
  const set = (i: number, patch: Partial<RuleForm>) =>
    onChange(forms.map((f, k) => (k === i ? { ...f, ...patch } : f)))
  return (
    <div className="space-y-3">
      {forms.map((f, i) => <RuleFormRow key={i} form={f} domain={domain} onChange={(p) => set(i, p)} />)}
    </div>
  )
}

function RuleFormRow({ form, domain, onChange }: {
  form: RuleForm; domain: string; onChange: (p: Partial<RuleForm>) => void
}) {
  const columns = useColumns(domain, form.dataset)
  const nd = form.not_done?.[0] ?? { column: "", value: "N" }
  return (
    <div className="space-y-2 rounded-md border bg-background/60 p-2.5">
      <p className="text-[12px] font-medium">From <Mono>{form.dataset}</Mono> — one record per test with a result</p>
      <MeasuresEditor rows={form.measures ?? []} columns={columns} withName
                      onChange={(r) => onChange({ measures: r })} />
      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
        <span>a visit is not done when</span>
        <ColSelect value={nd.column} columns={columns} width="w-40" placeholder="no flag"
                   onChange={(v) => onChange({ not_done: v ? [{ column: v, value: nd.value || "N" }] : [] })} />
        <span>=</span>
        <Input className="h-8 w-16 font-mono text-xs" value={nd.value ?? ""}
               onChange={(e) => onChange({ not_done: nd.column ? [{ column: nd.column, value: e.target.value }] : [] })} />
        <span>— reason from</span>
        <ColSelect value={form.reasnd_col} columns={columns} width="w-40" placeholder="none"
                   onChange={(v) => onChange({ reasnd_col: v })} />
      </div>
      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
        <span>date</span>
        {form.date_from ? (
          <span>from <Mono>{form.date_from.dataset}.{form.date_from.column}</Mono>, merged by{" "}
            <Mono>{form.date_from.keys.join(" + ")}</Mono></span>
        ) : (
          <ColSelect value={form.date_col} columns={columns} width="w-40" placeholder="none"
                     onChange={(v) => onChange({ date_col: v })} />
        )}
        <span>time</span>
        <ColSelect value={form.time_col} columns={columns} width="w-40" placeholder="none"
                   onChange={(v) => onChange({ time_col: v })} />
      </div>
    </div>
  )
}

/** Ordered source columns for compute — order matters when joining text. */
function ColSeqEditor({ cols, columns, onChange }: {
  cols: string[]; columns: string[]; onChange: (c: string[]) => void
}) {
  return (
    <div className="space-y-1.5">
      {cols.map((c, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <ColSelect value={c} columns={columns}
                     onChange={(v) => onChange(cols.map((x, k) => (k === i ? v : x)))} />
          <Button type="button" size="icon" variant="ghost" className="h-7 w-7"
                  onClick={() => onChange(cols.filter((_, k) => k !== i))}>
            <Trash2 className="h-3 w-3" /></Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...cols, ""])}>
        <Plus className="mr-1 h-3 w-3" />{cols.length ? "And then" : "Pick a column"}
      </Button>
    </div>
  )
}

/** Loads the step's dataset columns once, then hands them to whichever editor needs them. */
function StepColumns({ domain, dataset, children }: {
  domain: string; dataset?: string; children: (columns: string[]) => ReactNode
}) {
  const columns = useColumns(domain, dataset)
  if (!dataset) {
    return <p className="text-[11px] text-muted-foreground">Choose the dataset first — its columns will be offered here.</p>
  }
  return <>{children(columns)}</>
}

/** The step the build applied on its own (a stack of the forms, a findings transpose) —
 *  what this domain's preparation IS until the reader makes it their own. */
function appliedSteps(detail: DomainDetail): Step[] {
  const own = (detail.pipeline_draft ?? detail.pipeline ?? []) as Step[]
  if (own.length || !detail.prep) return []
  const { op, name, params } = detail.prep
  return [{ op, name, params: JSON.parse(JSON.stringify(params ?? {})) }]
}

export function PipelineEditor({ detail, onDone }: { detail: DomainDetail; onDone: () => void }) {
  const [steps, setSteps] = useState<Step[]>(() => {
    const own = (detail.pipeline_draft ?? detail.pipeline ?? []) as Step[]
    return JSON.parse(JSON.stringify(own.length ? own : appliedSteps(detail)))
  })
  // true while the list shows the build's automatic step, untouched: it is not a draft (no
  // autosave), Apply has nothing to apply, and the build keeps detecting it from the spec
  const [fromAuto, setFromAuto] = useState(() => appliedSteps(detail).length > 0)
  const [ops, setOps] = useState<Array<{ id: string; label: string }>>([])
  const [reports, setReports] = useState<Report[]>((detail.prep_reports as unknown as Report[]) ?? [])
  const [outs, setOuts] = useState<Record<string, { columns: string[]; sample: string[][]; rows: number }>>({})
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState(false)
  // dirty = edited since the last successful autosave. Shown next to "live preview" so the
  // reader can SEE it is safe to refresh instead of having to trust a timer they can't see.
  const [dirty, setDirty] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)
  // per-step minimize/maximize + the remembered previews toggle — as in SDTM Designer's
  // prep studio (👁 previews on/off, steps collapse to their one-line summary).
  // A pipeline with several steps OPENS collapsed: a numbered list of plain sentences,
  // where each row is opened only to edit it — twenty steps must read like a recipe,
  // not twenty forms.
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>(() => {
    const saved = (detail.pipeline_draft ?? detail.pipeline ?? []) as Step[]
    return saved.length > 1 ? Object.fromEntries(saved.map((_, i) => [i, true])) : {}
  })
  // the dataset name of the step whose FULL output is open in a dialog — "" when closed
  const [fullView, setFullView] = useState<string>("")
  // which output the domain's records follow. "" = the last step (the default). Pinning
  // it means adding prep2 later never silently moves the variables off prep1.
  const [recordsFrom, setRecordsFrom] = useState<string>(
    () => ((detail.override as { base?: string })?.base ?? ""))
  const [showPrev, setShowPrev] = useState<boolean>(() => {
    try { return localStorage.getItem("prepPreviews") !== "off" } catch { return true }
  })
  const togglePrev = () => setShowPrev((v) => {
    const n = !v
    try { localStorage.setItem("prepPreviews", n ? "on" : "off") } catch { /* fine */ }
    return n
  })

  useEffect(() => { void api.prepOps().then((r) => setOps(r.ops)) }, [])

  // The saved steps arrive with the detail; if this instance somehow mounted before they
  // did, adopt them rather than sit on an empty list the reader cannot edit.
  const touched = useRef(false)
  useEffect(() => {
    const saved = (detail.pipeline_draft ?? detail.pipeline ?? []) as Step[]
    if (!touched.current && !steps.length && saved.length) {
      setSteps(JSON.parse(JSON.stringify(saved)))
      if (saved.length > 1) setCollapsed(Object.fromEntries(saved.map((_, i) => [i, true])))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail])

  // Live preview: the pipeline runs as it is edited. Asking for a button press means working
  // blind between presses, which is where a wrong step survives long enough to be trusted.
  // The debounce is short (200ms) so little is at risk if a refresh interrupts it, and the
  // "dirty" flag below plus the beforeunload/visibility flush cover the rest of that gap.
  const firstRun = useRef(true)
  const hadSteps = useRef(false)
  useEffect(() => {
    if (!firstRun.current) { touched.current = true; setDirty(true); setFromAuto(false) }   // any change after mount pins this editor
    if (!steps.length) {
      setReports([]); setOuts({})
      // removing every step must forget the saved draft — but ONLY when the reader
      // actually removed them in this editor; an instance that merely mounted empty
      // must never clear a draft it never showed
      if (!firstRun.current && hadSteps.current) void api.previewPipeline(detail.domain, [])
      firstRun.current = false
      return
    }
    hadSteps.current = true
    const auto = firstRun.current && fromAuto     // the untouched automatic step: preview only
    const t = window.setTimeout(() => { void run(steps, true, !auto) }, firstRun.current ? 0 : 200)
    firstRun.current = false
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(steps)])

  const run = async (list: Step[], quiet = false, draft = true) => {
    if (!quiet) setBusy(true)
    setLive(true)
    try {
      const r = await api.previewPipeline(detail.domain, list, draft)
      if (!r.ok) { setReports([]); setOuts({}); setError(r.error ?? "failed"); setSaveFailed(draft); return }
      setError(""); setSaveFailed(false); setDirty(false)
      setReports((r.reports ?? []) as Report[])
      // EVERY step's output, so each step keeps its own preview table — adding prep2
      // must never take prep1's preview away
      setOuts((r.outputs ?? {}) as Record<string, { columns: string[]; sample: string[][]; rows: number }>)
    } catch (e) { setError((e as Error).message); setSaveFailed(true) } finally { setLive(false); if (!quiet) setBusy(false) }
  }

  // Flush a pending draft the instant the reader might leave: refresh/close (beforeunload),
  // switching tabs (visibilitychange), or navigating elsewhere in the app (unmount). Without
  // this, a change made inside the debounce window is lost if any of those happen first —
  // which is exactly how a second prep step can silently vanish.
  const stepsRef = useRef(steps)
  useEffect(() => { stepsRef.current = steps }, [steps])
  const dirtyRef = useRef(dirty)
  useEffect(() => { dirtyRef.current = dirty }, [dirty])
  useEffect(() => {
    const flush = () => {
      if (!dirtyRef.current) return
      try {
        const blob = new Blob([JSON.stringify({ steps: stepsRef.current })], { type: "application/json" })
        navigator.sendBeacon(`/api/domain/${detail.domain}/pipeline/preview`, blob)
      } catch { /* best effort — nothing more can be done on the way out */ }
    }
    const onVisibility = () => { if (document.visibilityState === "hidden") flush() }
    window.addEventListener("beforeunload", flush)
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      window.removeEventListener("beforeunload", flush)
      document.removeEventListener("visibilitychange", onVisibility)
      flush()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.domain])

  // Undo/redo over the whole step list — the answer to "what if I did something and want
  // to revert": every structural change (add, delete, reorder, insert, op swap) is one entry;
  // plain typing coalesces into a single entry per pause instead of one per keystroke, so
  // undo steps back through *edits*, not characters. Separately, "Revert to last saved"
  // (below) jumps straight back to the last Apply & rebuild, skipping the whole history.
  const [past, setPast] = useState<Step[][]>([])
  const [future, setFuture] = useState<Step[][]>([])
  const coalesceBase = useRef<Step[] | null>(null)
  const coalesceTimer = useRef<number | undefined>(undefined)
  const updateSteps = useCallback((updater: (s: Step[]) => Step[], coalesce = false) => {
    setSteps((cur) => {
      const next = updater(cur)
      if (next === cur) return cur
      if (coalesce) {
        if (coalesceBase.current === null) coalesceBase.current = cur
        window.clearTimeout(coalesceTimer.current)
        coalesceTimer.current = window.setTimeout(() => {
          const base = coalesceBase.current
          coalesceBase.current = null
          if (base) { setPast((p) => [...p.slice(-49), base]); setFuture([]) }
        }, 600)
      } else {
        window.clearTimeout(coalesceTimer.current)
        coalesceBase.current = null
        setPast((p) => [...p.slice(-49), cur]); setFuture([])
      }
      return next
    })
  }, [])
  const undo = () => {
    if (!past.length) return
    const prev = past[past.length - 1]
    setPast((p) => p.slice(0, -1))
    setFuture((f) => [...f, stepsRef.current])
    setSteps(prev)
  }
  const redo = () => {
    if (!future.length) return
    const next = future[future.length - 1]
    setFuture((f) => f.slice(0, -1))
    setPast((p) => [...p, stepsRef.current])
    setSteps(next)
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "z") return
      const tag = (e.target as HTMLElement | null)?.tagName
      if (tag === "INPUT" || tag === "TEXTAREA") return   // let native text-field undo work
      e.preventDefault()
      if (e.shiftKey) redo(); else undo()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [past, future])

  // the last-applied (Apply & rebuild) pipeline, frozen at mount — "Revert" goes back to
  // this exact baseline no matter how far editing has since wandered
  const savedBaseline = useRef<Step[]>(JSON.parse(JSON.stringify(
    (detail.pipeline ?? []).length ? detail.pipeline : appliedSteps(detail))))
  const revert = () => {
    updateSteps(() => JSON.parse(JSON.stringify(savedBaseline.current)))
  }

  const nextPrepName = (list: Step[]) => {
    const used = new Set(list.map((s, k) => s.name || `prep${k + 1}`))
    let n = list.length + 1
    while (used.has(`prep${n}`)) n++
    return `prep${n}`
  }
  // collapsed is keyed by position, so inserting/removing/reordering steps must move the
  // flags with the steps they describe — and a freshly added step always opens for editing
  const shiftCollapsedFrom = (idx: number) =>
    setCollapsed((c) => {
      const n: Record<number, boolean> = {}
      for (const [k, v] of Object.entries(c)) {
        const i = Number(k)
        n[i >= idx ? i + 1 : i] = v
      }
      n[idx] = false
      return n
    })
  const dropCollapsedAt = (idx: number) =>
    setCollapsed((c) => {
      const n: Record<number, boolean> = {}
      for (const [k, v] of Object.entries(c)) {
        const i = Number(k)
        if (i === idx) continue
        n[i > idx ? i - 1 : i] = v
      }
      return n
    })
  const swapCollapsed = (a: number, b: number) =>
    setCollapsed((c) => ({ ...c, [a]: c[b] ?? false, [b]: c[a] ?? false }))
  const insertStepAt = (idx: number) => {
    shiftCollapsedFrom(idx)
    updateSteps((s) => [...s.slice(0, idx), { op: "stack", name: nextPrepName(s), params: {} }, ...s.slice(idx)])
  }
  // "I did this for one raw dataset, now the same for another" — clone everything (every
  // field, every rule) and drop it right after the original, so the only thing left to do
  // is swap the dataset/column picker(s), not rebuild the whole step from scratch.
  const duplicateStepAt = (idx: number) => {
    shiftCollapsedFrom(idx + 1)
    updateSteps((s) => {
      const copy: Step = JSON.parse(JSON.stringify(s[idx]))
      copy.name = nextPrepName(s)
      return [...s.slice(0, idx + 1), copy, ...s.slice(idx + 1)]
    })
  }

  const setOp = (i: number, op: string) => updateSteps((s) => s.map((st, k) => (k === i ? { ...st, op, params: {} } : st)))
  const upd = (i: number, patch: Partial<Step>) =>
    updateSteps((s) => s.map((st, k) => (k === i ? { ...st, ...patch } : st)), true)
  const setParam = (i: number, k: string, v: unknown, coalesce = false) =>
    updateSteps((s) => s.map((st, idx) => {
      if (idx !== i) return st
      const p = { ...st.params }
      if (v === "" || v === undefined || (Array.isArray(v) && !v.length)) delete p[k]
      else p[k] = v
      return { ...st, params: p }
    }), coalesce)

  // earlier steps' outputs first, then the raw datasets, then any already-built domain
  // (as a distinct 'built_<domain>' entry, never the domain's raw dataset name — the two
  // can share the same letters, e.g. 'dm', and must never be confused) — deduplicated:
  // once a step's preview has run, its output is ALSO in the store (detail.datasets), and a
  // name listed twice renders as "prep1 prep1" in every picker
  const datasetsFor = (i: number) =>
    Array.from(new Set([...steps.slice(0, i).map((s, k) => s.name || `prep${k + 1}`),
                        ...detail.datasets,
                        ...detail.built_domains.map((d) => `built_${d.toLowerCase()}`)]))

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError("")
    try { await fn() } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  // every step starts expanded (collapsed defaults to {}, i.e. nothing minimised) — this
  // just flips them all at once, on top of each step's own chevron for one-at-a-time control
  const allCollapsed = steps.length > 0 && steps.every((_, i) => collapsed[i])
  const toggleAllCollapsed = () => {
    const target = !allCollapsed
    setCollapsed(Object.fromEntries(steps.map((_, i) => [i, target])))
  }

  return (
    <div className="space-y-3 rounded-lg border">
      <div className="flex flex-wrap items-center gap-2 border-b bg-muted/50 px-3 py-2">
        <h4 className="flex-1 text-sm font-medium">
          Prepare the data{steps.length ? ` — ${steps.length} step${steps.length > 1 ? "s" : ""}` : ""}
        </h4>
        <Button size="icon" variant="ghost" className="h-7 w-7" disabled={!past.length}
                title="undo the last change (Ctrl+Z)" onClick={undo}>
          <Undo2 className="h-3.5 w-3.5" /></Button>
        <Button size="icon" variant="ghost" className="h-7 w-7" disabled={!future.length}
                title="redo (Ctrl+Shift+Z)" onClick={redo}>
          <Redo2 className="h-3.5 w-3.5" /></Button>
        {steps.length > 0 && (
          <Button size="sm" variant="ghost" className="h-7 text-xs"
                  disabled={JSON.stringify(steps) === JSON.stringify(savedBaseline.current)}
                  title="discard every change since the last Apply & rebuild" onClick={revert}>
            <RotateCcw className="mr-1 h-3.5 w-3.5" />Revert to last saved</Button>
        )}
        <Button size="sm" variant="outline" className="h-7 text-xs"
                onClick={() => insertStepAt(steps.length)}>
          <Plus className="mr-1 h-3 w-3" />Add step
        </Button>
        {detail.prep && !fromAuto && !steps.length && (
          <Button size="sm" variant="outline" className="h-7 text-xs" disabled={busy}
                  onClick={() => void act(async () => {
                    const s = (await api.pipelineFromAuto(detail.domain)).steps as Step[]
                    updateSteps(() => s)
                  })}>
            Start from the detected step
          </Button>
        )}
        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={togglePrev}
                title={showPrev ? "hide the per-step preview tables (steps still run)"
                                : "show a preview table under every step"}>
          {showPrev ? <Eye className="mr-1 h-3.5 w-3.5" /> : <EyeOff className="mr-1 h-3.5 w-3.5" />}
          previews {showPrev ? "on" : "off"}
        </Button>
        {steps.length > 1 && (
          <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={toggleAllCollapsed}
                  title={allCollapsed ? "expand every step" : "collapse every step to one line each"}>
            {allCollapsed ? <ChevronsUpDown className="mr-1 h-3.5 w-3.5" /> : <ChevronsDownUp className="mr-1 h-3.5 w-3.5" />}
            {allCollapsed ? "expand all" : "collapse all"}
          </Button>
        )}
        <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          {live ? <><Loader2 className="h-3 w-3 animate-spin" />saving…</>
                : saveFailed ? <span className="text-destructive">draft not saved — will retry</span>
                : dirty ? <>unsaved…</>
                : fromAuto ? <>applied by the build</>
                : steps.length ? <>saved</> : null}
        </span>
        {steps.length > 0 && (
          <span className="flex items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground"
                  title="the output the domain's records are built from — variables whose columns it carries follow it; hand-edited variables always keep their own source">
              records from</span>
            <Select value={recordsFrom || "__last"}
                    onValueChange={(v) => setRecordsFrom(v === "__last" ? "" : v)}>
              <SelectTrigger className="h-7 w-44 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__last" className="text-xs">the last step (default)</SelectItem>
                {steps.map((s, k) => {
                  const n = s.name || `prep${k + 1}`
                  return <SelectItem key={n} value={n} className="text-xs">{n} (pinned)</SelectItem>
                })}
              </SelectContent>
            </Select>
          </span>
        )}
        <Button size="sm" className="h-7 text-xs" disabled={busy || fromAuto}
                title={fromAuto ? "nothing to apply — this is what the build already does; change a step first" : undefined}
                onClick={() => void act(async () => {
                  await api.setPipeline(detail.domain, steps, recordsFrom)
                  await api.rebuild(detail.domain); onDone()
                })}>Apply &amp; rebuild</Button>
        {steps.length > 0 && !fromAuto && (
          <Button size="sm" variant="ghost" className="h-7 text-xs" disabled={busy}
                  onClick={() => void act(async () => {
                    if (!window.confirm("Remove every step and rebuild without preparation? This applies immediately.")) return
                    await api.setPipeline(detail.domain, [])
                    await api.rebuild(detail.domain); onDone()
                  })}>Remove all</Button>
        )}
      </div>

      {fromAuto && detail.prep && (
        <div className="px-3">
          <Callout tone="info" title="The step the build applied on its own">
            {detail.prep.note} Change it, or add steps before or after it, then <b>Apply &amp; rebuild</b> to
            make this the domain's own preparation. Until then the build keeps detecting it from the
            spec and the raw data. To build without it, set Data preparation to off in Settings.
          </Callout>
        </div>
      )}

      {!steps.length && (
        <p className="px-3 pb-3 text-xs text-muted-foreground">
          No preparation steps — the build uses the detected record source as it is. Add a step to
          stack, merge, filter, reshape or summarise the raw data first.
        </p>
      )}

      {/* Pipeline map: with many steps, an index answers "where is the one that…?" without
          scrolling — each chip is the step's number, colour, op and output name; click jumps. */}
      {steps.length >= 4 && (
        <div className="mx-3 flex flex-wrap items-center gap-1.5 rounded-lg border border-dashed
                        bg-muted/30 px-2.5 py-2">
          <span className="mr-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            Map
          </span>
          {steps.map((s, k) => {
            const m = OP_META[s.op] ?? DEFAULT_META
            const Icon = m.icon
            return (
              <button key={k} type="button" title={stepSummary(s)}
                      onClick={() => {
                        setCollapsed((c) => ({ ...c, [k]: false }))
                        document.getElementById(`prep-step-${k}`)
                          ?.scrollIntoView({ behavior: "smooth", block: "center" })
                      }}>
                <Chip tone={m.tone} className="cursor-pointer transition hover:scale-105 hover:shadow-sm">
                  <span className="font-semibold">{k + 1}</span>
                  <Icon className="h-3 w-3" />
                  <span className="font-mono">{s.name || `prep${k + 1}`}</span>
                </Chip>
              </button>
            )
          })}
        </div>
      )}

      {steps.length > 0 && <InsertRow onInsert={() => insertStepAt(0)} />}
      {(() => {
        const childrenOf: Record<number, number[]> = {}
        steps.forEach((_, k) => {
          const p = stepParent(steps, k)
          if (p !== null) (childrenOf[p] ??= []).push(k)
        })
        return steps.map((st, i) => {
        const rep = reports.find((r) => r.step === i + 1)
        const isMin = !!collapsed[i]
        const meta = OP_META[st.op] ?? DEFAULT_META
        const OpIcon = meta.icon
        // a derive step saved before "several columns at once" existed still has the single
        // legacy 'target' — read it the same way the engine does, so re-opening an old step
        // never LOOKS like it lost its column just because the field renamed
        const deriveTargets = (st.params.targets as string[])?.length
          ? (st.params.targets as string[])
          : (st.params.target ? [String(st.params.target)] : [])
        const parent = stepParent(steps, i)
        const siblings = parent !== null ? (childrenOf[parent] ?? []) : []
        return (
          <div key={i} id={`prep-step-${i}`}>
          <div className="flex">
          <GraphGutter hasParent={parent !== null} hasChildren={(childrenOf[i] ?? []).length > 0}
                       isLastChild={siblings[siblings.length - 1] === i} />
          <div className={`min-w-0 flex-1 space-y-3 border-b border-l-4 px-3 pb-3 last:border-b-0 ${ACCENT_BORDER[meta.tone]}`}>
            <div className={cn(`-mx-1 flex flex-wrap items-center gap-2 rounded-lg px-1 py-1 ${HEAD_TINT[meta.tone]}`,
                               isMin && "cursor-pointer")}
                 onClick={isMin ? () => setCollapsed((c) => ({ ...c, [i]: false })) : undefined}
                 title={isMin ? "click to open this step" : undefined}>
              <Button size="icon" variant="ghost" className="h-7 w-7"
                      title={isMin ? "open this step" : "collapse this step to one line"}
                      onClick={(e) => { e.stopPropagation(); setCollapsed((c) => ({ ...c, [i]: !c[i] })) }}>
                {isMin ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
              </Button>
              <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-muted text-[10px] font-semibold">{i + 1}</span>
              <Chip tone={meta.tone} className="shrink-0"><OpIcon className="h-3 w-3" /></Chip>
              {isMin ? (
                <>
                  {/* collapsed: one readable sentence, no controls — the step as a recipe line */}
                  <span className="shrink-0 text-[13px] font-medium">
                    {(ops.find((o) => o.id === st.op)?.label ?? st.op).split(" — ")[0]}
                  </span>
                  <Mono>{st.name || `prep${i + 1}`}</Mono>
                  <StepSasLine step={st} summary={stepSummary(st)} />
                  {rep && (rep.ok
                    ? <span className="num-cell shrink-0 text-[11px] text-muted-foreground">
                        {rep.rows?.toLocaleString()} rec</span>
                    : <span className="shrink-0 text-[11px] font-medium text-destructive"
                            title={rep.error}>✗ error</span>)}
                </>
              ) : (
                <>
                  <Select value={st.op} onValueChange={(v) => setOp(i, v)}>
                    <SelectTrigger className="h-8 w-72 text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent>{ops.map((o) => {
                      const m = OP_META[o.id] ?? DEFAULT_META
                      const Icon = m.icon
                      return (
                        <SelectItem key={o.id} value={o.id} className="text-xs">
                          <span className="flex items-center gap-2">
                            <Chip tone={m.tone}><Icon className="h-3 w-3" /></Chip>{o.label}
                          </span>
                        </SelectItem>)
                    })}</SelectContent>
                  </Select>
                  <Input className="h-8 w-40 font-mono text-xs" value={st.name} placeholder="output name"
                         title="the name this step's output is known by everywhere else in the pipeline"
                         onChange={(e) => upd(i, { name: e.target.value })} />
                  <span className={`hidden min-w-0 flex-1 truncate text-[11.5px] italic xl:inline ${FIELD_TONE_TEXT[meta.tone]}`}
                        title={stepSummary(st)}>
                    {stepSummary(st)}
                  </span>
                </>
              )}
              <div className="ml-auto flex gap-1" onClick={(e) => e.stopPropagation()}>
                <Button size="icon" variant="ghost" className="h-7 w-7" disabled={i === 0}
                        title="move this step up"
                        onClick={() => { swapCollapsed(i - 1, i); updateSteps((s) => { const n = [...s]; [n[i - 1], n[i]] = [n[i], n[i - 1]]; return n }) }}>
                  <ArrowUp className="h-3.5 w-3.5" /></Button>
                <Button size="icon" variant="ghost" className="h-7 w-7" disabled={i === steps.length - 1}
                        title="move this step down"
                        onClick={() => { swapCollapsed(i, i + 1); updateSteps((s) => { const n = [...s]; [n[i + 1], n[i]] = [n[i], n[i + 1]]; return n }) }}>
                  <ArrowDown className="h-3.5 w-3.5" /></Button>
                <Button size="icon" variant="ghost" className="h-7 w-7"
                        title="duplicate this step — everything is copied, so only the dataset/columns need changing"
                        onClick={() => duplicateStepAt(i)}>
                  <CopyPlus className="h-3.5 w-3.5" /></Button>
                <Button size="icon" variant="ghost" className="h-7 w-7"
                        title="remove this step (Ctrl+Z to undo)"
                        onClick={() => { dropCollapsedAt(i); updateSteps((s) => s.filter((_, k) => k !== i)) }}>
                  <Trash2 className="h-3.5 w-3.5" /></Button>
              </div>
            </div>

            {!isMin && (parent !== null || (childrenOf[i] ?? []).length > 0) && (
              <p className="-mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 pl-9 text-[11px] text-primary">
                {parent !== null && (
                  <span className="inline-flex items-center gap-1">
                    <ArrowUp className="h-3 w-3 rotate-180" />reads <Mono>{stepName(steps, parent)}</Mono>
                  </span>
                )}
                {(childrenOf[i] ?? []).length > 0 && (
                  <span className="inline-flex flex-wrap items-center gap-1">
                    <ArrowDown className="h-3 w-3" />feeds
                    {(childrenOf[i] ?? []).map((k) => <Mono key={k}>{stepName(steps, k)}</Mono>)}
                  </span>
                )}
              </p>
            )}

            {!isMin && <StepSas step={st} />}

            {!isMin && <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {(FIELDS[st.op] ?? []).map((f) => {
                // a parameter appears only for the function it belongs to
                if (f.funcs && !f.funcs.includes(st.params.func as string)) return null
                if (f.notFuncs?.includes(st.params.func as string)) return null
                const v = st.params[f.k]
                const wide = ["mergeinputs", "joinkeys", "conds", "rules", "branches", "sasexpr", "ruleforms",
                              "renames", "datesources", "measures", "constants", "elsevalues"].includes(f.t)
                let control
                if (f.t === "sasexpr") {
                  control = <SasCodeField value={(v as string) ?? ""} rows={2} placeholder={f.ph}
                                          onChange={(x) => setParam(i, f.k, x, true)} />
                } else if (f.t === "ds") {
                  control = (
                    <Select value={(v as string) || "__none"}
                            onValueChange={(x) => setParam(i, f.k, x === "__none" ? "" : x)}>
                      <SelectTrigger className="h-8 text-xs"><SelectValue placeholder="—" /></SelectTrigger>
                      <SelectContent><SelectItem value="__none" className="text-xs">—</SelectItem>
                        {datasetsFor(i).map((d) => <SelectItem key={d} value={d} className="text-xs">{datasetLabel(d)}</SelectItem>)}
                      </SelectContent></Select>)
                } else if (f.t === "dslist") {
                  control = <MaximizableCheckList title={f.label ?? "Datasets"} options={datasetsFor(i)}
                                                  chosen={(v as string[]) ?? []} labelFor={datasetLabel}
                                                  onChange={(list) => setParam(i, f.k, list)} />
                } else if (f.t.startsWith("choice:")) {
                  const opts = f.t.slice(7).split(",")
                  control = (
                    <Select value={(v as string) || "__none"}
                            onValueChange={(x) => setParam(i, f.k, x === "__none" ? "" : x)}>
                      <SelectTrigger className="h-8 text-xs"><SelectValue placeholder="—" /></SelectTrigger>
                      <SelectContent><SelectItem value="__none" className="text-xs">—</SelectItem>
                        {opts.map((o) => <SelectItem key={o} value={o} className="text-xs">{o}</SelectItem>)}
                      </SelectContent></Select>)
                } else if (f.t === "mergeinputs") {
                  control = <MergeInputs rows={(v as MergeInput[]) ?? []} datasets={datasetsFor(i)}
                                         domain={detail.domain} on={(st.params.on as string[]) ?? []}
                                         onChange={(rows) => setParam(i, f.k, rows)} />
                } else if (f.t === "joinkeys") {
                  control = <JoinKeys value={(v as string[]) ?? []}
                                      inputs={(st.params.inputs as MergeInput[]) ?? []}
                                      domain={detail.domain}
                                      onChange={(keys) => setParam(i, f.k, keys)} />
                } else if (f.t === "choicel") {
                  control = (
                    <Select value={(v as string) || "__none"}
                            onValueChange={(x) => setParam(i, f.k, x === "__none" ? "" : x)}>
                      <SelectTrigger className="h-8 text-xs"><SelectValue placeholder="—" /></SelectTrigger>
                      <SelectContent><SelectItem value="__none" className="text-xs">—</SelectItem>
                        {(f.opts ?? []).map(([val, lab]) =>
                          <SelectItem key={val} value={val} className="text-xs">{lab}</SelectItem>)}
                      </SelectContent></Select>)
                } else if (f.t === "conds") {
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => <CondsEditor conds={(v as Cond[]) ?? []} columns={columns}
                                                 domain={detail.domain} dataset={st.params.dataset as string}
                                                 onChange={(c) => setParam(i, f.k, c)} />}
                    </StepColumns>)
                } else if (f.t === "rules") {
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => <RulesEditor rules={(v as Rule[]) ?? []} columns={columns}
                                                 targets={deriveTargets}
                                                 domain={detail.domain} dataset={st.params.dataset as string}
                                                 onChange={(r) => setParam(i, f.k, r)} />}
                    </StepColumns>)
                } else if (f.t === "targets") {
                  control = <ListInput value={deriveTargets} placeholder={f.ph}
                                      onChange={(list) => setParam(i, f.k, list)} />
                } else if (f.t === "elsevalues") {
                  const effective = (v as Record<string, string>)
                    ?? (st.params.else_value ? { [deriveTargets[0]]: String(st.params.else_value) } : {})
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => <ElseValuesEditor targets={deriveTargets} columns={columns} value={effective}
                                                      onChange={(vals) => setParam(i, f.k, vals)} />}
                    </StepColumns>)
                } else if (f.t === "branches") {
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => <BranchesEditor branches={(v as Branch[]) ?? []} columns={columns}
                                                    domain={detail.domain} dataset={st.params.dataset as string}
                                                    onChange={(b) => setParam(i, f.k, b)} />}
                    </StepColumns>)
                } else if (f.t === "renames") {
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => <RenamesEditor rows={(v as Array<{ from?: string; to?: string }>) ?? []}
                                                   columns={columns}
                                                   onChange={(r) => setParam(i, f.k, r)} />}
                    </StepColumns>)
                } else if (f.t === "constants") {
                  control = <ConstantsEditor rows={(v as Array<{ name?: string; value?: string }>) ?? []}
                                             onChange={(r) => setParam(i, f.k, r)} />
                } else if (f.t === "ruleforms") {
                  control = <RuleFormsEditor forms={(v as RuleForm[]) ?? []} domain={detail.domain}
                                             onChange={(r) => setParam(i, f.k, r)} />
                } else if (f.t === "measures") {
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => <MeasuresEditor rows={(v as Measure[]) ?? []} columns={columns}
                                                    onChange={(r) => setParam(i, f.k, r)} />}
                    </StepColumns>)
                } else if (f.t === "colseq") {
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => <ColSeqEditor cols={(v as string[]) ?? []} columns={columns}
                                                  onChange={(c) => setParam(i, f.k, c)} />}
                    </StepColumns>)
                } else if (f.t === "datesources") {
                  control = <DateSourcesEditor rows={(v as Array<{ dataset?: string; date_col?: string }>) ?? []}
                                               datasets={datasetsFor(i)} domain={detail.domain}
                                               onChange={(r) => setParam(i, f.k, r)} />
                } else if (f.t === "list") {
                  control = <ListInput value={Array.isArray(v) ? v as string[] : []} placeholder={f.ph}
                                      onChange={(list) => setParam(i, f.k, list)} />
                } else if (f.t === "collist") {
                  // a typed column name that doesn't exist in the dataset fails silently at
                  // build time (e.g. "needs at least one grouping column" for a key that just
                  // never matched) — offering only the columns that are actually there rules
                  // that mistake out entirely
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => <MaximizableCheckList title={f.label ?? "Columns"} options={columns}
                                                          chosen={(v as string[]) ?? []}
                                                          onChange={(list) => setParam(i, f.k, list)} />}
                    </StepColumns>)
                } else {
                  control = <Input className="h-8 text-xs" placeholder={f.ph ?? ""} value={(v as string) ?? ""}
                                   onChange={(e) => setParam(i, f.k, e.target.value, true)} />
                }
                return (
                  <div key={f.k} className={cn(wide ? "col-span-full" : "", "space-y-1 rounded-md border p-2",
                                                FIELD_TONE_BOX[FIELD_TONE[f.t] ?? "slate"])}>
                    <Label className={cn("text-[11px] uppercase tracking-wide",
                                         FIELD_TONE_TEXT[FIELD_TONE[f.t] ?? "slate"])}>
                      {f.label ?? f.k}</Label>
                    {control}
                  </div>)
              })}
            </div>}

            {!isMin && rep && (rep.ok
              ? <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>→ <Mono>{rep.name}</Mono>{" "}
                    {rep.rows?.toLocaleString()} records, {rep.columns?.length} columns
                    {rep.extra_outputs?.length ? ` · also produced ${rep.extra_outputs.join(", ")}` : ""}</span>
                  <Button type="button" size="sm" variant="ghost" className="h-6 px-2 text-[11px]"
                          onClick={() => setFullView(rep.name)}>
                    <Maximize2 className="mr-1 h-3 w-3" />View full data
                  </Button>
                </p>
              : <p className="text-xs text-destructive">✗ {rep.error}</p>)}
            {/* every step keeps its OWN preview — adding a later step never hides it */}
            {showPrev && !isMin && rep?.ok && outs[rep.name] && (
              <DataGrid height="11rem" rowNumbers={false} rows={outs[rep.name].sample}
                cols={outs[rep.name].columns.map((c, ci) => ({ id: c, head: c, kind: "text" as const,
                                                               cell: (r: string[]) => r[ci] }))} />
            )}
          </div>
          </div>
          <InsertRow onInsert={() => insertStepAt(i + 1)} />
          </div>
        )
      })
      })()}

      {error && <div className="px-3 pb-3"><Callout tone="bad">{error}</Callout></div>}
      {fullView && <FullDataDialog dataset={fullView} onClose={() => setFullView("")} />}
    </div>
  )
}


/** One step's SAS, as the program writes it: the one-line sentence with the SAS ▸ toggle. */
function StepSas({ step }: { step: Step }) {
  const lines = useSasLines(JSON.stringify(step), () => api.prepSas(step))
  return lines.length ? <SasForm lines={lines} title="SAS" /> : null
}

/** A collapsed step's one line: its SAS sentence (plain English on hover). */
function StepSasLine({ step, summary }: { step: Step; summary: string }) {
  const lines = useSasLines(JSON.stringify(step), () => api.prepSas(step))
  const line = sasSentence(lines)
  return (
    <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-sky-700 dark:text-sky-300"
          title={summary}>
      {line || summary}
    </span>)
}


/* ── SDTM Studio's prep, rendered the GUI way ───────────────────────────────
   A studio-imported study's steps are read-only here, but they deserve the same
   visual language as the editable pipeline: one colour per op, a plain sentence
   per step, and labelled field boxes — never a JSON dump. */

export type StudioStep = {
  step: number; op: string; name: string; reads: string[]; params: Record<string, unknown>
}

const STUDIO_OP_LABEL: Record<string, string> = {
  stack: "Stack", merge: "Merge", filter: "Filter", select: "Keep columns",
  drop: "Drop columns", rename: "Rename", constant: "Constants", derive: "Conditional derive",
  compute: "Compute", aggregate: "Summarise", date_extreme: "Date extreme", sort: "Sort",
  dedup: "De-duplicate", split: "Split", transpose_long: "Transpose to rows",
  transpose_findings: "Transpose findings", transpose_rule: "Transpose by rule",
  uppercase: "Uppercase",
}

function condSentence(c: Cond): string {
  if (!c?.column) return ""
  const o = c.operator ?? "=="
  if (!c.value && (o === "!=" || o === "==") )
    return `${c.column} ${o === "!=" ? "is not blank" : "is blank"}`
  const op = OP_SHORT[o] ?? o
  const v = c.value && !COND_NO_VALUE.has(o) ? ` ${c.value}` : ""
  return `${c.column} ${op}${v}`
}

function FieldBox({ label, tone, children }: { label: string; tone: ChipTone; children: ReactNode }) {
  return (
    <div className={cn("rounded-lg border p-2", FIELD_TONE_BOX[tone])}>
      <div className={cn("mb-1.5 text-[10px] font-semibold uppercase tracking-wide", FIELD_TONE_TEXT[tone])}>
        {label}
      </div>
      <div className="flex flex-wrap items-center gap-1.5 text-[12px]">{children}</div>
    </div>
  )
}

function ColumnChips({ cols, max = 24 }: { cols: string[]; max?: number }) {
  return (
    <>
      {cols.slice(0, max).map((c, i) => <Mono key={`${c}-${i}`}>{c}</Mono>)}
      {cols.length > max && <span className="text-[11px] text-muted-foreground">+{cols.length - max} more</span>}
    </>
  )
}

/** The step's params as the labelled boxes the editor would show — read-only. */
function StudioParamBoxes({ op, params }: { op: string; params: Record<string, unknown> }) {
  const p = params ?? {}
  const str = (k: string) => (typeof p[k] === "string" ? (p[k] as string) : "")
  const list = (k: string) => (Array.isArray(p[k]) ? (p[k] as unknown[]) : [])
  const names = (k: string) => list(k).filter((x): x is string => typeof x === "string" && !!x)
  const boxes: ReactNode[] = []
  const used = new Set<string>()
  const take = (...keys: string[]) => keys.forEach((k) => used.add(k))

  if (str("dataset")) { take("dataset"); boxes.push(
    <FieldBox key="ds" label="Dataset" tone="blue"><Mono>{str("dataset")}</Mono></FieldBox>) }
  if (names("datasets").length) { take("datasets"); boxes.push(
    <FieldBox key="dss" label="Datasets to append" tone="blue"><ColumnChips cols={names("datasets")} /></FieldBox>) }

  const inputs = list("inputs") as Array<{ dataset?: string; columns?: string[]; key?: string }>
  if (inputs.length) { take("inputs", "on", "how"); boxes.push(
    <FieldBox key="join" label="Datasets to join" tone="green">
      <div className="w-full space-y-1">
        {inputs.map((x, i) => (
          <div key={i} className="flex flex-wrap items-center gap-1.5">
            <Mono>{x.dataset ?? "?"}</Mono>
            <span className="text-[11px] text-muted-foreground">
              {x.columns?.length ? `keeps ${x.columns.length} column(s)` : "keeps every column"}
              {x.key ? <> · joins by its <Mono>{x.key}</Mono></> : null}
            </span>
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          <span className="text-[11px] text-muted-foreground">join on</span>
          <ColumnChips cols={names("on")} />
          {str("how") && <span className="text-[11px] text-muted-foreground">· keep records from: {str("how")}</span>}
        </div>
      </div>
    </FieldBox>) }

  const conds = list("conds") as Cond[]
  if (conds.length) { take("conds"); boxes.push(
    <FieldBox key="conds" label="Keep records where" tone="amber">
      <div className="w-full space-y-0.5">
        {conds.map((c, i) => <div key={i}>{i > 0 && <span className="mr-1 text-[11px] text-muted-foreground">and</span>}{condSentence(c)}</div>)}
      </div>
    </FieldBox>) }
  if (op === "filter" && str("expression")) { take("expression"); boxes.push(
    <FieldBox key="fx" label="Keep records where" tone="amber"><Mono>{str("expression")}</Mono></FieldBox>) }

  const targets = names("targets").length ? names("targets") : (str("target") ? [str("target")] : [])
  if (targets.length) { take("targets", "target"); boxes.push(
    <FieldBox key="tg" label="Column(s) to set" tone="fuchsia"><ColumnChips cols={targets} /></FieldBox>) }

  const rules = list("rules") as Array<{ conds?: Cond[]; value?: string; values?: Record<string, string> }>
  if (rules.length) { take("rules", "else_value", "else_values"); boxes.push(
    <FieldBox key="rules" label="If / else if" tone="fuchsia">
      <div className="w-full space-y-1">
        {rules.map((r, i) => (
          <div key={i} className="flex flex-wrap items-baseline gap-1.5">
            <span className="text-[11px] font-medium">{i === 0 ? "when" : "else when"}</span>
            <span>{(r.conds ?? []).map(condSentence).filter(Boolean).join(" and ") || "always"}</span>
            <span className="text-[11px] font-medium">→</span>
            <Mono>{r.value ?? (r.values ? Object.entries(r.values).map(([k, v]) => `${k}=${v}`).join(", ") : "")}</Mono>
          </div>
        ))}
        {(!!str("else_value") || p.else_values != null) && (
          <div className="flex flex-wrap items-baseline gap-1.5">
            <span className="text-[11px] font-medium">otherwise</span><span className="text-[11px] font-medium">→</span>
            <Mono>{str("else_value") || JSON.stringify(p.else_values)}</Mono>
          </div>
        )}
      </div>
    </FieldBox>) }

  const renames = list("renames") as Array<{ from?: string; to?: string }>
  if (renames.length) { take("renames"); boxes.push(
    <FieldBox key="rn" label="Rename" tone="violet">
      {renames.map((r, i) => (
        <span key={i} className="inline-flex items-center gap-1">
          <Mono>{r.from ?? "?"}</Mono><span className="text-[11px] text-muted-foreground">→</span><Mono>{r.to ?? "?"}</Mono>
        </span>
      ))}
    </FieldBox>) }

  if (op === "constant" && list("columns").length) { take("columns"); boxes.push(
    <FieldBox key="const" label="Columns to add" tone="violet">
      {(list("columns") as Array<{ column?: string; value?: string }>).map((c, i) => (
        <span key={i} className="inline-flex items-center gap-1">
          <Mono>{c.column ?? "?"}</Mono><span className="text-[11px] text-muted-foreground">=</span><Mono>{String(c.value ?? "")}</Mono>
        </span>
      ))}
    </FieldBox>) }
  else if (names("columns").length) { take("columns"); boxes.push(
    <FieldBox key="cols" label="Columns" tone="teal"><ColumnChips cols={names("columns")} /></FieldBox>) }

  if (op === "compute" && str("expression")) { take("expression", "out_col"); boxes.push(
    <FieldBox key="cx" label="Calculation" tone="blue">
      {str("out_col") && <><Mono>{str("out_col")}</Mono><span className="text-[11px] text-muted-foreground">=</span></>}
      <Mono>{str("expression")}</Mono>
    </FieldBox>) }

  if (names("id_vars").length) { take("id_vars"); boxes.push(
    <FieldBox key="idv" label={`Carry through · ${names("id_vars").length} column(s)`} tone="teal">
      <ColumnChips cols={names("id_vars")} max={10} />
    </FieldBox>) }
  if (names("value_vars").length) { take("value_vars"); boxes.push(
    <FieldBox key="vv" label="Columns to melt" tone="teal"><ColumnChips cols={names("value_vars")} /></FieldBox>) }
  const measures = list("measures") as Array<{ testcd?: string; value_col?: string; unit_col?: string }>
  if (measures.length) { take("measures"); boxes.push(
    <FieldBox key="ms" label="Measurements" tone="teal">
      <div className="w-full space-y-0.5">
        {measures.map((m, i) => (
          <div key={i} className="flex flex-wrap items-center gap-1.5">
            <Mono>{m.testcd ?? "?"}</Mono>
            <span className="text-[11px] text-muted-foreground">from</span><Mono>{m.value_col ?? "?"}</Mono>
            {m.unit_col && <><span className="text-[11px] text-muted-foreground">unit</span><Mono>{m.unit_col}</Mono></>}
          </div>
        ))}
      </div>
    </FieldBox>) }

  if (names("keys").length) { take("keys", "keep"); boxes.push(
    <FieldBox key="dd" label={`Keep the ${str("keep") || "first"} record per`} tone="red">
      <ColumnChips cols={names("keys")} /></FieldBox>) }
  if (names("group_by").length) { take("group_by", "func", "column", "out_col"); boxes.push(
    <FieldBox key="agg" label="Summarise" tone="violet">
      <span className="text-[11px] text-muted-foreground">{str("func") || "summarise"}</span>
      <Mono>{str("column") || "?"}</Mono>
      <span className="text-[11px] text-muted-foreground">per</span><ColumnChips cols={names("group_by")} />
      {str("out_col") && <><span className="text-[11px] text-muted-foreground">→</span><Mono>{str("out_col")}</Mono></>}
    </FieldBox>) }

  // whatever a studio step carries beyond the fields above, shown plainly — never dropped
  const rest = Object.entries(p).filter(([k, v]) =>
    !used.has(k) && v !== "" && v != null && !(Array.isArray(v) && !v.length))
  if (rest.length) boxes.push(
    <FieldBox key="rest" label="Also set" tone="slate">
      <div className="w-full space-y-0.5">
        {rest.map(([k, v]) => (
          <div key={k} className="flex flex-wrap items-baseline gap-1.5">
            <span className="text-[11px] text-muted-foreground">{k}</span>
            <Mono>{typeof v === "string" || typeof v === "number" || typeof v === "boolean"
              ? String(v)
              : Array.isArray(v) && v.every((x) => typeof x === "string") ? (v as string[]).join(", ")
              : JSON.stringify(v)}</Mono>
          </div>
        ))}
      </div>
    </FieldBox>)

  return <div className="grid grid-cols-1 gap-2 pt-2 sm:grid-cols-2">{boxes}</div>
}

/* ── editing a studio step ──────────────────────────────────────────────────
   The same pipeline the studio's Domain Build edits, edited here: Save writes the steps
   into the imported study with the studio's own save (same consolidation, same file), and
   the rebuild runs them with the studio's engine. The editor is shape-driven — whatever
   params a step carries become fields, so no studio op is ever locked out. */

export type StudioSaveStep = { op: string; name: string; params: Record<string, unknown> }

/** The params a step added HERE starts from — the studio's own shapes per op. */
const STUDIO_OP_TEMPLATE: Record<string, Record<string, unknown>> = {
  stack: { datasets: [""] },
  merge: { inputs: [{ dataset: "", columns: [] }, { dataset: "", columns: [] }], on: ["USUBJID"], how: "left" },
  filter: { dataset: "", expression: "" },
  rename: { dataset: "", renames: [{ from: "", to: "" }] },
  drop: { dataset: "", columns: [] },
  select: { dataset: "", columns: [] },
  constant: { dataset: "", columns: [{ column: "", value: "" }] },
  derive: { dataset: "", target: "", rules: [{ conds: [{ column: "", operator: "==", value: "" }], value: "" }], else_value: "" },
  compute: { dataset: "", target: "", expression: "" },
  uppercase: { dataset: "", columns: [] },
  aggregate: { dataset: "", group_by: [], func: "max", column: "", out_col: "" },
  transpose_findings: { dataset: "", id_vars: [], measures: [{ testcd: "", value_col: "", unit_col: "" }] },
}

/** What a new row of an object-list looks like, by param key (for lists that start empty). */
const ROW_SHAPE: Record<string, Record<string, unknown>> = {
  conds: { column: "", operator: "==", value: "" },
  rules: { conds: [{ column: "", operator: "==", value: "" }], value: "" },
  renames: { from: "", to: "" },
  inputs: { dataset: "", columns: [] },
  measures: { testcd: "", value_col: "", unit_col: "" },
  on_map: { left: "", right: "" },
  branches: { name: "", conds: [{ column: "", operator: "==", value: "" }] },
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v)
}

/** What a param is CALLED in the editor — the studio's words (IF / THEN / ELSE, join on, …),
 *  never the raw key. Context-sensitive: `value` inside a rule is the THEN, inside a
 *  constant column it is just the value. */
const STUDIO_KEY_LABEL: Record<string, string> = {
  dataset: "dataset", datasets: "datasets to append", inputs: "datasets to join",
  on: "join on", on_map: "join columns (left → right)", how: "keep records from",
  conds: "keep records WHERE", expression: "expression",
  target: "column to set", targets: "columns to set",
  rules: "IF / ELSE IF", else_value: "ELSE (otherwise)", else_values: "ELSE (otherwise)",
  renames: "rename", columns: "columns", keys: "keys", keep: "keep the",
  group_by: "per", func: "function", out_col: "output column", column: "column",
  id_vars: "carry through", value_vars: "columns to melt", measures: "measurements",
  branches: "branches",
  "rules.conds": "WHEN", "rules.value": "THEN set to", "rules.values": "THEN set",
  "conds.column": "column", "conds.operator": "", "conds.value": "value",
  "renames.from": "from", "renames.to": "to",
  "inputs.dataset": "dataset", "inputs.columns": "keep columns", "inputs.key": "join by its",
  "measures.testcd": "test code", "measures.value_col": "value from", "measures.unit_col": "unit from",
  "on_map.left": "left", "on_map.right": "right",
  "columns.column": "column", "columns.value": "value",
}

const studioKeyLabel = (parent: string, k: string): string => {
  const hit = STUDIO_KEY_LABEL[`${parent}.${k}`] ?? STUDIO_KEY_LABEL[k]
  return hit !== undefined ? hit : k.replace(/_/g, " ")
}

/** The operators a condition can use, worded as the step summaries word them. */
const STUDIO_COND_OPS: Array<[string, string]> = [
  ["==", "="], ["!=", "≠"], [">", ">"], [">=", "≥"], ["<", "<"], ["<=", "≤"],
  ["contains", "contains"], ["startswith", "starts with"], ["endswith", "ends with"],
  ["in", "in list"], ["notin", "not in list"], ["missing", "is blank"], ["notmissing", "is not blank"],
]

/** One editable param — the value's shape decides the control, so every studio op is
 *  editable without an op-specific form, and nothing a step carries is hidden. */
function StudioValueField({ k, value, onChange, parent = "" }: {
  k: string; value: unknown; onChange: (v: unknown) => void; parent?: string
}) {
  if (parent === "conds" && k === "operator") return (
    <Select value={typeof value === "string" && value ? value : "=="} onValueChange={onChange}>
      <SelectTrigger className="h-7 w-32 text-xs"><SelectValue /></SelectTrigger>
      <SelectContent>
        {STUDIO_COND_OPS.map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}
      </SelectContent>
    </Select>)
  if (typeof value === "boolean") return (
    <Select value={String(value)} onValueChange={(v) => onChange(v === "true")}>
      <SelectTrigger className="h-7 w-20 text-xs"><SelectValue /></SelectTrigger>
      <SelectContent><SelectItem value="true">yes</SelectItem><SelectItem value="false">no</SelectItem></SelectContent>
    </Select>)
  if (typeof value === "number") return (
    <Input className="h-7 w-24 font-mono text-xs" value={String(value)}
           onChange={(e) => onChange(e.target.value === "" ? 0 : Number(e.target.value) || 0)} />)
  if (Array.isArray(value)) {
    const objRows = value.some(isPlainObject) || (!value.length && k in ROW_SHAPE)
    if (!objRows) return (
      <Input className="h-7 min-w-44 flex-1 font-mono text-xs" placeholder="comma-separated"
             value={(value as unknown[]).map((x) => String(x)).join(", ")}
             onChange={(e) => onChange(e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} />)
    const rows = value.filter(isPlainObject)
    const shape = ROW_SHAPE[k]
      ?? (rows[0] ? Object.fromEntries(Object.keys(rows[0]).map((x) => [x, Array.isArray(rows[0][x]) ? [] : ""])) : { value: "" })
    return (
      <div className="w-full space-y-1">
        {rows.map((row, i) => (
          <div key={i} className="flex flex-wrap items-start gap-1.5 rounded-md border bg-surface/60 p-1.5">
            {k === "rules" && (
              <span className="mt-1 shrink-0 rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] font-bold text-primary">
                {i === 0 ? "IF" : "ELSE IF"}</span>)}
            <div className="min-w-0 flex-1">
              <StudioObjectFields obj={row} parent={k}
                                  onChange={(o) => onChange(rows.map((r, j) => (j === i ? o : r)))} />
            </div>
            <Button variant="ghost" size="icon" className="h-6 w-6 shrink-0" title="Remove this row"
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}>
              <Trash2 className="h-3 w-3" /></Button>
          </div>
        ))}
        <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]"
                onClick={() => onChange([...rows, structuredClone(shape)])}>
          <Plus className="mr-1 h-3 w-3" />
          add {k === "rules" ? (rows.length ? "an ELSE IF" : "the IF") : k === "conds" ? "a condition" : "one"}
        </Button>
      </div>)
  }
  if (isPlainObject(value)) return <StudioObjectFields obj={value} parent={k} onChange={onChange} />
  return (
    <Input className="h-7 min-w-44 flex-1 font-mono text-xs" value={value == null ? "" : String(value)}
           onChange={(e) => onChange(e.target.value)} />)
}

export function StudioObjectFields({ obj, onChange, parent = "" }: {
  obj: Record<string, unknown>; onChange: (o: Record<string, unknown>) => void; parent?: string
}) {
  return (
    <div className="flex w-full flex-wrap items-start gap-x-4 gap-y-1.5">
      {Object.entries(obj).map(([k, v]) => {
        const label = studioKeyLabel(parent, k)
        return (
          <div key={k} className={cn("flex items-start gap-1.5",
            Array.isArray(v) && (v.some(isPlainObject) || (!v.length && k in ROW_SHAPE)) ? "w-full" : "min-w-0")}>
            {label !== "" && (
              <span className="mt-1.5 shrink-0 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                {label}</span>)}
            <StudioValueField k={k} parent={parent} value={v} onChange={(nv) => onChange({ ...obj, [k]: nv })} />
          </div>
        )
      })}
    </div>
  )
}

export function StudioPrepList({ steps, domain, onDone }: {
  steps: StudioStep[]; domain: string; onDone: () => void
}) {
  const [edit, setEdit] = useState<StudioSaveStep[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState("")
  const startEdit = () => {
    setErr("")
    setEdit(steps.map((st) => structuredClone({ op: st.op, name: st.name, params: st.params ?? {} })))
  }
  const save = async () => {
    if (!edit) return
    setBusy(true); setErr("")
    try {
      await api.setStudioPrep(domain, edit)
      await api.rebuild(domain)
      setEdit(null)
      onDone()
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  const change = (i: number, st: StudioSaveStep) => setEdit((e) => e!.map((x, j) => (j === i ? st : x)))
  const move = (i: number, d: number) => setEdit((e) => {
    const n = [...e!]; const j = i + d
    if (j < 0 || j >= n.length) return e!
    ;[n[i], n[j]] = [n[j], n[i]]
    return n
  })

  if (edit) {
    return (
      <Panel title={`Prepare the data — editing ${edit.length} step${edit.length === 1 ? "" : "s"}`}
             description="The same pipeline SDTM Studio's Domain Build edits. Save writes it into this study with the studio's own save and rebuilds the domain with the studio's engine — Pull from the studio (Study setup) would replace it with the studio's latest."
             actions={<div className="flex items-center gap-2">
               <Button size="sm" variant="ghost" disabled={busy} onClick={() => { setEdit(null); setErr("") }}>Cancel</Button>
               <Button size="sm" disabled={busy} onClick={() => void save()}>
                 {busy ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />Saving…</> : "Save & rebuild"}
               </Button>
             </div>}>
        {err && <div className="mb-3"><Callout tone="bad" title="Could not save">{err}</Callout></div>}
        <div className="space-y-1.5">
          {edit.map((st, i) => {
            const meta = OP_META[st.op] ?? DEFAULT_META
            const OpIcon = meta.icon
            return (
              <div key={i} className={cn("rounded-lg border border-l-4", ACCENT_BORDER[meta.tone])}>
                <div className={cn("flex flex-wrap items-center gap-2 rounded-t-lg px-2 py-1.5", HEAD_TINT[meta.tone])}>
                  <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-muted text-[10px] font-semibold">{i + 1}</span>
                  <Chip tone={meta.tone} className="shrink-0"><OpIcon className="h-3 w-3" /></Chip>
                  <span className="shrink-0 text-[13px] font-medium">{STUDIO_OP_LABEL[st.op] ?? st.op}</span>
                  <Input className="h-7 w-44 font-mono text-xs" value={st.name} placeholder="output name"
                         onChange={(e) => change(i, { ...st, name: e.target.value })} />
                  <span className={cn("min-w-0 flex-1 truncate text-[12px] italic", FIELD_TONE_TEXT[meta.tone])}>
                    {stepSummary({ op: st.op, name: st.name, params: st.params })}
                  </span>
                  <span className="flex shrink-0 items-center">
                    <Button variant="ghost" size="icon" className="h-6 w-6" title="Move up" disabled={i === 0}
                            onClick={() => move(i, -1)}><ArrowUp className="h-3 w-3" /></Button>
                    <Button variant="ghost" size="icon" className="h-6 w-6" title="Move down" disabled={i === edit.length - 1}
                            onClick={() => move(i, 1)}><ArrowDown className="h-3 w-3" /></Button>
                    <Button variant="ghost" size="icon" className="h-6 w-6" title="Duplicate this step"
                            onClick={() => setEdit((e) => [...e!.slice(0, i + 1), structuredClone(st), ...e!.slice(i + 1)])}>
                      <CopyPlus className="h-3 w-3" /></Button>
                    <Button variant="ghost" size="icon" className="h-6 w-6 text-destructive" title="Remove this step"
                            onClick={() => setEdit((e) => e!.filter((_, j) => j !== i))}>
                      <Trash2 className="h-3 w-3" /></Button>
                  </span>
                </div>
                <div className="px-3 py-2.5">
                  <StudioObjectFields obj={st.params} onChange={(p) => change(i, { ...st, params: p })} />
                </div>
              </div>
            )
          })}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-dashed px-2.5 py-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Add a step</span>
          <Select value="" onValueChange={(op) => setEdit((e) => [...e!,
            { op, name: "", params: structuredClone(STUDIO_OP_TEMPLATE[op] ?? { dataset: "" }) }])}>
            <SelectTrigger className="h-7 w-56 text-xs"><SelectValue placeholder="choose the operation…" /></SelectTrigger>
            <SelectContent>
              {Object.keys(STUDIO_OP_TEMPLATE).map((op) => {
                const m = OP_META[op] ?? DEFAULT_META; const I = m.icon
                return <SelectItem key={op} value={op}>
                  <span className="flex items-center gap-1.5"><I className="h-3 w-3" />{STUDIO_OP_LABEL[op] ?? op}</span>
                </SelectItem>
              })}
            </SelectContent>
          </Select>
        </div>
      </Panel>
    )
  }

  if (!steps.length) {
    return (
      <div className="space-y-3">
        <Callout title="No prep steps">SDTM Studio has no saved prep for this domain — it builds straight
          from its raw forms. Prep can be designed right here, the studio's way.</Callout>
        <Button size="sm" variant="outline" onClick={startEdit}>
          <Plus className="mr-1.5 h-3.5 w-3.5" />Design the prep here</Button>
      </div>
    )
  }
  const asStep = (s: StudioStep): Step => ({ op: s.op, name: s.name, params: s.params ?? {} })
  return (
    <Panel title={`Prepare the data — ${steps.length} step${steps.length > 1 ? "s" : ""}, shared with SDTM Studio`}
           description="The same pipeline the studio's Domain Build holds. Edit it here (the save is the studio's own) or in the studio — Pull from the studio on Study setup brings its latest over."
           actions={<Button size="sm" variant="outline" onClick={startEdit}>
             <PenLine className="mr-1.5 h-3.5 w-3.5" />Edit the steps</Button>}>
      {steps.length >= 4 && (
        <div className="mb-3 flex flex-wrap items-center gap-1.5 rounded-lg border border-dashed bg-muted/30 px-2.5 py-2">
          <span className="mr-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Map</span>
          {steps.map((s) => {
            const m = OP_META[s.op] ?? DEFAULT_META
            const Icon = m.icon
            return (
              <button key={s.step} type="button" title={stepSummary(asStep(s))}
                      onClick={() => {
                        const el = document.getElementById(`studio-step-${s.step}`) as HTMLDetailsElement | null
                        if (el) { el.open = true; el.scrollIntoView({ behavior: "smooth", block: "center" }) }
                      }}>
                <Chip tone={m.tone} className="cursor-pointer transition hover:scale-105 hover:shadow-sm">
                  <span className="font-semibold">{s.step}</span>
                  <Icon className="h-3 w-3" />
                  <span className="font-mono">{s.name || `step${s.step}`}</span>
                </Chip>
              </button>
            )
          })}
        </div>
      )}
      <div className="space-y-1.5">
        {steps.map((s) => {
          const meta = OP_META[s.op] ?? DEFAULT_META
          const OpIcon = meta.icon
          return (
            <details key={s.step} id={`studio-step-${s.step}`}
                     className={cn("rounded-lg border border-l-4", ACCENT_BORDER[meta.tone])}>
              <summary className={cn("flex cursor-pointer flex-wrap items-center gap-2 rounded-lg px-2 py-1.5",
                                     HEAD_TINT[meta.tone])}>
                <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-muted text-[10px] font-semibold">{s.step}</span>
                <Chip tone={meta.tone} className="shrink-0"><OpIcon className="h-3 w-3" /></Chip>
                <span className="shrink-0 text-[13px] font-medium">{STUDIO_OP_LABEL[s.op] ?? s.op}</span>
                <Mono>{s.name || "—"}</Mono>
                <span className={cn("min-w-0 flex-1 truncate text-[12px] italic", FIELD_TONE_TEXT[meta.tone])}
                      title={stepSummary(asStep(s))}>
                  {stepSummary(asStep(s))}
                </span>
                {s.reads.length > 0 && (
                  <span className="shrink-0 text-[11px] text-muted-foreground">reads {s.reads.slice(0, 3).join(", ")}{s.reads.length > 3 ? "\u2026" : ""}</span>
                )}
              </summary>
              <div className="px-3 pb-3">
                <StudioParamBoxes op={s.op} params={s.params ?? {}} />
              </div>
            </details>
          )
        })}
      </div>
    </Panel>
  )
}
