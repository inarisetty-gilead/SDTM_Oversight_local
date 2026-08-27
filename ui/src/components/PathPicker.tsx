import { useCallback, useEffect, useState } from "react"
import { ChevronUp, File, Folder, Loader2 } from "lucide-react"
import { api } from "@/api"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"

type Entry = { name: string; path: string; size?: number }

export function PathPicker({
  open, mode, start, onClose, onPick, accept,
}: {
  open: boolean
  mode: "file" | "dir"
  start: string
  onClose: () => void
  onPick: (path: string) => void
  accept?: RegExp
}) {
  const [cwd, setCwd] = useState("")
  const [parent, setParent] = useState<string | null>(null)
  const [dirs, setDirs] = useState<Entry[]>([])
  const [files, setFiles] = useState<Entry[]>([])
  const [shortcuts, setShortcuts] = useState<Entry[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const load = useCallback(async (path: string) => {
    setBusy(true); setError("")
    try {
      const d = await api.browse(path)
      setCwd(d.path); setParent(d.parent); setDirs(d.dirs)
      setFiles(mode === "file"
        ? d.files.filter((f) => (accept ?? /\.(xlsx|xlsm|xls)$/i).test(f.name))
        : d.files)
      setShortcuts(d.shortcuts)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }, [mode, accept])

  useEffect(() => { if (open) void load(start) }, [open, start, load])

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{mode === "file" ? "Choose the mapping spec" : "Choose a folder"}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-wrap gap-1.5">
          {shortcuts.map((s) => (
            <Button key={s.path} variant="secondary" size="sm" className="h-7 text-xs"
                    onClick={() => void load(s.path)}>{s.name}</Button>
          ))}
        </div>

        <div className="flex items-center gap-2">
          <Button variant="outline" size="icon" className="h-8 w-8 shrink-0"
                  disabled={!parent} onClick={() => parent && void load(parent)}>
            <ChevronUp className="h-4 w-4" />
          </Button>
          <code className="min-w-0 flex-1 truncate rounded bg-muted px-2 py-1.5 text-xs"
                dir="rtl" title={cwd}>{cwd}</code>
        </div>

        <ScrollArea className="h-72 rounded-md border">
          {busy && <div className="flex h-24 items-center justify-center">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></div>}
          {error && <p className="p-3 text-sm text-destructive">{error}</p>}
          {!busy && !error && (
            <div className="p-1">
              {dirs.map((d) => (
                <button key={d.path} onClick={() => void load(d.path)}
                        className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-accent">
                  <Folder className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{d.name}</span>
                </button>
              ))}
              {mode === "file" && files.map((f) => (
                <button key={f.path} onClick={() => { onPick(f.path); onClose() }}
                        className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-accent">
                  <File className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{f.name}</span>
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                    {Math.round((f.size ?? 0) / 1024).toLocaleString()} KB</span>
                </button>
              ))}
              {mode === "dir" && files.length > 0 && (
                <p className="px-2 py-1.5 text-xs text-muted-foreground">
                  {files.length} data file(s) in this folder</p>
              )}
              {!dirs.length && !files.length && (
                <p className="px-2 py-1.5 text-sm text-muted-foreground">empty</p>
              )}
            </div>
          )}
        </ScrollArea>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          {mode === "dir" && (
            <Button onClick={() => { onPick(cwd); onClose() }}>Use this folder</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function PathField({
  value, onChange, placeholder, mode, onBrowse,
}: {
  value: string; onChange: (v: string) => void; placeholder: string
  mode: "file" | "dir"; onBrowse: () => void
}) {
  return (
    <div className="flex gap-2">
      <input
        value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder}
        spellCheck={false}
        className="flex h-9 min-w-0 flex-1 rounded-md border border-input bg-transparent px-3 py-1 font-mono text-xs shadow-xs outline-none transition focus-visible:ring-[3px] focus-visible:ring-ring/50"
      />
      <Button variant="outline" onClick={onBrowse}>
        {mode === "file" ? "Choose file" : "Choose folder"}
      </Button>
    </div>
  )
}
