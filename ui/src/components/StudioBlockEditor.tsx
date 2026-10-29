import { useEffect, useState } from "react"
import { ArrowDown, ArrowUp, Loader2, Plus, RotateCcw, Trash2 } from "lucide-react"
import { api } from "@/api"
import type { StudioBlock, StudioBlocksInfo, StudioValueRef } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"
import { Chip, Mono } from "./grid"
import { Callout } from "./shell"
import { StudioObjectFields } from "./PipelineEditor"

/* ── A studio variable's mapping, edited the way SDTM Studio's Domain Build edits it ──
   Same mapping types, same IF / THEN / ELSE editor, same save underneath (the edited
   block joins the study's saved Domain Build, so a later Pull or export agrees). */

const MTYPES: Array<{ k: string; l: string }> = [
  { k: "assign", l: "Copy from column" },
  { k: "constant", l: "Fixed value" },
  { k: "sequence", l: "Sequence number" },
  { k: "derived:fn", l: "SAS function (SCAN, PUT, dates …)" },
  { k: "derived:cond", l: "IF / THEN / ELSE" },
  { k: "derived:pipeline", l: "Multiple steps" },
  { k: "derived:retain", l: "Carry forward within a group (RETAIN)" },
  { k: "derived:lag", l: "Previous record within a group (LAG / DIF)" },
  { k: "derived", l: "Derived (other recipes)" },
  { k: "drop", l: "Exclude (drop)" },
  { k: "unmapped", l: "Unmapped" },
]
const SHORTCUTS = new Set(["fn", "cond", "pipeline", "retain", "lag"])
const mtypeKey = (b: StudioBlock) =>
  b.mtype === "derived" && b.recipe && SHORTCUTS.has(b.recipe) ? `derived:${b.recipe}` : b.mtype

const COND_OPS: Array<{ k: string; l: string }> = [
  { k: "eq", l: "=" }, { k: "ne", l: "≠" }, { k: "in", l: "in list" }, { k: "notin", l: "not in list" },
  { k: "contains", l: "contains" }, { k: "starts", l: "starts with" }, { k: "ends", l: "ends with" },
  { k: "missing", l: "is blank" }, { k: "notmissing", l: "is not blank" },
  { k: "gt", l: ">" }, { k: "lt", l: "<" }, { k: "ge", l: "≥" }, { k: "le", l: "≤" },
  { k: "between", l: "between" },
]
const COND_NOVAL = new Set(["missing", "notmissing"])

const FN_LIST: Array<{ k: string; l: string; multi: boolean; fields: string[] }> = [
  { k: "substr", l: "SUBSTR — extract part", multi: false, fields: ["start", "len"] },
  { k: "scan", l: "SCAN — nth delimited word", multi: false, fields: ["delim", "word"] },
  { k: "strip", l: "STRIP — trim both ends", multi: false, fields: [] },
  { k: "trim", l: "TRIM — trim trailing spaces", multi: false, fields: [] },
  { k: "left", l: "LEFT — trim leading spaces", multi: false, fields: [] },
  { k: "compress", l: "COMPRESS — remove characters", multi: false, fields: ["chars"] },
  { k: "upcase", l: "UPCASE — uppercase", multi: false, fields: [] },
  { k: "lowcase", l: "LOWCASE — lowercase", multi: false, fields: [] },
  { k: "propcase", l: "PROPCASE — Title Case", multi: false, fields: [] },
  { k: "reverse", l: "REVERSE — reverse text", multi: false, fields: [] },
  { k: "length", l: "LENGTH — character length", multi: false, fields: [] },
  { k: "index", l: "INDEX — position of substring", multi: false, fields: ["find"] },
  { k: "tranwrd", l: "TRANWRD — find & replace", multi: false, fields: ["find", "replace"] },
  { k: "catx", l: "CATX — join non-missing with separator", multi: true, fields: ["sep"] },
  { k: "cats", l: "CATS — concatenate, strip each", multi: true, fields: [] },
  { k: "cat", l: "CAT — concatenate as-is", multi: true, fields: [] },
]

type CondRule = { src?: StudioValueRef; op?: string; value?: string; value2?: string; then?: StudioValueRef }

const KW = ({ children }: { children: React.ReactNode }) => (
  <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] font-bold text-primary">
    {children}</span>
)

