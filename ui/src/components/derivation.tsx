/** Plain-language builders for derivation arguments.
 *
 * The engine executes typed structures; nobody mapping a variable should ever see them.
 * These controls read and write those structures behind dropdowns a SAS programmer
 * recognises — SUBSTR, SCAN, CATX, if/then — with the vocabulary they already use. */
import { useEffect, useState } from "react"
import { api } from "@/api"
import type { DomainDetail } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

type Dict = Record<string, unknown>

/* ── the SAS functions, in a SAS programmer's words ─────────────────────── */
export const FN_LABELS: Record<string, string> = {
  substr: "SUBSTR — part of the text", scan: "SCAN — nth word",
  strip: "STRIP — trim both ends", trim: "TRIM — trim trailing blanks",
  left: "LEFT — trim leading blanks", compress: "COMPRESS — remove characters",
  upcase: "UPCASE — uppercase", lowcase: "LOWCASE — lowercase",
  propcase: "PROPCASE — Title Case", reverse: "REVERSE — reverse the text",
  length: "LENGTH — number of characters", index: "INDEX — position of text",
  tranwrd: "TRANWRD — find and replace", catx: "CATX — join with a separator",
  cats: "CATS — join, trimming each", cat: "CAT — join as-is",
  coalesce: "COALESCE — first non-missing", compbl: "COMPBL — squeeze blanks",
  zeropad: "Zero-pad to a width", put: "PUT — number to text", input: "INPUT — text to number",
}
const FN_PARAMS: Record<string, Array<{ k: string; label: string; ph?: string }>> = {
  substr: [{ k: "start", label: "From position", ph: "1" }, { k: "len", label: "Length", ph: "to the end" }],
  scan: [{ k: "word", label: "Word number", ph: "-1 = last" }, { k: "delim", label: "Separated by", ph: "-" }],
  tranwrd: [{ k: "find", label: "Find" }, { k: "replace", label: "Replace with" }],
  index: [{ k: "find", label: "Find" }],
  compress: [{ k: "chars", label: "Characters to remove", ph: "blanks if empty" }],
  catx: [{ k: "sep", label: "Separator", ph: "-" }],
  zeropad: [{ k: "width", label: "Width", ph: "3" }],
}
const MULTI_INPUT = new Set(["catx", "cats", "cat", "coalesce"])

const OPERATORS: [string, string][] = [
  ["eq", "equals"], ["ne", "does not equal"], ["in", "is one of"], ["notin", "is not one of"],
  ["contains", "contains"], ["starts", "starts with"], ["ends", "ends with"],
  ["gt", "greater than"], ["lt", "less than"], ["ge", "at least"], ["le", "at most"],
  ["between", "between"], ["missing", "is missing"], ["notmissing", "is not missing"],
]
const NO_VALUE = new Set(["missing", "notmissing"])

