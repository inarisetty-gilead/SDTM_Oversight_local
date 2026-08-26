import { useCallback, useEffect, useRef, useState } from "react"
import { ArrowDown, ArrowUp, Loader2, Plus, Trash2 } from "lucide-react"
import { api } from "@/api"
import type { DomainDetail } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { DataGrid, Mono } from "./grid"
import { Callout } from "./shell"

type Step = { op: string; name: string; params: Record<string, unknown> }
type Report = { step: number; name: string; op: string; ok: boolean; rows?: number; columns?: string[]; error?: string; extra_outputs?: string[] }

const FIELDS: Record<string, Array<{ k: string; t: string; ph?: string; label?: string }>> = {
  stack: [{ k: "datasets", t: "dslist", label: "Datasets to append" }],
  merge: [{ k: "inputs", t: "mergeinputs", label: "Datasets to join" }, { k: "on", t: "joinkeys", label: "Join on" },
          { k: "how", t: "choice:left,inner,outer,right", label: "Keep records from" }],
  filter: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "conds", t: "json", label: "Keep records where" }],
  select: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "list", label: "Columns" }],
  drop: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "list", label: "Columns" }],
  rename: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "renames", t: "json", label: "Rename" }],
  derive: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "target", t: "text", ph: "new column", label: "Column to set" },
           { k: "else_value", t: "text", ph: "value when no rule matches", label: "Otherwise" }, { k: "rules", t: "json", label: "Rules" }],
  aggregate: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "group_by", t: "list", label: "Group by" }, { k: "column", t: "text", label: "Column to summarise" },
              { k: "func", t: "choice:min,max,first,last,count,sum,mean", label: "Summarise with" }, { k: "out_col", t: "text", label: "Result column" }],
  date_extreme: [{ k: "sources", t: "json", label: "Datasets and date columns" }, { k: "group_by", t: "list", ph: "USUBJID", label: "Per" },
                 { k: "func", t: "choice:min,max", label: "Take the" }, { k: "out_col", t: "text", label: "Result column" }],
  sort: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "columns", t: "list", label: "Columns" }, { k: "directions", t: "list", ph: "asc, desc", label: "Direction" }],
  dedup: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "keys", t: "list", label: "Group by" }, { k: "keep", t: "choice:first,last", label: "Keep" }],
  split: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "branches", t: "json", label: "Branches" }, { k: "other_name", t: "text", label: "Name for the rest" }],
  transpose_long: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "id_vars", t: "list", label: "Carry through" }, { k: "value_vars", t: "list", label: "Columns to melt" },
                   { k: "var_name", t: "text", ph: "TESTCD", label: "Name column" }, { k: "value_name", t: "text", ph: "ORRES", label: "Value column" }],
  transpose_findings: [{ k: "dataset", t: "ds", label: "Dataset" }, { k: "id_vars", t: "list", label: "Carry through" }, { k: "measures", t: "json", label: "Measurements" },
                       { k: "testcd_col", t: "text", label: "Test code column" }, { k: "test_col", t: "text", label: "Test name column" },
                       { k: "orres_col", t: "text", label: "Result column" }, { k: "orresu_col", t: "text", label: "Unit column" }],
}

