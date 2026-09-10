import type { ReactNode } from "react"
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react"
import { cn } from "@/lib/utils"
import { Chip, type ChipTone } from "./grid"

export function Rail({ children }: { children: ReactNode }) {
  return (
    <aside className="thin-scroll hidden w-60 shrink-0 overflow-y-auto border-r bg-rail lg:block">
      {children}
    </aside>
  )
}

export function RailSection({ title, action, children }: {
  title: string; action?: ReactNode; children: ReactNode
}) {
  return (
    <div className="px-3 py-3">
      <div className="mb-1.5 flex items-center gap-2 px-2">
        <span className="h-3 w-0.5 rounded-full bg-gradient-to-b from-primary to-[var(--accent-2)]" />
        <h3 className="flex-1 text-[10px] font-semibold uppercase tracking-[0.09em] text-muted-foreground">
          {title}
        </h3>
        {action}
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  )
}

export function RailItem({ icon, label, meta, active, disabled, tone, onClick }: {
  icon?: ReactNode; label: string; meta?: ReactNode; active?: boolean
  disabled?: boolean; tone?: ChipTone; onClick?: () => void
}) {
  return (
    <button
      onClick={onClick} disabled={disabled}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] transition-all",
        active
          ? "bg-gradient-to-r from-primary/15 to-primary/5 font-medium text-primary shadow-sm [box-shadow:inset_2.5px_0_0_0_var(--primary)]"
          : "text-foreground/85 hover:translate-x-0.5 hover:bg-accent hover:text-foreground",
        disabled && "cursor-not-allowed opacity-40 hover:translate-x-0 hover:bg-transparent")}>
      {icon && <span className={cn("shrink-0", active ? "opacity-100" : "opacity-70")}>{icon}</span>}
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {meta !== undefined && (typeof meta === "string" || typeof meta === "number"
        ? <Chip tone={tone ?? "slate"}>{meta}</Chip>
        : meta)}
    </button>
  )
}

export function TopBar({ children }: { children: ReactNode }) {
  return (
    <header className="fancy-topbar sticky top-0 z-40 flex h-14 shrink-0 items-center gap-3
                       bg-surface/85 px-4 backdrop-blur-md">
      {children}
    </header>
  )
}

