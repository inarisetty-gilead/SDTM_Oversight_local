import { useEffect, useMemo, useRef, useState } from "react"
import { api } from "@/api"
import type { DomainDetail, VariableRow } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { CondControl, CtControl, FnControl, PipelineControl } from "./derivation"
import { Mono } from "./grid"
import { Callout } from "./shell"

type Recipe = { id: string; label: string; desc?: string; hidden?: boolean
                fields: Array<Record<string, unknown>> }

export function VariableEditor({ detail, variable, onDone, onClose }: {
  detail: DomainDetail; variable: VariableRow
  onDone: () => void; onClose: () => void
}) {
  const [recipes, setRecipes] = useState<{ mtypes: string[]; recipes: Recipe[] } | null>(null)
  const [mtype, setMtype] = useState(variable.mapping_type)
  const [dataset, setDataset] = useState(variable.source.split(".")[0] ?? "")
  const [column, setColumn] = useState(variable.source.split(".").slice(1).join(".") ?? "")
  const [value, setValue] = useState(variable.constant ?? "")
  const [codelist, setCodelist] = useState(variable.codelist ?? "")
  const [recipe, setRecipe] = useState(variable.recipe ?? "")
  const [args, setArgs] = useState<Record<string, unknown>>({ ...(variable.args ?? {}) })
  const [cols, setCols] = useState<string[]>([])
  const [argCols, setArgCols] = useState<string[]>([])
  const [preview, setPreview] = useState<{ ok: boolean; text: string; samples: string[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [suggested, setSuggested] = useState("")
  // dirty = edited since the last Apply — nothing here autosaves (an untested mapping
  // change must never take effect on its own), so the reader needs a visible cue instead.
  const [dirty, setDirty] = useState(false)
  const firstRun = useRef(true)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    setDirty(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mtype, dataset, column, value, codelist, recipe, JSON.stringify(args)])
  // a native "leave site?" prompt is the only honest way to warn of unsaved work here —
  // silently applying a half-typed mapping would be worse than losing it
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = "" } }
    window.addEventListener("beforeunload", handler)
    return () => window.removeEventListener("beforeunload", handler)
  }, [dirty])

  useEffect(() => { void api.recipes().then(setRecipes) }, [])
  useEffect(() => {
    if (!dataset) { setCols([]); return }
    void api.columns(detail.domain, dataset).then((r) => setCols(r.columns)).catch(() => setCols([]))
  }, [dataset, detail.domain])
  useEffect(() => {
    const ds = (args.dataset as string) || dataset || detail.base
    if (!ds) { setArgCols([]); return }
    void api.columns(detail.domain, ds).then((r) => setArgCols(r.columns)).catch(() => setArgCols([]))
  }, [args.dataset, dataset, detail.base, detail.domain])

  const active = useMemo(() => recipes?.recipes.find((r) => r.id === recipe), [recipes, recipe])

  const payload = () => ({
    mtype,
    // a derived / constant / sequence mapping reads no direct source column — sending
    // the stale assign picks would leave the OLD source showing in the variables table
    dataset: mtype === "assign" ? dataset : "",
    column: mtype === "assign" ? column : "",
    value: mtype === "constant" ? value : "",
    codelist,
    recipe: mtype === "derived" ? recipe : "",
    args: mtype === "derived" ? args : mtype === "sequence" ? args : {},
  })

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError("")
    try { await fn() } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  const setArg = (k: string, v: unknown) =>
    setArgs((a) => { const n = { ...a }; if (v === "" || v === undefined) delete n[k]; else n[k] = v; return n })

  const field = (f: Record<string, unknown>) => {
    const k = f.k as string, t = f.t as string
    const v = args[k]
    // the structured builders replace every JSON textarea: the engine's format is not the
    // reader's format
    if (recipe === "pipeline" && k === "steps") {
      return (
        <div key={k} className="col-span-full space-y-1">
          <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Steps</Label>
          <PipelineControl steps={(v as Record<string, unknown>[]) ?? []} detail={detail}
                           onChange={(steps) => setArg(k, steps)} />
        </div>)
    }
    if (recipe === "fn" && k === "sources") {
      return (
        <div key={k} className="col-span-full space-y-1">
          <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Function</Label>
          <FnControl args={args} detail={detail} onChange={(a) => setArgs(a)} />
        </div>)
    }
    if (recipe === "fn" && ["fn", "start", "len", "delim", "word", "find", "replace",
                            "chars", "sep", "width"].includes(k)) {
      return null                        // FnControl renders the function and its parameters
    }
    if (recipe === "ct" && k === "sources") {
      return (
        <div key={k} className="col-span-full space-y-1">
          <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Controlled terminology</Label>
          <CtControl args={args} detail={detail} onChange={(a) => setArgs(a)} />
        </div>)
    }
    if (recipe === "ct" && k === "codelist") return null   // CtControl renders the codelist dropdown
    if (recipe === "cond" && k === "rules") {
      return (
        <div key={k} className="col-span-full space-y-1">
          <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Rules</Label>
          <CondControl args={args} detail={detail} onChange={(a) => setArgs(a)} />
        </div>)
    }
    if (recipe === "cond" && k === "else") return null   // CondControl renders OTHERWISE
    const wide = t === "json" || t === "sources"
    const heading = (f.label as string) || k
    let control
    if (t === "dataset") {
      control = <Picker value={(v as string) ?? ""} options={detail.datasets}
                        groupFirst={detail.prepared_datasets} groupLabel="Prepared in this study"
                        onChange={(x) => setArg(k, x)} />
    } else if (t === "column") {
      control = <Picker value={(v as string) ?? ""} options={argCols} onChange={(x) => setArg(k, x)} />
    } else if (t === "sdtmvar") {
      control = <Picker value={(v as string) ?? ""} options={detail.variables.map((x) => x.variable)}
                        onChange={(x) => setArg(k, x)} />
    } else if (t === "domain") {
      control = <Picker value={(v as string) ?? ""} options={detail.built_domains} onChange={(x) => setArg(k, x)} />
    } else if (t === "choice") {
      control = <Picker value={(v as string) ?? ""} options={f.options as string[]} onChange={(x) => setArg(k, x)} />
    } else if (t === "json") {
      control = <Textarea rows={3} className="font-mono text-xs"
                          value={v === undefined ? "" : JSON.stringify(v)}
                          onChange={(e) => {
                            const raw = e.target.value
                            if (!raw.trim()) return setArg(k, undefined)
                            try { setArg(k, JSON.parse(raw)); setError("") }
                            catch { setError(`${k}: not valid JSON yet`) }
                          }} />
    } else if (t === "sources") {
      control = <SourceRows rows={(v as SourceRow[]) ?? []} datasets={detail.datasets}
                            prepared={detail.prepared_datasets} domain={detail.domain}
                            onChange={(rows) => setArg(k, rows)} />
    } else if (t === "list") {
      control = <Input className="h-8 text-xs" value={Array.isArray(v) ? v.join(", ") : ((v as string) ?? "")}
                       onChange={(e) => setArg(k, e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} />
    } else {
      control = <Input className="h-8 text-xs" value={(v as string) ?? ""} onChange={(e) => setArg(k, e.target.value)} />
    }
    return (
      <div key={k} className={wide ? "col-span-full space-y-1" : "space-y-1"}>
        <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">{heading}</Label>
        {control}
        {f.help ? <p className="text-[11px] leading-snug text-muted-foreground">{f.help as string}</p> : null}
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div>
        <p className="text-xs text-muted-foreground">
          spec row {variable.spec_row}: <Mono>{variable.spec_action || "—"}</Mono>
          {variable.spec_input ? <> · inputs <Mono>{variable.spec_input}</Mono></> : null}
        </p>
        {variable.spec_rule ? <p className="mt-1 text-xs italic text-muted-foreground">{variable.spec_rule}</p> : null}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div className="space-y-1">
          <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Mapping type</Label>
          <Picker value={mtype} options={recipes?.mtypes ?? []} onChange={setMtype} />
        </div>

        {mtype === "assign" && <>
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Raw dataset</Label>
            <Picker value={dataset} options={detail.datasets}
                    groupFirst={detail.prepared_datasets} groupLabel="Prepared in this study"
                    onChange={(v) => { setDataset(v); setColumn("") }} />
          </div>
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Column</Label>
            <Picker value={column} options={cols} onChange={setColumn} />
          </div>
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Codelist</Label>
            {(detail.codelists ?? []).length ? (
              <Picker value={codelist} options={detail.codelists ?? []} onChange={setCodelist} />
            ) : (
              <Input className="h-8 text-xs" value={codelist} onChange={(e) => setCodelist(e.target.value)} />
            )}
            <p className="text-[11px] leading-snug text-muted-foreground">
              From the spec's Codelist sheet — raw values normalise to its submission values.</p>
          </div>
        </>}

        {mtype === "constant" && (
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Value</Label>
            <Input className="h-8 text-xs" value={value} onChange={(e) => setValue(e.target.value)} />
          </div>
        )}

        {mtype === "sequence" && (
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Number within</Label>
            <Input className="h-8 text-xs" value={(args.group as string) ?? "USUBJID"}
                   onChange={(e) => setArg("group", e.target.value)} />
          </div>
        )}

        {mtype === "derived" && <>
          <div className="col-span-full space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Derivation</Label>
            <Picker value={recipe}
                    options={(recipes?.recipes ?? []).filter((r) => !r.hidden).map((r) => r.id)}
                    labels={Object.fromEntries((recipes?.recipes ?? []).map((r) => [r.id, r.label]))}
                    onChange={(v) => {
                      setRecipe(v); setArgs({}); setSuggested("")
                      // seed the form from what the spec already states, rather than
                      // making the reader copy the datasets and columns out by hand
                      void api.suggestArgs(detail.domain, variable.variable, v)
                        .then((r) => {
                          if (Object.keys(r.args ?? {}).length) {
                            setArgs(r.args)
                            setSuggested(r.input_variables || "the spec")
                          }
                        })
                        .catch(() => undefined)
                    }} />
          </div>
          {suggested && (
            <div className="col-span-full">
              <Callout tone="good" title="Filled in from the spec's Input Variables">
                <Mono>{suggested}</Mono>
              </Callout>
            </div>
          )}
          {active?.desc && (
            <p className="col-span-full -mt-1 text-[12px] leading-snug text-muted-foreground">
              {active.desc}
            </p>
          )}
          {(active?.fields ?? []).filter((f) => !f.advanced).map(field)}
          {(active?.fields ?? []).some((f) => f.advanced) && (
            <div className="col-span-full">
              <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs"
                      onClick={() => setShowAdvanced((v) => !v)}>
                {showAdvanced ? "Hide" : "Show"} the columns it matched automatically
              </Button>
            </div>
          )}
          {showAdvanced && (active?.fields ?? []).filter((f) => f.advanced).map(field)}
        </>}

        {mtype === "drop" && (
          <p className="col-span-full text-xs text-muted-foreground">
            This variable will be excluded from the built dataset.</p>
        )}
      </div>

      {detail.unapplied_datasets.includes(dataset) && (
        <Callout tone="warn" title={`${dataset} comes from a pipeline you have not applied yet`}>
          You can map to it now, but it exists only while this preview stands. Apply the
          pipeline in <b>Prepare the data</b> to keep it.
        </Callout>
      )}
      {error && <Callout tone="bad">{error}</Callout>}
      {preview && (preview.ok
        ? <Callout tone="good"><span className="font-medium">{preview.text}</span>
            <div className="mt-1 flex flex-wrap gap-1">
              {preview.samples.map((s, i) => <Mono key={i}>{s}</Mono>)}</div></Callout>
        : <Callout tone="bad">{preview.text}</Callout>)}

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void run(async () => {
          const r = await api.previewEdit(detail.domain, variable.variable, payload())
          setPreview(r.ok
            ? { ok: true, samples: r.samples ?? [],
                text: `${r.how} — ${(r.populated ?? 0).toLocaleString()} of ${(r.rows ?? 0).toLocaleString()} records populated` }
            : { ok: false, samples: [], text: r.error || r.reason || "not built" })
        })}>Preview</Button>
        <Button size="sm" disabled={busy} onClick={() => void run(async () => {
          await api.setEdit(detail.domain, variable.variable, payload())
          await api.rebuild(detail.domain)
          setDirty(false)
          onDone()
        })}>Apply &amp; rebuild {detail.domain}</Button>
        {variable.edited && (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void run(async () => {
            await api.clearEdit(detail.domain, variable.variable)
            await api.rebuild(detail.domain)
            setDirty(false)
            onDone()
          })}>Revert to the spec</Button>
        )}
        <Button variant="ghost" size="sm"
                onClick={() => { if (!dirty || window.confirm("Discard the unsaved changes to this variable?")) onClose() }}>
          Cancel</Button>
        {dirty && <span className="text-[11px] text-amber-600">unsaved changes — Apply to keep them</span>}
      </div>
    </div>
  )
}

