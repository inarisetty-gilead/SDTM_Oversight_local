// The function library: the standard derivations lifted from the company SAS templates,
// and the user's own reusable functions — both applied at build time and labelled on
// every variable they fill. No JSON anywhere: the same dropdown builders as the
// variable editor.
import { useCallback, useEffect, useRef, useState } from "react"
import { ChevronDown, ChevronRight, FunctionSquare, Plus, Trash2 } from "lucide-react"
import { api } from "@/api"
import type { CustomFn, DomainDetail, TemplateFn, TemplateResolved } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { PipelineControl } from "@/components/derivation"
import { Chip, Mono } from "@/components/grid"
import { Callout, Panel } from "@/components/shell"

const BLANK: CustomFn = { name: "", description: "", variable: "", domains: [],
                          steps: [], override: false, enabled: true }

export function FunctionsView({ specDomains, ready }: { specDomains: string[]; ready: boolean }) {
  const [templates, setTemplates] = useState<TemplateFn[]>([])
  const [custom, setCustom] = useState<CustomFn[]>([])
  const [editing, setEditing] = useState<CustomFn | null>(null)
  const [origName, setOrigName] = useState("")
  const [ctx, setCtx] = useState<DomainDetail | null>(null)
  const [err, setErr] = useState("")
  const [saved, setSaved] = useState(false)

  const refresh = useCallback(() => {
    void api.listFunctions()
      .then((r) => { setTemplates(r.templates); setCustom(r.custom); setErr("") })
      .catch((e) => setErr((e as Error).message))
  }, [])
  useEffect(refresh, [refresh])

  // the editor's pickers read the first chosen domain's shape (raw datasets + spec variables)
  const ctxDomain = editing?.domains[0]
  useEffect(() => {
    if (!ctxDomain) { setCtx(null); return }
    void api.fnContext(ctxDomain)
      .then((c) => setCtx({ ...c, prep_outputs: [] } as unknown as DomainDetail))
      .catch(() => setCtx(null))
  }, [ctxDomain])

  // the library auto-saves like everything else — once a function is nameable, it is kept
  const firstRun = useRef(true)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    if (!editing || !editing.name.trim() || !editing.variable) return
    const t = window.setTimeout(() => {
      void (async () => {
        try {
          if (origName && origName !== editing.name.trim()) {
            await api.deleteFunction(origName)
            setOrigName(editing.name.trim())
          }
          await api.saveFunction(editing)
          setSaved(true); setErr(""); refresh()
        } catch (e) { setErr((e as Error).message) }
      })()
    }, 600)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(editing)])

  const startEdit = (fn: CustomFn) => { setEditing(JSON.parse(JSON.stringify(fn))); setOrigName(fn.name); setSaved(false) }
  const upd = (patch: Partial<CustomFn>) => { setEditing((f) => (f ? { ...f, ...patch } : f)); setSaved(false) }

  return (
    <div className="space-y-4">
      {err && <Callout tone="bad">{err}</Callout>}

      <Panel title="Standard derivations from the SAS templates"
             description="These fill variables the spec leaves without a workable mapping — only when their inputs exist in the study. Switch one off to keep it out of every build. Open one to see the derivation as it resolved for THIS study — and change it; the change applies on the next build.">
        <div className="divide-y">
          {templates.map((t) => (
            <TemplateRow key={t.variable} t={t} refresh={refresh} />
          ))}
        </div>
      </Panel>

      <Panel title="Your custom functions"
             description="Reusable derivations of your own. A function fills its variable in the chosen domains on the next build — deliberately, so it outranks the built-in templates and any name-match guess, and (only if you say so) the spec itself. Hand edits always win."
             actions={
               <Button size="sm" disabled={!ready}
                       onClick={() => { setEditing({ ...BLANK }); setOrigName(""); setSaved(false) }}>
                 <Plus className="mr-1.5 h-3.5 w-3.5" />New function
               </Button>}>
        {!custom.length && !editing && (
          <p className="text-xs text-muted-foreground">
            {ready ? "No custom functions yet — create one and it becomes part of this study, applied on every build."
                   : "Load a spec and raw data first — functions are written against this study's domains and columns."}
          </p>
        )}
        <div className="space-y-2">
          {custom.map((fn) => (
            <div key={fn.name}
                 className={`flex flex-wrap items-center gap-3 rounded-md border p-2.5 ${fn.enabled ? "" : "opacity-55"}`}>
              <FunctionSquare className="h-4 w-4 text-fuchsia-600" />
              <span className="text-sm font-medium">{fn.name}</span>
              <Chip tone="fuchsia">custom</Chip>
              <span className="text-xs text-muted-foreground">
                fills <Mono>{fn.variable}</Mono> in {fn.domains.length ? fn.domains.join(", ") : "every domain"}
                {fn.override ? " · replaces the spec mapping" : ""}
              </span>
              {fn.description && <span className="flex-1 truncate text-xs">{fn.description}</span>}
              <div className="ml-auto flex items-center gap-1">
                <label className="flex items-center gap-1.5 pr-2 text-[11px] text-muted-foreground">
                  <input type="checkbox" checked={fn.enabled}
                         onChange={(e) => void api.saveFunction({ ...fn, enabled: e.target.checked }).then(refresh)} />
                  apply
                </label>
                <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => startEdit(fn)}>Edit</Button>
                <Button size="icon" variant="ghost" className="h-7 w-7"
                        onClick={() => void api.deleteFunction(fn.name).then(() => {
                          if (origName === fn.name) setEditing(null)
                          refresh()
                        })}>
                  <Trash2 className="h-3.5 w-3.5" /></Button>
              </div>
            </div>
          ))}
        </div>
      </Panel>

      {editing && (
        <Panel title={origName ? `Edit — ${origName}` : "New function"}
               description="Write it once with the same building blocks as a variable derivation; the steps run top to bottom and the last value fills the variable."
               actions={
                 <div className="flex items-center gap-2">
                   {saved && <span className="text-[11px] text-emerald-600">saved ✓</span>}
                   <Button size="sm" variant="outline" onClick={() => setEditing(null)}>Close</Button>
                 </div>}>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-1">
              <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Name</Label>
              <Input className="h-8 text-xs" placeholder="e.g. imputed birth date"
                     value={editing.name} onChange={(e) => upd({ name: e.target.value })} />
            </div>
            <div className="space-y-1 lg:col-span-3">
              <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">What it does</Label>
              <Input className="h-8 text-xs" placeholder="a sentence for the build report"
                     value={editing.description} onChange={(e) => upd({ description: e.target.value })} />
            </div>
          </div>

          <div className="mt-3 space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Applies in</Label>
            <div className="flex flex-wrap items-center gap-1">
              {specDomains.map((d) => {
                const on = editing.domains.includes(d)
                return (
                  <button key={d} type="button"
                          onClick={() => upd({ domains: on ? editing.domains.filter((x) => x !== d)
                                                           : [...editing.domains, d] })}
                          className={`rounded-md border px-2 py-1 text-xs transition
                            ${on ? "border-primary bg-primary/10 font-medium" : "hover:bg-accent"}`}>
                    {d}
                  </button>
                )
              })}
            </div>
          </div>

          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Fills the variable</Label>
              {ctx ? (
                <Select value={editing.variable || "__none"}
                        onValueChange={(v) => upd({ variable: v === "__none" ? "" : v })}>
                  <SelectTrigger className="h-8 text-xs"><SelectValue placeholder="pick a variable" /></SelectTrigger>
                  <SelectContent className="max-h-64">
                    <SelectItem value="__none" className="text-xs">—</SelectItem>
                    {ctx.variables.map((v) => (
                      <SelectItem key={v.variable} value={v.variable} className="text-xs">{v.variable}</SelectItem>))}
                  </SelectContent>
                </Select>
              ) : (
                <p className="text-[11px] text-muted-foreground">Choose a domain above — its spec variables will be offered here.</p>
              )}
            </div>
            <label className="flex items-end gap-2 pb-1.5 text-xs">
              <input type="checkbox" checked={editing.override}
                     onChange={(e) => upd({ override: e.target.checked })} />
              Replace the spec's mapping too — otherwise the function only fills what the spec leaves unmapped
            </label>
          </div>

          <div className="mt-4 space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Steps</Label>
            {ctx ? (
              <PipelineControl steps={editing.steps as Array<Record<string, unknown>>}
                               onChange={(steps) => upd({ steps })}
                               detail={ctx} />
            ) : (
              <p className="text-[11px] text-muted-foreground">Choose a domain above to unlock the step builder.</p>
            )}
          </div>
        </Panel>
      )}
    </div>
  )
}


