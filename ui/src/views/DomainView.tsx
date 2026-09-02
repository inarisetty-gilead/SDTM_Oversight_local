import { useCallback, useEffect, useRef, useState } from "react"
import { ArrowDown, ArrowLeft, ArrowUp, BookOpen, ExternalLink, Search } from "lucide-react"
import { api } from "@/api"
import type { CtInspect, DomainDetail, JobState, VariableRow } from "@/api"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { SpecRowsPanel } from "@/components/SpecPeek"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Chip, DataGrid, Mono, STATUS_TONE, SegmentBar, type ChipTone } from "@/components/grid"
import { Callout, Metric, Metrics, PageHeader, Panel } from "@/components/shell"
import { DataView, RecordTable, VariableRecords, useDomainRecords } from "@/components/DataView"
import { PipelineEditor } from "@/components/PipelineEditor"
import { VariableEditor } from "@/components/VariableEditor"

const FILTERS = ["all", "built", "empty", "dropped", "not_built", "error"] as const
type Filter = (typeof FILTERS)[number]

// spec Mapping Action -> chip tone, matching SDTM Designer's color language:
// direct copies green, derivations teal, drops muted, supplemental violet
export const ACTION_TONE: Record<string, ChipTone> = {
  ASSIGN: "green", CODE: "teal", DERIVED: "teal", DERIVE: "teal",
  DROP: "slate", SUPP: "violet", CONSTANT: "amber", HARDCODE: "amber",
}

/** Designer-style CT inspector: the codelist as the spec states it, next to what the
 *  DATA holds and what each value normalises to. Unmatched values can be mapped by hand —
 *  to one of the codelist's submission values, or to a new value when it is extensible. */
