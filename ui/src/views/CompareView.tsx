import { Download, FileText, GitCompare, Play } from "lucide-react"
import type { CompareRow, JobState } from "@/api"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
import { Chip, DataGrid, Mono, SegmentBar } from "@/components/grid"
import { Callout, EmptyState, Metric, Metrics, PageHeader, Panel } from "@/components/shell"
import { PathField } from "@/components/PathPicker"

const structural = (r: CompareRow) =>
  r.status === "differences" && !r.value_differences && !r.only_built && !r.only_vendor

export function CompareView({
  vendorPath, setVendorPath, rows, job, err, busy, ready, synthetic,
  ignoreCase, setIgnoreCase, ignoreVars, setIgnoreVars, onCompare, onBrowse,
  builtDomains, selected, setSelected,
}: {
  vendorPath: string; setVendorPath: (v: string) => void
  rows: CompareRow[] | null; job: JobState | null; err: string; busy: boolean; ready: boolean
  synthetic: boolean
  ignoreCase: boolean; setIgnoreCase: (v: boolean) => void
  ignoreVars: string; setIgnoreVars: (v: string) => void
  onCompare: () => void; onBrowse: () => void
  builtDomains: string[]
  selected: string[]; setSelected: (d: string[]) => void
}) {
  const identical = rows?.filter((r) => r.status === "identical").length ?? 0
  const differing = rows?.filter((r) => r.status === "differences" && !structural(r)).length ?? 0
  const structOnly = rows?.filter(structural).length ?? 0
  const errored = rows?.filter((r) => r.status === "error").length ?? 0
  const recDiff = rows?.reduce((a, r) => a + r.only_built + r.only_vendor, 0) ?? 0
  const valDiff = rows?.reduce((a, r) => a + r.value_differences, 0) ?? 0

  return (
    <>
      <PageHeader title="Vendor comparison"
        subtitle="Check the delivered SDTM against the datasets rebuilt here, record by record and value by value." />

      <div className="space-y-4">
        <Panel title="Vendor delivery" description="Folder holding the delivered SDTM datasets.">
          <div className="flex flex-wrap gap-2">
            <div className="min-w-72 flex-1">
              <PathField value={vendorPath} onChange={setVendorPath} mode="dir"
                         placeholder="/path/to/vendor_sdtm" onBrowse={onBrowse} />
            </div>
            <Button onClick={onCompare} disabled={busy || !vendorPath || !ready}>
              <Play className="mr-1.5 h-3.5 w-3.5" />{busy ? "Comparing…" : "Run comparison"}
            </Button>
          </div>
          {builtDomains.length > 0 && (
            <div className="mt-4 space-y-1.5">
              <div className="flex items-baseline gap-2">
                <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
                  Domains to compare</span>
                <Button variant="link" size="sm" className="h-auto p-0 text-[11px]"
                        onClick={() => setSelected([])}>
                  {selected.length ? "compare all instead" : "all built domains"}
                </Button>
              </div>
              <div className="flex flex-wrap gap-1">
                {builtDomains.map((d) => {
                  const on = selected.includes(d)
                  return (
                    <button key={d} type="button"
                            onClick={() => setSelected(on
                              ? selected.filter((x) => x !== d) : [...selected, d])}
                            className={`rounded-md border px-2 py-1 text-xs transition
                              ${on ? "border-primary bg-primary/10 font-medium"
                                   : selected.length ? "opacity-45 hover:opacity-100" : "hover:bg-accent"}`}>
                      {d}
                    </button>
                  )
                })}
              </div>
              {selected.length > 0 && (
                <p className="text-[11px] text-muted-foreground">
                  Only {selected.join(", ")} (and their SUPP--) will be compared.
                </p>
              )}
            </div>
          )}
          <div className="mt-4 flex flex-wrap items-end gap-4">
            <label className="flex items-center gap-2 pb-2 text-xs">
              <Checkbox checked={ignoreCase} onCheckedChange={(v) => setIgnoreCase(!!v)} />
              ignore letter case
            </label>
            <div className="space-y-1.5">
              <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">
                Ignore variables</Label>
              <Input className="h-9 w-56 text-xs" value={ignoreVars} placeholder="e.g. AESEQ, VSSEQ"
                     onChange={(e) => setIgnoreVars(e.target.value)} />
            </div>
          </div>
        </Panel>

        {job && <Panel><Progress value={job.percent} className="h-1.5" />
          <p className="mt-2 text-xs text-muted-foreground">{job.message}</p></Panel>}
        {err && <Callout tone="bad">{err}</Callout>}
        {synthetic && (
          <Callout tone="bad" title="The build is from synthetic data — this is not evidence about the vendor">
            It tests the mapping logic only. Point the raw folder at the real extract before drawing
            any conclusion about a delivery.
          </Callout>
        )}

        {!rows?.length ? (
          <EmptyState icon={<GitCompare className="h-8 w-8" />} title="No comparison yet">
            {ready ? "Choose the vendor's SDTM folder and run the comparison."
                   : "Build the SDTM datasets first."}
          </EmptyState>
        ) : (
          <>
            <Metrics>
              <Metric value={identical} label="identical" tone="good" />
              <Metric value={differing} label="with differences" tone={differing ? "bad" : undefined} />
              <Metric value={structOnly} label="variables differ only" tone={structOnly ? "warn" : undefined} />
              <Metric value={errored} label="not comparable" tone={errored ? "warn" : undefined} />
              <Metric value={recDiff.toLocaleString()} label="record mismatches" tone={recDiff ? "bad" : "good"} />
              <Metric value={valDiff.toLocaleString()} label="value differences" tone={valDiff ? "bad" : "good"} />
            </Metrics>
            <SegmentBar segments={[
              { value: identical, tone: "green", label: "identical" },
              { value: structOnly, tone: "amber", label: "variables differ" },
              { value: differing, tone: "red", label: "differences" },
              { value: errored, tone: "slate", label: "not comparable" },
            ]} />

            <Panel title="Domains">
              <DataGrid rows={rows} height="26rem" rowKey={(r) => r.domain}
                cols={[
                  { id: "d", head: "Domain", kind: "key", sticky: true, width: 110, cell: (r) => r.domain },
                  { id: "s", head: "Status", kind: "tag", width: 150, cell: (r) =>
                      r.status === "error" ? <Chip tone="slate">{r.error}</Chip>
                      : r.status === "identical" ? <Chip tone="green">identical</Chip>
                      : structural(r) ? <Chip tone="amber">variables differ</Chip>
                      : <Chip tone="red">differences</Chip> },
                  { id: "rb", head: "Built", kind: "number", align: "right",
                    cell: (r) => r.rows_built.toLocaleString() },
                  { id: "rv", head: "Vendor", kind: "number", align: "right",
                    cell: (r) => r.rows_vendor.toLocaleString() },
                  { id: "m", head: "Matched", kind: "number", align: "right",
                    cell: (r) => r.status === "error" ? "—" : r.matched.toLocaleString() },
                  { id: "ob", head: "Only built", kind: "number", align: "right",
                    cell: (r) => r.only_built ? <Chip tone="red">{r.only_built}</Chip>
                      : (r.status === "error" ? "—" : "0") },
                  { id: "ov", head: "Only vendor", kind: "number", align: "right",
                    cell: (r) => r.only_vendor ? <Chip tone="red">{r.only_vendor}</Chip>
                      : (r.status === "error" ? "—" : "0") },
                  { id: "vd", head: "Value diffs", kind: "number", align: "right",
                    cell: (r) => r.value_differences ? <Chip tone="red">{r.value_differences}</Chip>
                      : (r.status === "error" ? "—" : "0") },
                  { id: "k", head: "Matched on", kind: "code",
                    cell: (r) => r.keys.length ? <Mono>{r.keys.join(", ")}</Mono> : null },
                ]} />
            </Panel>

            <Accordion type="multiple" className="space-y-3">
              {rows.filter((r) => r.status === "differences").map((r) => (
                <AccordionItem key={r.domain} value={r.domain} className="rounded-xl border bg-surface px-4">
                  <AccordionTrigger className="text-[14px]">
                    <span className="flex items-center gap-2">{r.domain}
                      {r.value_differences > 0 && <Chip tone="red">{r.value_differences} value diffs</Chip>}
                      {r.only_built > 0 && <Chip tone="amber">{r.only_built} only built</Chip>}
                      {r.only_vendor > 0 && <Chip tone="amber">{r.only_vendor} only vendor</Chip>}
                    </span>
                  </AccordionTrigger>
                  <AccordionContent className="space-y-3 pb-4">
                    {r.key_note && <p className="text-[12px] text-muted-foreground">{r.key_note}</p>}
                    {r.notes.map((n, i) => <p key={i} className="text-[12px] text-muted-foreground">{n}</p>)}
                    {r.vars_only_vendor.length > 0 && <Callout tone="warn"
                      title="Variables only in the vendor dataset"><Mono>{r.vars_only_vendor.join(", ")}</Mono></Callout>}
                    {r.vars_only_built.length > 0 && <Callout tone="warn"
                      title="Variables only in the built dataset"><Mono>{r.vars_only_built.join(", ")}</Mono></Callout>}
                    {r.not_built.length > 0 && <Callout title="Not built here, so not compared">
                      <Mono>{r.not_built.join(", ")}</Mono></Callout>}

                    {r.variables.length > 0 && (
                      <DataGrid rows={r.variables} height="16rem" rowKey={(v) => v.variable}
                        cols={[
                          { id: "v", head: "Variable", kind: "key", sticky: true, cell: (v) => v.variable },
                          { id: "d", head: "Differing", kind: "number", align: "right",
                            cell: (v) => <Chip tone="red">{v.differing}</Chip> },
                          { id: "c", head: "Compared", kind: "number", align: "right",
                            cell: (v) => v.compared.toLocaleString() },
                          { id: "a", head: "Agreement", kind: "number", align: "right",
                            cell: (v) => `${v.agreement}%` },
                          { id: "ob", head: "Only in build", kind: "number", align: "right",
                            cell: (v) => v.only_built_nonblank || "" },
                          { id: "ov", head: "Only in vendor", kind: "number", align: "right",
                            cell: (v) => v.only_vendor_nonblank || "" },
                        ]} />
                    )}

                    {r.variables.some((v) => v.examples.length) && (
                      <DataGrid height="16rem" rowNumbers={false}
                        rows={r.variables.flatMap((v) => v.examples.slice(0, 3).map((ex) => ({ v: v.variable, ex })))}
                        cols={[
                          { id: "v", head: "Variable", kind: "key", cell: (x) => x.v },
                          { id: "k", head: "Record", kind: "text", width: 320, cell: (x) => Object.entries(x.ex)
                              .filter(([k]) => k !== "built" && k !== "vendor")
                              .map(([k, val]) => `${k}=${val}`).join(" · ") },
                          { id: "b", head: "Built", kind: "code", cell: (x) => <Mono>{x.ex.built}</Mono> },
                          { id: "vd", head: "Vendor", kind: "code", cell: (x) => <Mono>{x.ex.vendor}</Mono> },
                        ]} />
                    )}
                  </AccordionContent>
                </AccordionItem>
              ))}
            </Accordion>

            <Panel title="Reports">
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" asChild>
                  <a href="/api/download?name=comparison">
                    <Download className="mr-1.5 h-3.5 w-3.5" />Comparison workbook</a>
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
