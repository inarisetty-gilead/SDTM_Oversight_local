import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Eye, EyeOff, Loader2, Maximize2, Plus, Trash2 } from "lucide-react"
import { api } from "@/api"
import type { DataPage, DomainDetail } from "@/api"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { FN_LABELS } from "./fnLabels"
import { RecordTable } from "./DataView"
import { DataGrid, Mono } from "./grid"
import { Callout } from "./shell"

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

type Field = { k: string; t: string; ph?: string; label?: string
               opts?: [string, string][]; funcs?: string[] }

const COMPUTE_FUNCS: [string, string][] = [
  ["complete_date", "Complete a partial date — fill missing month/day with 01"],
  ["year", "Take the year"],
  ...Object.entries(FN_LABELS),
]

const FIELDS: Record<string, Field[]> = {
  stack: [{ k: "datasets", t: "dslist", label: "Datasets to append" }],
  merge: [{ k: "inputs", t: "mergeinputs", label: "Datasets to join" }, { k: "on", t: "joinkeys", label: "Join on" },
          { k: "how", t: "choice:left,inner,outer,right", label: "Keep records from" }],
  filter: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "conds", t: "conds", label: "Keep records where" }],
  select: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "collist", label: "Columns" }],
  drop: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "collist", label: "Columns" }],
  rename: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "renames", t: "renames", label: "Rename" }],
  derive: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "target", t: "text", ph: "new column", label: "Column to set" },
           { k: "else_value", t: "text", ph: "value when no rule matches", label: "Otherwise" }, { k: "rules", t: "rules", label: "Rules" }],
  compute: [{ k: "dataset", t: "ds", label: "Dataset" },
            { k: "func", t: "choicel", label: "Calculation", opts: COMPUTE_FUNCS },
            { k: "columns", t: "colseq", label: "From column(s)" },
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
  transpose_findings: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "id_vars", t: "collist", label: "Carry through" }, { k: "measures", t: "measures", label: "Measurements" },
                       { k: "testcd_col", t: "text", label: "Test code column" }, { k: "test_col", t: "text", label: "Test name column" },
                       { k: "orres_col", t: "text", label: "Result column" }, { k: "orresu_col", t: "text", label: "Unit column" }],
}


type MergeInput = { dataset?: string; columns?: string[] }

