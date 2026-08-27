// The annotated-CRF check: every SDTM annotation pulled out of the aCRF PDF and held
// against the standards mapping (and the TA spec) — what is off-standard, what to do
// about it, and what the standards expected on the CRF that never appears.
import { useEffect, useState } from "react"
import { Download, FileSearch, GitCompare, Play } from "lucide-react"
import { api } from "@/api"
import type { AcrfReport, CrfCmp } from "@/api"
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
  const [ecrf, setEcrf] = useState("")
  const [report, setReport] = useState<AcrfReport | null>(null)
  const [stdAcrf, setStdAcrf] = useState("")
  const [stdEcrf, setStdEcrf] = useState("")
  const [cmp, setCmp] = useState<CrfCmp | null>(null)
  const [browse, setBrowse] = useState<"" | "acrf" | "standards" | "ta" | "ecrf" | "std_acrf" | "std_ecrf">("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState("")

  useEffect(() => {
    void api.getAcrf().then((r) => {
      setAcrf(r.acrf); setStandards(r.standards); setTa(r.ta)
      setEcrf(r.ecrf ?? ""); setReport(r.report)
      setStdAcrf(r.std_acrf ?? ""); setStdEcrf(r.std_ecrf ?? ""); setCmp(r.cmp ?? null)
    }).catch(() => {})
  }, [])

  const run = async () => {
    setBusy(true); setErr("")
    try {
      const r = await api.runAcrf({ acrf, standards, ta, ecrf })
      setReport(r.report)
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  const runCmp = async () => {
    setBusy(true); setErr("")
    try {
      const r = await api.compareCrfs({ vendor: acrf, standard: stdAcrf,
        vendor_ecrf: ecrf, standard_ecrf: stdEcrf, standards })
      setCmp(r.cmp)
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  const c = report?.counts ?? {}
  const cc = cmp?.counts ?? {}
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
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">eCRF spec (.xlsx — optional, supplies the question text)</Label>
            <PathField value={ecrf} onChange={setEcrf} mode="file"
                       placeholder="/path/to/ecrf_spec.xlsx" onBrowse={() => setBrowse("ecrf")} />
          </div>
        </div>
        {err && <div className="mt-3"><Callout tone="bad">{err}</Callout></div>}
      </Panel>

      <PathPicker open={browse !== ""} mode="file"
                  accept={browse === "acrf" || browse === "std_acrf" ? PDF_RE : undefined}
                  start={({ acrf, standards, ta, ecrf, std_acrf: stdAcrf, std_ecrf: stdEcrf
                          }[browse as string] as string) || acrf || standards}
                  onClose={() => setBrowse("")}
                  onPick={(p) => {
                    if (browse === "acrf") setAcrf(p)
                    else if (browse === "standards") setStandards(p)
                    else if (browse === "ta") setTa(p)
                    else if (browse === "std_acrf") setStdAcrf(p)
                    else if (browse === "std_ecrf") setStdEcrf(p)
                    else setEcrf(p)
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

      <Panel title="Vendor CRF vs the internal standards CRF"
             description="Point at your company's own annotated CRF: the questions of both CRFs are aligned — worded differently but related still pairs up, with the similarity shown — and each pair's SDTM mappings are compared. 'T-Primary Diagnosis' mapped to RS in the standards but FA by the vendor is exactly what this finds."
             actions={
               <Button onClick={() => void runCmp()} disabled={busy || !acrf || !stdAcrf}>
                 <GitCompare className="mr-1.5 h-3.5 w-3.5" />{busy ? "Comparing…" : "Compare the CRFs"}
               </Button>}>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Internal standards aCRF (.pdf)</Label>
            <PathField value={stdAcrf} onChange={setStdAcrf} mode="file"
                       placeholder="/path/to/standards_acrf.pdf" onBrowse={() => setBrowse("std_acrf")} />
          </div>
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Standards eCRF spec (.xlsx — optional, question text)</Label>
            <PathField value={stdEcrf} onChange={setStdEcrf} mode="file"
                       placeholder="/path/to/standards_ecrf_spec.xlsx" onBrowse={() => setBrowse("std_ecrf")} />
          </div>
          <p className="text-[11px] text-muted-foreground">
            The vendor side uses the aCRF and eCRF spec from the panel above.
          </p>
        </div>
      </Panel>

      {cmp && (
        <>
          {(cmp.notes ?? []).map((n, i) => <Callout key={i} tone="warn">{n}</Callout>)}
          <Metrics>
            <Metric value={cc.pairs ?? 0} label="questions aligned" />
            <Metric value={cc.agree ?? 0} label="same mapping" tone="good" />
            <Metric value={cc.different_domain ?? 0} label="different domain"
                    tone={(cc.different_domain ?? 0) > 0 ? "bad" : "good"} />
            <Metric value={cc.different_variable ?? 0} label="different variables"
                    tone={(cc.different_variable ?? 0) > 0 ? "warn" : "good"} />
            <Metric value={cc.standard_only ?? 0} label="standards only"
                    tone={(cc.standard_only ?? 0) > 0 ? "warn" : "good"} />
            <Metric value={cc.vendor_only ?? 0} label="vendor only"
                    tone={(cc.vendor_only ?? 0) > 0 ? "warn" : "good"} />
          </Metrics>

          <Panel title="Question by question"
                 description="Each aligned pair of questions with both mappings. Filter any column.">
            <ClientGrid rows={cmp.pairs} height="24rem"
              rowKey={(r, i) => `${r.standard_question}-${i}`}
              cols={[
                { id: "sq", head: "Standards question", kind: "key", width: 230, sticky: true,
                  value: (r) => r.standard_question, cell: (r) => <span className="text-xs">{r.standard_question}</span> },
                { id: "vq", head: "Vendor question", width: 230,
                  value: (r) => r.vendor_question, cell: (r) => <span className="text-xs">{r.vendor_question}</span> },
                { id: "m", head: "Match", kind: "tag", width: 110,
                  value: (r) => r.match,
                  cell: (r) => <Chip tone={r.match === "exact" ? "green" : "blue"}>
                    {r.match === "exact" ? "same question" : `similar · ${r.similarity}`}</Chip> },
                { id: "sm", head: "Standards mapping", width: 170,
                  value: (r) => r.standard_mapping, cell: (r) => <Mono>{r.standard_mapping}</Mono> },
                { id: "vm", head: "Vendor mapping", width: 170,
                  value: (r) => r.vendor_mapping, cell: (r) => <Mono>{r.vendor_mapping}</Mono> },
                { id: "v", head: "Verdict", kind: "tag", width: 150,
                  value: (r) => r.verdict.replace(/_/g, " "),
                  cell: (r) => <Chip tone={r.verdict === "same_mapping" ? "green"
                    : r.verdict === "different_domain" ? "red" : "amber"}>
                    {r.verdict.replace(/_/g, " ")}</Chip> },
                { id: "a", head: "What to do", width: 380,
                  value: (r) => r.advice, cell: (r) => <span className="text-xs">{r.advice || "—"}</span> },
              ]} />
          </Panel>

          {(cmp.standard_only.length > 0 || cmp.vendor_only.length > 0) && (
            <Panel title="Questions only one CRF has">
              <ClientGrid rows={[...cmp.standard_only.map((x) => ({ ...x, side: "standards CRF only" })),
                                 ...cmp.vendor_only.map((x) => ({ ...x, side: "vendor CRF only" }))]}
                height="16rem" rowKey={(r, i) => `${r.side}-${i}`}
                cols={[
                  { id: "s", head: "Side", kind: "tag", width: 150, sticky: true,
                    value: (r) => r.side,
                    cell: (r) => <Chip tone={r.side.startsWith("standards") ? "amber" : "blue"}>{r.side}</Chip> },
                  { id: "q", head: "Question", width: 280,
                    value: (r) => r.question, cell: (r) => <span className="text-xs">{r.question}</span> },
                  { id: "f", head: "Form", width: 160, value: (r) => r.form, cell: (r) => r.form },
                  { id: "m", head: "Mapping", width: 170,
                    value: (r) => r.mapping, cell: (r) => <Mono>{r.mapping}</Mono> },
                  { id: "a", head: "What to do", width: 360,
                    value: (r) => r.advice, cell: (r) => <span className="text-xs">{r.advice}</span> },
                ]} />
            </Panel>
          )}

          {(cmp.ann_vendor_only.length > 0 || cmp.ann_standard_only.length > 0) && (
            <Panel title="Annotation names only one CRF uses"
                   description="Independent of question text — useful when a CRF's questions could not be read.">
              <div className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-2">
                <div>
                  <p className="mb-1 font-medium">Vendor only</p>
                  {cmp.ann_vendor_only.length
                    ? cmp.ann_vendor_only.map((v) => <Mono key={v}>{v}</Mono>).reduce((acc, el, i) =>
                        acc.length ? [...acc, <span key={`s${i}`}> </span>, el] : [el], [] as React.ReactNode[])
                    : <span className="text-muted-foreground">none</span>}
                </div>
                <div>
                  <p className="mb-1 font-medium">Standards only</p>
                  {cmp.ann_standard_only.length
                    ? cmp.ann_standard_only.map((v) => <Mono key={v}>{v}</Mono>).reduce((acc, el, i) =>
                        acc.length ? [...acc, <span key={`s${i}`}> </span>, el] : [el], [] as React.ReactNode[])
                    : <span className="text-muted-foreground">none</span>}
                </div>
              </div>
            </Panel>
          )}
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
