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
/* Both helpers live at MODULE level on purpose: defined inside CondControl they would
   be a new component type on every keystroke, React would remount the <Input>, and the
   reader could never type more than one letter before losing focus. */
function CondResult({ value, onChange: oc, detail, allowSelf }: {
  value: Dict; onChange: (v: Dict) => void; detail: DomainDetail; allowSelf?: boolean
}) {
  const isMissing = (value?.kind as string) === "missing"
  return (
    <span className="inline-flex items-center gap-1.5">
      <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
        <input type="checkbox" checked={isMissing}
               onChange={(e) => oc(e.target.checked ? { kind: "missing" } : { kind: "text", text: "" })} />
        leave blank
      </label>
      {!isMissing && <SourceControl value={value} onChange={oc} detail={detail} allowSelf={allowSelf} />}
    </span>
  )
}

/** src + operator + value inputs for one condition — the rule itself or an AND extra. */
function CondInputs({ cond, onPatch, detail, allowSelf }: {
  cond: Dict; onPatch: (p: Dict) => void; detail: DomainDetail; allowSelf?: boolean
}) {
  const op = (cond.op as string) ?? "eq"
  return (
    <>
      <SourceControl value={(cond.src as Dict) ?? {}} detail={detail} allowSelf={allowSelf}
                     onChange={(v) => onPatch({ src: v })} />
      <Sel width="w-40" value={op} options={OPERATORS} onChange={(o) => onPatch({ op: o })} />
      {!NO_VALUE.has(op) && (
        <Input className="h-8 w-36 text-xs"
               placeholder={op === "in" || op === "notin" ? "A, B, C" : "value"}
               value={(cond.value as string) ?? ""}
               onChange={(e) => onPatch({ value: e.target.value })} />)}
      {op === "between" && (
        <Input className="h-8 w-24 text-xs" placeholder="and"
               value={(cond.value2 as string) ?? ""}
               onChange={(e) => onPatch({ value2: e.target.value })} />)}
    </>
  )
}