// ── the template derivations, resolved for this study and editable in place ──

function useCols(domain: string, dataset?: string) {
  const [cols, setCols] = useState<string[]>([])
  useEffect(() => {
    let alive = true
    if (!domain || !dataset) { setCols([]); return }
    void api.columns(domain, dataset)
      .then((r) => { if (alive) setCols(r.columns) })
      .catch(() => { if (alive) setCols([]) })
    return () => { alive = false }
  }, [domain, dataset])
  return cols
}

function PickCol({ value, options, onChange, width = "w-44", placeholder = "—" }: {
  value?: string; options: string[]; onChange: (v: string) => void
  width?: string; placeholder?: string
}) {
  const all = value && !options.includes(value) ? [value, ...options] : options
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

function TemplateRow({ t, refresh }: { t: TemplateFn; refresh: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="py-2">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={t.enabled}
                 onChange={(e) => void api.saveTemplate(t.variable, { enabled: e.target.checked }).then(refresh)} />
          <Mono>{t.variable}</Mono>
        </label>
        <Chip tone="violet">template</Chip>
        {t.edit && <Chip tone="blue">adjusted</Chip>}
        <span className="text-xs text-muted-foreground">
          {t.domains.length ? t.domains.join(", ") : "any domain with a baseline"}</span>
        <span className="flex-1 text-xs">{t.describe}</span>
        <span className="text-[11px] text-muted-foreground">{t.source}</span>
        <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => setOpen(!open)}>
          {open ? <ChevronDown className="mr-1 h-3.5 w-3.5" /> : <ChevronRight className="mr-1 h-3.5 w-3.5" />}
          derivation
        </Button>
      </div>
      {open && <TemplateDerivation t={t} refresh={refresh} />}
    </div>
  )
}