/** Dataset picker — grouped the way the studio groups them: prepared, raw, built SDTM. */
function DsPick({ val, onCh, info }: { val: string; onCh: (v: string) => void; info: StudioBlocksInfo }) {
  const prep = new Set(info.prep_outputs)
  const names = Object.keys(info.sources)
  const groups: Array<[string, string[]]> = [
    ["Prepared", names.filter((n) => prep.has(n)).sort()],
    ["Raw", names.filter((n) => !prep.has(n) && !n.startsWith("sdtm.")).sort()],
    ["Built SDTM", names.filter((n) => n.startsWith("sdtm.")).sort()],
  ]
  return (
    <Select value={val || ""} onValueChange={onCh}>
      <SelectTrigger className="h-7 w-44 font-mono text-xs"><SelectValue placeholder="dataset…" /></SelectTrigger>
      <SelectContent className="max-h-72">
        {groups.filter(([, ns]) => ns.length).map(([g, ns]) => (
          <SelectGroup key={g}>
            <SelectLabel className="text-[10px] uppercase tracking-wide">{g}</SelectLabel>
            {ns.map((n) => <SelectItem key={n} value={n} className="font-mono text-xs">{n}</SelectItem>)}
          </SelectGroup>
        ))}
      </SelectContent>
    </Select>
  )
}

/** Column picker — a select when the dataset's columns are known, free text otherwise. */
function ColPick({ cols, val, onCh, ph = "column" }: {
  cols?: string[]; val: string; onCh: (v: string) => void; ph?: string
}) {
  if (!cols?.length) return (
    <Input className="h-7 w-40 font-mono text-xs" value={val} placeholder={ph}
           onChange={(e) => onCh(e.target.value.toUpperCase())} />)
  return (
    <Select value={val || ""} onValueChange={onCh}>
      <SelectTrigger className="h-7 w-44 font-mono text-xs"><SelectValue placeholder={`${ph}…`} /></SelectTrigger>
      <SelectContent className="max-h-72">
        {cols.map((c) => <SelectItem key={c} value={c} className="font-mono text-xs">{c}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

/** A value in a rule: a raw/prepared column, another variable of this domain, a text —
 *  and, inside a Multiple-steps mapping, the running value or an earlier step's value. */
function ValuePick({ val, onCh, info, vars, kinds, allowMissing, allowSelf, priorSteps = 0 }: {
  val?: StudioValueRef; onCh: (v: StudioValueRef) => void
  info: StudioBlocksInfo; vars: string[]
  kinds?: string[]; allowMissing?: boolean; allowSelf?: boolean; priorSteps?: number
}) {
  const opts = kinds ?? ["column", "var", "text"]
  const k = val?.kind || opts[0]
  const set = (patch: StudioValueRef) => onCh({ ...val, ...patch })
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Select value={k} onValueChange={(kind) => onCh({ kind })}>
        <SelectTrigger className="h-7 w-28 text-xs text-muted-foreground"><SelectValue /></SelectTrigger>
        <SelectContent>
          {opts.includes("column") && <SelectItem value="column">column</SelectItem>}
          {opts.includes("var") && <SelectItem value="var">variable</SelectItem>}
          {opts.includes("text") && <SelectItem value="text">text</SelectItem>}
          {allowSelf && <SelectItem value="self">value so far</SelectItem>}
          {priorSteps > 0 && <SelectItem value="step">step value</SelectItem>}
          {allowMissing && <SelectItem value="missing">blank</SelectItem>}
        </SelectContent>
      </Select>
      {k === "column" && <>
        <DsPick val={val?.dataset ?? ""} onCh={(dataset) => set({ dataset, column: "" })} info={info} />
        <ColPick cols={info.sources[val?.dataset ?? ""]} val={val?.column ?? ""}
                 onCh={(column) => set({ column })} />
      </>}
      {k === "var" && (
        <Select value={val?.var || ""} onValueChange={(v) => set({ var: v })}>
          <SelectTrigger className="h-7 w-40 font-mono text-xs"><SelectValue placeholder="variable…" /></SelectTrigger>
          <SelectContent className="max-h-72">
            {vars.map((v) => <SelectItem key={v} value={v} className="font-mono text-xs">{v}</SelectItem>)}
          </SelectContent>
        </Select>)}
      {k === "text" && (
        <Input className="h-7 w-44 min-w-32 font-mono text-xs" value={val?.text ?? ""} placeholder="text"
               onChange={(e) => set({ text: e.target.value })} />)}
      {k === "self" && <Mono>&lt;value so far&gt;</Mono>}
      {k === "step" && (
        <Select value={String(val?.step || 1)} onValueChange={(n) => set({ step: Number(n) })}>
          <SelectTrigger className="h-7 w-24 font-mono text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            {Array.from({ length: priorSteps }, (_, j) => j + 1).map((n) => (
              <SelectItem key={n} value={String(n)} className="text-xs">step {n}</SelectItem>))}
          </SelectContent>
        </Select>)}
      {k === "missing" && <span className="text-[11px] text-muted-foreground">(blank)</span>}
    </span>
  )
}