type SourceRow = { dataset?: string; date_col?: string }

/** Repeatable dataset + date-column rows — the shape `date_extreme` needs, without JSON. */
function SourceRows({ rows, datasets, prepared, domain, onChange }: {
  rows: SourceRow[]; datasets: string[]; prepared: string[]; domain: string
  onChange: (rows: SourceRow[]) => void
}) {
  const [cols, setCols] = useState<Record<string, string[]>>({})

  const loadCols = async (ds: string) => {
    if (!ds || cols[ds]) return
    try {
      const r = await api.columns(domain, ds)
      setCols((c) => ({ ...c, [ds]: r.columns }))
    } catch { setCols((c) => ({ ...c, [ds]: [] })) }
  }
  useEffect(() => { rows.forEach((r) => r.dataset && void loadCols(r.dataset)) },
            // eslint-disable-next-line react-hooks/exhaustive-deps
            [rows.map((r) => r.dataset).join("|")])

  const set = (i: number, patch: SourceRow) =>
    onChange(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)))

  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <span className="w-5 shrink-0 text-[11px] text-muted-foreground">{i + 1}</span>
          <div className="w-48">
            <Picker value={r.dataset ?? ""} options={datasets} groupFirst={prepared}
                    groupLabel="Prepared in this study"
                    onChange={(v) => { set(i, { dataset: v, date_col: "" }); void loadCols(v) }} />
          </div>
          <span className="text-[11px] text-muted-foreground">·</span>
          <div className="w-48">
            <Picker value={r.date_col ?? ""} options={cols[r.dataset ?? ""] ?? []}
                    onChange={(v) => set(i, { date_col: v })} />
          </div>
          <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs"
                  onClick={() => onChange(rows.filter((_, k) => k !== i))}>Remove</Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...rows, { dataset: "", date_col: "" }])}>
        Add a dataset
      </Button>
      {!rows.length && (
        <p className="text-[11px] text-muted-foreground">
          No datasets yet — add one row per raw form that holds a date to consider.
        </p>
      )}
    </div>
  )
}

function Picker({ value, options, labels, onChange, groupFirst, groupLabel }: {
  value: string; options: string[]; labels?: Record<string, string>
  onChange: (v: string) => void
  /** these are listed first under their own heading — prepared datasets are easy to lose
   *  among fifty raw forms */
  groupFirst?: string[]; groupLabel?: string
}) {
  const first = (groupFirst ?? []).filter((o) => options.includes(o))
  const rest = options.filter((o) => !first.includes(o))
  return (
    <Select value={value || "__none"} onValueChange={(v) => onChange(v === "__none" ? "" : v)}>
      <SelectTrigger className="h-8 w-full text-xs"><SelectValue placeholder="—" /></SelectTrigger>
      <SelectContent>
        <SelectItem value="__none" className="text-xs">—</SelectItem>
        {first.length > 0 && (
          <>
            <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              {groupLabel ?? "Prepared"}
            </div>
            {first.map((o) => (
              <SelectItem key={o} value={o} className="text-xs">{labels?.[o] ?? o}</SelectItem>))}
            <div className="my-1 border-t" />
            <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              Raw datasets
            </div>
          </>
        )}
        {rest.map((o) => <SelectItem key={o} value={o} className="text-xs">{labels?.[o] ?? o}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}