export function CondControl({ args, onChange, detail, allowSelf }: {
  args: Dict; onChange: (a: Dict) => void; detail: DomainDetail; allowSelf?: boolean
}) {
  const rules = (args.rules as Dict[]) ?? []
  const els = (args["else"] as Dict) ?? { kind: "missing" }
  const set = (patch: Dict) => onChange({ ...args, ...patch })
  const patchRule = (i: number, patch: Dict) =>
    set({ rules: rules.map((x, k) => k === i ? { ...x, ...patch } : x) })

  return (
    <div className="space-y-2">
      {rules.map((r, i) => {
        const ands = (r.and as Dict[]) ?? []
        return (
          <div key={i} className="space-y-1.5 rounded-md border p-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] font-medium">{i === 0 ? "IF" : "ELSE IF"}</span>
              <CondInputs cond={r} detail={detail} allowSelf={allowSelf} onPatch={(p) => patchRule(i, p)} />
              <Button type="button" size="sm" variant="outline" className="h-7 px-2 text-xs"
                      title="add another condition this rule must ALSO satisfy"
                      onClick={() => patchRule(i, { and: [...ands, { src: {}, op: "eq", value: "" }] })}>
                + AND</Button>
              <Button type="button" size="sm" variant="ghost" className="ml-auto h-7 px-2 text-xs"
                      onClick={() => set({ rules: rules.filter((_, k) => k !== i) })}>Remove</Button>
            </div>
            {ands.map((c, j) => (
              <div key={j} className="flex flex-wrap items-center gap-1.5 pl-6">
                <span className="text-[11px] font-medium">AND</span>
                <CondInputs cond={c} detail={detail} allowSelf={allowSelf}
                            onPatch={(p) => patchRule(i, { and: ands.map((x, k) => k === j ? { ...x, ...p } : x) })} />
                <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs"
                        onClick={() => patchRule(i, { and: ands.filter((_, k) => k !== j) })}>
                  Remove</Button>
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-1.5 pl-6">
              <span className="text-[11px] font-medium">THEN {detail ? "" : ""}set it to</span>
              <CondResult detail={detail} allowSelf={allowSelf} value={(r.then as Dict) ?? { kind: "text", text: "" }}
                      onChange={(v) => patchRule(i, { then: v })} />
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
        <CondResult detail={detail} allowSelf={allowSelf} value={els} onChange={(v) => set({ else: v })} />
      </div>
      <p className="text-[10px] text-muted-foreground">Rules run in order — the first that matches wins,
        like a SAS IF/ELSE IF chain. “+ AND” adds extra conditions to a rule; all of them must hold
        (e.g. IF RGMDTN is not missing AND RGSCAT = …).
        {allowSelf && <> A condition or result can also be “the value so far” — the previous
          step's output — so this step can act on what a max-date, function or another rule
          just produced.</>}</p>
    </div>
  )
}

/* ── a pipeline: ordered steps on one variable ──────────────────────────── */
// the sdtm.oak algorithm set, as dropdown steps — same names a mapper knows from R
const STEP_KINDS: [string, string][] = [
  ["assign", "Take a raw column — assign_no_ct"],
  ["constant", "Set a fixed value — hardcode_no_ct"],
  ["ct", "Apply controlled terminology — assign_ct"],
  ["iso_date", "Collected date \u2192 ISO 8601 — assign_datetime"],
  ["study_day", "Study day — derive_study_day"],
  ["date_extreme", "Earliest / latest date per subject"],
  ["age", "Age at a reference date, with fallback"],
  ["fn", "Apply a SAS function"],
  ["cond", "If / then rules — condition_add"],
]

const STEP_INIT: Record<string, Dict> = {
  assign: { op: "assign" },
  constant: { op: "constant" },
  ct: { op: "ct", args: { sources: [{}], codelist: "" } },
  iso_date: { op: "iso_date", args: { dataset: "", date_col: "" } },
  study_day: { op: "study_day", args: { dtc_var: "", ref_var: "RFSTDTC" } },
  date_extreme: { op: "date_extreme", args: { func: "min", sources: [] } },
  age: { op: "age", args: { age_col: "", age_dataset: "", birth_var: "BRTHDTC", ref_var: ["RFSTDTC"] } },
  fn: { op: "fn", args: { fn: "", sources: [{ kind: "self" }] } },
  cond: { op: "cond", args: { rules: [], else: { kind: "missing" } } },
}

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
              <Sel width="w-64" value={op} options={STEP_KINDS}
                   onChange={(o) => upd(i, { dataset: undefined, column: undefined,
                                             value: undefined, args: undefined,
                                             ...(STEP_INIT[o] ?? { op: o }) })} />
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
                <CondControl args={(st.args as Dict) ?? {}} detail={detail} allowSelf={i > 0}
                             onChange={(a) => upd(i, { args: a })} />
              </div>)}
            {op === "ct" && (
              <div className="mt-2 pl-6">
                <CtControl args={(st.args as Dict) ?? {}} detail={detail} allowSelf={i > 0}
                           onChange={(a) => upd(i, { args: a })} />
              </div>)}
            {op === "iso_date" && (
              <div className="mt-2 pl-6">
                <IsoDateControl args={(st.args as Dict) ?? {}} detail={detail}
                                onChange={(a) => upd(i, { args: a })} />
              </div>)}
            {op === "study_day" && (
              <div className="mt-2 pl-6">
                <StudyDayControl args={(st.args as Dict) ?? {}} detail={detail}
                                 onChange={(a) => upd(i, { args: a })} />
              </div>)}
            {op === "date_extreme" && (
              <div className="mt-2 pl-6">
                <DateExtremeControl args={(st.args as Dict) ?? {}} detail={detail}
                                    onChange={(a) => upd(i, { args: a })} />
              </div>)}
            {op === "age" && (
              <div className="mt-2 pl-6">
                <AgeControl args={(st.args as Dict) ?? {}} detail={detail}
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


/* ── sdtm.oak-style step controls ─────────────────────────────────────────── */

/** assign_ct / hardcode_ct: a source normalised to the codelist's submission values. */
export function CtControl({ args, onChange, detail, allowSelf }: {
  args: Dict; onChange: (a: Dict) => void; detail: DomainDetail; allowSelf?: boolean
}) {
  const sources = (args.sources as Dict[]) ?? [{}]
  const codelists = ((detail as unknown as { codelists?: string[] }).codelists) ?? []
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">normalise</span>
      <SourceControl value={sources[0] ?? {}} detail={detail} allowSelf={allowSelf}
                     onChange={(v) => onChange({ ...args, sources: [v] })} />
      <span className="text-muted-foreground">to the submission values of codelist</span>
      {codelists.length ? (
        <Sel width="w-44" placeholder="codelist" value={(args.codelist as string) ?? ""}
             options={codelists.map((c): [string, string] => [c, c])}
             onChange={(c) => onChange({ ...args, codelist: c })} />
      ) : (
        <Input className="h-8 w-40 text-xs" placeholder="codelist name"
               value={(args.codelist as string) ?? ""}
               onChange={(e) => onChange({ ...args, codelist: e.target.value })} />
      )}
      <span className="text-muted-foreground">— unmatched values pass through and are flagged</span>
    </div>
  )
}

/** assign_datetime / create_iso8601: a collected date (and time) to ISO 8601. */
export function IsoDateControl({ args, onChange, detail }: {
  args: Dict; onChange: (a: Dict) => void; detail: DomainDetail
}) {
  const ds = (args.dataset as string) ?? ""
  const [cols, setCols] = useState<string[]>([])
  useEffect(() => {
    if (!ds) { setCols([]); return }
    let live = true
    api.columns(detail.domain, ds).then((r) => live && setCols(r.columns)).catch(() => setCols([]))
    return () => { live = false }
  }, [ds, detail.domain])
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">the date collected in</span>
      <Sel width="w-40" placeholder="dataset" value={ds}
           options={[...detail.prepared_datasets.map((d): [string, string] => [d, `${d} (prepared)`]),
                     ...detail.datasets.filter((d) => !detail.prepared_datasets.includes(d))
                       .map((d): [string, string] => [d, d])]}
           onChange={(d) => onChange({ ...args, dataset: d, date_col: "", time_col: "" })} />
      <Sel width="w-40" placeholder="date column" value={(args.date_col as string) ?? ""}
           options={cols.map((c): [string, string] => [c, c])}
           onChange={(c) => onChange({ ...args, date_col: c })} />
      <span className="text-muted-foreground">time (optional)</span>
      <Sel width="w-36" placeholder="none" value={(args.time_col as string) ?? ""}
           options={cols.map((c): [string, string] => [c, c])}
           onChange={(c) => onChange({ ...args, time_col: c })} />
      <span className="text-muted-foreground">
        → ISO 8601; split year/month/day parts beside the column are found on their own</span>
    </div>
  )
}

/** derive_study_day: (--DTC − reference) + 1, no day zero. */
export function StudyDayControl({ args, onChange, detail }: {
  args: Dict; onChange: (a: Dict) => void; detail: DomainDetail
}) {
  const vars = detail.variables.map((x): [string, string] => [x.variable, x.variable])
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">days from</span>
      <Sel width="w-40" placeholder="reference (RFSTDTC)" value={(args.ref_var as string) ?? "RFSTDTC"}
           options={vars} onChange={(v) => onChange({ ...args, ref_var: v })} />
      <span className="text-muted-foreground">to the event date</span>
      <Sel width="w-40" placeholder="--DTC variable" value={(args.dtc_var as string) ?? ""}
           options={vars} onChange={(v) => onChange({ ...args, dtc_var: v })} />
      <span className="text-muted-foreground">— +1 on or after the reference, never day 0</span>
    </div>
  )
}

/** Earliest / latest date per subject, pooled across datasets. */
export function DateExtremeControl({ args, onChange, detail }: {
  args: Dict; onChange: (a: Dict) => void; detail: DomainDetail
}) {
  const sources = (args.sources as Dict[]) ?? []
  return (
    <div className="space-y-1.5 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="text-muted-foreground">take the</span>
        <Sel width="w-32" value={(args.func as string) ?? "min"}
             options={[["min", "earliest"], ["max", "latest"]]}
             onChange={(f) => onChange({ ...args, func: f })} />
        <span className="text-muted-foreground">per subject, across:</span>
      </div>
      {sources.map((src, i) => (
        <DateExtremeSourceRow key={i} src={src} detail={detail}
          onChange={(nr) => onChange({ ...args, sources: sources.map((x, k) => (k === i ? nr : x)) })}
          onRemove={() => onChange({ ...args, sources: sources.filter((_, k) => k !== i) })} />
      ))}
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs"
              onClick={() => onChange({ ...args, sources: [...sources, {}] })}>
        Add a dataset</Button>
    </div>
  )
}

function DateExtremeSourceRow({ src, detail, onChange, onRemove }: {
  src: Dict; detail: DomainDetail; onChange: (v: Dict) => void; onRemove: () => void
}) {
  const ds = (src.dataset as string) ?? ""
  const [cols, setCols] = useState<string[]>([])
  useEffect(() => {
    if (!ds) { setCols([]); return }
    let live = true
    api.columns(detail.domain, ds).then((r) => live && setCols(r.columns)).catch(() => setCols([]))
    return () => { live = false }
  }, [ds, detail.domain])
  return (
    <div className="flex items-center gap-1.5">
      <Sel width="w-40" placeholder="dataset" value={ds}
           options={[...detail.prepared_datasets.map((d): [string, string] => [d, `${d} (prepared)`]),
                     ...detail.datasets.filter((d) => !detail.prepared_datasets.includes(d))
                       .map((d): [string, string] => [d, d])]}
           onChange={(d) => onChange({ dataset: d, date_col: "" })} />
      <span className="text-[11px] text-muted-foreground">date in</span>
      <Sel width="w-40" placeholder="date column" value={(src.date_col as string) ?? ""}
           options={cols.map((c): [string, string] => [c, c])}
           onChange={(c) => onChange({ ...src, date_col: c })} />
      <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs"
              onClick={onRemove}>Remove</Button>
    </div>
  )
}

/** One AGE date input: a built SDTM variable, or a raw/prepared dataset column read
 *  directly — e.g. a birth or randomization date the spec never turned into its own
 *  variable (BRTHIMDT in a prepared merge, RGMNDTN in an IxRS extract). */
function AgeDateSpec({ value, detail, onChange, placeholder }: {
  value: string | Dict; detail: DomainDetail; onChange: (v: string | Dict) => void
  placeholder: string
}) {
  const isRaw = typeof value === "object" && value !== null
  const ds = isRaw ? ((value as Dict).dataset as string) ?? "" : ""
  const [cols, setCols] = useState<string[]>([])
  useEffect(() => {
    if (!isRaw || !ds) { setCols([]); return }
    let live = true
    api.columns(detail.domain, ds).then((r) => live && setCols(r.columns)).catch(() => setCols([]))
    return () => { live = false }
  }, [isRaw, ds, detail.domain])
  const vars = detail.variables.map((x): [string, string] => [x.variable, x.variable])

  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <Sel width="w-36" value={isRaw ? "raw" : "var"}
           options={[["var", "a built variable"], ["raw", "a raw/prepared column"]]}
           onChange={(k) => onChange(k === "raw" ? { dataset: "", column: "" } : "")} />
      {isRaw ? (
        <>
          <Sel width="w-40" placeholder="dataset" value={ds}
               options={[...detail.prepared_datasets.map((d): [string, string] => [d, `${d} (prepared)`]),
                         ...detail.datasets.filter((d) => !detail.prepared_datasets.includes(d))
                           .map((d): [string, string] => [d, d])]}
               onChange={(d) => onChange({ dataset: d, column: "" })} />
          <Sel width="w-40" placeholder="column" value={((value as Dict).column as string) ?? ""}
               options={cols.map((c): [string, string] => [c, c])}
               onChange={(c) => onChange({ dataset: ds, column: c })} />
        </>
      ) : (
        <Sel width="w-40" placeholder={placeholder} value={(value as string) ?? ""}
             options={vars} onChange={(v) => onChange(v)} />
      )}
    </span>
  )
}

/** AGE: reported age column first, then birth→reference whole years underneath — with a
 *  priority-ordered fallback list of reference dates (e.g. randomization, then consent, as
 *  a company template's age1/age2 SAS macro chain would). */
export function AgeControl({ args, onChange, detail }: {
  args: Dict; onChange: (a: Dict) => void; detail: DomainDetail
}) {
  const ds = (args.age_dataset as string) ?? ""
  const [cols, setCols] = useState<string[]>([])
  useEffect(() => {
    if (!ds) { setCols([]); return }
    let live = true
    api.columns(detail.domain, ds).then((r) => live && setCols(r.columns)).catch(() => setCols([]))
    return () => { live = false }
  }, [ds, detail.domain])
  const refs = Array.isArray(args.ref_var) ? (args.ref_var as (string | Dict)[])
    : args.ref_var ? [args.ref_var as string | Dict] : []
  const setRefs = (r: (string | Dict)[]) => onChange({ ...args, ref_var: r })

  return (
    <div className="space-y-1.5 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-muted-foreground">reported age from</span>
        <Sel width="w-40" placeholder="dataset" value={ds}
             options={[...detail.prepared_datasets.map((d): [string, string] => [d, `${d} (prepared)`]),
                       ...detail.datasets.filter((d) => !detail.prepared_datasets.includes(d))
                         .map((d): [string, string] => [d, d])]}
             onChange={(d) => onChange({ ...args, age_dataset: d, age_col: "" })} />
        <Sel width="w-40" placeholder="age column (optional)" value={(args.age_col as string) ?? ""}
             options={cols.map((c): [string, string] => [c, c])}
             onChange={(c) => onChange({ ...args, age_col: c })} />
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-muted-foreground">records with none collected get whole years from</span>
        <AgeDateSpec value={(args.birth_var as string | Dict) ?? "BRTHDTC"} detail={detail}
                     placeholder="birth date variable"
                     onChange={(v) => onChange({ ...args, birth_var: v })} />
        <span className="text-muted-foreground">to:</span>
      </div>
      <div className="space-y-1">
        {refs.map((r, i) => (
          <div key={i} className="flex items-center gap-1.5 pl-4">
            <span className="w-16 shrink-0 text-[11px] text-muted-foreground">{i === 0 ? "first try" : "then try"}</span>
            <AgeDateSpec value={r} detail={detail} placeholder="reference date variable"
                        onChange={(v) => setRefs(refs.map((x, k) => (k === i ? v : x)))} />
            <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs"
                    onClick={() => setRefs(refs.filter((_, k) => k !== i))}>Remove</Button>
          </div>
        ))}
        <Button type="button" size="sm" variant="outline" className="ml-4 h-7 text-xs"
                onClick={() => setRefs([...refs, ""])}>
          {refs.length ? "Add a fallback reference date" : "Add a reference date"}</Button>
      </div>
      <p className="pl-4 text-[10px] text-muted-foreground">
        Anniversary rule, never a fraction. A partial birth date is completed to its 1st for
        the comparison — a bare year 1980 becomes 1980-01-01, a year-month 1980-06 becomes
        1980-06-01. With more than one reference date listed, the first one present for a
        record wins — a later one only fills what an earlier one left missing (e.g.
        randomization date, then consent date for screen failures).</p>
    </div>
  )
}

