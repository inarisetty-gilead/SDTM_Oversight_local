import { useState } from "react"
import { Sparkles } from "lucide-react"
import { api } from "@/api"
import type { RawInfo, SpecInfo } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Chip, DataGrid, Mono, SegmentBar } from "@/components/grid"
import { Callout, Metric, Metrics, PageHeader, Panel } from "@/components/shell"
import { PathField } from "@/components/PathPicker"

export function SetupView({
  specPath, setSpecPath, rawPath, setRawPath, spec, raw, err, busy,
  onSpec, onRaw, onBrowse, onSynth,
}: {
  specPath: string; setSpecPath: (v: string) => void
  rawPath: string; setRawPath: (v: string) => void
  spec: SpecInfo | null; raw: RawInfo | null
  err: Record<string, string>; busy: string
  onSpec: () => void; onRaw: () => void
  onBrowse: (target: "spec" | "raw", mode: "file" | "dir") => void
  onSynth: (dir: string) => void
}) {
  const cov = spec?.coverage?.totals ?? {}
  const hasCov = Object.keys(cov).length > 0

  return (
    <>
      <PageHeader title="Study setup"
        subtitle="Point the tool at the mapping spec and the raw extract. Everything after this reads only those two." />

      <div className="space-y-4">
        <Panel title="Mapping specification"
               description="One worksheet per SDTM domain. A title row above the headings is handled.">
          <div className="flex flex-wrap gap-2">
            <div className="min-w-72 flex-1">
              <PathField value={specPath} onChange={setSpecPath} mode="file"
                         placeholder="/path/to/MAPPING_SPEC.xlsx"
                         onBrowse={() => onBrowse("spec", "file")} />
            </div>
            <Button onClick={onSpec} disabled={busy === "spec" || !specPath}>
              {busy === "spec" ? "Reading…" : "Load spec"}
            </Button>
          </div>
          {err.spec && <div className="mt-3"><Callout tone="bad">{err.spec}</Callout></div>}

          {spec && (
            <div className="mt-4 space-y-4">
              <Metrics>
                <Metric value={spec.domains.length} label="domains" />
                {spec.variables ? <Metric value={spec.variables.toLocaleString()} label="variables" /> : null}
                {hasCov && <Metric value={`${cov.pct_actionable}%`} label="the spec can build"
                                   tone={cov.pct_actionable >= 60 ? "good" : "warn"} />}
                {hasCov && <Metric value={cov.with_source.toLocaleString()} label="from a raw source" />}
                {hasCov && <Metric value={cov.derived.toLocaleString()} label="derived" />}
                {hasCov && <Metric value={cov.needs_a_source.toLocaleString()} label="no source stated"
                                   tone="warn" />}
              </Metrics>

              {hasCov && <>
                <SegmentBar segments={[
                  { value: cov.actionable, tone: "green", label: "the spec can build" },
                  { value: cov.dropped_by_spec, tone: "slate", label: "the spec says DROP" },
                  { value: cov.needs_a_source, tone: "amber", label: "no source stated" },
                ]} />
                <Callout title="This is the ceiling on coverage, before any raw data is read.">
                  {cov.assign_without_source.toLocaleString()} rows say ASSIGN with a blank source and{" "}
                  {cov.blank_rows.toLocaleString()} are entirely blank. Those can still be picked up by
                  name matching when you build, or mapped by hand in a domain.
                </Callout>
              </>}

              <div className="flex flex-wrap gap-1">
                {spec.domains.map((d) => <Chip key={d} tone="violet">{d}</Chip>)}
              </div>
            </div>
          )}
        </Panel>

        <Panel title="Raw datasets"
               description="Folder of extracts — .sas7bdat, .xpt, .csv, .xlsx, .parquet. Subfolders are searched."
               actions={spec ? <SynthButton onGenerated={onSynth} /> : undefined}>
          <div className="flex flex-wrap gap-2">
            <div className="min-w-72 flex-1">
              <PathField value={rawPath} onChange={setRawPath} mode="dir"
                         placeholder="/path/to/rawdata" onBrowse={() => onBrowse("raw", "dir")} />
            </div>
            <Button onClick={onRaw} disabled={busy === "raw" || !rawPath || !spec}>
              {busy === "raw" ? "Scanning…" : "Scan raw data"}
            </Button>
          </div>
          {err.raw && <div className="mt-3"><Callout tone="bad">{err.raw}</Callout></div>}

          {raw && raw.datasets.length > 0 && (
            <div className="mt-4 space-y-4">
              {raw.synthetic && (
                <Callout tone="warn" title="This folder holds synthetic data">
                  {raw.synthetic.subjects} subjects, {raw.synthetic.visits} visits, generated from the
                  spec. Good for checking that the spec builds — not a basis for judging a vendor.
                </Callout>
              )}
              <Metrics>
                <Metric value={raw.datasets.length} label="datasets" />
                <Metric value={raw.datasets.reduce((a, d) => a + (d.rows ?? 0), 0).toLocaleString()}
                        label="records" />
                <Metric value={raw.coverage.filter((c) => c.resolved > 0).length} label="domains with sources" />
                <Metric value={raw.missing.length} label="missing sources"
                        tone={raw.missing.length ? "warn" : "good"} />
              </Metrics>

              {raw.missing.length > 0 ? (
                <Callout tone="warn"
                         title={`${raw.missing.length} source(s) the spec names are not in this folder`}>
                  <ul className="mt-1 space-y-0.5">
                    {raw.missing.slice(0, 8).map((m) => (
                      <li key={m.source}><Mono>{m.source}</Mono> — used by {m.used_by.slice(0, 5).join(", ")}</li>
                    ))}
                    {raw.missing.length > 8 && <li>… and {raw.missing.length - 8} more</li>}
                  </ul>
                </Callout>
              ) : (
                <Callout tone="good" title="Every source the spec names resolves." />
              )}

              <DataGrid height="22rem" rows={raw.datasets} rowKey={(d) => d.name}
                cols={[
                  { id: "n", head: "Dataset", kind: "key", sticky: true, cell: (d) => d.name },
                  { id: "r", head: "Records", kind: "number", align: "right",
                    cell: (d) => d.rows?.toLocaleString() ?? "—" },
                  { id: "c", head: "Columns", kind: "number", align: "right", cell: (d) => d.cols ?? "—" },
                  { id: "f", head: "File", kind: "text", cell: (d) => <Mono>{d.file}</Mono> },
                  { id: "e", head: "", kind: "text",
                    cell: (d) => d.error ? <Chip tone="red">{d.error}</Chip> : null },
                ]} />
            </div>
          )}
        </Panel>
      </div>
    </>
  )
}

