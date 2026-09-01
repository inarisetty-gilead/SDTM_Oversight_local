import { Database, Download, FileText, FolderOpen, Play } from "lucide-react"
import type { BuildResults, DomainRow, JobState } from "@/api"
import { api } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
import { Chip, DataGrid, Mono, SegmentBar } from "@/components/grid"
import { Callout, EmptyState, Metric, Metrics, PageHeader, Panel } from "@/components/shell"

export function BuildView({
  build, job, err, busy, opts, setOpts, onBuild, onOpenDomain, ready,
  specDomains, inactive, selected, setSelected,
}: {
  build: BuildResults | null; job: JobState | null; err: string; busy: boolean
  opts: { fmt: string; studyid: string; nameMatch: string; structure: string }
  setOpts: (o: Partial<{ fmt: string; studyid: string; nameMatch: string; structure: string }>) => void
  onBuild: () => void; onOpenDomain: (d: string) => void; ready: boolean
  specDomains: string[]; inactive: string[]
  selected: string[]; setSelected: (d: string[]) => void
}) {
  const ok = build?.domains.filter((d) => d.ok) ?? []
  const failed = build?.domains.filter((d) => !d.ok) ?? []
  const built = ok.reduce((a, d) => a + d.built, 0)
  const guessed = ok.reduce((a, d) => a + d.name_matched, 0)
  const notBuilt = ok.reduce((a, d) => a + d.not_built, 0)
  const dropped = ok.reduce((a, d) => a + d.dropped, 0)

  return (
    <>
      <PageHeader title="Build"
        subtitle="Rebuild every domain the spec defines, from the raw data — independently of the vendor."
        actions={
          <Button onClick={onBuild} disabled={busy || !ready}>
            <Play className="mr-1.5 h-3.5 w-3.5" />
            {busy ? "Building…"
              : selected.length === 1 ? `Build ${selected[0]}`
              : selected.length ? `Build ${selected.length} domains`
              : "Build all domains"}
          </Button>} />

      <div className="space-y-4">
        {specDomains.length > 0 && (
          <Panel title="Domains to build"
                 description="Pick one or a few to work one domain at a time — each build ADDS to what is already built, it never wipes the others. Nothing selected builds every active domain.">
            <div className="flex flex-wrap items-center gap-1">
              {specDomains.map((d) => {
                const on = selected.includes(d)
                const off = inactive.includes(d)
                const built = build?.domains.some((x) => x.domain === d && x.ok)
                return (
                  <button key={d} type="button"
                          onClick={() => setSelected(on ? selected.filter((x) => x !== d)
                                                        : [...selected, d])}
                          title={off ? "Active = N in the TOC — buildable by selecting it" : ""}
                          className={`rounded-md border px-2 py-1 text-xs transition
                            ${on ? "border-primary bg-primary/10 font-medium"
                                 : selected.length ? "opacity-45 hover:opacity-100" : "hover:bg-accent"}
                            ${off && !on ? "opacity-35" : ""}`}>
                    {d}{built && <span className="ml-1 text-emerald-600">✓</span>}
                  </button>
                )
              })}
              {selected.length > 0 && (
                <Button variant="link" size="sm" className="h-auto p-0 pl-2 text-[11px]"
                        onClick={() => setSelected([])}>clear — build all active</Button>
              )}
            </div>
          </Panel>
        )}

        <Panel title="Options">
          <div className="flex flex-wrap items-end gap-4">
            <Field label="Output format">
              <Pick value={opts.fmt} onChange={(v) => setOpts({ fmt: v })} width="w-52" options={[
                ["xpt", "SAS transport (.xpt v5)"], ["csv", "CSV"], ["parquet", "Parquet"],
                ["none", "None — in memory only"]]} />
            </Field>
            <Field label="Dataset structure">
              <Pick value={opts.structure} onChange={(v) => setOpts({ structure: v })} width="w-64" options={[
                ["full", "full — every variable the spec defines"],
                ["populated", "populated only — omit what was not built"]]} />
            </Field>
            <Field label="Name matching">
              <Pick value={opts.nameMatch} onChange={(v) => setOpts({ nameMatch: v })} width="w-56" options={[
                ["70", "on — 70% name similarity"], ["85", "strict — 85%"],
                ["45", "loose — 45%"], ["0", "off — only what the spec states"]]} />
            </Field>
            <Field label="STUDYID override">
              <Input className="h-9 w-44 text-xs" value={opts.studyid}
                     placeholder="only if raw has none"
                     onChange={(e) => setOpts({ studyid: e.target.value })} />
            </Field>
          </div>
        </Panel>

        {job && (
          <Panel>
            <Progress value={job.percent} className="h-1.5" />
            <p className="mt-2 text-xs text-muted-foreground">
              {job.message}{job.total ? ` (${job.step}/${job.total})` : ""}
            </p>
          </Panel>
        )}

        {err && <Callout tone="bad">{err}</Callout>}
        {build?.synthetic && (
          <Callout tone="warn" title="This build stands on synthetic raw data">
            {build.synthetic.warning}
          </Callout>
        )}

        {!build?.domains.length ? (
          <EmptyState icon={<Database className="h-8 w-8" />} title="Nothing built yet"
            action={<Button onClick={onBuild} disabled={!ready}>
              <Play className="mr-1.5 h-3.5 w-3.5" />Build all domains</Button>}>
            {ready ? "Run the build to rebuild every domain from the raw data."
                   : "Load a mapping spec and scan a raw folder first."}
          </EmptyState>
        ) : (
          <>
            <Metrics>
              <Metric value={ok.length} label="domains built" />
              <Metric value={ok.reduce((a, d) => a + d.rows, 0).toLocaleString()} label="records" />
              <Metric value={built.toLocaleString()} label="variables built" tone="good" />
              <Metric value={guessed.toLocaleString()} label="matched by name"
                      tone={guessed ? "warn" : undefined} />
              <Metric value={notBuilt.toLocaleString()} label="not built"
                      tone={notBuilt ? "warn" : "good"} />
              <Metric value={failed.length} label="build error" tone={failed.length ? "bad" : undefined} />
            </Metrics>

            <SegmentBar segments={[
              { value: built - guessed, tone: "green", label: "built from the spec" },
              { value: guessed, tone: "amber", label: "matched by name" },
              { value: dropped, tone: "slate", label: "dropped by the spec" },
              { value: notBuilt, tone: "red", label: "not built" },
            ]} />

            <Panel title="Domains" description="Click a domain to inspect its variables and edit its mapping.">
              <DataGrid rows={build.domains} height="30rem" rowKey={(d) => d.domain}
                groupBy={(d) => d.ok ? "Built" : "Failed to build"}
                onRowClick={(d) => onOpenDomain(d.domain)}
                cols={domainCols()} />
            </Panel>

            {build.not_built_reasons?.length > 0 && (
              <Accordion type="single" collapsible defaultValue="why">
                <AccordionItem value="why" className="rounded-xl border bg-surface px-4">
                  <AccordionTrigger className="text-[14px]">
                    Why {notBuilt.toLocaleString()} variable(s) were not built
                  </AccordionTrigger>
                  <AccordionContent className="space-y-3 pb-4">
                    <p className="text-[13px] text-muted-foreground">
                      Coverage is limited by what the spec states, not by the engine. Each group is a
                      different cause, and most have a fix.
                    </p>
                    <DataGrid rows={build.not_built_reasons} height="18rem" rowNumbers={false}
                      cols={[
                        { id: "r", head: "Cause", kind: "text", width: 420, cell: (r) => r.reason },
                        { id: "c", head: "Variables", kind: "number", align: "right", cell: (r) => r.count },
                        { id: "e", head: "For example", kind: "code", cell: (r) => (
                            <span className="flex flex-wrap gap-1">
                              {r.examples.map((x) => <Mono key={x}>{x}</Mono>)}</span>) },
                      ]} />
                  </AccordionContent>
                </AccordionItem>
              </Accordion>
            )}

            <Panel title="Output" description={build.out_dir}>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" onClick={() => void api.reveal()}>
                  <FolderOpen className="mr-1.5 h-3.5 w-3.5" />Open folder
                </Button>
                <Button variant="outline" size="sm" asChild>
                  <a href="/api/download?name=manifest">
                    <Download className="mr-1.5 h-3.5 w-3.5" />Build manifest</a>
                </Button>
                <Button variant="outline" size="sm" asChild>
                  <a href="/api/report" target="_blank" rel="noreferrer">
                    <FileText className="mr-1.5 h-3.5 w-3.5" />Full report</a>
                </Button>
              </div>
            </Panel>
          </>
        )}
      </div>
    </>
  )
}