/** IF / THEN / ELSE — one row per IF, the ELSE last, as the studio's own editor shows it. */
function CondRules({ args, onArgs, info, vars, target, allowSelf, priorSteps = 0 }: {
  args: Record<string, unknown>; onArgs: (a: Record<string, unknown>) => void
  info: StudioBlocksInfo; vars: string[]; target: string
  allowSelf?: boolean; priorSteps?: number
}) {
  const rules = (Array.isArray(args.rules) ? args.rules : []) as CondRule[]
  const els = (args.else ?? { kind: allowSelf ? "self" : "missing" }) as StudioValueRef
  const setA = (patch: Record<string, unknown>) => onArgs({ ...args, ...patch })
  const setRule = (k: number, patch: CondRule) =>
    setA({ rules: rules.map((r, j) => (j === k ? { ...r, ...patch } : r)) })
  return (
    <div className="space-y-1.5">
      {rules.length === 0 && (
        <p className="text-[12px] text-muted-foreground">No IF yet — add the first rule below.</p>)}
      {rules.map((r, k) => {
        const noVal = COND_NOVAL.has(r.op ?? ""), list = r.op === "in" || r.op === "notin"
        return (
          <div key={k} className="flex flex-wrap items-center gap-1.5 rounded-lg border bg-surface/60 p-2">
            <KW>{k === 0 ? "IF" : "ELSE IF"}</KW>
            <ValuePick val={r.src} onCh={(src) => setRule(k, { src })} info={info} vars={vars}
                       kinds={["column", "var"]} allowSelf={allowSelf} priorSteps={priorSteps} />
            <Select value={r.op || "eq"} onValueChange={(op) => setRule(k, { op })}>
              <SelectTrigger className="h-7 w-32 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                {COND_OPS.map((o) => <SelectItem key={o.k} value={o.k}>{o.l}</SelectItem>)}
              </SelectContent>
            </Select>
            {!noVal && (
              <Input className="h-7 w-40 font-mono text-xs" value={r.value ?? ""}
                     placeholder={list ? "A,B,C" : "value"}
                     title={list ? "comma-separated values" : "compared as text; > < ≥ ≤ compare as numbers"}
                     onChange={(e) => setRule(k, { value: e.target.value })} />)}
            {r.op === "between" && <>
              <span className="text-[11px] text-muted-foreground">and</span>
              <Input className="h-7 w-20 font-mono text-xs" value={r.value2 ?? ""}
                     onChange={(e) => setRule(k, { value2: e.target.value })} />
            </>}
            <KW>THEN</KW><Mono>{target} =</Mono>
            <ValuePick val={r.then} onCh={(then) => setRule(k, { then })} info={info} vars={vars}
                       allowSelf={allowSelf} priorSteps={priorSteps} />
            <Button variant="ghost" size="icon" className="ml-auto h-6 w-6 shrink-0" title="Remove this rule"
                    onClick={() => setA({ rules: rules.filter((_, j) => j !== k) })}>
              <Trash2 className="h-3 w-3" /></Button>
          </div>
        )
      })}
      <div className="flex flex-wrap items-center gap-1.5 rounded-lg border bg-surface/60 p-2">
        <KW>ELSE</KW><Mono>{target} =</Mono>
        <ValuePick val={els} onCh={(e) => setA({ else: e })} info={info} vars={vars} allowMissing
                   allowSelf={allowSelf} priorSteps={priorSteps} />
      </div>
      <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]"
              onClick={() => setA({ rules: [...rules,
                { src: { kind: allowSelf ? "self" : "column" }, op: "eq", value: "", then: { kind: "text" } }] })}>
        <Plus className="mr-1 h-3 w-3" />{rules.length ? "add ELSE IF" : "add the IF"}
      </Button>
      <p className="text-[11px] text-muted-foreground">The first true IF wins; ELSE covers every other record.</p>
    </div>
  )
}

