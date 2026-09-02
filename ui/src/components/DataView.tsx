import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  type ColumnDef, type ColumnFiltersState, type Header, type SortingState,
  getCoreRowModel, getFacetedUniqueValues, getFilteredRowModel,
  getSortedRowModel, useReactTable,
} from "@tanstack/react-table"
import { useVirtualizer } from "@tanstack/react-virtual"
import {
  AlignLeft, ArrowDownWideNarrow, ArrowUpNarrowWide, Calendar, Hash, Key,
  Loader2, RefreshCw, Sigma,
} from "lucide-react"
import { api } from "@/api"
import type { DataColumn, DataPage } from "@/api"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Chip, STATUS_TONE } from "./grid"
import { Callout } from "./shell"
import { cn } from "@/lib/utils"

const ROW_H = 30
const FULL = 100_000
const DROPDOWN_MAX = 40
// frozen while scrolling left/right — the two columns a reader needs on screen no matter
// how far right they've scrolled to compare a value against its subject
const STICKY_KEYS = new Set(["STUDYID", "USUBJID"])

type Row = string[]

/** The records themselves: the whole dataset in one virtualised table — sortable, filterable
 *  per column, resizable — rather than a page at a time. */
/** The table itself: whole dataset, virtualised, sortable, resizable, filtered per column.
 *  Used for a domain, for its SUPP, for a raw input, and for a single variable. */