function TemplateDerivation({ t, refresh }: { t: TemplateFn; refresh: () => void }) {
  const r = t.resolved
  const [saving, setSaving] = useState(false)
  const save = (edit: Record<string, unknown>) => {
    setSaving(true)
    void api.saveTemplate(t.variable, { edit: { ...(t.edit ?? {}), ...edit } })
      .then(refresh).finally(() => setSaving(false))
  }
  const clear = () => {
    setSaving(true)
    void api.saveTemplate(t.variable, { clear_edit: true }).then(refresh).finally(() => setSaving(false))
  }

  if (!r) {
    return (
      <div className="mt-2 rounded-md border bg-muted/30 p-2.5 text-xs text-muted-foreground">
        Not applied in the last build — build {t.domains.join("/")} first to see how this
        derivation resolves for your study (it also stays out when its inputs are missing,
        or when the spec already maps the variable).
        {t.edit && (
          <span className="ml-2">A saved adjustment is waiting for the next build.{" "}
            <button className="underline" onClick={clear}>Remove it</button></span>)}
      </div>
    )
  }

  return (
    <div className="mt-2 space-y-2 rounded-md border bg-muted/30 p-2.5">
      <p className="text-xs">{r.reason}</p>
      {r.recipe === "age" && <AgeForm r={r} save={save} />}
      {r.recipe !== "age" && r.mtype === "assign" && <AssignForm r={r} save={save} />}
      {r.recipe !== "age" && r.mtype === "constant" && <ConstantForm r={r} save={save} />}
      {r.recipe === "cond" && <CondSourceForm r={r} save={save} />}
      {r.recipe === "date_extreme" && <DateExtremeForm r={r} save={save} />}
      <p className="text-[11px] text-muted-foreground">
        {saving ? "saving…" : "Changes save into the study and apply when you next build."}
        {t.edit && <>{" · "}<button className="underline" onClick={clear}>back to the template's own derivation</button></>}
      </p>
    </div>
  )
}

/** AGE: reported age column first, birth→reference whole years underneath. */
function AgeForm({ r, save }: {
  r: TemplateResolved; save: (e: Record<string, unknown>) => void
}) {
  const a = r.args as { age_col?: string; age_dataset?: string; birth_var?: string; ref_var?: string }
  const [ctxDatasets, setCtxDatasets] = useState<string[]>([])
  const [vars, setVars] = useState<string[]>([])
  useEffect(() => {
    void api.fnContext(r.domain).then((c) => {
      setCtxDatasets(c.datasets); setVars(c.variables.map((v) => v.variable))
    }).catch(() => {})
  }, [r.domain])
  const cols = useCols(r.domain, a.age_dataset || undefined)
  const patch = (args: Record<string, unknown>) => save({ args: { ...a, ...args } })
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-xs">
      <span className="text-muted-foreground">reported age from</span>
      <PickCol value={a.age_dataset} options={ctxDatasets} placeholder="dataset"
               onChange={(v) => patch({ age_dataset: v, age_col: "" })} />
      <PickCol value={a.age_col} options={cols.length ? cols : (a.age_col ? [a.age_col] : [])}
               placeholder="age column" onChange={(v) => patch({ age_col: v })} />
      <span className="text-muted-foreground">— records with none collected get whole years from</span>
      <PickCol value={a.birth_var} options={vars} width="w-36" onChange={(v) => patch({ birth_var: v })} />
      <span className="text-muted-foreground">to</span>
      <PickCol value={a.ref_var} options={vars} width="w-36" onChange={(v) => patch({ ref_var: v })} />
      <span className="text-muted-foreground">(anniversary rule; partial birth dates complete with 01)</span>
    </div>
  )
}

