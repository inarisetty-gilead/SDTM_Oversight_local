// Data hooks behind the SAS components (components/sas.tsx): the function catalog, the SAS
// lines for something being edited, and the one-line sentence taken from them.
import { useEffect, useRef, useState } from "react"
import { api } from "@/api"
import type { SasCatalog } from "@/api"

let catalog$: Promise<SasCatalog> | null = null
/** The SAS function catalog, fetched once per page. */
export function useSasCatalog(): SasCatalog | null {
  const [cat, setCat] = useState<SasCatalog | null>(null)
  useEffect(() => {
    catalog$ ??= api.sasCatalog()
    let live = true
    catalog$.then((c) => { if (live) setCat(c) }).catch(() => { catalog$ = null })
    return () => { live = false }
  }, [])
  return cat
}

/** SAS lines for something being edited, refetched (debounced) whenever `key` changes. */
export function useSasLines(key: string, fetch: () => Promise<{ lines: string[] }>): string[] {
  const [lines, setLines] = useState<string[]>([])
  const fetchRef = useRef(fetch)
  useEffect(() => { fetchRef.current = fetch })
  useEffect(() => {
    let live = true
    const t = window.setTimeout(() => {
      fetchRef.current().then((r) => { if (live) setLines(r.lines) }).catch(() => { if (live) setLines([]) })
    }, 250)
    return () => { live = false; window.clearTimeout(t) }
  }, [key])
  return lines
}

/** The mapping or step as ONE line of SAS: its first DATA step (or statements) joined up to
 *  RUN — `data prep2; set ds; if DSCAT = 'X';` — the way a SAS programmer reads it. */
export function sasSentence(lines: string[]): string {
  const out: string[] = []
  for (const raw of lines) {
    const l = raw.trim()
    if (!l || l.startsWith("/*") && out.length) continue
    if (/^(run;|quit;)$/i.test(l)) { if (out.length) break; continue }
    out.push(l)
    if (/\brun;$/i.test(l)) break
  }
  return out.join(" ").replace(/\s+/g, " ")
}
