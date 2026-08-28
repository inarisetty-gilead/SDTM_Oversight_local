// Thin typed wrapper over the local FastAPI. Every call is same-origin; the application
// makes no outbound network requests.

export interface StudyCard {
  id: string; name: string; created: string; updated: string
  spec_path: string; raw_path: string; vendor_path: string; studyid: string
  spec_exists: boolean; raw_exists: boolean; last_run: string
  counts: { edits: number; pipelines: number; overrides: number; domains_touched: number }
}

export interface JobState {
  kind: string; status: "idle" | "running" | "done" | "error";
  message: string; step: number; total: number; percent: number; error: string; detail: string;
}

export interface SpecCoverage {
  totals: Record<string, number>;
  domains: Array<Record<string, number | string>>;
}

export interface SpecDomain {
  domain: string; variables: number; supp: number
  active: boolean; in_toc: boolean; label: string; class: string; structure: string
}

export interface SpecSheetRow {
  variable: string; label: string; action: string; input_variables: string
  mapping_rule: string; sas_code: string; codelist: string; role: string
  origin: string; dataset: string; type: string; length: string
  supp: boolean; sheet_row: number
}

export interface SpecInfo {
  path: string; cleared: boolean; domains: string[]; variables: number;
  codelists: number; coverage: SpecCoverage;
  toc?: Record<string, { active: boolean; label: string }>
  active?: string[]; inactive?: string[];
  skipped: Array<{ sheet: string; why: string }>;
}

export interface SyntheticMarker {
  synthetic: boolean; studyid: string; subjects: number; visits: number; warning: string
}

export interface RawInfo {
  path: string; cleared: boolean; built?: string[]; synthetic?: SyntheticMarker | null;
  datasets: Array<{ name: string; rows: number | null; cols: number | null; file: string; error: string }>;
  coverage: Array<{ domain: string; sources: number; resolved: number; has_own_dataset: boolean }>;
  missing: Array<{ source: string; used_by: string[]; count: number }>;
}

export interface DomainRow {
  domain: string; ok: boolean; error: string; base: string; rows: number; supp_rows: number;
  built: number; dropped: number; not_built: number; empty: number
  name_matched: number; edited: number;
  warnings: string[]; prep: { op: string; name: string; note: string } | null;
}

export interface BuildResults {
  domains: DomainRow[]; out_dir: string; name_match: number; synthetic?: SyntheticMarker | null;
  not_built: Array<{ domain: string; variable: string; label: string; why: string; spec_rule: string; spec_row: number }>;
  not_built_reasons: Array<{ reason: string; count: number; examples: string[] }>;
  outputs: Record<string, string>;
}

export interface VariableRow {
  variable: string; label: string; status: string; target: string; how: string;
  mapping_type: string; recipe: string; source: string; constant: string; codelist: string;
  origin: string; role: string; spec_action: string; spec_input: string; spec_rule: string;
  spec_sas: string; spec_row: number; reason: string; error: string;
  edited: boolean; edit_note: string; spec_method: string;
  method_source: string; confidence: number; args: Record<string, unknown>; supp: boolean;
  populated: number | null; samples: string[];
}

export interface DomainDetail {
  domain: string; ok: boolean; error: string; base: string; rows: number; supp_rows: number;
  columns: string[]; prep: { op: string; name: string; note: string; params: Record<string, unknown> } | null;
  prep_reports: Array<Record<string, unknown>>; prep_outputs: string[];
  warnings: string[]; counts: Record<string, number>;
  override: Record<string, unknown>; dedup: Record<string, unknown>;
  edits: Record<string, unknown>; pipeline: Array<Record<string, unknown>>;
  pipeline_draft?: Array<Record<string, unknown>> | null;
  datasets: string[]; prepared_datasets: string[]; unapplied_datasets: string[]
  built_domains: string[]; variables: VariableRow[]; codelists?: string[];
}

export interface DataColumn {
  name: string; label: string; status: string; method_source: string
  confidence: number; populated: number; numeric: boolean
  distinct?: string[] | null
}

export interface DataPage {
  domain?: string; dataset?: string; part?: string
  nrows: number; total?: number; offset: number; limit: number
  sort?: string; dir?: string; notes?: string[]
  columns: DataColumn[]; rows: string[][]; has_supp?: boolean
}

export interface DataQuery {
  offset: number; limit: number
  sort?: string; dir?: "asc" | "desc"; filters?: Record<string, string>
  /** narrow the table to these columns — record keys plus one variable */
  only?: string
}

export interface ValueCount { value: string; count: number }

export interface ColumnProfile {
  n: number; populated: number; blank: number; distinct: number
  top: ValueCount[]; truncated: boolean
  numeric?: { min: number; median: number; max: number; mean: number }
  dates?: { complete: number; partial: number; unparseable: number; earliest: string; latest: string }
  dataset?: string; column?: string
}

export interface VariableProfile {
  domain: string; variable: string; label: string; status: string
  method: string; method_source: string; confidence: number; reason: string
  codelist: string; source: string
  built: ColumnProfile | null
  input: ColumnProfile | null
  ct: { codelist: string; allowed: string[]; violations: ValueCount[]; violating_records: number } | null
}