const HINTS: Record<string, string> = {
  conds: '[{"column": "DSCAT", "operator": "==", "value": "PROTOCOL MILESTONE"}]',
  rules: '[{"conds": [{"column": "DSCAT", "operator": "==", "value": "X"}], "value": "MILESTONE"}]',
  branches: '[{"name": "milestones", "conds": [{"column": "DSCAT", "operator": "==", "value": "X"}]}]',
  renames: '[{"from": "DSSTDAT", "to": "DSSTDTC"}]',
  inputs: '[{"dataset": "ae"}, {"dataset": "dm", "columns": ["SEXCD"]}]',
  sources: '[{"dataset": "ae", "date_col": "AESTDAT"}]',
  measures: '[{"testcd": "SYSBP", "value_col": "SYSBP", "unit_col": "SYSBPU"}]',
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

export function PipelineEditor({ detail, onDone }: { detail: DomainDetail; onDone: () => void }) {
  const [steps, setSteps] = useState<Step[]>(() => JSON.parse(JSON.stringify(detail.pipeline ?? [])))
  const [ops, setOps] = useState<Array<{ id: string; label: string }>>([])
  const [reports, setReports] = useState<Report[]>((detail.prep_reports as unknown as Report[]) ?? [])
  const [out, setOut] = useState<{ name: string; columns: string[]; sample: string[][]; rows: number } | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState(false)

  useEffect(() => { void api.prepOps().then((r) => setOps(r.ops)) }, [])

  // Live preview: the pipeline runs as it is edited. Asking for a button press means working
  // blind between presses, which is where a wrong step survives long enough to be trusted.
  const firstRun = useRef(true)
  useEffect(() => {
    if (!steps.length) { setReports([]); setOut(null); return }
    const t = window.setTimeout(() => { void run(steps, true) }, firstRun.current ? 0 : 450)
    firstRun.current = false
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(steps)])

  const run = async (list: Step[], quiet = false) => {
    if (!quiet) setBusy(true)
    setLive(true)
    try {
      const r = await api.previewPipeline(detail.domain, list)
      if (!r.ok) { setReports([]); setOut(null); setError(r.error ?? "failed"); return }
      setError("")
      setReports((r.reports ?? []) as Report[])
      const names = Object.keys(r.outputs ?? {})
      const last = names[names.length - 1]
      setOut(last ? { name: last, ...(r.outputs![last]) } : null)
    } catch (e) { setError((e as Error).message) } finally { setLive(false); if (!quiet) setBusy(false) }
  }

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

  const datasetsFor = (i: number) => [...steps.slice(0, i).map((s, k) => s.name || `prep${k + 1}`), ...detail.datasets]

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
        <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          {live ? <><Loader2 className="h-3 w-3 animate-spin" />running…</>
                : steps.length ? <>live preview</> : null}
        </span>
        <Button size="sm" className="h-7 text-xs" disabled={busy}
                onClick={() => void act(async () => {
                  await api.setPipeline(detail.domain, steps)
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
        return (
          <div key={i} className="space-y-3 border-b px-3 pb-3 last:border-b-0">
            <div className="flex flex-wrap items-center gap-2">
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

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {(FIELDS[st.op] ?? []).map((f) => {
                const v = st.params[f.k]
                const wide = f.t === "json" || f.t === "mergeinputs" || f.t === "joinkeys"
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
                } else if (f.t === "json") {
                  control = <Textarea rows={2} className="font-mono text-[11px]"
                                      placeholder={HINTS[f.k] ?? ""}
                                      value={v === undefined ? "" : JSON.stringify(v)}
                                      onChange={(e) => {
                                        const raw = e.target.value
                                        if (!raw.trim()) return setParam(i, f.k, undefined)
                                        try { setParam(i, f.k, JSON.parse(raw)); setError("") }
                                        catch { setError(`step ${i + 1}, ${f.k}: not valid JSON yet`) }
                                      }} />
                } else if (f.t === "list") {
                  control = <Input className="h-8 text-xs" placeholder={f.ph ?? "comma separated"}
                                   value={Array.isArray(v) ? v.join(", ") : ((v as string) ?? "")}
                                   onChange={(e) => setParam(i, f.k, e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} />
                } else {
                  control = <Input className="h-8 text-xs" placeholder={f.ph ?? ""} value={(v as string) ?? ""}
                                   onChange={(e) => setParam(i, f.k, e.target.value)} />
                }
                return (
                  <div key={f.k} className={wide ? "col-span-full space-y-1" : "space-y-1"}>
                    <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">
                      {f.label ?? f.k}</Label>
                    {control}
                    {f.t === "json" && HINTS[f.k] && (
                      <p className="text-[10px] text-muted-foreground">{HINTS[f.k]}</p>)}
                  </div>)
              })}
            </div>

            {rep && (rep.ok
              ? <p className="text-xs text-muted-foreground">→ <Mono>{rep.name}</Mono>{" "}
                  {rep.rows?.toLocaleString()} records, {rep.columns?.length} columns
                  {rep.extra_outputs?.length ? ` · also produced ${rep.extra_outputs.join(", ")}` : ""}</p>
              : <p className="text-xs text-destructive">✗ {rep.error}</p>)}
          </div>
        )
      })}

      {error && <div className="px-3 pb-3"><Callout tone="bad">{error}</Callout></div>}
      {out && (
        <div className="space-y-2 px-3 pb-3">
          <p className="text-xs text-muted-foreground">
            <Mono>{out.name}</Mono> — first records of {out.rows.toLocaleString()}</p>
          <DataGrid height="14rem" rowNumbers={false} rows={out.sample}
            cols={out.columns.map((c, ci) => ({ id: c, head: c, kind: "text" as const,
                                                cell: (r: string[]) => r[ci] }))} />
        </div>
      )}
    </div>
  )
}
