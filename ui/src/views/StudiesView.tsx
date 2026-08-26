import { useCallback, useEffect, useState } from "react"
import { AlertTriangle, FileSpreadsheet, FolderOpen, Plus, Trash2 } from "lucide-react"
import { api } from "@/api"
import type { StudyCard } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Chip } from "@/components/grid"
import { Callout, EmptyState, PageHeader } from "@/components/shell"

/** The landing view: a study is a named piece of work that survives closing the application. */
export function StudiesView({ onOpen }: { onOpen: (id: string, name: string) => void }) {
  const [studies, setStudies] = useState<StudyCard[]>([])
  const [name, setName] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const load = useCallback(async () => {
    try { setStudies((await api.studies()).studies) }
    catch (e) { setError((e as Error).message) }
  }, [])
  useEffect(() => { void load() }, [load])

  const create = async () => {
    if (!name.trim()) return
    setBusy(true); setError("")
    try {
      const r = await api.createStudy(name.trim())
      onOpen(r.id, name.trim())
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <>
      <PageHeader title="Studies"
        subtitle="Each study keeps its own spec, raw data, mappings and preparation steps. Everything you decide is saved as you decide it." />

      <div className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Input className="h-9 max-w-sm flex-1 text-sm" placeholder="New study name, e.g. GS-US-576-4001"
                 value={name} onChange={(e) => setName(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && void create()} />
          <Button onClick={() => void create()} disabled={busy || !name.trim()}>
            <Plus className="mr-1.5 h-3.5 w-3.5" />Create study
          </Button>
        </div>

        {error && <Callout tone="bad">{error}</Callout>}

        {!studies.length ? (
          <EmptyState icon={<FileSpreadsheet className="h-8 w-8" />} title="No studies yet">
            Create one to begin. A study holds the mapping spec, the raw data folder, and every
            mapping decision you make — reopen it and the work is where you left it.
          </EmptyState>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {studies.map((s) => (
              <button key={s.id} onClick={() => onOpen(s.id, s.name)}
                      className="group rounded-xl border bg-surface p-4 text-left transition
                                 hover:border-primary/50 hover:shadow-sm">
                <div className="flex items-start gap-2">
                  <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold">{s.name}</h3>
                  <span role="button" tabIndex={0}
                        onClick={(e) => { e.stopPropagation()
                          if (confirm(`Delete "${s.name}"? The spec and raw data are untouched; the mappings you made here are removed.`))
                            void api.deleteStudy(s.id).then(load) }}
                        className="opacity-0 transition group-hover:opacity-60 hover:!opacity-100">
                    <Trash2 className="h-3.5 w-3.5" />
                  </span>
                </div>

                <div className="mt-2 space-y-1 text-[11px] text-muted-foreground">
                  <div className="flex items-center gap-1.5">
                    <FileSpreadsheet className="h-3 w-3 shrink-0" />
                    <span className="truncate" dir="rtl" title={s.spec_path}>
                      {s.spec_path || "no spec yet"}</span>
                    {s.spec_path && !s.spec_exists && <AlertTriangle className="h-3 w-3 text-amber-500" />}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <FolderOpen className="h-3 w-3 shrink-0" />
                    <span className="truncate" dir="rtl" title={s.raw_path}>
                      {s.raw_path || "no raw data yet"}</span>
                    {s.raw_path && !s.raw_exists && <AlertTriangle className="h-3 w-3 text-amber-500" />}
                  </div>
                </div>

                <div className="mt-3 flex flex-wrap gap-1">
                  {s.counts.domains_touched > 0 &&
                    <Chip tone="violet">{s.counts.domains_touched} domain(s) configured</Chip>}
                  {s.counts.edits > 0 && <Chip tone="blue">{s.counts.edits} hand edit(s)</Chip>}
                  {s.counts.pipelines > 0 && <Chip tone="teal">{s.counts.pipelines} prep step(s)</Chip>}
                  {!s.counts.domains_touched && <Chip>new</Chip>}
                </div>

                <p className="mt-3 text-[10px] text-muted-foreground">
                  updated {s.updated.replace("T", " ").slice(0, 16)}
                </p>
              </button>
            ))}
          </div>
        )}

        <Callout title="Where the work is kept">
          Each study is a folder under <code>studies/</code> holding a single{" "}
          <code>study.json</code> — readable, diffable, and independent of this application.
          A build is reproducible from the spec and the raw data; the study file is the record
          of the judgements applied on top of them.
        </Callout>
      </div>
    </>
  )
}
