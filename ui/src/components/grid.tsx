import type { ReactNode } from "react"
import { useMemo, useRef, useState } from "react"
import { useVirtualizer } from "@tanstack/react-virtual"
import {
  AlignLeft, ArrowDownAZ, ArrowDownWideNarrow, ArrowUpNarrowWide, Calendar,
  ChevronDown, ChevronRight, Hash, Key, Ruler, Sigma, Tag, Type,
} from "lucide-react"
import { cn } from "@/lib/utils"

/** Column kinds carry an icon, the way a database grid tells you what a field holds. */
export type ColKind = "text" | "number" | "date" | "key" | "tag" | "code" | "measure" | "calc" | "sort"

const KIND_ICON: Record<ColKind, typeof Type> = {
  text: AlignLeft, number: Hash, date: Calendar, key: Key, tag: Tag,
  code: Type, measure: Ruler, calc: Sigma, sort: ArrowDownAZ,
}

export type GridCol<T> = {
  id: string
  head: string
  kind?: ColKind
  cell: (row: T, index: number) => ReactNode
  width?: number
  align?: "left" | "right"
  sticky?: boolean
  /** value used for grouping and for the plain-text export of a cell */
  value?: (row: T) => string
}

export function DataGrid<T>({
  cols, rows, rowKey, onRowClick, groupBy, height = "auto", empty, dense = true, rowNumbers = true,
  sort, onSort, filters, onFilter, filterOptions,
}: {
  cols: GridCol<T>[]
  rows: T[]
  rowKey?: (row: T, i: number) => string
  onRowClick?: (row: T) => void
  groupBy?: (row: T) => string
  height?: string
  empty?: ReactNode
  dense?: boolean
  rowNumbers?: boolean
  /** sorting and filtering are handled by the caller, against the whole dataset */
  sort?: { col: string; dir: "asc" | "desc" }
  onSort?: (col: string) => void
  filters?: Record<string, string>
  onFilter?: (col: string, value: string) => void
  filterOptions?: Record<string, string[] | null | undefined>
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})

  // Ungrouped, height-bounded grids render only the rows in view — a 10,000-row table
  // costs the same as a 40-row one, however long the study gets.
  const scrollRef = useRef<HTMLDivElement>(null)
  const VROW = dense ? 33 : 41
  const flat = !groupBy && height !== "auto"
  const virt = useVirtualizer({
    count: flat ? rows.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => VROW,
    overscan: 16,
  })

  const groups = useMemo(() => {
    if (!groupBy) return [["", rows]] as [string, T[]][]
    const m = new Map<string, T[]>()
    rows.forEach((r) => {
      const k = groupBy(r)
      if (!m.has(k)) m.set(k, [])
      m.get(k)!.push(r)
    })
    return [...m.entries()]
  }, [rows, groupBy])

  if (!rows.length) {
    return (
      <div className="fancy-card rounded-xl border bg-surface p-8 text-center text-sm text-muted-foreground">
        {empty ?? "Nothing to show"}
      </div>
    )
  }

  const pad = dense ? "py-1.5" : "py-2.5"
  let running = 0

  const bodyRow = (r: T, i: number, n: number, fixed: boolean) => (
    <tr key={rowKey ? rowKey(r, i) : `r-${n}`}
        onClick={() => onRowClick?.(r)}
        style={fixed ? { height: VROW } : undefined}
        className={cn("grid-row transition-colors", n % 2 === 0 && "row-alt",
                      onRowClick && "cursor-pointer")}>
      {rowNumbers && (
        <td className="num-cell sticky left-0 z-10 border-b bg-surface px-2 text-[11px]
                       text-muted-foreground">{n}</td>
      )}
      {cols.map((c) => (
        <td key={c.id}
            className={cn("whitespace-nowrap border-b px-3", pad,
              c.align === "right" && "num-cell text-right",
              c.sticky && "sticky left-10 z-10 bg-surface font-medium")}>
          {c.cell(r, n - 1)}
        </td>
      ))}
    </tr>
  )

  const vItems = flat ? virt.getVirtualItems() : []
  const vPadTop = vItems.length ? vItems[0].start : 0
  const vPadBottom = vItems.length ? virt.getTotalSize() - vItems[vItems.length - 1].end : 0

  return (
    <div ref={scrollRef} className="fancy-card thin-scroll overflow-auto rounded-xl border bg-surface"
         style={height === "auto" ? undefined : { maxHeight: height }}>
      <table className="w-full border-separate border-spacing-0 text-[13px]">
        <thead className="sticky top-0 z-20">
          <tr className="grid-head-row">
            {rowNumbers && (
              <th className="sticky left-0 z-30 w-10 border-b bg-muted/80 px-2 py-2 text-left
                             text-[11px] font-medium text-muted-foreground backdrop-blur">#</th>
            )}
            {cols.map((c) => {
              const Icon = KIND_ICON[c.kind ?? "text"]
              const sorted = sort?.col === c.id
              return (
                <th key={c.id}
                    style={c.width ? { minWidth: c.width } : undefined}
                    className={cn(
                      "whitespace-nowrap border-b bg-muted/80 px-3 py-2 text-[11px] font-medium",
                      "uppercase tracking-wide text-muted-foreground backdrop-blur",
                      c.align === "right" ? "text-right" : "text-left",
                      c.sticky && "sticky left-10 z-30",
                      sorted && "th-sorted")}>
                  <button type="button" disabled={!onSort}
                          onClick={() => onSort?.(c.id)}
                          className={cn("inline-flex items-center gap-1.5",
                            onSort && "hover:text-foreground",
                            sorted && "text-foreground")}>
                    <Icon className="h-3 w-3 shrink-0 opacity-60" />{c.head}
                    {sorted && (sort!.dir === "asc"
                      ? <ArrowUpNarrowWide className="h-3 w-3" />
                      : <ArrowDownWideNarrow className="h-3 w-3" />)}
                  </button>
                </th>
              )
            })}
          </tr>
          {onFilter && (
            <tr>
              {rowNumbers && <th className="sticky left-0 z-30 border-b bg-muted/60 backdrop-blur" />}
              {cols.map((c) => {
                const opts = filterOptions?.[c.id]
                return (
                  <th key={c.id} className={cn("border-b bg-muted/60 px-1.5 pb-1.5 backdrop-blur",
                                               c.sticky && "sticky left-10 z-30")}>
                    {opts && opts.length > 0 ? (
                      <select value={filters?.[c.id] ?? ""}
                              onChange={(e) => onFilter(c.id, e.target.value)}
                              className={cn("filter-control h-6 w-full rounded-md border px-1 text-[11px]",
                                            "font-normal normal-case text-foreground",
                                            filters?.[c.id] && "filter-active")}>
                        <option value="">All</option>
                        {opts.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    ) : (
                      <input value={filters?.[c.id] ?? ""} placeholder="Search…"
                             onChange={(e) => onFilter(c.id, e.target.value)}
                             className={cn("filter-control h-6 w-full rounded-md border px-1.5 text-[11px]",
                                           "font-normal normal-case text-foreground",
                                           "placeholder:text-muted-foreground/60",
                                           filters?.[c.id] && "filter-active")} />
                    )}
                  </th>
                )
              })}
            </tr>
          )}
        </thead>
        <tbody>
          {flat ? (
            <>
              {vPadTop > 0 && <tr><td style={{ height: vPadTop }} /></tr>}
              {vItems.map((vi) => bodyRow(rows[vi.index], vi.index, vi.index + 1, true))}
              {vPadBottom > 0 && <tr><td style={{ height: vPadBottom }} /></tr>}
            </>
          ) : groups.map(([g, list]) => (
            <>
              {groupBy && (
                <tr key={`g-${g}`}>
                  <td colSpan={cols.length + (rowNumbers ? 1 : 0)}
                      className="group-band sticky left-0 border-b px-3 py-1.5">
                    <button onClick={() => setCollapsed((c) => ({ ...c, [g]: !c[g] }))}
                            className="inline-flex items-center gap-1.5 text-[12px] font-medium">
                      {collapsed[g] ? <ChevronRight className="h-3.5 w-3.5" />
                                    : <ChevronDown className="h-3.5 w-3.5" />}
                      {g}
                      <span className="ml-1 rounded-full bg-background/70 px-1.5 py-px text-[10px]
                                       font-normal text-muted-foreground">{list.length}</span>
                    </button>
                  </td>
                </tr>
              )}
              {!collapsed[g] && list.map((r, i) => {
                running += 1
                return bodyRow(r, i, running, false)
              })}
            </>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** DataGrid with self-contained sorting and per-column filtering, for tables whose rows are
 *  already fully in hand (the comparison views). Each column provides `value` — the plain
 *  text behind its rendered cell — and gets a header filter: a dropdown of the actual values
 *  where there are few enough, a search box otherwise. */
export function ClientGrid<T>({ cols, rows, dropdownMax = 40, ...rest }: {
  cols: GridCol<T>[]
  rows: T[]
  dropdownMax?: number
  rowKey?: (row: T, i: number) => string
  onRowClick?: (row: T) => void
  groupBy?: (row: T) => string
  height?: string
  empty?: ReactNode
  rowNumbers?: boolean
}) {
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [sort, setSort] = useState<{ col: string; dir: "asc" | "desc" } | undefined>()

  const valueOf = useMemo(() => {
    const map: Record<string, (row: T) => string> = {}
    for (const c of cols) map[c.id] = c.value ?? (() => "")
    return map
  }, [cols])

  const options = useMemo(() => {
    const out: Record<string, string[] | undefined> = {}
    for (const c of cols) {
      if (!c.value) continue
      const distinct = [...new Set(rows.map((r) => c.value!(r)).filter((v) => v !== ""))]
      out[c.id] = distinct.length > 0 && distinct.length <= dropdownMax
        ? distinct.sort() : undefined
    }
    return out
  }, [cols, rows, dropdownMax])

  const shown = useMemo(() => {
    let out = rows.filter((r) =>
      Object.entries(filters).every(([col, needle]) => {
        if (!needle) return true
        const v = valueOf[col]?.(r) ?? ""
        return options[col]                       // dropdowns match exactly, search boxes contain
          ? v === needle
          : v.toLowerCase().includes(needle.toLowerCase())
      }))
    if (sort) {
      const get = valueOf[sort.col] ?? (() => "")
      out = [...out].sort((a, b) => {
        const va = get(a), vb = get(b)
        const na = Number(va.replace(/[,%]/g, "")), nb = Number(vb.replace(/[,%]/g, ""))
        const cmp = (!Number.isNaN(na) && !Number.isNaN(nb) && va !== "" && vb !== "")
          ? na - nb : va.localeCompare(vb)
        return sort.dir === "asc" ? cmp : -cmp
      })
    }
    return out
  }, [rows, filters, sort, valueOf, options])

  const filterable = cols.some((c) => c.value)
  return (
    <DataGrid {...rest} cols={cols} rows={shown}
              sort={sort}
              onSort={(col) => setSort(sort?.col === col && sort.dir === "asc"
                ? { col, dir: "desc" } : { col, dir: "asc" })}
              filters={filterable ? filters : undefined}
              onFilter={filterable ? (col, v) => setFilters((f) => ({ ...f, [col]: v })) : undefined}
              filterOptions={options} />
  )
}

/** A coloured chip, the way a grid shows a single-select value. */
export type ChipTone = "violet" | "green" | "amber" | "red" | "blue" | "slate" | "teal" | "fuchsia"

const TONE: Record<ChipTone, string> = {
  violet: "bg-violet-500/12 text-violet-700 ring-violet-500/25 dark:text-violet-300",
  green: "bg-emerald-500/12 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300",
  amber: "bg-amber-500/15 text-amber-700 ring-amber-500/30 dark:text-amber-300",
  red: "bg-rose-500/12 text-rose-700 ring-rose-500/25 dark:text-rose-300",
  blue: "bg-sky-500/12 text-sky-700 ring-sky-500/25 dark:text-sky-300",
  teal: "bg-teal-500/12 text-teal-700 ring-teal-500/25 dark:text-teal-300",
  fuchsia: "bg-fuchsia-500/12 text-fuchsia-700 ring-fuchsia-500/25 dark:text-fuchsia-300",
  slate: "bg-muted text-muted-foreground ring-border",
}

export function Chip({ tone = "slate", children, className }: {
  tone?: ChipTone; children: ReactNode; className?: string
}) {
  return (
    <span className={cn(
      "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset",
      TONE[tone], className)}>{children}</span>
  )
}

export const STATUS_TONE: Record<string, ChipTone> = {
  built: "green", dropped: "slate", not_built: "amber", error: "red", empty: "amber",
  spec: "slate", name_match: "amber", edit: "blue", convention: "teal", template: "violet",
  custom: "fuchsia",
  identical: "green", differences: "red",
}

export function Cell({ children, muted }: { children: ReactNode; muted?: boolean }) {
  return <span className={cn("truncate", muted && "text-muted-foreground")}>{children}</span>
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">{children}</code>
}

/** A thin segmented bar — coverage at a glance without a chart library. */
export function SegmentBar({ segments, className }: {
  segments: Array<{ value: number; tone: ChipTone; label: string }>; className?: string
}) {
  const total = segments.reduce((a, s) => a + s.value, 0) || 1
  const FILL: Record<ChipTone, string> = {
    violet: "bg-violet-500", green: "bg-emerald-500", amber: "bg-amber-500",
    red: "bg-rose-500", blue: "bg-sky-500", teal: "bg-teal-500",
    fuchsia: "bg-fuchsia-500", slate: "bg-muted-foreground/35",
  }
  return (
    <div className={cn("flex h-2 overflow-hidden rounded-full bg-muted", className)}>
      {segments.filter((s) => s.value > 0).map((s, i) => (
        <div key={i} title={`${s.label}: ${s.value.toLocaleString()}`}
             className={FILL[s.tone]} style={{ width: `${(100 * s.value) / total}%` }} />
      ))}
    </div>
  )
}