export interface VariableDiff {
  variable: string; compared: number; differing: number; agreement: number;
  only_built_nonblank: number; only_vendor_nonblank: number;
  examples: Array<Record<string, string>>;
}

export interface CompareRow {
  domain: string; status: "identical" | "differences" | "error"; error: string;
  keys: string[]; key_note: string; notes: string[];
  rows_built: number; rows_vendor: number; matched: number;
  only_built: number; only_vendor: number; value_differences: number;
  vars_only_built: string[]; vars_only_vendor: string[]; not_built: string[];
  variables: VariableDiff[];
}

function qs(q: DataQuery): string {
  const p = new URLSearchParams({ offset: String(q.offset), limit: String(q.limit) })
  if (q.sort) { p.set("sort", q.sort); p.set("dir", q.dir ?? "asc") }
  const active = Object.fromEntries(Object.entries(q.filters ?? {}).filter(([, v]) => v !== ""))
  if (Object.keys(active).length) p.set("filters", JSON.stringify(active))
  if (q.only) p.set("only", q.only)
  return p.toString()
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((body as { detail?: string }).detail || `${res.status} ${res.statusText}`)
  return body as T
}

export interface TemplateResolved {
  domain: string; mtype: string; dataset: string; column: string; value: string
  recipe: string; args: Record<string, unknown>; reason: string
}
export interface TemplateFn {
  variable: string; domains: string[]; source: string; describe: string; enabled: boolean
  resolved: TemplateResolved | null; edit: Record<string, unknown> | null
}
export interface CustomFn {
  name: string; description: string; variable: string; domains: string[]
  steps: Array<Record<string, unknown>>; override: boolean; enabled: boolean
}
export interface FnContext {
  domain: string; datasets: string[]; prepared_datasets: string[]
  variables: Array<{ variable: string }>
}

export interface AcrfRow {
  page: number; kind: string; domain: string; variable: string; value: string
  text: string; verdict: string; advice: string; question?: string; form?: string
}
export interface AcrfMissing {
  domain: string; variable: string; label: string; origin: string; advice: string
}
export interface AcrfReport {
  pages: number; rows: AcrfRow[]; missing: AcrfMissing[]
  counts: Record<string, number>; domains_annotated: string[]; origins_recorded: boolean
  notes?: string[]
}

export interface CrfPair {
  standard_question: string; vendor_question: string; similarity: number; match: string
  standard_form: string; vendor_form: string
  standard_mapping: string; vendor_mapping: string; verdict: string; advice: string
}
export interface CrfOnly { question: string; form: string; mapping: string; advice: string }
export interface CrfCmp {
  pairs: CrfPair[]; standard_only: CrfOnly[]; vendor_only: CrfOnly[]
  ann_vendor_only: string[]; ann_standard_only: string[]
  counts: Record<string, number>; notes: string[]
}

const post = <T,>(p: string, b?: unknown) =>
  call<T>(p, { method: "POST", body: JSON.stringify(b ?? {}) })