/** Merge inputs as pickers: one row per dataset, with the columns to keep. */
function MergeInputs({ rows, datasets, domain, onChange }: {
  rows: MergeInput[]; datasets: string[]; domain: string
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

  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => {
        const available = cols[r.dataset ?? ""] ?? []
        const chosen = r.columns ?? []
        return (
          <div key={i} className="rounded-md border p-2">
            <div className="flex items-center gap-2">
              <span className="w-5 text-[11px] text-muted-foreground">{i + 1}</span>
              <Select value={r.dataset || "__none"}
                      onValueChange={(v) => { set(i, { dataset: v === "__none" ? "" : v, columns: [] })
                                              void loadCols(v) }}>
                <SelectTrigger className="h-8 w-52 text-xs"><SelectValue placeholder="dataset" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none" className="text-xs">—</SelectItem>
                  {datasets.map((d) => <SelectItem key={d} value={d} className="text-xs">{d}</SelectItem>)}
                </SelectContent>
              </Select>
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

/** Join keys, offered from the columns the chosen datasets actually share. */
function JoinKeys({ value, inputs, domain, onChange }: {
  value: string[]; inputs: MergeInput[]; domain: string; onChange: (keys: string[]) => void
}) {
  const [shared, setShared] = useState<string[]>([])
  const names = inputs.map((i) => i.dataset).filter(Boolean).join("|")

  useEffect(() => { void (async () => {
    const sets: string[][] = []
    for (const i of inputs) {
      if (!i.dataset) continue
      try { sets.push((await api.columns(domain, i.dataset)).columns) } catch { /* skip */ }
    }
    setShared(sets.length < 2 ? []
      : sets.reduce((a, b) => a.filter((c) => b.includes(c))).sort())
  })() }, [names, domain, inputs])

  if (!shared.length) {
    return (
      <p className="text-[11px] text-muted-foreground">
        Choose two or more datasets and their shared columns will be offered here. Left empty,
        the merge joins on the subject key they have in common.
      </p>
    )
  }
  return (
    <div className="max-h-32 overflow-auto rounded-md border p-2">
      {shared.map((c) => (
        <label key={c} className="flex items-center gap-2 text-[11px]">
          <input type="checkbox" checked={value.includes(c)}
                 onChange={(e) => onChange(e.target.checked
                   ? [...value, c] : value.filter((x) => x !== c))} />{c}
        </label>
      ))}
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

function ColSelect({ value, columns, onChange, placeholder = "column", width = "w-44" }: {
  value?: string; columns: string[]; onChange: (v: string) => void
  placeholder?: string; width?: string
}) {
  // a hand-set or stale name stays selectable so an existing pipeline never renders blank
  const all = value && !columns.includes(value) ? [value, ...columns] : columns
  return (
    <Select value={value || "__none"} onValueChange={(v) => onChange(v === "__none" ? "" : v)}>
      <SelectTrigger className={`h-8 ${width} text-xs`}><SelectValue placeholder={placeholder} /></SelectTrigger>
      <SelectContent className="max-h-64">
        <SelectItem value="__none" className="text-xs">—</SelectItem>
        {all.map((c) => <SelectItem key={c} value={c} className="text-xs">{c}</SelectItem>)}
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

type Rule = { conds?: Cond[]; value?: string }

/** Derive rules: IF conditions THEN set the column to a value; rules run in order. */
function RulesEditor({ rules, columns, onChange, domain, dataset }: {
  rules: Rule[]; columns: string[]; onChange: (r: Rule[]) => void
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
          <div className="flex items-center gap-1.5">
            <span className="w-10 text-right text-[11px] text-muted-foreground">then</span>
            <span className="text-[11px] text-muted-foreground">set it to</span>
            <Input className="h-8 w-56 text-xs" placeholder="value" value={r.value ?? ""}
                   onChange={(e) => set(i, { value: e.target.value })} />
          </div>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...rules, { conds: [{ operator: "==" }], value: "" }])}>
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

/** One dataset + its date column; the column list follows the chosen dataset. */
function DateSourceRow({ row, datasets, domain, onChange, onRemove }: {
  row: { dataset?: string; date_col?: string }; datasets: string[]; domain: string
  onChange: (r: { dataset?: string; date_col?: string }) => void; onRemove: () => void
}) {
  const columns = useColumns(domain, row.dataset)
  return (
    <div className="flex items-center gap-1.5">
      <ColSelect value={row.dataset} columns={datasets} placeholder="dataset"
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

type Measure = { testcd?: string; value_col?: string; unit_col?: string }

function MeasuresEditor({ rows, columns, onChange }: {
  rows: Measure[]; columns: string[]; onChange: (r: Measure[]) => void
}) {
  const set = (i: number, patch: Measure) =>
    onChange(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex flex-wrap items-center gap-1.5">
          <Input className="h-8 w-32 font-mono text-xs" placeholder="TESTCD, e.g. SYSBP"
                 value={r.testcd ?? ""} onChange={(e) => set(i, { testcd: e.target.value })} />
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

export function PipelineEditor({ detail, onDone }: { detail: DomainDetail; onDone: () => void }) {
  const [steps, setSteps] = useState<Step[]>(() =>
    JSON.parse(JSON.stringify(detail.pipeline_draft ?? detail.pipeline ?? [])))
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
  // prep studio (👁 previews on/off, steps collapse to their one-line summary)
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({})
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
    if (!firstRun.current) { touched.current = true; setDirty(true) }   // any change after mount pins this editor
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
    const t = window.setTimeout(() => { void run(steps, true) }, firstRun.current ? 0 : 200)
    firstRun.current = false
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(steps)])

  const run = async (list: Step[], quiet = false) => {
    if (!quiet) setBusy(true)
    setLive(true)
    try {
      const r = await api.previewPipeline(detail.domain, list)
      if (!r.ok) { setReports([]); setOuts({}); setError(r.error ?? "failed"); setSaveFailed(true); return }
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

  const upd = (i: number, patch: Partial<Step>) =>
    setSteps((s) => s.map((st, k) => (k === i ? { ...st, ...patch } : st)))
  const setParam = (i: number, k: string, v: unknown) =>
    setSteps((s) => s.map((st, idx) => {
      if (idx !== i) return st
      const p = { ...st.params }
      if (v === "" || v === undefined || (Array.isArray(v) && !v.length)) delete p[k]
      else p[k] = v
      return { ...st, params: p }
    }))

  // earlier steps' outputs first, then the raw datasets — deduplicated: once a step's
  // preview has run, its output is ALSO in the store (detail.datasets), and a name
  // listed twice renders as "prep1 prep1" in every picker
  const datasetsFor = (i: number) =>
    Array.from(new Set([...steps.slice(0, i).map((s, k) => s.name || `prep${k + 1}`),
                        ...detail.datasets]))

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError("")
    try { await fn() } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <div className="space-y-3 rounded-lg border">
      <div className="flex flex-wrap items-center gap-2 border-b bg-muted/50 px-3 py-2">
        <h4 className="flex-1 text-sm font-medium">
          Prepare the data{steps.length ? ` — ${steps.length} step${steps.length > 1 ? "s" : ""}` : ""}
        </h4>
        <Button size="sm" variant="outline" className="h-7 text-xs"
                onClick={() => setSteps((s) => [...s, { op: "stack", name: `prep${s.length + 1}`, params: {} }])}>
          <Plus className="mr-1 h-3 w-3" />Add step
        </Button>
        {detail.prep && (
          <Button size="sm" variant="outline" className="h-7 text-xs" disabled={busy}
                  onClick={() => void act(async () => setSteps((await api.pipelineFromAuto(detail.domain)).steps as Step[]))}>
            Start from the detected step
          </Button>
        )}
        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={togglePrev}
                title={showPrev ? "hide the per-step preview tables (steps still run)"
                                : "show a preview table under every step"}>
          {showPrev ? <Eye className="mr-1 h-3.5 w-3.5" /> : <EyeOff className="mr-1 h-3.5 w-3.5" />}
          previews {showPrev ? "on" : "off"}
        </Button>
        <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          {live ? <><Loader2 className="h-3 w-3 animate-spin" />saving…</>
                : saveFailed ? <span className="text-destructive">draft not saved — will retry</span>
                : dirty ? <>unsaved…</>
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
        <Button size="sm" className="h-7 text-xs" disabled={busy}
                onClick={() => void act(async () => {
                  await api.setPipeline(detail.domain, steps, recordsFrom)
                  await api.rebuild(detail.domain); onDone()
                })}>Apply &amp; rebuild</Button>
        {steps.length > 0 && (
          <Button size="sm" variant="ghost" className="h-7 text-xs" disabled={busy}
                  onClick={() => void act(async () => {
                    await api.setPipeline(detail.domain, [])
                    await api.rebuild(detail.domain); onDone()
                  })}>Remove all</Button>
        )}
      </div>

      {!steps.length && (
        <p className="px-3 pb-3 text-xs text-muted-foreground">
          No preparation steps — the build uses the detected record source as it is. Add a step to
          stack, merge, filter, reshape or summarise the raw data first.
        </p>
      )}

      {steps.map((st, i) => {
        const rep = reports.find((r) => r.step === i + 1)
        const isMin = !!collapsed[i]
        return (
          <div key={i} className="space-y-3 border-b px-3 pb-3 last:border-b-0">
            <div className="flex flex-wrap items-center gap-2">
              <Button size="icon" variant="ghost" className="h-7 w-7"
                      title={isMin ? "maximise this step" : "minimise this step"}
                      onClick={() => setCollapsed((c) => ({ ...c, [i]: !c[i] }))}>
                {isMin ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
              </Button>
              <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-muted text-[10px] font-semibold">{i + 1}</span>
              <Select value={st.op} onValueChange={(v) => upd(i, { op: v, params: {} })}>
                <SelectTrigger className="h-8 w-72 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>{ops.map((o) =>
                  <SelectItem key={o.id} value={o.id} className="text-xs">{o.label}</SelectItem>)}</SelectContent>
              </Select>
              <Input className="h-8 w-40 font-mono text-xs" value={st.name} placeholder="output name"
                     onChange={(e) => upd(i, { name: e.target.value })} />
              <div className="ml-auto flex gap-1">
                <Button size="icon" variant="ghost" className="h-7 w-7" disabled={i === 0}
                        onClick={() => setSteps((s) => { const n = [...s]; [n[i - 1], n[i]] = [n[i], n[i - 1]]; return n })}>
                  <ArrowUp className="h-3.5 w-3.5" /></Button>
                <Button size="icon" variant="ghost" className="h-7 w-7" disabled={i === steps.length - 1}
                        onClick={() => setSteps((s) => { const n = [...s]; [n[i + 1], n[i]] = [n[i], n[i + 1]]; return n })}>
                  <ArrowDown className="h-3.5 w-3.5" /></Button>
                <Button size="icon" variant="ghost" className="h-7 w-7"
                        onClick={() => setSteps((s) => s.filter((_, k) => k !== i))}>
                  <Trash2 className="h-3.5 w-3.5" /></Button>
              </div>
            </div>

            {!isMin && <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {(FIELDS[st.op] ?? []).map((f) => {
                // a parameter appears only for the function it belongs to
                if (f.funcs && !f.funcs.includes(st.params.func as string)) return null
                const v = st.params[f.k]
                const wide = ["mergeinputs", "joinkeys", "conds", "rules", "branches",
                              "renames", "datesources", "measures"].includes(f.t)
                let control
                if (f.t === "ds") {
                  control = (
                    <Select value={(v as string) || "__none"}
                            onValueChange={(x) => setParam(i, f.k, x === "__none" ? "" : x)}>
                      <SelectTrigger className="h-8 text-xs"><SelectValue placeholder="—" /></SelectTrigger>
                      <SelectContent><SelectItem value="__none" className="text-xs">—</SelectItem>
                        {datasetsFor(i).map((d) => <SelectItem key={d} value={d} className="text-xs">{d}</SelectItem>)}
                      </SelectContent></Select>)
                } else if (f.t === "dslist") {
                  const chosen = (v as string[]) ?? []
                  control = (
                    <div className="max-h-28 space-y-1 overflow-auto rounded-md border p-2">
                      {datasetsFor(i).map((d) => (
                        <label key={d} className="flex items-center gap-2 text-xs">
                          <input type="checkbox" checked={chosen.includes(d)}
                                 onChange={(e) => setParam(i, f.k, e.target.checked
                                   ? [...chosen, d] : chosen.filter((x) => x !== d))} />{d}
                        </label>))}
                    </div>)
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
                                         domain={detail.domain}
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
                                                 domain={detail.domain} dataset={st.params.dataset as string}
                                                 onChange={(r) => setParam(i, f.k, r)} />}
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
                  control = <Input className="h-8 text-xs" placeholder={f.ph ?? "comma separated"}
                                   value={Array.isArray(v) ? v.join(", ") : ((v as string) ?? "")}
                                   onChange={(e) => setParam(i, f.k, e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} />
                } else if (f.t === "collist") {
                  // a typed column name that doesn't exist in the dataset fails silently at
                  // build time (e.g. "needs at least one grouping column" for a key that just
                  // never matched) — offering only the columns that are actually there rules
                  // that mistake out entirely
                  control = (
                    <StepColumns domain={detail.domain} dataset={st.params.dataset as string}>
                      {(columns) => {
                        const chosen = (v as string[]) ?? []
                        return (
                          <div className="max-h-28 space-y-1 overflow-auto rounded-md border p-2">
                            {columns.map((c) => (
                              <label key={c} className="flex items-center gap-2 text-xs">
                                <input type="checkbox" checked={chosen.includes(c)}
                                       onChange={(e) => setParam(i, f.k, e.target.checked
                                         ? [...chosen, c] : chosen.filter((x) => x !== c))} />{c}
                              </label>))}
                            {!columns.length && <p className="text-[11px] text-muted-foreground">no columns</p>}
                          </div>
                        )
                      }}
                    </StepColumns>)
                } else {
                  control = <Input className="h-8 text-xs" placeholder={f.ph ?? ""} value={(v as string) ?? ""}
                                   onChange={(e) => setParam(i, f.k, e.target.value)} />
                }
                return (
                  <div key={f.k} className={wide ? "col-span-full space-y-1" : "space-y-1"}>
                    <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">
                      {f.label ?? f.k}</Label>
                    {control}
                  </div>)
              })}
            </div>}

            {rep && (rep.ok
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
        )
      })}

      {error && <div className="px-3 pb-3"><Callout tone="bad">{error}</Callout></div>}
      {fullView && <FullDataDialog dataset={fullView} onClose={() => setFullView("")} />}
    </div>
  )
}
