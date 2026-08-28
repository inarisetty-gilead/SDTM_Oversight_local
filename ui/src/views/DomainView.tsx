import { useCallback, useEffect, useState } from "react"
import { ArrowLeft, Search } from "lucide-react"
import { api } from "@/api"
import type { DomainDetail, JobState, VariableRow } from "@/api"
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
const ACTION_TONE: Record<string, ChipTone> = {
  ASSIGN: "green", CODE: "teal", DERIVED: "teal", DERIVE: "teal",
  DROP: "slate", SUPP: "violet", CONSTANT: "amber", HARDCODE: "amber",
}

export function DomainView({ domain, onBack, onChanged }: {
  domain: string; onBack: () => void; onChanged: () => void
}) {
  const [d, setD] = useState<DomainDetail | null>(null)
  const [filter, setFilter] = useState<Filter>("all")
  const [search, setSearch] = useState("")
  const [group, setGroup] = useState("none")
  const [editing, setEditing] = useState<VariableRow | null>(null)
  const [pane, setPane] = useState<"data" | "edit">("data")
  const [job, setJob] = useState<JobState | null>(null)
  const [error, setError] = useState("")
  // bumped after any rebuild so the data view reloads rather than showing stale records
  const [dataKey, setDataKey] = useState(0)
  const records = useDomainRecords(domain, dataKey)

  const load = useCallback(async (dom: string) => {
    setError("")
    try { setD(await api.domain(dom)) }
    catch (e) { setError((e as Error).message); setD(null) }
  }, [])
  useEffect(() => { setEditing(null); setPane("data"); void load(domain) }, [domain, load])

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
        actions={<Button variant="ghost" onClick={onBack}>
          <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />All domains</Button>} />

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
              <div className="relative ml-auto min-w-56 flex-1">
                <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
                <Input className="h-8 pl-8 text-xs" placeholder="Filter by variable, label or source"
                       value={search} onChange={(e) => setSearch(e.target.value)} />
              </div>
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
                { id: "l", head: "Label", kind: "text", width: 210,
                  cell: (v) => <span className="text-muted-foreground">{v.label}</span> },
                // the spec's verdict for the row — ASSIGN / CODE / DERIVED / DROP / SUPP,
                // prominent like SDTM Designer's "Mapping Action" column
                { id: "act", head: "Action", kind: "tag",
                  cell: (v) => v.spec_action ? <Chip tone={ACTION_TONE[v.spec_action.toUpperCase()] ?? "blue"}>
                    {v.spec_action.toUpperCase()}</Chip> : null },
                { id: "cl", head: "Codelist", kind: "tag",
                  cell: (v) => v.codelist ? <Chip tone="teal">{v.codelist}</Chip> : null },
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
                  {d.domain}{editing ? ` · ${editing.variable} highlighted` : ""}
                </span>
              </div>
              {records.error
                ? <Callout tone="bad">{records.error}</Callout>
                : <RecordTable page={records.page} busy={records.busy} height="26rem"
                               highlight={editing?.variable}
                               onRefresh={() => void records.reload()} />}
            </div>
          </TabsContent>

          <TabsContent value="data" className="pt-4">
            <DataView domain={d.domain} datasets={d.datasets} refreshKey={dataKey} />
          </TabsContent>

          <TabsContent value="prepare" className="pt-4">
            <PipelineEditor detail={d} onDone={() => void afterRebuild()} />
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

      <Button disabled={busy} onClick={() => void (async () => {
        setBusy(true)
        try {
          await api.domainSettings(detail.domain, {
            base, sort: split(sort), prep_mode: prepMode, keys: split(keys) })
          await api.domainDedup(detail.domain, { enabled: ddOn, keys: split(ddKeys), keep: ddKeep })
          await api.rebuild(detail.domain); onDone()
        } finally { setBusy(false) }
      })()}>Save &amp; rebuild {detail.domain}</Button>
    </div>
  )
}

function F({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="space-y-1.5">
    <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</Label>
    {children}</div>
}