export function RecordTable({ page, busy, height = "34rem", onRefresh, highlight }: {
  page: DataPage | null; busy?: boolean; height?: string; onRefresh?: () => void
  /** the column the reader is working on — scrolled into view and tinted */
  highlight?: string
}) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [colFilters, setColFilters] = useState<ColumnFiltersState>([])
  useEffect(() => { setSorting([]); setColFilters([]) }, [page?.domain, page?.dataset, page?.part])

  const columns = useMemo<ColumnDef<Row, string>[]>(() => (page?.columns ?? []).map((c, i) => ({
    id: c.name,
    accessorFn: (row: Row) => row[i] ?? "",
    header: c.name,
    size: c.numeric ? 110 : 150,
    filterFn: (c.distinct && c.distinct.length <= DROPDOWN_MAX) ? "equalsString" : "includesString",
    meta: c,
  })), [page])
  const data = useMemo(() => page?.rows ?? [], [page])

  const table = useReactTable({
    data, columns,
    state: { sorting, columnFilters: colFilters },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColFilters,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getFacetedUniqueValues: getFacetedUniqueValues(),
    columnResizeMode: "onChange",
  })

  const rows = table.getRowModel().rows
  // left offset (px) for each sticky column, in the order they actually appear — the #
  // row-number column is 44px (w-11), then each sticky column stacks after the ones before it
  const stickyLeft = (() => {
    const map: Record<string, number> = {}
    let left = 44
    for (const col of table.getAllLeafColumns()) {
      if (!STICKY_KEYS.has(col.id)) continue
      map[col.id] = left
      left += col.getSize()
    }
    return map
  })()
  const scrollRef = useRef<HTMLDivElement>(null)
  const virt = useVirtualizer({
    count: rows.length, getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_H, overscan: 14,
  })
  // bring the column being edited into view rather than making the reader hunt for it
  useEffect(() => {
    if (!highlight || !scrollRef.current) return
    const el = scrollRef.current.querySelector<HTMLElement>(`[data-col="${CSS.escape(highlight)}"]`)
    if (el) {
      const box = scrollRef.current.getBoundingClientRect()
      const cell = el.getBoundingClientRect()
      if (cell.left < box.left + 60 || cell.right > box.right) {
        scrollRef.current.scrollLeft += cell.left - box.left - 80
      }
    }
  }, [highlight, page])

  const items = virt.getVirtualItems()
  const padTop = items.length ? items[0].start : 0
  const padBottom = items.length ? virt.getTotalSize() - items[items.length - 1].end : 0
  const total = page?.nrows ?? 0

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="num-cell">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                : rows.length === total ? <>{total.toLocaleString()} records</>
                : <>{rows.length.toLocaleString()} of {total.toLocaleString()} records</>}
        </span>
        {colFilters.length > 0 && (
          <Button size="sm" variant="ghost" className="h-7 text-xs"
                  onClick={() => setColFilters([])}>Clear {colFilters.length} filter(s)</Button>
        )}
        {sorting.length > 0 && (
          <Button size="sm" variant="ghost" className="h-7 text-xs"
                  onClick={() => setSorting([])}>Clear sort</Button>
        )}
        {onRefresh && (
          <Button size="sm" variant="ghost" className="ml-auto h-7 text-xs" disabled={busy}
                  onClick={onRefresh}><RefreshCw className="mr-1.5 h-3.5 w-3.5" />Refresh</Button>
        )}
      </div>

      <div ref={scrollRef} className="thin-scroll relative overflow-auto rounded-xl border bg-surface"
           style={{ height }}>
        <table className="border-separate border-spacing-0 text-[12px]"
               style={{ width: table.getTotalSize() + 44 }}>
          <thead className="sticky top-0 z-20">
            <tr className="grid-head-row">
              <th className="sticky left-0 z-30 w-11 border-b bg-muted px-2 py-1.5 text-left
                             text-[10px] font-medium text-muted-foreground">#</th>
              {table.getHeaderGroups()[0].headers.map((h: Header<Row, unknown>) => {
                const meta = h.column.columnDef.meta as DataColumn
                const sorted = h.column.getIsSorted()
                const Icon = iconFor(meta)
                const stuck = stickyLeft[h.column.id]
                return (
                  <th key={h.id} style={{ width: h.getSize(), left: stuck }} data-col={h.column.id}
                      className={cn("group relative border-b bg-muted px-2 py-1.5 text-left",
                                    stuck !== undefined && "sticky z-30",
                                    sorted && "th-sorted",
                                    highlight === h.column.id && "bg-primary/20")}>
                    <button onClick={h.column.getToggleSortingHandler()}
                            title={meta?.label || h.column.id}
                            className="flex w-full items-center gap-1 text-[10px] font-medium
                                       uppercase tracking-wide text-muted-foreground
                                       hover:text-foreground">
                      <Icon className="h-3 w-3 shrink-0 opacity-60" />
                      <span className="truncate">{h.column.id}</span>
                      {sorted === "asc" && <ArrowUpNarrowWide className="ml-auto h-3 w-3" />}
                      {sorted === "desc" && <ArrowDownWideNarrow className="ml-auto h-3 w-3" />}
                    </button>
                    <div onMouseDown={h.getResizeHandler()} onTouchStart={h.getResizeHandler()}
                         className="absolute right-0 top-0 h-full w-1 cursor-col-resize select-none
                                    opacity-0 group-hover:bg-primary/40 group-hover:opacity-100" />
                  </th>
                )
              })}
            </tr>
            <tr>
              <th className="sticky left-0 z-30 border-b bg-muted/70" />
              {table.getHeaderGroups()[0].headers.map((h: Header<Row, unknown>) => {
                const meta = h.column.columnDef.meta as DataColumn
                const value = (h.column.getFilterValue() as string) ?? ""
                const opts = meta?.distinct
                const stuck = stickyLeft[h.column.id]
                return (
                  <th key={h.id} style={{ left: stuck }}
                      className={cn("border-b bg-muted/70 px-1 pb-1", stuck !== undefined && "sticky z-30")}>
                    {opts && opts.length && opts.length <= DROPDOWN_MAX ? (
                      <select value={value}
                              onChange={(e) => h.column.setFilterValue(e.target.value || undefined)}
                              className={cn("filter-control h-6 w-full rounded-md border px-1 text-[11px]",
                                            value && "filter-active")}>
                        <option value="">All</option>
                        {opts.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    ) : (
                      <input value={value} placeholder="Search…"
                             onChange={(e) => h.column.setFilterValue(e.target.value || undefined)}
                             className={cn("filter-control h-6 w-full rounded-md border px-1.5 text-[11px]",
                                           "placeholder:text-muted-foreground/60",
                                           value && "filter-active")} />
                    )}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {padTop > 0 && <tr><td style={{ height: padTop }} /></tr>}
            {items.map((vi) => {
              const row = rows[vi.index]
              return (
                <tr key={row.id} className={cn("grid-row", vi.index % 2 === 1 && "row-alt")}
                    style={{ height: ROW_H }}>
                  <td className="num-cell sticky left-0 z-10 border-b bg-surface px-2
                                 text-[10px] text-muted-foreground">{vi.index + 1}</td>
                  {row.getVisibleCells().map((cell) => {
                    const meta = cell.column.columnDef.meta as DataColumn
                    const v = cell.getValue() as string
                    const stuck = stickyLeft[cell.column.id]
                    return (
                      <td key={cell.id} style={{ width: cell.column.getSize(), left: stuck }}
                          data-col={cell.column.id}
                          className={cn("truncate border-b px-2",
                            meta?.numeric && "num-cell text-right",
                            stuck !== undefined && "sticky z-10 bg-surface font-medium",
                            highlight === cell.column.id && "col-hot")}>
                        {v === "" ? <span className="text-muted-foreground/40">—</span> : v}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
            {padBottom > 0 && <tr><td style={{ height: padBottom }} /></tr>}
            {!rows.length && !busy && (
              <tr><td colSpan={columns.length + 1}
                      className="p-8 text-center text-sm text-muted-foreground">
                {colFilters.length ? "No records match these filters" : "No records"}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** The built dataset for a domain, reloaded whenever the build changes. */
export function useDomainRecords(domain: string, refreshKey: number, part: "parent" | "supp" = "parent") {
  const [page, setPage] = useState<DataPage | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const load = useCallback(async () => {
    setBusy(true); setError("")
    try { setPage(await api.domainData(domain, { offset: 0, limit: FULL }, part)) }
    catch (e) { setError((e as Error).message); setPage(null) } finally { setBusy(false) }
  }, [domain, part])
  useEffect(() => { void load() }, [load, refreshKey])
  return { page, busy, error, reload: load }
}


/** One variable, read row by row, in the same table as everything else. */
export function VariableRecords({ domain, variable, keys, refreshKey }: {
  domain: string; variable: string; keys: string[]; refreshKey: number
}) {
  const [page, setPage] = useState<DataPage | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const load = useCallback(async () => {
    setBusy(true); setError("")
    try {
      setPage(await api.domainData(domain,
        { offset: 0, limit: FULL, only: [...keys, variable].join(",") }))
    } catch (e) { setError((e as Error).message); setPage(null) } finally { setBusy(false) }
  }, [domain, variable, keys])

  useEffect(() => { void load() }, [load, refreshKey])

  if (error) return <Callout tone="bad">{error}</Callout>
  return <RecordTable page={page} busy={busy} height="22rem" onRefresh={() => void load()} />
}

export function DataView({ domain, datasets, refreshKey, hasSuppVars }: {
  domain: string; datasets: string[]; refreshKey: number
  /** the domain has SUPP-- variables in its spec, even if none of them ended up with a
   *  value this build — the picker must offer SUPP either way, or there is no way to see
   *  the (rightly) empty result and tell that apart from the option being missing entirely */
  hasSuppVars?: boolean
}) {
  const [source, setSource] = useState("parent")
  const [page, setPage] = useState<DataPage | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const load = useCallback(async (src: string) => {
    setBusy(true); setError("")
    try {
      const q = { offset: 0, limit: FULL }
      setPage(src === "parent" || src === "supp"
        ? await api.domainData(domain, q, src as "parent" | "supp")
        : await api.rawData(src.replace(/^raw:/, ""), q))
    } catch (e) { setError((e as Error).message); setPage(null) } finally { setBusy(false) }
  }, [domain])

  useEffect(() => { void load(source) }, [source, load, refreshKey])

  const empties = page?.columns.filter((c) => c.populated === 0).length ?? 0
  const capped = (page?.nrows ?? 0) >= FULL
  const showSupp = page?.has_supp || hasSuppVars

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={source} onValueChange={setSource}>
          <SelectTrigger className="h-8 w-64 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="parent" className="text-xs">{domain} — built dataset</SelectItem>
            {showSupp && <SelectItem value="supp" className="text-xs">SUPP{domain}</SelectItem>}
            {datasets.map((d) => (
              <SelectItem key={d} value={`raw:${d}`} className="text-xs">input · {d}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {error && <Callout tone="bad">{error}</Callout>}
      {capped && (
        <Callout tone="warn" title={`Showing the first ${FULL.toLocaleString()} records`}>
          The dataset is larger than one view can hold. The written file in the run folder has
          all of it.
        </Callout>
      )}
      {page && source === "supp" && page.nrows === 0 && (
        <Callout tone="warn" title="No qualifier values were populated">
          SUPP{domain} variables are defined in the spec, but none of them had a value to
          carry for any record in this build.
        </Callout>
      )}
      {page && empties > 0 && source === "parent" && (
        <Callout tone="warn" title={`${empties} column(s) are present but empty`}>
          A submission-shaped dataset carries every variable the spec defines. An empty column
          is one the spec could not populate — the Variables tab says why for each.
        </Callout>
      )}

      <RecordTable page={page} busy={busy} onRefresh={() => void load(source)} />

      {page && source === "parent" && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
          <span>Columns:</span>
          {["built", "empty", "not_built"].map((st) => {
            const n = page.columns.filter((c) => c.status === st).length
            return n ? <span key={st} className="flex items-center gap-1">
              <Chip tone={STATUS_TONE[st] ?? "slate"}>{st.replace("_", " ")}</Chip>{n}</span> : null
          })}
          {["name_match", "edit", "convention", "template"].map((ms) => {
            const n = page.columns.filter((c) => c.method_source === ms).length
            return n ? <span key={ms} className="flex items-center gap-1">
              <Chip tone={STATUS_TONE[ms] ?? "slate"}>
                {ms === "name_match" ? "name match" : ms === "edit" ? "hand edit"
                  : ms === "template" ? "template" : "convention"}
              </Chip>{n}</span> : null
          })}
          <span className="ml-auto">Drag a column edge to resize · click a header to sort</span>
        </div>
      )}
    </div>
  )
}

function iconFor(c?: DataColumn) {
  if (!c) return AlignLeft
  if (c.numeric) return Hash
  if (/DTC$|DAT$|DATE$/.test(c.name)) return Calendar
  if (/^(USUBJID|STUDYID|SUBJID|DOMAIN)$/.test(c.name)) return Key
  if (/SEQ$/.test(c.name)) return Sigma
  return AlignLeft
}