export function PageHeader({ title, subtitle, actions }: {
  title: ReactNode; subtitle?: ReactNode; actions?: ReactNode
}) {
  return (
    <div className="rise-in mb-5 flex flex-wrap items-start gap-3">
      <div className="min-w-0 flex-1">
        <h2 className="text-[20px] font-semibold tracking-tight">{title}</h2>
        <div className="mt-1.5 h-[3px] w-10 rounded-full bg-gradient-to-r from-primary to-[var(--accent-2)]" />
        {subtitle && <p className="mt-2 text-[13px] text-muted-foreground">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

/** Icon disc tints for panel headers — one colour per panel, so a page of panels reads as
 *  a set of landmarks, not a wall of identical boxes. */
const PANEL_TONE: Record<ChipTone, string> = {
  violet: "bg-indigo-500/12 text-indigo-600 dark:text-indigo-400",
  green: "bg-emerald-500/12 text-emerald-600 dark:text-emerald-400",
  amber: "bg-amber-500/14 text-amber-600 dark:text-amber-400",
  red: "bg-rose-500/12 text-rose-600 dark:text-rose-400",
  blue: "bg-sky-500/12 text-sky-600 dark:text-sky-400",
  teal: "bg-teal-500/12 text-teal-600 dark:text-teal-400",
  fuchsia: "bg-purple-500/12 text-purple-600 dark:text-purple-400",
  slate: "bg-primary/10 text-primary",
}

export function Panel({ title, description, actions, icon, tone = "slate", children, className }: {
  title?: ReactNode; description?: ReactNode; actions?: ReactNode
  icon?: ReactNode; tone?: ChipTone
  children: ReactNode; className?: string
}) {
  return (
    <section className={cn(
      "fancy-card fancy-card-hover rise-in overflow-hidden rounded-xl border bg-surface",
      className)}>
      {(title || actions) && (
        <div className="panel-head flex flex-wrap items-center gap-3 border-b px-4 py-3">
          {icon && (
            <span className={cn("grid h-8 w-8 shrink-0 place-items-center rounded-lg shadow-sm",
              PANEL_TONE[tone])}>
              {icon}
            </span>
          )}
          <div className="min-w-0 flex-1">
            {title && <h3 className="text-[14px] font-semibold tracking-tight">{title}</h3>}
            {description && <p className="mt-0.5 text-[12px] text-muted-foreground">{description}</p>}
          </div>
          {actions}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

const METRIC_BAR: Record<string, string> = {
  good: "from-emerald-500 to-emerald-400",
  warn: "from-amber-500 to-amber-400",
  bad: "from-rose-500 to-rose-400",
  none: "from-primary to-[var(--accent-2)]",
}

export function Metric({ value, label, tone, hint }: {
  value: ReactNode; label: string; tone?: "good" | "warn" | "bad"; hint?: string
}) {
  return (
    <div className="fancy-card fancy-card-hover rise-in relative overflow-hidden rounded-xl border bg-surface px-4 py-3"
         title={hint}>
      <div className={cn("absolute inset-x-0 top-0 h-[3px] bg-gradient-to-r opacity-80",
        METRIC_BAR[tone ?? "none"])} />
      <div className="text-[10.5px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </div>
      <div className={cn("num-cell mt-1.5 text-[25px] font-semibold leading-none tracking-tight",
        tone === "good" && "text-emerald-600 dark:text-emerald-400",
        tone === "warn" && "text-amber-600 dark:text-amber-400",
        tone === "bad" && "text-rose-600 dark:text-rose-400")}>{value}</div>
    </div>
  )
}

export function Metrics({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">{children}</div>
}

const CALLOUT_ICON = {
  info: Info, good: CheckCircle2, warn: AlertTriangle, bad: XCircle,
} as const

export function Callout({ tone = "info", title, children }: {
  tone?: "info" | "good" | "warn" | "bad"; title?: ReactNode; children?: ReactNode
}) {
  const Icon = CALLOUT_ICON[tone]
  return (
    <div className={cn(
      "rise-in flex gap-3 rounded-xl border border-l-4 px-4 py-3 text-[13px] shadow-sm",
      tone === "good" && "border-emerald-600/25 border-l-emerald-500 bg-emerald-500/8",
      tone === "warn" && "border-amber-600/25 border-l-amber-500 bg-amber-500/8",
      tone === "bad" && "border-rose-600/25 border-l-rose-500 bg-rose-500/8",
      tone === "info" && "border-l-primary/60 bg-muted/40")}>
      <Icon className={cn("mt-0.5 h-4 w-4 shrink-0",
        tone === "good" && "text-emerald-600 dark:text-emerald-400",
        tone === "warn" && "text-amber-600 dark:text-amber-400",
        tone === "bad" && "text-rose-600 dark:text-rose-400",
        tone === "info" && "text-primary")} />
      <div className="min-w-0 flex-1">
        {title && <div className="mb-1 font-medium">{title}</div>}
        {children && <div className="text-muted-foreground [&_code]:text-foreground">{children}</div>}
      </div>
    </div>
  )
}

export function EmptyState({ icon, title, children, action }: {
  icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode
}) {
  return (
    <div className="rise-in rounded-xl border border-dashed bg-surface/60 px-6 py-14 text-center">
      {icon && (
        <div className="mb-4 flex justify-center">
          <div className="glow-mark grid h-14 w-14 place-items-center rounded-2xl text-primary-foreground">
            {icon}
          </div>
        </div>
      )}
      <h3 className="text-[15px] font-semibold tracking-tight">{title}</h3>
      {children && <p className="mx-auto mt-1.5 max-w-md text-[13px] text-muted-foreground">{children}</p>}
      {action && <div className="mt-5 flex justify-center gap-2">{action}</div>}
    </div>
  )
}