export function domainCols() {
  const num = (v: number) => v ? v.toLocaleString() : "0"
  return [
    { id: "d", head: "Domain", kind: "key" as const, sticky: true, width: 96,
      cell: (d: DomainRow) => d.domain },
    { id: "r", head: "Records", kind: "number" as const, align: "right" as const,
      cell: (d: DomainRow) => d.ok ? d.rows.toLocaleString() : "—" },
    { id: "s", head: "SUPP", kind: "number" as const, align: "right" as const,
      cell: (d: DomainRow) => d.supp_rows ? d.supp_rows.toLocaleString() : "" },
    { id: "b", head: "Built", kind: "number" as const, align: "right" as const,
      cell: (d: DomainRow) => d.ok ? <Chip tone="green">{num(d.built)}</Chip> : "—" },
    { id: "nm", head: "Name matched", kind: "number" as const, align: "right" as const,
      cell: (d: DomainRow) => d.name_matched ? <Chip tone="amber">{d.name_matched}</Chip> : "" },
    { id: "e", head: "Hand edits", kind: "number" as const, align: "right" as const,
      cell: (d: DomainRow) => d.edited ? <Chip tone="blue">{d.edited}</Chip> : "" },
    { id: "dr", head: "Dropped", kind: "number" as const, align: "right" as const,
      cell: (d: DomainRow) => d.ok ? d.dropped : "—" },
    { id: "nb", head: "Not built", kind: "number" as const, align: "right" as const,
      cell: (d: DomainRow) => d.ok ? (d.not_built ? <Chip tone="amber">{d.not_built}</Chip> : "0") : "—" },
    { id: "em", head: "Empty", kind: "number" as const, align: "right" as const,
      cell: (d: DomainRow) => d.empty ? <Chip tone="amber">{d.empty}</Chip> : "" },
    { id: "p", head: "Prepared", kind: "tag" as const,
      cell: (d: DomainRow) => d.prep
        ? <Chip tone="violet">{d.prep.op === "stack" ? "stacked" : "transposed"}</Chip> : "" },
    { id: "base", head: "Record source / error", kind: "code" as const,
      cell: (d: DomainRow) => d.ok ? <Mono>{d.base}</Mono>
        : <span className="text-[12px] text-destructive">{d.error}</span> },
  ]
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</Label>
      {children}
    </div>
  )
}

function Pick({ value, onChange, options, width }: {
  value: string; onChange: (v: string) => void; options: [string, string][]; width: string
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className={`h-9 ${width} text-xs`}><SelectValue /></SelectTrigger>
      <SelectContent>
        {options.map(([v, l]) => <SelectItem key={v} value={v} className="text-xs">{l}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}
