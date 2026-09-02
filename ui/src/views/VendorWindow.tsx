// A standalone window browsing the vendor's delivered SDTM datasets — the same picker as
// Vendor delivery, but as its own page for a second monitor. Opened as #vendor or
// #vendor/DATASETNAME to land straight on one dataset. Same server session as the main window.
import { useCallback, useEffect, useMemo, useState } from "react"
import { Search } from "lucide-react"
import { api } from "@/api"
import type { DataPage } from "@/api"
import { Input } from "@/components/ui/input"
import { Chip, Mono } from "@/components/grid"
import { Callout } from "@/components/shell"
import { RecordTable } from "@/components/DataView"

const FULL = 100_000

export function VendorWindow({ dataset }: { dataset?: string }) {
  const [vendorPath, setVendorPath] = useState("")
  const [list, setList] = useState<Array<{ name: string; file: string; label: string }> | null>(null)
  const [schema, setSchema] = useState<Record<string, string[]> | null>(null)
  const [q, setQ] = useState("")
  const [sel, setSel] = useState(dataset ?? "")
  const [page, setPage] = useState<DataPage | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => { document.title = "Vendor delivery — SDTM Oversight" }, [])

  useEffect(() => {
    void api.state().then((st) => setVendorPath((st as { vendor?: string }).vendor ?? ""))
    api.vendorDatasets()
      .then((r) => { setList(r.datasets); if (!dataset && r.datasets.length) setSel((s) => s || r.datasets[0].name) })
      .catch((e) => setError((e as Error).message))
    // the column map loads in the background — the filter box searches columns too
    api.vendorColumns().then((r) => setSchema(r.columns)).catch(() => setSchema({}))
  }, [dataset])

  const load = useCallback(async (name: string) => {
    if (!name) return
    setBusy(true); setError("")
    try { setPage(await api.vendorData(name, { offset: 0, limit: FULL })) }
    catch (e) { setError((e as Error).message); setPage(null) }
    finally { setBusy(false) }
  }, [])
  useEffect(() => { void load(sel) }, [sel, load])

  const shown = useMemo(() => {
    if (!list) return []
    if (!q.trim()) return list.map((d) => ({ ...d, hit: "" }))
    const needle = q.toLowerCase()
    return list
      .map((d) => {
        const byName = `${d.name} ${d.file} ${d.label}`.toLowerCase().includes(needle)
        const hit = byName ? ""
          : (schema?.[d.name] ?? []).find((c) => c.toLowerCase().includes(needle)) ?? ""
        return { ...d, hit, keep: byName || !!hit }
      })
      .filter((d) => d.keep)
  }, [list, q, schema])

  return (
    <div className="flex h-screen flex-col gap-3 p-4">
      <div className="flex items-baseline gap-2">
        <h1 className="text-[15px] font-semibold">Vendor delivery</h1>
        <span className="text-xs text-muted-foreground">
          read-only reference · same vendor folder as the main window
          {vendorPath ? <> · <Mono>{vendorPath}</Mono></> : ""}</span>
      </div>
      <div className="flex min-h-0 flex-1 gap-4">
        <div className="flex w-64 shrink-0 flex-col gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
            <Input className="h-8 pl-8 text-xs"
                   placeholder={schema ? "Dataset or column…" : "Filter datasets…"}
                   value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto rounded-md border p-1">
            {!list && <p className="p-2 text-xs text-muted-foreground">Loading…</p>}
            {shown.map((d) => (
              <button key={d.name} type="button" onClick={() => setSel(d.name)}
                      className={"block w-full rounded px-2 py-1.5 text-left text-xs "
                                 + (sel === d.name ? "bg-muted font-medium" : "hover:bg-muted/50")}>
                <span className="font-mono">{d.name}</span>
                {d.hit && <Chip tone="amber" className="ml-1.5">has {d.hit}</Chip>}
                {d.file && <span className="block truncate text-[10px] text-muted-foreground">{d.file}</span>}
              </button>
            ))}
            {list && !shown.length && (
              <p className="p-2 text-xs text-muted-foreground">No datasets match.</p>)}
          </div>
          {list && <p className="text-[11px] text-muted-foreground">{list.length} dataset(s)</p>}
        </div>
        <div className="min-h-0 min-w-0 flex-1 space-y-2">
          {error && <Callout tone="bad">{error}</Callout>}
          {page && (
            <p className="text-xs text-muted-foreground">
              <Mono>{page.dataset}</Mono> — {(page.total ?? page.nrows).toLocaleString()} record(s),{" "}
              {page.columns.length} column(s)
              {(page.total ?? 0) >= FULL ? " · showing the first page" : ""}</p>
          )}
          <RecordTable page={page} busy={busy} height="calc(100vh - 10rem)"
                       onRefresh={() => void load(sel)} />
        </div>
      </div>
    </div>
  )
}
