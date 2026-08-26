import { useCallback, useEffect, useState } from "react"
import { Search } from "lucide-react"
import { api } from "@/api"
import type { SpecDomain, SpecSheetRow } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Chip, DataGrid, Mono } from "@/components/grid"
import { Callout, Metric, Metrics, PageHeader } from "@/components/shell"

const ACTION_TONE: Record<string, "green" | "slate" | "amber" | "blue"> = {
  ASSIGN: "green", DROP: "slate", CODE: "blue", "": "amber",
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
      </div>
    </>
  )
}
