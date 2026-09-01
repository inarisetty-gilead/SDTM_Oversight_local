import { useCallback, useEffect, useState } from "react"
import {
  Database, FileSpreadsheet, GitCompare, Layers, Settings2, ShieldCheck, Sparkles,
  FileSearch, FunctionSquare, Table2,
} from "lucide-react"
import { api } from "@/api"
import type { BuildResults, CompareRow, JobState, RawInfo, SpecInfo } from "@/api"
import { Button } from "@/components/ui/button"
import { Chip } from "@/components/grid"
import { Rail, RailItem, RailSection, TopBar } from "@/components/shell"
import { PathPicker } from "@/components/PathPicker"
import { SetupView } from "@/views/SetupView"
import { SpecView } from "@/views/SpecView"
import { StudiesView } from "@/views/StudiesView"
import { BuildView } from "@/views/BuildView"
import { CompareView } from "@/views/CompareView"
import { FunctionsView } from "@/views/FunctionsView"
import { CrfView } from "@/views/CrfView"
import { DomainView } from "@/views/DomainView"
import { SpecWindow } from "@/components/SpecPeek"
import { RawDataView } from "@/views/RawDataView"
import { VarsWindow } from "@/views/VarsWindow"

type View = "studies" | "setup" | "spec" | "rawdata" | "build" | "functions" | "compare" | "acrf" | { domain: string }

export default function App() {
  // #spec/DM opens a standalone read-only spec window — a reference the reader can put
  // on a second monitor while mapping in the main window (same server session)
  const specHash = /^#spec\/([A-Za-z0-9_]+)$/.exec(window.location.hash)
  if (specHash) return <SpecWindow domain={specHash[1].toUpperCase()} />
  // #vars/DM: the built variables grid alone in its own window, same idea
  const varsHash = /^#vars\/([A-Za-z0-9_]+)$/.exec(window.location.hash)
  if (varsHash) return <VarsWindow domain={varsHash[1].toUpperCase()} />
  return <MainApp />
}