function Sel({ value, onChange, options, width = "w-44", placeholder = "—" }: {
  value: string; onChange: (v: string) => void
  options: [string, string][]; width?: string; placeholder?: string
}) {
  return (
    <Select value={value || "__none"} onValueChange={(v) => onChange(v === "__none" ? "" : v)}>
      <SelectTrigger className={`h-8 ${width} text-xs`}><SelectValue placeholder={placeholder} /></SelectTrigger>
      <SelectContent>
        <SelectItem value="__none" className="text-xs">{placeholder}</SelectItem>
        {options.map(([v, l]) => <SelectItem key={v} value={v} className="text-xs">{l}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

/* ── one value source: a raw column, another variable, text, or the running value ── */
export function SourceControl({ value, onChange, detail, allowSelf }: {
  value: Dict; onChange: (v: Dict) => void; detail: DomainDetail; allowSelf?: boolean
}) {
  const v = value ?? {}
  const kind = (v.kind as string)
    || (v.var ? "var" : v.text !== undefined ? "text" : v.dataset || v.column ? "raw" : "raw")
  const [cols, setCols] = useState<string[]>([])
  const ds = (v.dataset as string) ?? ""

  useEffect(() => {
    if (kind !== "raw" || !ds) { setCols([]); return }
    let live = true
    api.columns(detail.domain, ds).then((r) => live && setCols(r.columns)).catch(() => setCols([]))
    return () => { live = false }
  }, [kind, ds, detail.domain])

  const kinds: [string, string][] = [
    ["raw", "Raw column"], ["var", "Another variable"], ["text", "Fixed text"],
    ...(allowSelf ? [["self", "The value so far"] as [string, string]] : []),
  ]
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <Sel width="w-36" value={kind === "raw" && !ds && !v.column ? kind : kind}
           options={kinds}
           onChange={(k) => onChange(k === "raw" ? {} : k === "text" ? { kind: "text", text: "" }
                                    : { kind: k })} />
      {kind === "raw" && <>
        <Sel width="w-40" placeholder="dataset" value={ds}
             options={[...detail.prepared_datasets.map((d): [string, string] => [d, `${d} (prepared)`]),
                       ...detail.datasets.filter((d) => !detail.prepared_datasets.includes(d))
                         .map((d): [string, string] => [d, d])]}
             onChange={(d) => onChange({ dataset: d, column: "" })} />
        <Sel width="w-40" placeholder="column" value={(v.column as string) ?? ""}
             options={cols.map((c): [string, string] => [c, c])}
             onChange={(c) => onChange({ dataset: ds, column: c })} />
      </>}
      {kind === "var" && (
        <Sel width="w-40" placeholder="variable" value={(v.var as string) ?? ""}
             options={detail.variables.map((x): [string, string] => [x.variable, x.variable])}
             onChange={(name) => onChange({ kind: "var", var: name })} />
      )}
      {kind === "text" && (
        <Input className="h-8 w-40 text-xs" placeholder="the text" value={(v.text as string) ?? ""}
               onChange={(e) => onChange({ kind: "text", text: e.target.value })} />
      )}
    </span>
  )
}

/* ── a SAS function with its inputs and parameters ──────────────────────── */
export function FnControl({ args, onChange, detail, allowSelf }: {
  args: Dict; onChange: (a: Dict) => void; detail: DomainDetail; allowSelf?: boolean
}) {
  const fn = (args.fn as string) ?? ""
  const sources = (args.sources as Dict[]) ?? []
  const set = (patch: Dict) => onChange({ ...args, ...patch })
  const multi = MULTI_INPUT.has(fn)

  return (
    <div className="space-y-2">
      <Sel width="w-64" placeholder="choose a function" value={fn}
           options={Object.entries(FN_LABELS)}
           onChange={(f) => onChange({ fn: f, sources: sources.length ? sources
             : [allowSelf ? { kind: "self" } : {}] })} />
      {fn && (
        <div className="space-y-1.5">
          {(sources.length ? sources : [allowSelf ? { kind: "self" } : {}]).map((src, i) => (
            <div key={i} className="flex flex-wrap items-center gap-1.5">
              <span className="w-12 text-[11px] text-muted-foreground">
                {multi ? `input ${i + 1}` : "of"}</span>
              <SourceControl value={src} detail={detail} allowSelf={allowSelf}
                             onChange={(v) => set({ sources: sources.map((x, k) => k === i ? v : x) })} />
              {multi && sources.length > 1 && (
                <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs"
                        onClick={() => set({ sources: sources.filter((_, k) => k !== i) })}>
                  Remove</Button>)}
            </div>
          ))}
          {multi && (
            <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
                    onClick={() => set({ sources: [...sources, {}] })}>Add an input</Button>)}
          <div className="flex flex-wrap gap-3">
            {(FN_PARAMS[fn] ?? []).map((pdef) => (
              <label key={pdef.k} className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                {pdef.label}
                <Input className="h-7 w-24 text-xs" placeholder={pdef.ph ?? ""}
                       value={(args[pdef.k] as string) ?? ""}
                       onChange={(e) => set({ [pdef.k]: e.target.value })} />
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/* ── if / then rules ────────────────────────────────────────────────────── */
export function CondControl({ args, onChange, detail }: {
  args: Dict; onChange: (a: Dict) => void; detail: DomainDetail
}) {
  const rules = (args.rules as Dict[]) ?? []
  const els = (args["else"] as Dict) ?? { kind: "missing" }
  const set = (patch: Dict) => onChange({ ...args, ...patch })

  const Result = ({ value, onChange: oc }: { value: Dict; onChange: (v: Dict) => void }) => {
    const isMissing = (value?.kind as string) === "missing"
    return (
      <span className="inline-flex items-center gap-1.5">
        <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
          <input type="checkbox" checked={isMissing}
                 onChange={(e) => oc(e.target.checked ? { kind: "missing" } : { kind: "text", text: "" })} />
          leave blank
        </label>
        {!isMissing && <SourceControl value={value} onChange={oc} detail={detail} />}
      </span>
    )
  }

  return (
    <div className="space-y-2">
      {rules.map((r, i) => {
        const op = (r.op as string) ?? "eq"
        return (
          <div key={i} className="space-y-1.5 rounded-md border p-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] font-medium">{i === 0 ? "IF" : "ELSE IF"}</span>
              <SourceControl value={(r.src as Dict) ?? {}} detail={detail}
                             onChange={(v) => set({ rules: rules.map((x, k) => k === i ? { ...x, src: v } : x) })} />
              <Sel width="w-40" value={op} options={OPERATORS}
                   onChange={(o) => set({ rules: rules.map((x, k) => k === i ? { ...x, op: o } : x) })} />
              {!NO_VALUE.has(op) && (
                <Input className="h-8 w-36 text-xs"
                       placeholder={op === "in" || op === "notin" ? "A, B, C" : "value"}
                       value={(r.value as string) ?? ""}
                       onChange={(e) => set({ rules: rules.map((x, k) => k === i ? { ...x, value: e.target.value } : x) })} />)}
              {op === "between" && (
                <Input className="h-8 w-24 text-xs" placeholder="and"
                       value={(r.value2 as string) ?? ""}
                       onChange={(e) => set({ rules: rules.map((x, k) => k === i ? { ...x, value2: e.target.value } : x) })} />)}
              <Button type="button" size="sm" variant="ghost" className="ml-auto h-7 px-2 text-xs"
                      onClick={() => set({ rules: rules.filter((_, k) => k !== i) })}>Remove</Button>
            </div>
            <div className="flex flex-wrap items-center gap-1.5 pl-6">
              <span className="text-[11px] font-medium">THEN {detail ? "" : ""}set it to</span>
              <Result value={(r.then as Dict) ?? { kind: "text", text: "" }}
                      onChange={(v) => set({ rules: rules.map((x, k) => k === i ? { ...x, then: v } : x) })} />
            </div>
          </div>
        )
      })}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
                onClick={() => set({ rules: [...rules, { src: {}, op: "eq", value: "",
                                                        then: { kind: "text", text: "" } }] })}>
          Add a rule</Button>
        <span className="text-[11px] font-medium">OTHERWISE</span>
        <Result value={els} onChange={(v) => set({ else: v })} />
      </div>
      <p className="text-[10px] text-muted-foreground">Rules run in order — the first that matches wins, like a SAS IF/ELSE IF chain.</p>
    </div>
  )
}

/* ── a pipeline: ordered steps on one variable ──────────────────────────── */
const STEP_KINDS: [string, string][] = [
  ["assign", "Take a raw column"], ["constant", "Set a fixed value"],
  ["fn", "Apply a SAS function"], ["cond", "If / then rules"],
]

export function PipelineControl({ steps, onChange, detail }: {
  steps: Dict[]; onChange: (s: Dict[]) => void; detail: DomainDetail
}) {
  const upd = (i: number, patch: Dict) => onChange(steps.map((s, k) => k === i ? { ...s, ...patch } : s))
  const [colsFor, setColsFor] = useState<Record<string, string[]>>({})
  const loadCols = (ds: string) => {
    if (!ds || colsFor[ds]) return
    api.columns(detail.domain, ds).then((r) => setColsFor((c) => ({ ...c, [ds]: r.columns })))
      .catch(() => setColsFor((c) => ({ ...c, [ds]: [] })))
  }
  useEffect(() => { steps.forEach((s) => s.op === "assign" && loadCols(s.dataset as string)) })

  return (
    <div className="space-y-2">
      {steps.map((st, i) => {
        const op = (st.op as string) ?? "fn"
        return (
          <div key={i} className="rounded-md border p-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="grid h-5 w-5 place-items-center rounded-full bg-muted text-[10px] font-semibold">{i + 1}</span>
              <Sel width="w-44" value={op} options={STEP_KINDS}
                   onChange={(o) => upd(i, o === "fn"
                     ? { op: o, args: { fn: "", sources: [{ kind: "self" }] }, dataset: undefined, column: undefined, value: undefined }
                     : o === "cond" ? { op: o, args: { rules: [], else: { kind: "missing" } } }
                     : { op: o, args: undefined })} />
              {op === "assign" && <>
                <Sel width="w-40" placeholder="dataset" value={(st.dataset as string) ?? ""}
                     options={[...detail.prepared_datasets.map((d): [string, string] => [d, `${d} (prepared)`]),
                               ...detail.datasets.filter((d) => !detail.prepared_datasets.includes(d))
                                 .map((d): [string, string] => [d, d])]}
                     onChange={(d) => { upd(i, { dataset: d, column: "" }); loadCols(d) }} />
                <Sel width="w-40" placeholder="column" value={(st.column as string) ?? ""}
                     options={(colsFor[(st.dataset as string) ?? ""] ?? []).map((c): [string, string] => [c, c])}
                     onChange={(c) => upd(i, { column: c })} />
              </>}
              {op === "constant" && (
                <Input className="h-8 w-44 text-xs" placeholder="the value"
                       value={(st.value as string) ?? ""}
                       onChange={(e) => upd(i, { value: e.target.value })} />)}
              <span className="ml-auto flex gap-1">
                <Button type="button" size="icon" variant="ghost" className="h-7 w-7" disabled={i === 0}
                        onClick={() => { const n = [...steps]; [n[i - 1], n[i]] = [n[i], n[i - 1]]; onChange(n) }}>↑</Button>
                <Button type="button" size="icon" variant="ghost" className="h-7 w-7"
                        disabled={i === steps.length - 1}
                        onClick={() => { const n = [...steps]; [n[i + 1], n[i]] = [n[i], n[i + 1]]; onChange(n) }}>↓</Button>
                <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs"
                        onClick={() => onChange(steps.filter((_, k) => k !== i))}>Remove</Button>
              </span>
            </div>
            {op === "fn" && (
              <div className="mt-2 pl-6">
                <FnControl args={(st.args as Dict) ?? {}} detail={detail} allowSelf={i > 0}
                           onChange={(a) => upd(i, { args: a })} />
              </div>)}
            {op === "cond" && (
              <div className="mt-2 pl-6">
                <CondControl args={(st.args as Dict) ?? {}} detail={detail}
                             onChange={(a) => upd(i, { args: a })} />
              </div>)}
          </div>
        )
      })}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange([...steps, steps.length === 0
                ? { op: "assign" } : { op: "fn", args: { fn: "", sources: [{ kind: "self" }] } }])}>
        Add a step</Button>
      {steps.length === 0 && (
        <p className="text-[11px] text-muted-foreground">
          Start with the column to take, then add the transformations to apply to it.</p>)}
    </div>
  )
}
