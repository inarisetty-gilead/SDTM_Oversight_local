import { useCallback, useEffect, useState } from "react"
import { Download, RefreshCw, Search } from "lucide-react"
import { api } from "@/api"
import type { ReviewComment, SpecCheckIssue, SpecDomain, SpecSheetRow } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { PathField, PathPicker } from "@/components/PathPicker"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Chip, DataGrid, Mono } from "@/components/grid"
import { Callout, EmptyState, Metric, Metrics, PageHeader, Panel } from "@/components/shell"

const ACTION_TONE: Record<string, "green" | "slate" | "amber" | "blue"> = {
  ASSIGN: "green", DROP: "slate", CODE: "blue", "": "amber",
}

const ISSUE_TONE: Record<SpecCheckIssue["kind"], "amber" | "red" | "blue"> = {
  cross_domain_reference: "red", duplicate_variable: "amber", missing_codelist: "blue",
  type_mismatch: "amber", long_variable_name: "blue", missing_identifier: "red",
  standards_mismatch: "red",
}
const ISSUE_LABEL: Record<SpecCheckIssue["kind"], string> = {
  cross_domain_reference: "Wrong-domain reference", duplicate_variable: "Duplicate variable",
  missing_codelist: "Missing codelist", type_mismatch: "Type mismatch",
  long_variable_name: "Name too long", missing_identifier: "Missing identifier",
  standards_mismatch: "Off-standard",
}

/** Static checks on the spec's own text — a row copied from a similar domain and never
 *  renamed, or a variable defined twice. Each finding can be sent straight to that domain's
 *  Review comments, so the programmer sees it in the same place as everything else reviewed. */
function SpecChecksPanel() {
  const [result, setResult] = useState<{ totals: Record<string, number>; issues: SpecCheckIssue[] } | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState<Set<number>>(new Set())
  const [standards, setStandards] = useState("")
  const [ta, setTa] = useState("")
  const [browse, setBrowse] = useState<"" | "standards" | "ta">("")
  const [refBusy, setRefBusy] = useState(false)

  const load = useCallback(async () => {
    setBusy(true); setError("")
    try { setResult(await api.specChecks()) }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }, [])
  useEffect(() => { void load() }, [load])
  useEffect(() => { void api.specCheckReference().then((r) => {
    setStandards(r.standards); setTa(r.ta)
  }).catch(() => undefined) }, [])

  const saveReference = async () => {
    setRefBusy(true); setError("")
    try { await api.setSpecCheckReference({ standards, ta }); await load() }
    catch (e) { setError((e as Error).message) } finally { setRefBusy(false) }
  }

  const sendToReview = async (issue: SpecCheckIssue, i: number) => {
    try {
      const { reviews } = await api.reviews()
      const existing = reviews[issue.domain] ?? {}
      const comments: ReviewComment[] = [...(existing.comments ?? []),
        { variable: issue.variable, text: issue.message }]
      await api.saveReview(issue.domain, { status: existing.status ?? "not_reviewed", comments })
      setSent((s) => new Set(s).add(i))
    } catch (e) { setError((e as Error).message) }
  }

  return (
    <div className="space-y-4">
      <Panel title="Reference specs (optional)"
             description="Point at the company standards mapping and/or the TA spec — the same workbook shape as the mapping spec — to also flag a Codelist/Type/Role/Origin/Action that disagrees with the standard. The TA spec wins where both name the same variable.">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">Standards mapping (.xlsx)</Label>
            <PathField value={standards} onChange={setStandards} mode="file"
                       placeholder="/path/to/standards_mapping.xlsx" onBrowse={() => setBrowse("standards")} />
          </div>
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wide text-muted-foreground">TA spec (.xlsx)</Label>
            <PathField value={ta} onChange={setTa} mode="file"
                       placeholder="/path/to/ta_spec.xlsx" onBrowse={() => setBrowse("ta")} />
          </div>
        </div>
        <Button size="sm" className="mt-3" disabled={refBusy} onClick={() => void saveReference()}>
          {refBusy ? "Saving…" : "Save & re-run checks"}</Button>
      </Panel>
      <PathPicker open={browse !== ""} mode="file"
                  start={(browse === "standards" ? standards : ta) || standards || ta}
                  onClose={() => setBrowse("")}
                  onPick={(p) => { if (browse === "standards") setStandards(p); else if (browse === "ta") setTa(p) }} />

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" className="h-8 text-xs" disabled={busy} onClick={() => void load()}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />{busy ? "Checking…" : "Re-run checks"}</Button>
        <Button size="sm" variant="outline" className="h-8 text-xs" asChild>
          <a href="/api/spec/checks/export">
            <Download className="mr-1.5 h-3.5 w-3.5" />Download findings (Excel)</a>
        </Button>
      </div>
      {error && <Callout tone="bad">{error}</Callout>}
      {result && (
        <>
          <Metrics>
            <Metric value={result.totals.domains_checked} label="active domains checked" />
            <Metric value={result.totals.cross_domain_references} label="wrong-domain references"
                    tone={result.totals.cross_domain_references ? "bad" : "good"} />
            <Metric value={result.totals.duplicate_variables} label="duplicate variables"
                    tone={result.totals.duplicate_variables ? "warn" : "good"} />
            <Metric value={result.totals.missing_codelists} label="missing codelists"
                    tone={result.totals.missing_codelists ? "warn" : "good"} />
            <Metric value={result.totals.type_mismatches} label="type mismatches"
                    tone={result.totals.type_mismatches ? "warn" : "good"} />
            <Metric value={result.totals.long_variable_names} label="names over 8 characters"
                    tone={result.totals.long_variable_names ? "warn" : "good"} />
            <Metric value={result.totals.missing_identifiers} label="missing identifiers"
                    tone={result.totals.missing_identifiers ? "bad" : "good"} />
            <Metric value={result.totals.standards_mismatches} label="off-standard"
                    tone={result.totals.standards_mismatches ? "bad" : "good"} />
          </Metrics>
          {result.issues.length === 0 ? (
            <EmptyState title="No issues found">
              Every active domain's Mapping Rule / Implemented SAS Code stayed within its own
              domain, no variable is duplicated, misnamed or mistyped, and every codelist
              named is defined.
            </EmptyState>
          ) : (
            <DataGrid rows={result.issues} height="30rem" rowKey={(_r, i) => String(i)}
              groupBy={(r) => r.domain}
              cols={[
                { id: "k", head: "Issue", kind: "tag", width: 160,
                  cell: (r) => <Chip tone={ISSUE_TONE[r.kind]}>{ISSUE_LABEL[r.kind]}</Chip> },
                { id: "v", head: "Variable", kind: "key", width: 110, cell: (r) => r.variable },
                { id: "row", head: "Row", kind: "number", align: "right", width: 60,
                  cell: (r) => r.row || "" },
                { id: "l", head: "Label", kind: "text", width: 160,
                  cell: (r) => <span className="text-muted-foreground">{r.label}</span> },
                { id: "f", head: "Found", kind: "code", width: 110,
                  cell: (r) => r.found ? <Mono>{r.found}</Mono> : null },
                { id: "e", head: "Expected", kind: "code", width: 110,
                  cell: (r) => r.expected ? <Mono>{r.expected}</Mono> : null },
                { id: "msg", head: "Details", kind: "text", width: 420,
                  cell: (r) => r.message },
                { id: "act", head: "", kind: "tag", width: 140,
                  cell: (r, i) => (
                    <Button size="sm" variant="outline" className="h-6 px-2 text-[11px]"
                            disabled={sent.has(i)}
                            onClick={(e) => { e.stopPropagation(); void sendToReview(r, i) }}>
                      {sent.has(i) ? "Sent ✓" : "Send to review"}
                    </Button>) },
              ]} />
          )}
        </>
      )}
    </div>
  )
}