function CtInspector({ domain, variable, onClose }: {
  domain: string; variable: string; onClose: (changed: boolean) => void
}) {
  const [ct, setCt] = useState<CtInspect | null>(null)
  const [err, setErr] = useState("")
  const [dirty, setDirty] = useState(false)
  const [showTerms, setShowTerms] = useState(false)
  const [custom, setCustom] = useState<Record<string, string>>({})

  const load = useCallback(() => {
    api.variableCt(domain, variable).then(setCt).catch((e) => setErr((e as Error).message))
  }, [domain, variable])
  useEffect(() => { load() }, [load])

  const setMap = async (raw: string, target: string) => {
    setErr("")
    try {
      await api.saveCtMap(domain, variable, { raw_value: raw, ct_value: target })
      setDirty(true); load()
    } catch (e) { setErr((e as Error).message) }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(dirty) }}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2 text-[15px]">
            {variable} · <Mono>{ct?.codelist ?? "…"}</Mono>
            {ct?.label && <span className="text-[13px] font-normal text-muted-foreground">{ct.label}</span>}
            {ct && <Chip tone={ct.extensible ? "green" : "slate"}>
              {ct.extensible ? "extensible" : "not extensible"}</Chip>}
            {ct && <span className="text-[12px] font-normal text-muted-foreground">
              {ct.n_terms} term(s) in the spec</span>}
          </DialogTitle>
        </DialogHeader>
        {err && <Callout tone="bad">{err}</Callout>}
        {!ct && !err && <p className="text-sm text-muted-foreground">Loading…</p>}
        {ct && (
          <div className="space-y-4">
            <div>
              <h4 className="mb-1 text-[13px] font-medium">Your data → controlled terminology</h4>
              <p className="mb-2 text-[12px] text-muted-foreground">
                Every distinct value the source column holds, and the submission value it
                normalises to. An unmatched value passes through and is flagged — map it here
                {ct.extensible ? " to any value (this codelist is extensible)"
                               : " to one of the codelist's submission values"}.
              </p>
              {ct.data.length === 0 && <p className="text-[12px] text-muted-foreground">
                No raw column values to show — the input is not a plain column.</p>}
              <div className="space-y-1">
                {ct.data.map((d) => (
                  <div key={d.value} className="flex flex-wrap items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs">
                    <Mono>{d.value}</Mono>
                    <span className="text-muted-foreground">× {d.count}</span>
                    <span className="text-muted-foreground">→</span>
                    {d.matched
                      ? <Chip tone={d.manual ? "blue" : "green"}>{d.maps_to}{d.manual ? " · manual" : ""}</Chip>
                      : <Chip tone="red">not in CT — passes through</Chip>}
                    <span className="ml-auto flex items-center gap-1.5">
                      <Select value={ct.overrides[d.value] ?? "__none"}
                              onValueChange={(v) => { if (v !== "__custom") void setMap(d.value, v === "__none" ? "" : v) }}>
                        <SelectTrigger className="h-7 w-44 text-xs">
                          <SelectValue placeholder="map to…" /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__none" className="text-xs">— no manual mapping —</SelectItem>
                          {ct.submission_values.map((sv) => (
                            <SelectItem key={sv} value={sv} className="text-xs">{sv}</SelectItem>))}
                          {ct.extensible && <SelectItem value="__custom" className="text-xs">new value…</SelectItem>}
                        </SelectContent>
                      </Select>
                      {ct.extensible && (
                        <span className="flex items-center gap-1">
                          <Input className="h-7 w-28 text-xs" placeholder="new value"
                                 value={custom[d.value] ?? ""}
                                 onChange={(e) => setCustom((c) => ({ ...c, [d.value]: e.target.value }))} />
                          <Button size="sm" variant="outline" className="h-7 px-2 text-xs"
                                  disabled={!(custom[d.value] ?? "").trim()}
                                  onClick={() => void setMap(d.value, custom[d.value].trim())}>Map</Button>
                        </span>
                      )}
                    </span>
                  </div>
                ))}
              </div>
              {ct.unmatched_records > 0 && (
                <p className="mt-2 text-[12px] text-amber-600 dark:text-amber-400">
                  {ct.unmatched_records.toLocaleString()} record(s) hold values not in the codelist.</p>
              )}
            </div>
            <div>
              <Button variant="link" size="sm" className="h-auto p-0 text-xs"
                      onClick={() => setShowTerms((v) => !v)}>
                {showTerms ? "Hide" : "Show"} the codelist's {ct.n_terms} term(s)</Button>
              {showTerms && (
                <div className="mt-2 max-h-60 overflow-y-auto rounded-md border">
                  {ct.terms.map((t, i) => (
                    <div key={i} className="flex items-baseline gap-2 border-b px-2.5 py-1 text-xs last:border-0">
                      <Mono>{t.value}</Mono>
                      <span className="text-muted-foreground">{t.decode}</span>
                    </div>))}
                </div>
              )}
            </div>
            <p className="text-[11px] text-muted-foreground">
              Manual mappings save as a hand edit on {variable} and apply on rebuild —
              closing this window rebuilds {domain} when anything changed.</p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

/** The build written out as a standalone program — Python/pandas or house-style SAS,
 *  generated from the exact blocks the build executed (hand edits included). */
function ProgramPane({ domain, refreshKey }: { domain: string; refreshKey: number }) {
  const [lang, setLang] = useState<"python" | "sas">("python")
  const [prog, setProg] = useState<{ program: string; filename: string } | null>(null)
  const [err, setErr] = useState("")
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let live = true
    setProg(null); setErr(""); setCopied(false)
    api.domainProgram(domain, lang)
      .then((p) => { if (live) setProg(p) })
      .catch((e) => { if (live) setErr((e as Error).message) })
    return () => { live = false }
  }, [domain, lang, refreshKey])

  const copy = () => {
    if (!prog) return
    void navigator.clipboard.writeText(prog.program).then(() => {
      setCopied(true); setTimeout(() => setCopied(false), 1500)
    })
  }
  const download = () => {
    if (!prog) return
    const url = URL.createObjectURL(new Blob([prog.program], { type: "text/plain" }))
    const a = document.createElement("a")
    a.href = url; a.download = prog.filename; a.click()
    URL.revokeObjectURL(url)
  }
  const todos = prog ? (prog.program.match(/TODO \(hand-code\)/g) ?? []).length : 0

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {(["python", "sas"] as const).map((l) => (
          <Button key={l} size="sm" variant={lang === l ? "default" : "outline"}
                  className="h-7 text-xs" onClick={() => setLang(l)}>
            {l === "python" ? "Python (pandas)" : "SAS"}</Button>
        ))}
        <div className="ml-auto flex gap-1.5">
          <Button size="sm" variant="outline" className="h-7 text-xs" disabled={!prog} onClick={copy}>
            {copied ? "✓ Copied" : "Copy"}</Button>
          <Button size="sm" variant="outline" className="h-7 text-xs" disabled={!prog} onClick={download}>
            Download {prog ? prog.filename : ""}</Button>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        Generated from the exact mappings this build executed — hand edits included, the
        spec's controlled terminology inlined{todos > 0 ? <>; <b>{todos}</b> spot(s) the tool
        runs internally are marked <Mono>TODO</Mono> for a programmer to hand-code</> : null}.
        Regenerated on every build.
      </p>
      {err && <Callout tone="bad">{err}</Callout>}
      {!err && !prog && <p className="text-sm text-muted-foreground">Generating…</p>}
      {prog && (
        <pre className="max-h-[36rem] overflow-auto rounded-xl border bg-muted/30 p-4 text-[11.5px] leading-relaxed">
          {prog.program}</pre>
      )}
    </div>
  )
}