/** A SAS transform function over one or more inputs — the studio's common-function list. */
function FnArgs({ args, onArgs, info, vars, allowSelf, priorSteps = 0 }: {
  args: Record<string, unknown>; onArgs: (a: Record<string, unknown>) => void
  info: StudioBlocksInfo; vars: string[]; allowSelf?: boolean; priorSteps?: number
}) {
  const fn = typeof args.fn === "string" ? args.fn : ""
  const meta = FN_LIST.find((f) => f.k === fn)
  const srcs = (Array.isArray(args.sources) ? args.sources : []) as StudioValueRef[]
  const setA = (patch: Record<string, unknown>) => onArgs({ ...args, ...patch })
  const newSrc = (): StudioValueRef =>
    ({ kind: priorSteps > 0 ? "step" : allowSelf ? "self" : "column" })
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <KW>FUNCTION</KW>
        <Select value={fn} onValueChange={(k) => {
          const m = FN_LIST.find((f) => f.k === k)
          const keep = m?.multi ? (srcs.length ? srcs : [newSrc()]) : [srcs[0] ?? newSrc()]
          onArgs({ ...args, fn: k, sources: keep })
        }}>
          <SelectTrigger className="h-7 w-72 text-xs"><SelectValue placeholder="choose the function…" /></SelectTrigger>
          <SelectContent className="max-h-72">
            {FN_LIST.map((f) => <SelectItem key={f.k} value={f.k}>{f.l}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      {srcs.map((s, k) => (
        <div key={k} className="flex flex-wrap items-center gap-1.5">
          <KW>{srcs.length > 1 ? `INPUT ${k + 1}` : "INPUT"}</KW>
          <ValuePick val={s} onCh={(v) => setA({ sources: srcs.map((x, j) => (j === k ? v : x)) })}
                     info={info} vars={vars} allowSelf={allowSelf} priorSteps={priorSteps} />
          {meta?.multi && srcs.length > 1 && (
            <Button variant="ghost" size="icon" className="h-6 w-6" title="Remove this input"
                    onClick={() => setA({ sources: srcs.filter((_, j) => j !== k) })}>
              <Trash2 className="h-3 w-3" /></Button>)}
        </div>
      ))}
      {meta?.multi && (
        <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]"
                onClick={() => setA({ sources: [...srcs, newSrc()] })}>
          <Plus className="mr-1 h-3 w-3" />add an input</Button>)}
      {(meta?.fields ?? []).map((f) => (
        <div key={f} className="flex items-center gap-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{f}</span>
          <Input className="h-7 w-40 font-mono text-xs" value={String(args[f] ?? "")}
                 onChange={(e) => setA({ [f]: e.target.value })} />
        </div>
      ))}
    </div>
  )
}

/** Multiple steps — the studio's pipeline recipe: each step transforms the value so far,
 *  and a later step can also read an earlier step's value. */
const STEP_OPS: Array<{ k: string; l: string }> = [
  { k: "assign", l: "Copy from column" }, { k: "constant", l: "Fixed value" },
  { k: "fn", l: "Transform function" }, { k: "cond", l: "Conditional (if / then / else)" },
]

type PipeStep = {
  op?: string; dataset?: string; column?: string; value?: string
  args?: Record<string, unknown>
}