/** A straight column pick — DTHDTC's collected death date, AGEU's collected unit. */
function AssignForm({ r, save }: {
  r: TemplateResolved; save: (e: Record<string, unknown>) => void
}) {
  const [ctxDatasets, setCtxDatasets] = useState<string[]>([])
  useEffect(() => {
    void api.fnContext(r.domain).then((c) => setCtxDatasets(c.datasets)).catch(() => {})
  }, [r.domain])
  const [ds, setDs] = useState(r.dataset)
  const cols = useCols(r.domain, ds || undefined)
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-xs">
      <span className="text-muted-foreground">take the value from</span>
      <PickCol value={ds} options={ctxDatasets} placeholder="dataset"
               onChange={(v) => setDs(v)} />
      <PickCol value={ds === r.dataset ? r.column : ""} options={cols} placeholder="column"
               onChange={(v) => v && save({ dataset: ds, column: v })} />
    </div>
  )
}

function ConstantForm({ r, save }: {
  r: TemplateResolved; save: (e: Record<string, unknown>) => void
}) {
  const [v, setV] = useState(r.value)
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-muted-foreground">every record gets the fixed value</span>
      <Input className="h-8 w-40 text-xs" value={v} onChange={(e) => setV(e.target.value)}
             onBlur={() => v !== r.value && save({ value: v })} />
    </div>
  )
}

