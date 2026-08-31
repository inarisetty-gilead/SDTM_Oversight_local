import { useEffect, useMemo, useRef, useState } from "react"
import { Search } from "lucide-react"
import { api } from "@/api"
import type { SpecSheetRow } from "@/api"
import { Input } from "@/components/ui/input"
import { Chip, Mono } from "./grid"
import { Callout } from "./shell"

const ACTION_TONE: Record<string, "green" | "slate" | "amber" | "blue" | "teal" | "violet"> = {
  ASSIGN: "green", CODE: "teal", DERIVED: "teal", DERIVE: "teal",
  DROP: "slate", SUPP: "violet", CONSTANT: "amber", HARDCODE: "amber",
}

/** The domain's spec sheet as a compact, searchable reference — made to sit BESIDE the
 *  mapping work (a slide-over, or its own window), not to replace the full Spec view. */
export function SpecRowsPanel({ domain, highlight }: { domain: string; highlight?: string }) {
  const [rows, setRows] = useState<SpecSheetRow[] | null>(null)
  const [err, setErr] = useState("")
  const [q, setQ] = useState("")
  const hiRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    setRows(null); setErr("")
    api.specRows(domain).then((r) => setRows(r.rows)).catch((e) => setErr((e as Error).message))
  }, [domain])

  // land on the variable being mapped, so the reader doesn't scroll for it
  useEffect(() => { hiRef.current?.scrollIntoView({ block: "center" }) }, [rows, highlight])

  const shown = useMemo(() => {
    if (!rows) return []
    if (!q.trim()) return rows
    const needle = q.toLowerCase()
    return rows.filter((r) =>
      [r.variable, r.label, r.input_variables, r.mapping_rule, r.codelist]
        .join(" ").toLowerCase().includes(needle))
  }, [rows, q])

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <div className="relative">
        <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
        <Input className="h-8 pl-8 text-xs" placeholder="Filter the spec rows…"
               value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {err && <Callout tone="bad">{err}</Callout>}
      {!rows && !err && <p className="text-sm text-muted-foreground">Loading the spec…</p>}
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
        {shown.map((r) => {
          const hi = highlight && r.variable === highlight
          return (
            <div key={`${r.variable}-${r.sheet_row}`} ref={hi ? hiRef : undefined}
                 className={"rounded-md border px-2.5 py-1.5 text-xs "
                            + (hi ? "border-primary/60 bg-muted/40" : "")}>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-[12px] font-semibold">{r.variable}</span>
                <Chip tone={ACTION_TONE[r.action] ?? "slate"}>{r.action || "(blank)"}</Chip>
                {r.codelist && <Chip tone="teal">{r.codelist}</Chip>}
                {r.supp && <Chip tone="violet">SUPP</Chip>}
                <span className="ml-auto text-[10px] text-muted-foreground">row {r.sheet_row}</span>
              </div>
              <p className="mt-0.5 text-muted-foreground">{r.label}</p>
              {r.input_variables && <p className="mt-0.5"><Mono>{r.input_variables}</Mono></p>}
              {r.mapping_rule && <p className="mt-0.5 italic text-muted-foreground">{r.mapping_rule}</p>}
              {r.sas_code && <p className="mt-0.5"><Mono>{r.sas_code.length > 120
                ? r.sas_code.slice(0, 117) + "…" : r.sas_code}</Mono></p>}
            </div>
          )
        })}
        {rows && !shown.length && (
          <p className="py-4 text-center text-xs text-muted-foreground">No spec rows match.</p>)}
      </div>
    </div>
  )
}

/** A standalone spec window (#spec/DM) — the whole page is the reference, so it can live
 *  on a second monitor beside the mapping work. Same server session as the main window. */
export function SpecWindow({ domain }: { domain: string }) {
  useEffect(() => { document.title = `${domain} spec — SDTM Oversight` }, [domain])
  return (
    <div className="flex h-screen flex-col gap-3 p-4">
      <div className="flex items-baseline gap-2">
        <h1 className="text-[15px] font-semibold">{domain} — mapping spec</h1>
        <span className="text-xs text-muted-foreground">
          read-only reference · reflects the spec loaded in the main window</span>
      </div>
      <div className="min-h-0 flex-1">
        <SpecRowsPanel domain={domain} />
      </div>
    </div>
  )
}