function PipelineSteps({ args, onArgs, info, vars, target }: {
  args: Record<string, unknown>; onArgs: (a: Record<string, unknown>) => void
  info: StudioBlocksInfo; vars: string[]; target: string
}) {
  const steps = (Array.isArray(args.steps) ? args.steps : []) as PipeStep[]
  const setSteps = (ns: PipeStep[]) => onArgs({ ...args, steps: ns })
  const setStep = (k: number, patch: PipeStep) =>
    setSteps(steps.map((s, j) => (j === k ? { ...s, ...patch } : s)))
  const move = (k: number, d: number) => {
    const j = k + d
    if (j < 0 || j >= steps.length) return
    const n = [...steps]; [n[k], n[j]] = [n[j], n[k]]; setSteps(n)
  }
  return (
    <div className="space-y-1.5">
      <p className="text-[11px] text-muted-foreground">
        Steps run top to bottom, each on <Mono>{target}</Mono>'s value so far.</p>
      {steps.length === 0 && (
        <p className="text-[12px] text-muted-foreground">
          No steps — add the first one (e.g. copy a column, then transform it).</p>)}
      {steps.map((st, k) => (
        <div key={k} className="space-y-1.5 rounded-lg border bg-surface/60 p-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <KW>STEP {k + 1}</KW>
            <Select value={st.op || "fn"} onValueChange={(op) =>
              setStep(k, { op, args: {}, dataset: "", column: "", value: "" })}>
              <SelectTrigger className="h-7 w-56 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                {STEP_OPS.map((o) => <SelectItem key={o.k} value={o.k}>{o.l}</SelectItem>)}
              </SelectContent>
            </Select>
            <span className="ml-auto flex items-center">
              <Button variant="ghost" size="icon" className="h-6 w-6" title="Move up" disabled={k === 0}
                      onClick={() => move(k, -1)}><ArrowUp className="h-3 w-3" /></Button>
              <Button variant="ghost" size="icon" className="h-6 w-6" title="Move down"
                      disabled={k === steps.length - 1}
                      onClick={() => move(k, 1)}><ArrowDown className="h-3 w-3" /></Button>
              <Button variant="ghost" size="icon" className="h-6 w-6 text-destructive" title="Remove this step"
                      onClick={() => setSteps(steps.filter((_, j) => j !== k))}>
                <Trash2 className="h-3 w-3" /></Button>
            </span>
          </div>
          {st.op === "assign" && (
            <div className="flex flex-wrap items-center gap-1.5">
              <Mono>{target} = strip(</Mono>
              <DsPick val={st.dataset ?? ""} onCh={(dataset) => setStep(k, { dataset, column: "" })} info={info} />
              <ColPick cols={info.sources[st.dataset ?? ""]} val={st.column ?? ""}
                       onCh={(column) => setStep(k, { column })} />
              <Mono>)</Mono>
            </div>)}
          {st.op === "constant" && (
            <div className="flex flex-wrap items-center gap-1.5">
              <Mono>{target} =</Mono>
              <Input className="h-7 w-56 font-mono text-xs" value={st.value ?? ""} placeholder="fixed value"
                     onChange={(e) => setStep(k, { value: e.target.value })} />
            </div>)}
          {st.op === "fn" && (
            <FnArgs args={st.args ?? {}} onArgs={(a) => setStep(k, { args: a })}
                    info={info} vars={vars} allowSelf={k > 0} priorSteps={k} />)}
          {st.op === "cond" && (
            <CondRules args={st.args ?? {}} onArgs={(a) => setStep(k, { args: a })}
                       info={info} vars={vars} target={target} allowSelf={k > 0} priorSteps={k} />)}
        </div>
      ))}
      <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]"
              onClick={() => setSteps([...steps, steps.length
                ? { op: "fn", args: { fn: "upcase", sources: [{ kind: "self" }] } }
                : { op: "assign", dataset: "", column: "" }])}>
        <Plus className="mr-1 h-3 w-3" />add a step</Button>
    </div>
  )
}

/** RETAIN (carry forward) and LAG / DIF (previous record) within a BY group — the studio's
 *  own wording and args: source variable, the groups, the record order, the mode. */