function MainApp() {
  const [view, setView] = useState<View>("studies")
  const [study, setStudy] = useState<{ id: string; name: string } | null>(null)
  const [specPath, setSpecPath] = useState("")
  const [rawPath, setRawPath] = useState("")
  const [vendorPath, setVendorPath] = useState("")
  const [spec, setSpec] = useState<SpecInfo | null>(null)
  const [raw, setRaw] = useState<RawInfo | null>(null)
  const [build, setBuild] = useState<BuildResults | null>(null)
  const [compare, setCompare] = useState<CompareRow[] | null>(null)
  const [job, setJob] = useState<JobState | null>(null)
  const [picker, setPicker] = useState<{ mode: "file" | "dir"; target: string } | null>(null)
  const [err, setErr] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState("")
  const [opts, setOptsState] = useState({ fmt: "xpt", studyid: "", nameMatch: "70", structure: "full" })
  const [ignoreCase, setIgnoreCase] = useState(false)
  const [ignoreVars, setIgnoreVars] = useState("")
  const [compareSel, setCompareSel] = useState<string[]>([])
  const [buildSel, setBuildSel] = useState<string[]>([])
  const [appBuild, setAppBuild] = useState<{ version: string; built: string } | null>(null)
  const [stale, setStale] = useState(false)

  const setOpts = (o: Partial<typeof opts>) => setOptsState((p) => ({ ...p, ...o }))
  const fail = (k: string, e: unknown) => setErr((p) => ({ ...p, [k]: (e as Error).message }))

  const loadBuild = useCallback(async () => setBuild(await api.buildResults()), [])
  const loadCompare = useCallback(async () => setCompare((await api.compareResults()).domains), [])

  // A browser holding an old index.html keeps loading whichever bundle it named when it was
  // cached, so the app silently stops updating while appearing to work. Compare the bundle
  // this page actually loaded against the one the server is serving, and say so.
  useEffect(() => {
    const check = async () => {
      try {
        const id = await api.buildId()
        setAppBuild({ version: id.version, built: id.built })
        const loaded = [...document.querySelectorAll("script[src]")]
          .map((e) => (e as HTMLScriptElement).src)
          .find((src) => src.includes("/assets/"))
        if (loaded && id.assets.length) {
          const current = id.assets.find((a) => a.endsWith(".js"))
          setStale(!!current && !loaded.endsWith(current.replace("assets/", "")))
        }
      } catch { /* offline or older server */ }
    }
    void check()
    const t = window.setInterval(check, 30_000)
    return () => window.clearInterval(t)
  }, [])

  useEffect(() => { void (async () => {
    try {
      const st = await api.state() as Record<string, unknown>
      if (st.study_id) {
        setStudy({ id: st.study_id as string, name: (st.study_name as string) || "" })
        setView("setup")
      }
      if (st.spec) setSpecPath(st.spec as string)
      if (st.raw) setRawPath(st.raw as string)
      if (st.vendor) setVendorPath(st.vendor as string)
      if ((st.domains as string[])?.length) {
        setSpec({ path: st.spec as string, cleared: false, domains: st.domains as string[],
                  variables: 0, codelists: 0, skipped: [], coverage: { totals: {}, domains: [] } })
      }
      if ((st.built as string[])?.length) {
        setRaw({ path: st.raw as string, cleared: false, datasets: [], coverage: [], missing: [] })
        await loadBuild(); setView("build")
      }
      if ((st.compared as string[])?.length) await loadCompare()
    } catch { /* fresh session */ }
  })() }, [loadBuild, loadCompare])

  const waitJob = async () => {
    for (;;) {
      await new Promise((r) => setTimeout(r, 400))
      const j = await api.job(); setJob(j)
      if (j.status !== "running") { setJob(null); return j }
    }
  }

  const onSpec = async () => {
    setBusy("spec"); setErr((p) => ({ ...p, spec: "" }))
    try {
      const s = await api.setSpec(specPath); setSpec(s)
      if (s.cleared) { setBuild(null); setCompare(null) }
    } catch (e) { fail("spec", e); setSpec(null) } finally { setBusy("") }
  }
  const onRaw = async () => {
    setBusy("raw"); setErr((p) => ({ ...p, raw: "" }))
    try {
      const r = await api.setRaw(rawPath); setRaw(r)
      if (r.cleared) { setBuild(null); setCompare(null) }
      else if (r.built?.length) await loadBuild()
    } catch (e) { fail("raw", e); setRaw(null) } finally { setBusy("") }
  }
  const onBuild = async () => {
    setBusy("build"); setErr((p) => ({ ...p, build: "" })); setView("build")
    try {
      await api.build({ fmt: opts.fmt, studyid: opts.studyid, domains: buildSel,
        include_unbuilt: opts.structure === "full", name_match: Number(opts.nameMatch) })
      const j = await waitJob()
      if (j.status === "error") { fail("build", new Error(j.error)); return }
      await loadBuild(); setCompare(null)
    } catch (e) { fail("build", e) } finally { setBusy("") }
  }
  const onCompare = async () => {
    setBusy("compare"); setErr((p) => ({ ...p, compare: "" }))
    try {
      await api.compare({ path: vendorPath, ignore_case: ignoreCase, domains: compareSel,
        ignore_vars: ignoreVars.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean) })
      const j = await waitJob()
      if (j.status === "error") { fail("compare", new Error(j.error)); return }
      await loadCompare()
    } catch (e) { fail("compare", e) } finally { setBusy("") }
  }

  const okDomains = build?.domains.filter((d) => d.ok) ?? []
  const current = typeof view === "object" ? view.domain : null
  const cmpFor = (d: string) => compare?.find((c) => c.domain === d)

  const openStudy = async (id: string, name: string) => {
    try {
      const r = await api.openStudy(id)
      setStudy({ id, name: r.name || name })
      if (r.problems?.length) setErr((p) => ({ ...p, spec: r.problems!.join(" ") }))
      setSpecPath(r.spec ?? ""); setRawPath(r.raw ?? ""); setVendorPath(r.vendor ?? "")
      setSpec(null); setRaw(null); setBuild(null); setCompare(null)
      // land where the reader left off: their last build and comparison, when the server
      // restored them, rather than an empty setup form
      if (r.built?.length) {
        if (r.raw) setRaw({ path: r.raw, cleared: false, datasets: [], coverage: [], missing: [] })
        await loadBuild()
        if (r.compared?.length) await loadCompare()
        setView("build")
      } else {
        setView("setup")
      }
    } catch (e) { fail("spec", e) }
  }

  if (view === "studies") {
    return (
      <div className="app-wash flex h-screen flex-col overflow-hidden">
        <TopBar>
          <div className="flex items-center gap-2">
            <div className="grid h-8 w-8 place-items-center rounded-lg bg-primary text-primary-foreground">
              <ShieldCheck className="h-4 w-4" />
            </div>
            <div className="leading-tight">
              <div className="text-[13px] font-semibold">SDTM Oversight</div>
              <div className="text-[11px] text-muted-foreground">local only · no network · no AI</div>
            </div>
          </div>
          <span className="ml-auto" />
          {appBuild && (
            <span className="hidden text-[11px] text-muted-foreground xl:inline">
              v{appBuild.version} · {appBuild.built.slice(5, 16)}
            </span>
          )}
        </TopBar>
        <main className="thin-scroll min-w-0 flex-1 overflow-y-auto px-6 py-6">
          <div className="mx-auto max-w-[1200px]">
            <StudiesView onOpen={(id, name) => void openStudy(id, name)} />
          </div>
        </main>
      </div>
    )
  }

  return (
    <div className="app-wash flex h-screen flex-col overflow-hidden">
      <TopBar>
        <div className="flex items-center gap-2">
          <div className="grid h-8 w-8 place-items-center rounded-lg bg-primary text-primary-foreground">
            <ShieldCheck className="h-4 w-4" />
          </div>
          <div className="leading-tight">
            <div className="text-[13px] font-semibold">{study?.name ?? "SDTM Oversight"}</div>
            <div className="text-[11px] text-muted-foreground">
              {study ? "saved as you work · local only" : "local only · no network · no AI"}
            </div>
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={() => {
          if (study) void api.closeStudy(study.id)
          setStudy(null); setSpec(null); setRaw(null); setBuild(null); setCompare(null)
          setView("studies")
        }}>
          <Layers className="mr-1.5 h-3.5 w-3.5" />Studies
        </Button>
        <div className="ml-2 hidden min-w-0 flex-1 items-center gap-2 md:flex">
          {spec && <Chip tone="violet">{spec.domains.length} domains</Chip>}
          {raw?.synthetic && <Chip tone="amber"><Sparkles className="h-3 w-3" />synthetic data</Chip>}
          {build?.out_dir && (
            <span className="truncate font-mono text-[11px] text-muted-foreground" dir="rtl"
                  title={build.out_dir}>{build.out_dir}</span>
          )}
        </div>
        {appBuild && (
          <span className="hidden text-[11px] text-muted-foreground xl:inline"
                title={`interface built ${appBuild.built}`}>
            v{appBuild.version} · {appBuild.built.slice(5, 16)}
          </span>
        )}
      </TopBar>

      {stale && (
        <div className="flex flex-wrap items-center gap-3 border-b border-amber-600/30
                        bg-amber-500/15 px-4 py-2 text-[13px]">
          <span>
            <b>You are looking at an older version of this page.</b> Your browser kept a cached
            copy; the server has a newer one.
          </span>
          <Button size="sm" className="h-7 text-xs"
                  onClick={() => window.location.reload()}>Load the current version</Button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <Rail>
          <RailSection title="Workflow">
            <RailItem icon={<Settings2 className="h-4 w-4" />} label="Setup"
                      active={view === "setup"} onClick={() => setView("setup")}
                      meta={spec ? "✓" : undefined} tone="green" />
            <RailItem icon={<FileSpreadsheet className="h-4 w-4" />} label="Spec"
                      active={view === "spec"} disabled={!spec} onClick={() => setView("spec")}
                      meta={spec?.inactive?.length
                        ? `${spec.active?.length}/${spec.domains.length}` : undefined}
                      tone="violet" />
            <RailItem icon={<Table2 className="h-4 w-4" />} label="Raw data"
                      active={view === "rawdata"} disabled={!raw}
                      onClick={() => setView("rawdata")} tone="violet" />
            <RailItem icon={<FunctionSquare className="h-4 w-4" />} label="Functions"
                      active={view === "functions"}
                      onClick={() => setView("functions")} tone="violet" />
            <RailItem icon={<Database className="h-4 w-4" />} label="Build"
                      active={view === "build"} disabled={!raw} onClick={() => setView("build")}
                      meta={okDomains.length || undefined} tone="violet" />
            <RailItem icon={<GitCompare className="h-4 w-4" />} label="Compare"
                      active={view === "compare"} disabled={!build?.domains.length}
                      onClick={() => setView("compare")}
                      meta={compare?.length || undefined}
                      tone={compare?.some((c) => c.status === "differences") ? "red" : "green"} />
            <RailItem icon={<FileSearch className="h-4 w-4" />} label="aCRF"
                      active={view === "acrf"} onClick={() => setView("acrf")} tone="violet" />
          </RailSection>

          {okDomains.length > 0 && (
            <RailSection title={`Domains · ${okDomains.length}`}>
              {okDomains.map((d) => {
                const c = cmpFor(d.domain)
                const tone = c?.status === "differences" ? "red"
                  : c?.status === "identical" ? "green"
                  : d.not_built ? "amber" : "slate"
                return (
                  <RailItem key={d.domain} icon={<Layers className="h-3.5 w-3.5" />}
                            label={d.domain} active={current === d.domain}
                            onClick={() => setView({ domain: d.domain })}
                            meta={d.rows.toLocaleString()} tone={tone} />
                )
              })}
            </RailSection>
          )}
        </Rail>

        <main className="thin-scroll min-w-0 flex-1 overflow-y-auto px-6 py-6">
          <div className="mx-auto max-w-[1500px]">
            {view === "setup" && (
              <SetupView specPath={specPath} setSpecPath={setSpecPath}
                         rawPath={rawPath} setRawPath={setRawPath}
                         spec={spec} raw={raw} err={err} busy={busy}
                         onSpec={() => void onSpec()} onRaw={() => void onRaw()}
                         onBrowse={(target, mode) => setPicker({ mode, target })}
                         onSynth={(dir) => { setRawPath(dir); setTimeout(() => void onRaw(), 0) }} />
            )}
            {view === "spec" && <SpecView />}
            {view === "rawdata" && <RawDataView />}
            {view === "build" && (
              <BuildView build={build} job={job?.kind === "build" ? job : null}
                         err={err.build ?? ""} busy={busy === "build"} ready={!!raw}
                         opts={opts} setOpts={setOpts} onBuild={() => void onBuild()}
                         specDomains={spec?.domains ?? []} inactive={spec?.inactive ?? []}
                         selected={buildSel} setSelected={setBuildSel}
                         onOpenDomain={(d) => setView({ domain: d })} />
            )}
            {view === "functions" && (
              <FunctionsView specDomains={spec?.domains ?? []} ready={!!spec && !!raw} />
            )}
            {view === "acrf" && <CrfView />}
            {view === "compare" && (
              <CompareView vendorPath={vendorPath} setVendorPath={setVendorPath}
                           rows={compare} job={job?.kind === "compare" ? job : null}
                           err={err.compare ?? ""} busy={busy === "compare"}
                           ready={!!build?.domains.length} synthetic={!!build?.synthetic}
                           ignoreCase={ignoreCase} setIgnoreCase={setIgnoreCase}
                           ignoreVars={ignoreVars} setIgnoreVars={setIgnoreVars}
                           builtDomains={okDomains.map((d) => d.domain)}
                           selected={compareSel} setSelected={setCompareSel}
                           onCompare={() => void onCompare()}
                           onBrowse={() => setPicker({ mode: "dir", target: "vendor" })} />
            )}
            {current && (
              <DomainView key={current} domain={current} onBack={() => setView("build")}
                          onChanged={() => { void loadBuild(); setCompare(null) }} />
            )}
          </div>
        </main>
      </div>

      <PathPicker open={!!picker} mode={picker?.mode ?? "dir"}
                  start={picker?.target === "spec" ? specPath
                       : picker?.target === "raw" ? rawPath : vendorPath}
                  onClose={() => setPicker(null)}
                  onPick={(p) => {
                    if (picker?.target === "spec") setSpecPath(p)
                    else if (picker?.target === "raw") setRawPath(p)
                    else setVendorPath(p)
                  }} />
    </div>
  )
}