export function DomainView({ domain, onBack, onChanged }: {
  domain: string; onBack: () => void; onChanged: () => void
}) {
  const [d, setD] = useState<DomainDetail | null>(null)
  const [filter, setFilter] = useState<Filter>("all")
  const [search, setSearch] = useState("")
  const [group, setGroup] = useState("none")
  const [editing, setEditing] = useState<VariableRow | null>(null)
  const [ctVar, setCtVar] = useState<string | null>(null)   // CT inspector target
  const [specOpen, setSpecOpen] = useState(false)           // spec slide-over
  const [pane, setPane] = useState<"data" | "edit">("data")
  const [job, setJob] = useState<JobState | null>(null)
  const [error, setError] = useState("")
  // bumped after any rebuild so the data view reloads rather than showing stale records
  const [dataKey, setDataKey] = useState(0)
  const [recPart, setRecPart] = useState<"parent" | "supp">("parent")
  const records = useDomainRecords(domain, dataKey, recPart)

  const load = useCallback(async (dom: string) => {
    setError("")
    try { setD(await api.domain(dom)) }
    catch (e) { setError((e as Error).message); setD(null) }
  }, [])
  useEffect(() => { setEditing(null); setPane("data"); setRecPart("parent"); void load(domain) }, [domain, load])

  const afterRebuild = async () => {
    for (;;) {
      await new Promise((r) => setTimeout(r, 400))
      const j = await api.job(); setJob(j)
      if (j.status !== "running") {
        setJob(null)
        if (j.status === "error") { setError(j.error); return }
        break
      }
    }
    await load(domain); setEditing(null); setDataKey((k) => k + 1); onChanged()
  }

  if (!d) {
    return (
      <>
        <PageHeader title={domain} actions={<Button variant="ghost" onClick={onBack}>
          <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />Back</Button>} />
        {error ? <Callout tone="bad">{error}</Callout>
               : <p className="text-sm text-muted-foreground">Loading…</p>}
      </>
    )
  }

  const rows = d.variables.filter((v) => {
    if (filter !== "all" && v.status !== filter) return false
    if (!search.trim()) return true
    const q = search.toLowerCase()
    return [v.variable, v.label, v.source, v.how, v.spec_input].join(" ").toLowerCase().includes(q)
  })
  const counts = Object.fromEntries(FILTERS.map((f) => [f,
    f === "all" ? d.variables.length : d.variables.filter((v) => v.status === f).length]))
  const guessed = d.variables.filter((v) => v.method_source === "name_match" && v.status === "built").length
  const edits = Object.keys(d.edits ?? {}).length
  const notBuilt = (d.counts.not_built ?? 0) + (d.counts.error ?? 0)

  const groupFn = group === "status" ? (v: VariableRow) => v.status.replace("_", " ")
    : group === "role" ? (v: VariableRow) => v.role || "no role"
    : group === "source" ? (v: VariableRow) => (v.source.split(".")[0] || "not from a raw column")
    : undefined

  return (
    <>
      <PageHeader
        title={<span className="flex items-center gap-2">{d.domain}
          {d.prep && <Chip tone="violet">{d.prep.op === "stack" ? "stacked" : "transposed"}</Chip>}
        </span>}
        subtitle={<>{d.rows.toLocaleString()} record(s) from <Mono>{d.base}</Mono>
          {d.supp_rows ? <> · SUPP{d.domain}: {d.supp_rows.toLocaleString()}</> : null}</>}
        actions={<span className="flex items-center gap-1">
          <Button variant="outline" size="sm" className="h-8 text-xs" onClick={() => setSpecOpen(true)}
                  title="the spec's rows for this domain, beside your mapping">
            <BookOpen className="mr-1.5 h-3.5 w-3.5" />Spec</Button>
          <Button variant="outline" size="sm" className="h-8 px-2 text-xs"
                  title="open the spec in its own window (for a second monitor)"
                  onClick={() => window.open(`#spec/${d.domain}`, "_blank",
                                             "width=920,height=860,noopener")}>
            <ExternalLink className="h-3.5 w-3.5" /></Button>
          <Button variant="ghost" onClick={onBack}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />All domains</Button>
        </span>} />

      <div className="space-y-4">
        {error && <Callout tone="bad">{error}</Callout>}
        {job && <Panel><Progress value={job.percent} className="h-1.5" />
          <p className="mt-2 text-xs text-muted-foreground">{job.message}</p></Panel>}
        {!d.ok && <Callout tone="bad">{d.error}</Callout>}
        {d.prep && <Callout tone="warn" title="Data preparation applied">{d.prep.note}</Callout>}
        {(d.counts.empty ?? 0) > 0 && (
          <Callout tone="warn" title={`${d.counts.empty} mapping(s) built but produced no values`}>
            These ran without error and populated nothing — usually a source that holds nothing
            for these records, or a join that matched no subject. Filter to <b>empty</b> below.
          </Callout>
        )}
        {guessed > 0 && (
          <Callout tone="warn" title={`${guessed} variable(s) were matched to a raw column by name`}>
            The spec names no source for these. They are built, but they are guesses — agreement with
            the vendor here shows the two guesses coincide, not that the spec was followed.
          </Callout>
        )}
        {edits > 0 && (
          <Callout tone="warn" title={`${edits} mapping(s) are set by hand, not by the spec`}>
            For those variables this build is not an independent rebuild.{" "}
            <Button variant="link" size="sm" className="h-auto p-0 text-[13px]"
                    onClick={() => void (async () => {
                      await api.clearEdits(d.domain); await api.rebuild(d.domain); await afterRebuild()
                    })()}>revert all to the spec</Button>
          </Callout>
        )}

        <Metrics>
          <Metric value={d.counts.built} label="built" tone="good" />
          <Metric value={guessed} label="matched by name" tone={guessed ? "warn" : undefined} />
          <Metric value={edits} label="hand edits" />
          <Metric value={d.counts.dropped} label="dropped by spec" />
          <Metric value={notBuilt} label="not built" tone={notBuilt ? "warn" : "good"} />
          <Metric value={d.rows.toLocaleString()} label="records" />
        </Metrics>
        <SegmentBar segments={[
          { value: d.counts.built - guessed, tone: "green", label: "built from the spec" },
          { value: guessed, tone: "amber", label: "matched by name" },
          { value: d.counts.dropped, tone: "slate", label: "dropped" },
          { value: notBuilt, tone: "red", label: "not built" },
        ]} />

        <Tabs defaultValue="variables">
          <TabsList>
            <TabsTrigger value="prepare">Prepare the data</TabsTrigger>
            <TabsTrigger value="variables">Variables</TabsTrigger>
            <TabsTrigger value="settings">Settings</TabsTrigger>
            <TabsTrigger value="data">Data</TabsTrigger>
            <TabsTrigger value="program">Program</TabsTrigger>
          </TabsList>

          <TabsContent value="variables" className="space-y-3 pt-4">
            <div className="flex flex-wrap items-center gap-2">
              {FILTERS.filter((f) => (f !== "error" || counts.error)
                                  && (f !== "empty" || counts.empty)).map((f) => (
                <Button key={f} size="sm" variant={filter === f ? "default" : "outline"}
                        className="h-7 text-xs capitalize" onClick={() => setFilter(f)}>
                  {f.replace("_", " ")}<span className="ml-1.5 opacity-60">{counts[f]}</span>
                </Button>
              ))}
              <Select value={group} onValueChange={setGroup}>
                <SelectTrigger className="h-7 w-36 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none" className="text-xs">No grouping</SelectItem>
                  <SelectItem value="status" className="text-xs">Group by status</SelectItem>
                  <SelectItem value="role" className="text-xs">Group by role</SelectItem>
                  <SelectItem value="source" className="text-xs">Group by source dataset</SelectItem>
                </SelectContent>
              </Select>
              {d.pipeline.length > 0 && (
                <span className="flex items-center gap-1.5">
                  <span className="text-[11px] text-muted-foreground"
                        title="the prepared output the domain's records are built from — variables whose columns it carries follow it; hand-edited variables keep their own source">
                    records from</span>
                  <Select value={((d.override as { base?: string })?.base || "__last")}
                          onValueChange={(v) => void (async () => {
                            await api.setRecordsFrom(d.domain, v === "__last" ? "" : v)
                            await api.rebuild(d.domain)
                            await afterRebuild()
                          })()}>
                    <SelectTrigger className="h-7 w-44 text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__last" className="text-xs">the last step (default)</SelectItem>
                      {d.pipeline.map((s, k) => {
                        const n = (s as { name?: string }).name || `prep${k + 1}`
                        return <SelectItem key={n} value={n} className="text-xs">{n} (pinned)</SelectItem>
                      })}
                    </SelectContent>
                  </Select>
                </span>
              )}
              <div className="relative ml-auto min-w-56 flex-1">
                <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
                <Input className="h-8 pl-8 text-xs" placeholder="Filter by variable, label or source"
                       value={search} onChange={(e) => setSearch(e.target.value)} />
              </div>
              <Button size="icon" variant="outline" className="h-8 w-8"
                      title="open this variables table in its own window — it follows every rebuild"
                      onClick={() => window.open(`#vars/${d.domain}`, "_blank",
                        "width=1400,height=900,noopener")}>
                <ExternalLink className="h-3.5 w-3.5" /></Button>
            </div>

            {editing && (
              <div className="space-y-3 rounded-xl border border-primary/40 bg-muted/20 p-4">
                <div className="flex flex-wrap items-baseline gap-2">
                  <h4 className="text-[15px] font-semibold">{editing.variable}</h4>
                  <span className="text-[13px] text-muted-foreground">{editing.label}</span>
                  <span className="text-[12px] text-muted-foreground">
                    {editing.how}{editing.source ? ` · ${editing.source}` : ""}
                  </span>
                  <div className="ml-auto flex gap-1">
                    <Button size="sm" variant={pane === "edit" ? "default" : "outline"}
                            className="h-7 text-xs" onClick={() => setPane(pane === "edit" ? "data" : "edit")}>
                      {pane === "edit" ? "Done" : "Edit mapping"}
                    </Button>
                    <Button size="sm" variant="ghost" className="h-7 text-xs"
                            onClick={() => { setEditing(null); setPane("data") }}>Close</Button>
                  </div>
                </div>
                {/* the variable's own records: the subject key and the value, nothing else */}
                <VariableRecords domain={d.domain} variable={editing.variable} refreshKey={dataKey}
                                 keys={["USUBJID", `${d.domain}SEQ`, "VISIT", "VISITNUM"]
                                   .filter((k) => d.columns.includes(k))} />
                {pane === "edit" && (
                  <VariableEditor detail={d} variable={editing}
                                  onDone={() => void afterRebuild()}
                                  onClose={() => setPane("data")} />
                )}
              </div>
            )}

            <DataGrid rows={rows} height="34rem" rowKey={(v) => v.variable}
                      groupBy={groupFn}
                      onRowClick={(v) => {
                        if (v.variable !== editing?.variable) setPane("data")
                        setEditing(v)
                      }}
                      empty="No variables match this filter"
              cols={[
                { id: "v", head: "Variable", kind: "key", sticky: true, width: 130,
                  cell: (v) => v.variable },
                // reorder the BUILD, right on the row — a derivation can only read
                // variables built before it; the dataset's columns stay in spec order
                { id: "mv", head: "", kind: "tag", width: 52,
                  cell: (v) => (
                    <span className="flex gap-0.5" onClick={(e) => e.stopPropagation()}>
                      <button type="button" title="build this variable earlier"
                              className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
                              onClick={() => void (async () => {
                                await api.moveVariable(d.domain, v.variable, "up")
                                await api.rebuild(d.domain); await afterRebuild()
                              })().catch(() => undefined)}>
                        <ArrowUp className="h-3 w-3" /></button>
                      <button type="button" title="build this variable later"
                              className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
                              onClick={() => void (async () => {
                                await api.moveVariable(d.domain, v.variable, "down")
                                await api.rebuild(d.domain); await afterRebuild()
                              })().catch(() => undefined)}>
                        <ArrowDown className="h-3 w-3" /></button>
                    </span>) },
                { id: "l", head: "Label", kind: "text", width: 210,
                  cell: (v) => <span className="text-muted-foreground">{v.label}</span> },
                // the spec's verdict for the row — ASSIGN / CODE / DERIVED / DROP / SUPP,
                // prominent like SDTM Designer's "Mapping Action" column
                { id: "act", head: "Action", kind: "tag",
                  cell: (v) => v.spec_action ? <Chip tone={ACTION_TONE[v.spec_action.toUpperCase()] ?? "blue"}>
                    {v.spec_action.toUpperCase()}</Chip> : null },
                { id: "cl", head: "Codelist", kind: "tag",
                  cell: (v) => v.codelist ? (
                    <button type="button" title="see the codelist and what your data maps to"
                            onClick={(e) => { e.stopPropagation(); setCtVar(v.variable) }}>
                      <Chip tone="teal" className="cursor-pointer underline decoration-dotted underline-offset-2">
                        {v.codelist}</Chip>
                    </button>) : null },
                { id: "s", head: "Status", kind: "tag",
                  cell: (v) => <Chip tone={STATUS_TONE[v.status] ?? "slate"}>
                    {v.status.replace("_", " ")}</Chip> },
                { id: "by", head: "Mapped by", kind: "tag",
                  cell: (v) => <Chip tone={STATUS_TONE[v.method_source] ?? "slate"}>
                    {v.method_source === "name_match" ? `name match ${v.confidence}%`
                      : v.method_source === "edit" ? "hand edit"
                      : v.method_source === "convention" ? "convention"
                      : v.method_source === "template" ? "template" : "spec"}</Chip> },
                { id: "h", head: "How it was built", kind: "calc", width: 180, cell: (v) => v.how },
                { id: "src", head: "Source", kind: "code", width: 170,
                  cell: (v) => v.source ? <Mono>{v.source}</Mono>
                    : v.constant ? <Mono>"{v.constant}"</Mono> : null },
                { id: "p", head: "Populated", kind: "number", align: "right",
                  cell: (v) => v.populated === null ? "" : v.populated.toLocaleString() },
                { id: "vals", head: "Values", kind: "text", width: 220,
                  cell: (v) => <span className="flex gap-1">
                    {v.samples.slice(0, 3).map((x, i) => <Mono key={i}>{x}</Mono>)}</span> },
                { id: "why", head: "Reason", kind: "text", width: 300,
                  cell: (v) => <span className={v.error ? "text-destructive" : "text-muted-foreground"}>
                    {v.error || v.reason}</span> },
                { id: "row", head: "Spec row", kind: "number", align: "right",
                  cell: (v) => v.spec_row || "" },
              ]} />
            <p className="text-xs text-muted-foreground">
              Click a variable to highlight its column below, and to change how it is mapped.</p>

            <div className="space-y-2">
              <div className="flex items-baseline gap-2">
                <h4 className="text-[13px] font-medium">Resulting dataset</h4>
                <span className="text-[12px] text-muted-foreground">
                  {recPart === "supp" ? `SUPP${d.domain}` : d.domain}
                  {editing ? ` · ${editing.variable} highlighted` : ""}
                </span>
                {(d.supp_rows > 0 || d.variables.some((v) => v.supp)) && (
                  <div className="ml-auto flex gap-1">
                    <Button size="sm" variant={recPart === "parent" ? "secondary" : "ghost"}
                            className="h-6 px-2 text-[11px]" onClick={() => setRecPart("parent")}>
                      {d.domain}</Button>
                    <Button size="sm" variant={recPart === "supp" ? "secondary" : "ghost"}
                            className="h-6 px-2 text-[11px]" onClick={() => setRecPart("supp")}>
                      SUPP{d.domain}</Button>
                  </div>
                )}
              </div>
              {recPart === "supp" && records.page && records.page.nrows === 0 && (
                <Callout tone="warn" title="No qualifier values were populated">
                  SUPP{d.domain} variables are defined in the spec, but none of them had a
                  value to carry for any record in this build.
                </Callout>
              )}
              {records.error
                ? <Callout tone="bad">{records.error}</Callout>
                : <RecordTable page={records.page} busy={records.busy} height="26rem"
                               highlight={recPart === "parent" ? editing?.variable : undefined}
                               onRefresh={() => void records.reload()} />}
            </div>
          </TabsContent>

          <TabsContent value="data" className="pt-4">
            <DataView domain={d.domain} datasets={d.datasets} refreshKey={dataKey}
                      hasSuppVars={d.variables.some((v) => v.supp)} />
          </TabsContent>

          <TabsContent value="program" className="pt-4">
            <ProgramPane domain={d.domain} refreshKey={dataKey} />
          </TabsContent>

          <Sheet open={specOpen} onOpenChange={setSpecOpen}>
            <SheetContent side="right" className="flex w-full flex-col sm:max-w-xl">
              <SheetHeader>
                <SheetTitle className="text-[14px]">{d.domain} — mapping spec</SheetTitle>
              </SheetHeader>
              <div className="min-h-0 flex-1 pt-1">
                <SpecRowsPanel domain={d.domain} highlight={editing?.variable} />
              </div>
            </SheetContent>
          </Sheet>

          {ctVar && (
            <CtInspector domain={d.domain} variable={ctVar}
                         onClose={(changed) => {
                           setCtVar(null)
                           if (changed) void (async () => {
                             await api.rebuild(d.domain); await afterRebuild()
                           })()
                         }} />
          )}

          <TabsContent value="prepare" className="pt-4">
            {/* keyed by domain: the step list initialises from the detail ON MOUNT, and a
                stale instance from another domain would show no steps (and could even
                clear the saved draft) while the pipeline is right there in the detail */}
            <PipelineEditor key={d.domain} detail={d} onDone={() => void afterRebuild()} />
          </TabsContent>

          <TabsContent value="settings" className="pt-4">
            <DomainSettings detail={d} onDone={() => void afterRebuild()} />
          </TabsContent>
        </Tabs>

        {d.warnings.length > 0 && (
          <div className="space-y-2">{d.warnings.map((w, i) => <Callout key={i}>{w}</Callout>)}</div>
        )}
      </div>
    </>
  )
}

