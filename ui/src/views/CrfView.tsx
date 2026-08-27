// The annotated-CRF check: every SDTM annotation pulled out of the aCRF PDF and held
// against the standards mapping (and the TA spec) — what is off-standard, what to do
// about it, and what the standards expected on the CRF that never appears.
import { useEffect, useState } from "react"
import { Download, FileSearch, Play } from "lucide-react"
import { api } from "@/api"
import type { AcrfReport } from "@/api"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { PathField, PathPicker } from "@/components/PathPicker"
import { Chip, ClientGrid, Mono } from "@/components/grid"
import type { ChipTone } from "@/components/grid"
import { Callout, Metric, Metrics, Panel } from "@/components/shell"

const PDF_RE = /\.pdf$/i

const VERDICT: Record<string, { label: string; tone: ChipTone }> = {
  matched: { label: "per standard", tone: "green" },
  ta_only: { label: "TA spec only", tone: "amber" },
  supp: { label: "supplemental", tone: "blue" },
  not_in_standard: { label: "not in standard", tone: "red" },
  unknown_domain: { label: "unknown domain", tone: "red" },
  note: { label: "not submitted", tone: "slate" },
}

export function CrfView() {
  const [acrf, setAcrf] = useState("")
  const [standards, setStandards] = useState("")
  const [ta, setTa] = useState("")
  const [report, setReport] = useState<AcrfReport | null>(null)
  const [browse, setBrowse] = useState<"" | "acrf" | "standards" | "ta">("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState("")

  useEffect(() => {
    void api.getAcrf().then((r) => {
      setAcrf(r.acrf); setStandards(r.standards); setTa(r.ta); setReport(r.report)
    }).catch(() => {})
  }, [])

  const run = async () => {
    setBusy(true); setErr("")
    try {
      const r = await api.runAcrf({ acrf, standards, ta })
      setReport(r.report)
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  const c = report?.counts ?? {}
  return (
    <div className="space-y-4">
      <Panel title="Annotated CRF vs the standards"
             description="Point at the aCRF PDF and the standards mapping (the TA spec is optional). Every SDTM annotation is extracted — annotation boxes and flattened text — and judged against the specs; the reverse look lists what the standards collect on the CRF that was never annotated."
             actions={
               <div className="flex items-center gap-2">
                 {report && (
                   <Button variant="outline" onClick={() => { window.location.href = "/api/acrf/export" }}>
                     <Download className="mr-1.5 h-3.5 w-3.5" />Export to Excel
                   </Button>)}
                 <Button onClick={() => void run()} disabled={busy || !acrf || !standards}>
                   <Play className="mr-1.5 h-3.5 w-3.5" />{busy ? "Checking…" : "Run the check"}
                 </Button>
               </div>}>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Annotated CRF (.pdf)</Label>
            <PathField value={acrf} onChange={setAcrf} mode="file"
                       placeholder="/path/to/acrf.pdf" onBrowse={() => setBrowse("acrf")} />
          </div>
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Standards mapping (.xlsx)</Label>
            <PathField value={standards} onChange={setStandards} mode="file"
                       placeholder="/path/to/standards_mapping.xlsx" onBrowse={() => setBrowse("standards")} />
          </div>
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">TA spec (.xlsx — optional)</Label>
            <PathField value={ta} onChange={setTa} mode="file"
                       placeholder="/path/to/ta_spec.xlsx" onBrowse={() => setBrowse("ta")} />
          </div>
        </div>
        {err && <div className="mt-3"><Callout tone="bad">{err}</Callout></div>}
      </Panel>

      <PathPicker open={browse !== ""} mode="file"
                  accept={browse === "acrf" ? PDF_RE : undefined}
                  start={(browse === "acrf" ? acrf : browse === "standards" ? standards : ta) || acrf || standards}
                  onClose={() => setBrowse("")}
                  onPick={(p) => {
                    if (browse === "acrf") setAcrf(p)
                    else if (browse === "standards") setStandards(p)
                    else setTa(p)
                    setBrowse("")
                  }} />

      {report && (
        <>
          {(report.notes ?? []).map((n, i) => (
            <Callout key={i} tone="warn">{n}</Callout>
          ))}
          <Metrics>
            <Metric value={report.pages} label="CRF pages" />
            <Metric value={c.annotations ?? 0} label="annotations" />
            <Metric value={c.matched ?? 0} label="per standard" tone="good" />
            {(c.ta_only ?? 0) > 0 && <Metric value={c.ta_only} label="TA spec only" tone="warn" />}
            {(c.supp ?? 0) > 0 && <Metric value={c.supp} label="supplemental" />}
            <Metric value={c.off_standard ?? 0} label="off standard"
                    tone={(c.off_standard ?? 0) > 0 ? "bad" : "good"} />
            <Metric value={c.missing ?? 0} label="never annotated"
                    tone={(c.missing ?? 0) > 0 ? "warn" : "good"} />
          </Metrics>

          <Panel title="Every annotation, judged"
                 description={`Domains annotated: ${report.domains_annotated.join(", ") || "none recognised"}. Filter any column; the advice says what to do about each finding.`}>
            <ClientGrid rows={report.rows} height="26rem"
              rowKey={(r, i) => `${r.page}-${r.variable}-${i}`}
              cols={[
                { id: "p", head: "Page", width: 70, align: "right",
                  value: (r) => String(r.page), cell: (r) => r.page },
                { id: "f", head: "Form", width: 160,
                  value: (r) => r.form ?? "", cell: (r) => <span className="text-xs">{r.form || "—"}</span> },
                { id: "a", head: "Annotation", kind: "key", width: 200, sticky: true,
                  value: (r) => r.domain && r.variable ? `${r.domain}.${r.variable}` : (r.variable || r.value),
                  cell: (r) => <Mono>{r.domain && r.variable ? `${r.domain}.${r.variable}` : (r.variable || r.value)}</Mono> },
                { id: "q", head: "CRF question", width: 240,
                  value: (r) => r.question ?? "",
                  cell: (r) => <span className="text-xs">{r.question || "—"}</span> },
                { id: "val", head: "Value", width: 130,
                  value: (r) => r.value, cell: (r) => r.value },
                { id: "v", head: "Verdict", kind: "tag", width: 140,
                  value: (r) => VERDICT[r.verdict]?.label ?? r.verdict,
                  cell: (r) => <Chip tone={VERDICT[r.verdict]?.tone ?? "slate"}>
                    {VERDICT[r.verdict]?.label ?? r.verdict}</Chip> },
                { id: "adv", head: "What to do", width: 420,
                  value: (r) => r.advice, cell: (r) => <span className="text-xs">{r.advice || "—"}</span> },
                { id: "t", head: "As annotated", width: 260,
                  value: (r) => r.text,
                  cell: (r) => <span className="text-[11px] text-muted-foreground">{r.text}</span> },
              ]} />
          </Panel>

          <Panel title="In the standards, never annotated"
                 description={report.origins_recorded
                   ? "Variables the standards collect on the CRF for the annotated domains, absent from the aCRF."
                   : "Variables the standards define for the annotated domains, absent from the aCRF. This standards workbook records no origins, so derived variables appear here too — read with that in mind."}>
            {report.missing.length ? (
              <ClientGrid rows={report.missing} height="20rem"
                rowKey={(r) => `${r.domain}.${r.variable}`}
                cols={[
                  { id: "d", head: "Domain", kind: "key", width: 90, sticky: true,
                    value: (r) => r.domain, cell: (r) => r.domain },
                  { id: "v", head: "Variable", kind: "key", width: 120,
                    value: (r) => r.variable, cell: (r) => <Mono>{r.variable}</Mono> },
                  { id: "l", head: "Label", width: 220,
                    value: (r) => r.label, cell: (r) => r.label },
                  { id: "o", head: "Origin", width: 110,
                    value: (r) => r.origin, cell: (r) => r.origin || "—" },
                  { id: "adv", head: "What to do", width: 420,
                    value: (r) => r.advice, cell: (r) => <span className="text-xs">{r.advice}</span> },
                ]} />
            ) : (
              <p className="text-xs text-muted-foreground">
                Nothing — every expected variable of the annotated domains appears on the aCRF.</p>
            )}
          </Panel>
        </>
      )}

      {!report && !busy && (
        <div className="flex items-center gap-2 rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          <FileSearch className="h-4 w-4" />
          No check has been run yet in this study.
        </div>
      )}
    </div>
  )
}