function RetainLag({ recipe, args, onArgs, vars, target }: {
  recipe: "retain" | "lag"; args: Record<string, unknown>
  onArgs: (a: Record<string, unknown>) => void; vars: string[]; target: string
}) {
  const setA = (patch: Record<string, unknown>) => onArgs({ ...args, ...patch })
  const src = (args.source ?? {}) as StudioValueRef
  const asText = (x: unknown, dflt = "") =>
    x == null ? dflt : Array.isArray(x) ? x.join(" ") : String(x)
  const isRetain = recipe === "retain"
  const first = (typeof args.mode === "string" ? args.mode : "locf") === "first"
  const dif = args.fn === "dif"
  const varPick = (
    <Select value={src.var || ""} onValueChange={(v) => setA({ source: { kind: "var", var: v } })}>
      <SelectTrigger className="h-7 w-40 font-mono text-xs"><SelectValue placeholder="variable…" /></SelectTrigger>
      <SelectContent className="max-h-72">
        {vars.filter((x) => x !== target).map((x) => (
          <SelectItem key={x} value={x} className="font-mono text-xs">{x}</SelectItem>))}
      </SelectContent>
    </Select>)
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Mono>{target} =</Mono>
        {isRetain ? <>
          <KW>RETAIN</KW>{varPick}
          <Select value={first ? "first" : "locf"} onValueChange={(mode) => setA({ mode })}>
            <SelectTrigger className="h-7 w-64 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="locf">last non-missing so far (LOCF)</SelectItem>
              <SelectItem value="first">first non-missing of the group</SelectItem>
            </SelectContent>
          </Select>
        </> : <>
          <Select value={dif ? "dif" : "lag"} onValueChange={(fn) => setA({ fn })}>
            <SelectTrigger className="h-7 w-56 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="lag">LAG — previous value</SelectItem>
              <SelectItem value="dif">DIF — change from previous</SelectItem>
            </SelectContent>
          </Select>
          <Input className="h-7 w-14 font-mono text-xs" value={String(args.n ?? 1)}
                 title="how many records back"
                 onChange={(e) => setA({ n: parseInt(e.target.value, 10) || 1 })} />
          <Mono>(</Mono>{varPick}<Mono>)</Mono>
        </>}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <KW>BY</KW>
        <Input className="h-7 w-52 font-mono text-xs" value={asText(args.by, "USUBJID")}
               placeholder="USUBJID"
               title="the groups — a value never carries from one group into the next"
               onChange={(e) => setA({ by: e.target.value.toUpperCase() })} />
        <KW>ORDER</KW>
        <Input className="h-7 w-52 font-mono text-xs" value={asText(args.order)}
               placeholder={`e.g. ${target.slice(0, 2)}DTC VISITNUM`}
               title="record order inside a group (missing sorts first)"
               onChange={(e) => setA({ order: e.target.value.toUpperCase() })} />
      </div>
      <p className="text-[11px] text-muted-foreground">
        {isRetain
          ? "Carries the value down each BY group, like RETAIN in a sorted data step."
          : "Reads the earlier record in each BY group, like LAG/DIF in a sorted data step."}
        {" "}Runs after every other variable of the domain; the rows then go back to their own order.</p>
    </div>
  )
}