function SynthButton({ onGenerated }: { onGenerated: (dir: string) => void }) {
  const [open, setOpen] = useState(false)
  const [subjects, setSubjects] = useState("40")
  const [visits, setVisits] = useState("5")
  const [studyid, setStudyid] = useState("SYNTH-001")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  if (!open) {
    return (
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <Sparkles className="mr-1.5 h-3.5 w-3.5" />No raw data yet?
      </Button>
    )
  }
  return (
    <div className="flex flex-wrap items-end gap-2 rounded-lg border border-dashed p-2">
      <div className="space-y-1">
        <Label className="text-[10px] uppercase text-muted-foreground">Subjects</Label>
        <Input className="h-8 w-20 text-xs" value={subjects} onChange={(e) => setSubjects(e.target.value)} />
      </div>
      <div className="space-y-1">
        <Label className="text-[10px] uppercase text-muted-foreground">Visits</Label>
        <Input className="h-8 w-16 text-xs" value={visits} onChange={(e) => setVisits(e.target.value)} />
      </div>
      <div className="space-y-1">
        <Label className="text-[10px] uppercase text-muted-foreground">STUDYID</Label>
        <Input className="h-8 w-36 text-xs" value={studyid} onChange={(e) => setStudyid(e.target.value)} />
      </div>
      <Button size="sm" disabled={busy} onClick={() => void (async () => {
        setBusy(true); setError("")
        try {
          const r = await api.synth({ subjects: Number(subjects) || 40, visits: Number(visits) || 5, studyid })
          onGenerated(r.out_dir); setOpen(false)
        } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
      })()}>{busy ? "Generating…" : "Generate from spec"}</Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      {error && <span className="text-xs text-destructive">{error}</span>}
    </div>
  )
}