export const api = {
  studies: () => call<{ studies: StudyCard[]; open: string }>("/api/studies"),
  createStudy: (name: string) => post<{ id: string }>("/api/studies", { name }),
  openStudy: (id: string) => post<{ id: string; name: string; spec: string; raw: string
    vendor: string; restored: Record<string, number>
    built: string[]; compared: string[]; problems?: string[] }>(`/api/studies/${id}/open`),
  closeStudy: (id: string) => post(`/api/studies/${id}/close`),
  deleteStudy: (id: string) => call(`/api/studies/${id}`, { method: "DELETE" }),
  buildId: () => call<{ version: string; assets: string[]; built: string }>("/api/build-id"),
  state: () => call<Record<string, unknown>>("/api/state"),
  job: () => call<JobState>("/api/job"),
  reset: () => post("/api/reset"),

  browse: (path: string) => call<{
    path: string; parent: string | null;
    dirs: Array<{ name: string; path: string }>;
    files: Array<{ name: string; path: string; size: number }>;
    shortcuts: Array<{ name: string; path: string }>;
  }>(`/api/browse?path=${encodeURIComponent(path)}`),

  setSpec: (path: string) => post<SpecInfo>("/api/spec", { path }),
  specDomains: () => call<{ domains: SpecDomain[]; has_toc: boolean; toc_only: string[] }>(
    "/api/spec/domains"),
  specRows: (d: string) => call<{ domain: string; active: boolean; rows: SpecSheetRow[] }>(
    `/api/spec/${d}/rows`),
  setRaw: (path: string) => post<RawInfo>("/api/raw", { path }),
  synth: (b: Record<string, unknown>) => post<{
    out_dir: string; studyid: string; rows: number; subjects: number
    datasets: Array<{ dataset: string; rows: number; columns: number; grain: string }>
  }>("/api/synth", b),

  build: (b: Record<string, unknown>) => post("/api/build", b),
  buildResults: () => call<BuildResults>("/api/build/results"),
  preview: (domain: string) => call<{ domain: string; columns: string[]; nrows: number; rows: string[][] }>(
    `/api/build/preview/${domain}`),
  domainData: (domain: string, q: DataQuery, part: "parent" | "supp" = "parent") =>
    call<DataPage>(`/api/domain/${domain}/data?part=${part}&${qs(q)}`),
  rawData: (dataset: string, q: DataQuery) =>
    call<DataPage>(`/api/raw/${encodeURIComponent(dataset)}/data?${qs(q)}`),

  compare: (b: Record<string, unknown>) => post("/api/compare", b),
  compareResults: () => call<{ domains: CompareRow[]; vendor_path: string; outputs: Record<string, string> }>(
    "/api/compare/results"),

  domain: (d: string) => call<DomainDetail>(`/api/domain/${d}`),
  domainSettings: (d: string, b: unknown) => post(`/api/domain/${d}/settings`, b),
  domainDedup: (d: string, b: unknown) => post(`/api/domain/${d}/dedup`, b),
  rebuild: (d: string) => post(`/api/domain/${d}/build`),
  getAcrf: () => call<{ acrf: string; standards: string; ta: string; ecrf?: string
    std_acrf?: string; std_ecrf?: string; report: AcrfReport | null; cmp?: CrfCmp | null }>("/api/acrf"),
  compareCrfs: (b: { vendor: string; standard: string; vendor_ecrf?: string
                     standard_ecrf?: string; standards?: string }) =>
    post<{ ok: boolean; cmp: CrfCmp }>("/api/acrf/compare", b),
  runAcrf: (b: { acrf: string; standards: string; ta: string; ecrf?: string }) =>
    post<{ ok: boolean; report: AcrfReport }>("/api/acrf", b),

  listFunctions: () => call<{ templates: TemplateFn[]; custom: CustomFn[] }>("/api/functions"),
  saveFunction: (fn: CustomFn) => post("/api/functions", fn),
  deleteFunction: (name: string) =>
    call(`/api/functions/${encodeURIComponent(name)}`, { method: "DELETE" }),
  saveTemplate: (variable: string, body: { enabled?: boolean; edit?: Record<string, unknown>; clear_edit?: boolean }) =>
    post(`/api/functions/template/${variable}`, body),
  fnContext: (d: string) => call<FnContext>(`/api/functions/context/${d}`),

  columns: (d: string, ds: string) => call<{ dataset: string; columns: string[] }>(
    `/api/domain/${d}/columns/${encodeURIComponent(ds)}`),

  variableProfile: (d: string, v: string) =>
    call<VariableProfile>(`/api/domain/${d}/variable/${v}/profile`),
  suggestArgs: (d: string, v: string, recipe: string) =>
    call<{ args: Record<string, unknown>; input_variables: string }>(
      `/api/domain/${d}/variable/${v}/suggest?recipe=${recipe}`),
  recipes: () => call<{ mtypes: string[]; recipes: Array<{ id: string; label: string; fields: Array<Record<string, unknown>> }> }>(
    "/api/recipes"),
  previewEdit: (d: string, v: string, b: unknown) => post<{
    ok: boolean; status?: string; error?: string; reason?: string; how?: string;
    rows?: number; populated?: number; samples?: string[]
  }>(`/api/domain/${d}/variable/${v}/preview`, b),
  setEdit: (d: string, v: string, b: unknown) => post(`/api/domain/${d}/variable/${v}`, b),
  clearEdit: (d: string, v: string) => call(`/api/domain/${d}/variable/${v}`, { method: "DELETE" }),
  clearEdits: (d: string) => call(`/api/domain/${d}/edits`, { method: "DELETE" }),

  prepOps: () => call<{ ops: Array<{ id: string; label: string }>; conditions: Array<{ id: string; label: string }> }>(
    "/api/prep/ops"),
  getPipeline: (d: string) => call<{ steps: Array<Record<string, unknown>>; datasets: string[] }>(
    `/api/domain/${d}/pipeline`),
  previewPipeline: (d: string, steps: unknown) => post<{
    ok: boolean; error?: string; reports?: Array<Record<string, unknown>>;
    outputs?: Record<string, { rows: number; columns: string[]; sample: string[][] }>
  }>(`/api/domain/${d}/pipeline/preview`, { steps }),
  setPipeline: (d: string, steps: unknown) => post(`/api/domain/${d}/pipeline`, { steps }),
  pipelineFromAuto: (d: string) => post<{ steps: Array<Record<string, unknown>> }>(
    `/api/domain/${d}/pipeline/from-auto`),

  reveal: () => call("/api/reveal?name=out_dir"),
}

export function useJobPoll(onDone: (err: JobState | null) => void) {
  return async function poll(setJob: (j: JobState | null) => void) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 400))
      let j: JobState
      try { j = await api.job() } catch { continue }
      setJob(j)
      if (j.status === "done") { setJob(null); onDone(null); return }
      if (j.status === "error") { setJob(null); onDone(j); return }
    }
  }
}