function DomainSettings({ detail, onDone }: { detail: DomainDetail; onDone: () => void }) {
  const ov = detail.override as Record<string, unknown>
  const dd = detail.dedup as Record<string, unknown>
  const [base, setBase] = useState((ov.base as string) ?? "")
  const [prepMode, setPrepMode] = useState((ov.prep_mode as string) ?? "auto")
  const [sort, setSort] = useState(((ov.sort as string[]) ?? []).join(", "))
  const [keys, setKeys] = useState(((ov.keys as string[]) ?? []).join(", "))
  const [ddOn, setDdOn] = useState(!!dd.enabled)
  const [ddKeys, setDdKeys] = useState(((dd.keys as string[]) ?? []).join(", "))
  const [ddKeep, setDdKeep] = useState((dd.keep as string) ?? "first")
  const [busy, setBusy] = useState(false)
  const split = (v: string) => v.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean)
  // dirty = edited since the last Save — this form has no autosave (an untried record
  // source or dedup rule must never take effect on its own), so show it instead.
  const [dirty, setDirty] = useState(false)
  const firstRun = useRef(true)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    setDirty(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, prepMode, sort, keys, ddOn, ddKeys, ddKeep])
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = "" } }
    window.addEventListener("beforeunload", handler)
    return () => window.removeEventListener("beforeunload", handler)
  }, [dirty])

  return (
    <div className="space-y-4">
      <Panel title="Records">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <F label="Record source">
            <Select value={base || "__auto"} onValueChange={(v) => setBase(v === "__auto" ? "" : v)}>
              <SelectTrigger className="h-8 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="__auto" className="text-xs">auto — from the spec</SelectItem>
                {detail.datasets.map((x) => <SelectItem key={x} value={x} className="text-xs">{x}</SelectItem>)}
              </SelectContent></Select>
          </F>
          <F label="Data preparation">
            <Select value={prepMode} onValueChange={setPrepMode}>
              <SelectTrigger className="h-8 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="auto" className="text-xs">auto — detect stack / transpose</SelectItem>
                <SelectItem value="off" className="text-xs">off — one raw form as it is</SelectItem>
                <SelectItem value="custom" className="text-xs">custom — the pipeline</SelectItem>
              </SelectContent></Select>
          </F>
          <F label="Sort before --SEQ">
            <Input className="h-8 text-xs" value={sort} onChange={(e) => setSort(e.target.value)}
                   placeholder={`USUBJID, ${detail.domain}DTC`} />
          </F>
          <F label="Comparison keys">
            <Input className="h-8 text-xs" value={keys} onChange={(e) => setKeys(e.target.value)}
                   placeholder="auto — derived per domain" />
          </F>
        </div>
      </Panel>

      <Panel title="De-duplicate" description="Applied after the columns are built, before --SEQ numbering.">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <label className="flex items-center gap-2 text-xs">
            <Checkbox checked={ddOn} onCheckedChange={(v) => setDdOn(!!v)} />
            keep only one record per group
          </label>
          <F label="Group by">
            <Input className="h-8 text-xs" value={ddKeys} onChange={(e) => setDdKeys(e.target.value)}
                   placeholder="USUBJID, VISITNUM" />
          </F>
          <F label="Keep">
            <Select value={ddKeep} onValueChange={setDdKeep}>
              <SelectTrigger className="h-8 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="first" className="text-xs">first record</SelectItem>
                <SelectItem value="last" className="text-xs">last record</SelectItem>
              </SelectContent></Select>
          </F>
        </div>
      </Panel>

      <div className="flex items-center gap-3">
        <Button disabled={busy} onClick={() => void (async () => {
          setBusy(true)
          try {
            await api.domainSettings(detail.domain, {
              base, sort: split(sort), prep_mode: prepMode, keys: split(keys) })
            await api.domainDedup(detail.domain, { enabled: ddOn, keys: split(ddKeys), keep: ddKeep })
            await api.rebuild(detail.domain)
            setDirty(false)
            onDone()
          } finally { setBusy(false) }
        })()}>Save &amp; rebuild {detail.domain}</Button>
        {dirty && <span className="text-[11px] text-amber-600">unsaved changes — Save to keep them</span>}
      </div>
    </div>
  )
}

function F({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="space-y-1.5">
    <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</Label>
    {children}</div>
}