/** DTHFL: Y when a date exists in the chosen source. */
function CondSourceForm({ r, save }: {
  r: TemplateResolved; save: (e: Record<string, unknown>) => void
}) {
  const rules = (r.args as { rules?: Array<{ src?: { dataset?: string; column?: string } }> }).rules ?? []
  const src = rules[0]?.src ?? {}
  const [ctxDatasets, setCtxDatasets] = useState<string[]>([])
  useEffect(() => {
    void api.fnContext(r.domain).then((c) => setCtxDatasets(c.datasets)).catch(() => {})
  }, [r.domain])
  const [ds, setDs] = useState(src.dataset ?? "")
  const cols = useCols(r.domain, ds || undefined)
  const pick = (column: string) => {
    if (!ds || !column) return
    save({ args: { rules: [{ src: { dataset: ds, column }, op: "notmissing", value: "",
                             then: { kind: "text", text: "Y" } }],
                   else: { kind: "missing" } } })
  }
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-xs">
      <span className="text-muted-foreground">set <Mono>Y</Mono> when a date exists in</span>
      <PickCol value={ds} options={ctxDatasets} placeholder="dataset" onChange={setDs} />
      <PickCol value={ds === src.dataset ? (src.column ?? "") : ""} options={cols}
               placeholder="column" onChange={pick} />
      <span className="text-muted-foreground">, blank otherwise</span>
    </div>
  )
}


/** Reference dates: earliest/latest per subject over dataset+date-column pairs. */
function DateExtremeForm({ r, save }: {
  r: TemplateResolved; save: (e: Record<string, unknown>) => void
}) {
  const a = r.args as { func?: string; sources?: Array<{ dataset?: string; date_col?: string }> }
  const sources = a.sources ?? []
  const [ctxDatasets, setCtxDatasets] = useState<string[]>([])
  useEffect(() => {
    void api.fnContext(r.domain).then((c) => setCtxDatasets(c.datasets)).catch(() => {})
  }, [r.domain])
  const patch = (next: Array<{ dataset?: string; date_col?: string }>, func?: string) =>
    save({ args: { func: func ?? a.func ?? "min",
                   sources: next.filter((x) => x.dataset && x.date_col) } })
  return (
    <div className="space-y-1.5 text-xs">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground">take the</span>
        <Select value={a.func ?? "min"} onValueChange={(v) => patch(sources, v)}>
          <SelectTrigger className="h-8 w-32 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="min" className="text-xs">earliest</SelectItem>
            <SelectItem value="max" className="text-xs">latest</SelectItem>
          </SelectContent>
        </Select>
        <span className="text-muted-foreground">per subject, across:</span>
      </div>
      {sources.map((src, i) => (
        <DateExtremeRow key={i} src={src} datasets={ctxDatasets} domain={r.domain}
          onChange={(nr) => patch(sources.map((x, k) => (k === i ? nr : x)))}
          onRemove={() => patch(sources.filter((_, k) => k !== i))} />
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => save({ args: { func: a.func ?? "min",
                                            sources: [...sources, {}] } })}>
        <Plus className="mr-1 h-3 w-3" />Add a dataset
      </Button>
    </div>
  )
}

function DateExtremeRow({ src, datasets, domain, onChange, onRemove }: {
  src: { dataset?: string; date_col?: string }; datasets: string[]; domain: string
  onChange: (r: { dataset?: string; date_col?: string }) => void; onRemove: () => void
}) {
  const cols = useCols(domain, src.dataset || undefined)
  return (
    <div className="flex items-center gap-1.5">
      <PickCol value={src.dataset} options={datasets} placeholder="dataset"
               onChange={(v) => onChange({ dataset: v, date_col: "" })} />
      <span className="text-[11px] text-muted-foreground">date in</span>
      <PickCol value={src.date_col} options={cols.length ? cols : (src.date_col ? [src.date_col] : [])}
               onChange={(v) => onChange({ ...src, date_col: v })} />
      <Button type="button" size="icon" variant="ghost" className="h-7 w-7" onClick={onRemove}>
        <Trash2 className="h-3 w-3" /></Button>
    </div>
  )
}