/** The mapping spec, reviewable as written — before anything is built from it. */
export function SpecView() {
  const [domains, setDomains] = useState<SpecDomain[]>([])
  const [hasToc, setHasToc] = useState(false)
  const [tocOnly, setTocOnly] = useState<string[]>([])
  const [selected, setSelected] = useState<string>("")
  const [rows, setRows] = useState<SpecSheetRow[]>([])
  const [search, setSearch] = useState("")
  const [action, setAction] = useState("all")
  const [error, setError] = useState("")

  useEffect(() => { void (async () => {
    try {
      const d = await api.specDomains()
      setDomains(d.domains); setHasToc(d.has_toc); setTocOnly(d.toc_only)
    } catch (e) { setError((e as Error).message) }
  })() }, [])

  const open = useCallback(async (dom: string) => {
    setSelected(dom); setSearch(""); setAction("all")
    try { setRows((await api.specRows(dom)).rows) }
    catch (e) { setError((e as Error).message); setRows([]) }
  }, [])

  const active = domains.filter((d) => d.active)
  const inactive = domains.filter((d) => !d.active)
  const cur = domains.find((d) => d.domain === selected)

  const shown = rows.filter((r) => {
    if (action !== "all" && (r.action || "(blank)") !== action) return false
    if (!search.trim()) return true
    const q = search.toLowerCase()
    return [r.variable, r.label, r.input_variables, r.mapping_rule, r.sas_code]
      .join(" ").toLowerCase().includes(q)
  })
  const actions = ["all", ...new Set(rows.map((r) => r.action || "(blank)"))]

  return (
    <>
      <PageHeader title="Mapping specification"
        subtitle="The spec as written — every domain sheet and every row, before anything is built from it." />

      <div className="space-y-4">
        {error && <Callout tone="bad">{error}</Callout>}

        <Metrics>
          <Metric value={domains.length} label="domain sheets" />
          {hasToc && <Metric value={active.length} label="active per TOC" tone="good" />}
          {hasToc && <Metric value={inactive.length} label="inactive (Active = N)"
                             tone={inactive.length ? "warn" : undefined} />}
          <Metric value={domains.reduce((a, d) => a + d.variables, 0).toLocaleString()}
                  label="spec variables" />
        </Metrics>

        {hasToc && (
          <Callout title="The TOC drives the build">
            Building “all domains” builds the {active.length} the TOC marks Active = Y.
            Inactive domains stay reviewable here and can still be built by naming them.
          </Callout>
        )}
        {tocOnly.length > 0 && (
          <Callout tone="warn"
                   title={`${tocOnly.length} dataset(s) in the TOC have no spec sheet`}>
            <Mono>{tocOnly.join(", ")}</Mono>
          </Callout>
        )}

        <Tabs defaultValue="browse">
          <TabsList>
            <TabsTrigger value="browse">Browse</TabsTrigger>
            <TabsTrigger value="checks">Checks</TabsTrigger>
          </TabsList>

          <TabsContent value="browse" className="space-y-3 pt-4">
            <div className="flex flex-wrap gap-1">
              {[...active, ...inactive].map((d) => (
                <button key={d.domain} onClick={() => void open(d.domain)}
                        title={d.label || d.domain}
                        className={`rounded-md border px-2 py-1 text-xs transition
                          ${selected === d.domain ? "border-primary bg-primary/10 font-medium" : "hover:bg-accent"}
                          ${!d.active ? "opacity-45" : ""}`}>
                  {d.domain}
                  <span className="ml-1 text-muted-foreground">{d.variables}</span>
                  {!d.active && <span className="ml-1 text-[10px]">off</span>}
                </button>
              ))}
            </div>

            {cur && (
              <div className="space-y-3">
                <div className="flex flex-wrap items-baseline gap-2">
                  <h3 className="text-[15px] font-semibold">{cur.domain}</h3>
                  {cur.label && <span className="text-[13px] text-muted-foreground">{cur.label}</span>}
                  {cur.class && <Chip tone="violet">{cur.class}</Chip>}
                  {!cur.active && <Chip tone="amber">inactive — Active = N in the TOC</Chip>}
                  {cur.structure && (
                    <span className="text-[11px] text-muted-foreground">{cur.structure}</span>)}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  {actions.map((a) => (
                    <Button key={a} size="sm" variant={action === a ? "default" : "outline"}
                            className="h-7 text-xs" onClick={() => setAction(a)}>
                      {a === "all" ? `All ${rows.length}` : a}
                    </Button>
                  ))}
                  <div className="relative ml-auto min-w-56 flex-1">
                    <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
                    <Input className="h-8 pl-8 text-xs"
                           placeholder="Filter by variable, label, source or rule"
                           value={search} onChange={(e) => setSearch(e.target.value)} />
                  </div>
                </div>

                <DataGrid rows={shown} height="34rem" rowKey={(r) => r.variable + r.sheet_row}
                          empty="No spec rows match"
                  cols={[
                    { id: "v", head: "Variable", kind: "key", sticky: true, width: 120,
                      cell: (r) => <span className="flex items-center gap-1.5">{r.variable}
                        {r.supp && <Chip tone="teal">SUPP</Chip>}</span> },
                    { id: "l", head: "Label", kind: "text", width: 210,
                      cell: (r) => <span className="text-muted-foreground">{r.label}</span> },
                    { id: "a", head: "Action", kind: "tag",
                      cell: (r) => <Chip tone={ACTION_TONE[r.action] ?? "slate"}>
                        {r.action || "(blank)"}</Chip> },
                    { id: "iv", head: "Input variables", kind: "code", width: 260,
                      cell: (r) => r.input_variables
                        ? <Mono>{r.input_variables.length > 60
                            ? r.input_variables.slice(0, 57) + "…" : r.input_variables}</Mono> : null },
                    { id: "sas", head: "SAS code", kind: "code", width: 220,
                      cell: (r) => r.sas_code ? <Mono>{r.sas_code.length > 48
                        ? r.sas_code.slice(0, 45) + "…" : r.sas_code}</Mono> : null },
                    { id: "rule", head: "Mapping rule", kind: "text", width: 320,
                      cell: (r) => <span className="text-muted-foreground" title={r.mapping_rule}>
                        {r.mapping_rule.length > 90 ? r.mapping_rule.slice(0, 87) + "…" : r.mapping_rule}
                      </span> },
                    { id: "cl", head: "Codelist", kind: "tag",
                      cell: (r) => r.codelist ? <Chip tone="teal">{r.codelist}</Chip> : null },
                    { id: "role", head: "Role", kind: "text", cell: (r) => r.role },
                    { id: "or", head: "Origin", kind: "text", cell: (r) => r.origin },
                    { id: "row", head: "Sheet row", kind: "number", align: "right",
                      cell: (r) => r.sheet_row },
                  ]} />
              </div>
            )}
            {!cur && domains.length > 0 && (
              <p className="text-sm text-muted-foreground">Pick a domain above to read its sheet.</p>
            )}
          </TabsContent>

          <TabsContent value="checks" className="pt-4">
            <SpecChecksPanel />
          </TabsContent>
        </Tabs>
      </div>
    </>
  )
}

