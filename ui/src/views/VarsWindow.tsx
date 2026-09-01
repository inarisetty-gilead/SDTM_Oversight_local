// A standalone window with the domain's built variables — the same grid as the
// Variables tab, and the same editor: click a variable, change its mapping, Apply.
// Opened as #vars/DM; both windows talk to the same server session, so a rebuild
// here is a rebuild there.
import { useCallback, useEffect, useRef, useState } from "react"
import { Search } from "lucide-react"
import { api } from "@/api"
import type { DomainDetail, VariableRow } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Chip, DataGrid, Mono, STATUS_TONE } from "@/components/grid"
import { Callout } from "@/components/shell"
import { VariableEditor } from "@/components/VariableEditor"
import { ACTION_TONE } from "@/views/DomainView"

export function VarsWindow({ domain }: { domain: string }) {
  const [d, setD] = useState<DomainDetail | null>(null)
  const [error, setError] = useState("")
  const [search, setSearch] = useState("")
  const [editing, setEditing] = useState<VariableRow | null>(null)
  const busyRef = useRef(false)

  useEffect(() => { document.title = `${domain} variables — SDTM Oversight` }, [domain])

  const load = useCallback(async () => {
    try { setD(await api.domain(domain)); setError("") }
    catch (e) { setError((e as Error).message) }
  }, [domain])

  useEffect(() => {
    void load()
    // the main window rebuilds too; this one follows without being told — but never
    // mid-edit, a refresh under the reader's cursor would throw their work away
    const t = window.setInterval(() => { if (!busyRef.current && !editing) void load() }, 5000)
    return () => window.clearInterval(t)
  }, [load, editing])

  const afterRebuild = async () => {
    busyRef.current = true
    try {
      for (;;) {
        await new Promise((r) => setTimeout(r, 400))
        const j = await api.job()
        if (j.status !== "running") {
          if (j.status === "error") setError(j.error)
          break
        }
      }
      await load()
      setEditing(null)
    } finally { busyRef.current = false }
  }

  const rows = (d?.variables ?? []).filter((v) => {
    if (!search.trim()) return true
    const needle = search.toLowerCase()
    return `${v.variable} ${v.label} ${v.source}`.toLowerCase().includes(needle)
  })

  return (
    <div className="flex h-screen flex-col gap-3 overflow-y-auto p-4">
      <div className="flex items-baseline gap-2">
        <h1 className="text-[15px] font-semibold">{domain} — variables as built</h1>
        <span className="text-xs text-muted-foreground">
          click a variable to edit it · same session as the main window</span>
        <div className="relative ml-auto w-72">
          <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
          <Input className="h-8 pl-8 text-xs" placeholder="Filter by variable, label or source"
                 value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
      </div>
      {error && <Callout tone="bad">{error}</Callout>}

      {editing && d && (
        <div className="space-y-3 rounded-xl border border-primary/40 bg-muted/20 p-4">
          <div className="flex flex-wrap items-baseline gap-2">
            <h2 className="text-[15px] font-semibold">{editing.variable}</h2>
            <span className="text-[13px] text-muted-foreground">{editing.label}</span>
            <span className="text-[12px] text-muted-foreground">
              {editing.how}{editing.source ? ` · ${editing.source}` : ""}
            </span>
            <Button size="sm" variant="ghost" className="ml-auto h-7 text-xs"
                    onClick={() => setEditing(null)}>Close</Button>
          </div>
          <VariableEditor detail={d} variable={editing}
                          onDone={() => void afterRebuild()}
                          onClose={() => setEditing(null)} />
        </div>
      )}

      <div className="min-h-0 flex-1">
        <DataGrid rows={rows} height="100%" rowKey={(v: VariableRow) => v.variable}
                  onRowClick={(v: VariableRow) => setEditing(v)}
                  empty={d ? "No variables match this filter" : "Loading…"}
          cols={[
            { id: "v", head: "Variable", kind: "key", sticky: true, width: 130,
              cell: (v: VariableRow) => v.variable },
            { id: "l", head: "Label", kind: "text", width: 210,
              cell: (v: VariableRow) => <span className="text-muted-foreground">{v.label}</span> },
            { id: "act", head: "Action", kind: "tag",
              cell: (v: VariableRow) => v.spec_action
                ? <Chip tone={ACTION_TONE[v.spec_action.toUpperCase()] ?? "blue"}>
                    {v.spec_action.toUpperCase()}</Chip> : null },
            { id: "cl", head: "Codelist", kind: "tag",
              cell: (v: VariableRow) => v.codelist ? <Chip tone="teal">{v.codelist}</Chip> : null },
            { id: "s", head: "Status", kind: "tag",
              cell: (v: VariableRow) => <Chip tone={STATUS_TONE[v.status] ?? "slate"}>
                {v.status.replace("_", " ")}</Chip> },
            { id: "by", head: "Mapped by", kind: "tag",
              cell: (v: VariableRow) => <Chip tone={STATUS_TONE[v.method_source] ?? "slate"}>
                {v.method_source === "name_match" ? `name match ${v.confidence}%`
                  : v.method_source === "edit" ? "hand edit"
                  : v.method_source === "convention" ? "convention"
                  : v.method_source === "template" ? "template" : "spec"}</Chip> },
            { id: "h", head: "How it was built", kind: "calc", width: 180,
              cell: (v: VariableRow) => v.how },
            { id: "src", head: "Source", kind: "code", width: 170,
              cell: (v: VariableRow) => v.source ? <Mono>{v.source}</Mono>
                : v.constant ? <Mono>"{v.constant}"</Mono> : null },
            { id: "p", head: "Populated", kind: "number", align: "right",
              cell: (v: VariableRow) => v.populated === null ? "" : v.populated.toLocaleString() },
            { id: "vals", head: "Values", kind: "text", width: 220,
              cell: (v: VariableRow) => <span className="flex gap-1">
                {v.samples.slice(0, 3).map((x, i) => <Mono key={i}>{x}</Mono>)}</span> },
            { id: "why", head: "Reason", kind: "text", width: 300,
              cell: (v: VariableRow) => <span className={v.error ? "text-destructive" : "text-muted-foreground"}>
                {v.error || v.reason}</span> },
          ]} />
      </div>
    </div>
  )
}