export function StudioVariableEditor({ domain, variable, onDone, onClose }: {
  domain: string; variable: string; onDone: () => void; onClose: () => void
}) {
  const [info, setInfo] = useState<StudioBlocksInfo | null>(null)
  const [blk, setBlk] = useState<StudioBlock | null>(null)
  const [busy, setBusy] = useState("")
  const [err, setErr] = useState("")
  useEffect(() => {
    let dead = false
    api.studioBlocks(domain)
      .then((r) => { if (!dead) {
        setInfo(r)
        setBlk(r.blocks.find((b) => b.variable === variable)
          ?? { variable, mtype: "unmapped", args: {} }) } })
      .catch((e) => !dead && setErr((e as Error).message))
    return () => { dead = true }
  }, [domain, variable])

  if (err && !info) return <Callout tone="bad" title="Could not load the studio's mapping">{err}</Callout>
  if (!info || !blk) return (
    <div className="flex items-center gap-2 py-4 text-[13px] text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" />Reading the studio's mapping…</div>)

  const vars = info.variables
  const set = (patch: Partial<StudioBlock>) => setBlk({ ...blk, ...patch })
  const setArgs = (args: Record<string, unknown>) => set({ args })
  const args = (blk.args ?? {}) as Record<string, unknown>
  const key = mtypeKey(blk)

  const run = async (what: "save" | "reset") => {
    setBusy(what); setErr("")
    try {
      if (what === "save") await api.setStudioBlock(domain, variable, blk)
      else await api.clearStudioBlock(domain, variable)
      await api.rebuild(domain)
      onClose()
      onDone()
    } catch (e) { setErr((e as Error).message) } finally { setBusy("") }
  }

  return (
    <div className="space-y-3 rounded-xl border bg-surface/70 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone="teal">SDTM Studio mapping</Chip>
        <Mono>{variable}</Mono>
        {blk.codelist ? <Chip tone="violet">{blk.codelist}</Chip> : null}
        {blk.supp ? <Chip tone="amber">SUPP</Chip> : null}
        <Select value={key} onValueChange={(v) => {
          const [mtype, recipe] = v.split(":")
          set({ mtype, recipe: recipe || "", args: {} })
        }}>
          <SelectTrigger className="h-7 w-80 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            {MTYPES.map((t) => <SelectItem key={t.k} value={t.k}>{t.l}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>

      {key === "assign" && (
        <div className="flex flex-wrap items-center gap-1.5">
          <KW>ASSIGN</KW><Mono>{variable} = strip(</Mono>
          <DsPick val={blk.dataset ?? ""} onCh={(dataset) => set({ dataset, column: "" })} info={info} />
          <ColPick cols={info.sources[blk.dataset ?? ""]} val={blk.column ?? ""}
                   onCh={(column) => set({ column })} />
          <Mono>)</Mono>
        </div>)}
      {key === "constant" && (
        <div className="flex flex-wrap items-center gap-1.5">
          <KW>ASSIGN</KW><Mono>{variable} =</Mono>
          <Input className="h-7 w-64 font-mono text-xs" value={blk.value ?? ""} placeholder="fixed value"
                 onChange={(e) => set({ value: e.target.value })} />
        </div>)}
      {key === "sequence" && (
        <p className="text-[12px] text-muted-foreground">
          Numbered per subject in the domain's key order, exactly as the studio numbers it.</p>)}
      {key === "drop" && (
        <p className="text-[12px] text-muted-foreground">The variable is excluded from the dataset.</p>)}
      {key === "unmapped" && (
        <p className="text-[12px] text-muted-foreground">
          Not mapped — pick a mapping type above to build it.</p>)}
      {key === "derived:cond" && (
        <CondRules args={args} onArgs={setArgs} info={info} vars={vars} target={variable} />)}
      {key === "derived:fn" && (
        <FnArgs args={args} onArgs={setArgs} info={info} vars={vars} />)}
      {key === "derived:pipeline" && (
        <PipelineSteps args={args} onArgs={setArgs} info={info} vars={vars} target={variable} />)}
      {key === "derived:retain" && (
        <RetainLag recipe="retain" args={args} onArgs={setArgs} vars={vars} target={variable} />)}
      {key === "derived:lag" && (
        <RetainLag recipe="lag" args={args} onArgs={setArgs} vars={vars} target={variable} />)}
      {key === "derived" && (
        <div className="space-y-2">
          <div className="flex items-center gap-1.5">
            <KW>RECIPE</KW>
            <Input className="h-7 w-44 font-mono text-xs" value={blk.recipe ?? ""}
                   placeholder="e.g. iso_date, study_day" onChange={(e) => set({ recipe: e.target.value })} />
          </div>
          {typeof args.code === "string" ? (
            <textarea className={cn("min-h-32 w-full rounded-lg border bg-surface p-2 font-mono text-[12px]",
                                    "focus:outline-none focus:ring-2 focus:ring-primary/30")}
                      value={args.code} spellCheck={false}
                      onChange={(e) => setArgs({ ...args, code: e.target.value })} />
          ) : Object.keys(args).length ? (
            <StudioObjectFields obj={args} onChange={setArgs} />
          ) : (
            <p className="text-[12px] text-muted-foreground">
              This recipe has no settings yet — they appear here as the studio saves them.</p>
          )}
        </div>)}

      {err && <Callout tone="bad" title="Could not save">{err}</Callout>}
      <div className="flex flex-wrap items-center gap-2 border-t pt-2">
        <Button size="sm" disabled={!!busy} onClick={() => void run("save")}>
          {busy === "save" ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />Saving…</> : "Save & rebuild"}
        </Button>
        <Button size="sm" variant="ghost" disabled={!!busy} onClick={onClose}>Cancel</Button>
        <Button size="sm" variant="outline" disabled={!!busy} className="ml-auto"
                title="Drop the saved edit so the variable follows the studio's spec-derived mapping again"
                onClick={() => void run("reset")}>
          {busy === "reset" ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
            : <RotateCcw className="mr-1.5 h-3.5 w-3.5" />}Back to the spec's mapping</Button>
      </div>
    </div>
  )
}
