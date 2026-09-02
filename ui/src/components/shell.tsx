import type { ReactNode } from "react"
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
        <h3 className="flex-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
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
          ? "bg-gradient-to-r from-primary/15 to-primary/5 font-medium text-primary [box-shadow:inset_2.5px_0_0_0_var(--primary)]"
          : "hover:translate-x-0.5 hover:bg-accent",
        disabled && "cursor-not-allowed opacity-40 hover:bg-transparent")}>
      {icon && <span className="shrink-0 opacity-70">{icon}</span>}
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
                       bg-background/85 px-4 backdrop-blur-md">
      {children}
    </header>
  )
}

export function PageHeader({ title, subtitle, actions }: {
  title: ReactNode; subtitle?: ReactNode; actions?: ReactNode
}) {
  return (
    <div className="mb-5 flex flex-wrap items-start gap-3">
      <div className="min-w-0 flex-1">
        <h2 className="text-[19px] font-semibold tracking-tight">{title}</h2>
        {subtitle && <p className="mt-1 text-[13px] text-muted-foreground">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function Panel({ title, description, actions, children, className }: {
  title?: ReactNode; description?: ReactNode; actions?: ReactNode
  children: ReactNode; className?: string
}) {
  return (
    <section className={cn("fancy-card fancy-card-hover rounded-xl border bg-surface", className)}>
      {(title || actions) && (
        <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
          <div className="min-w-0 flex-1">
            {title && <h3 className="text-[14px] font-medium">{title}</h3>}
            {description && <p className="mt-0.5 text-[12px] text-muted-foreground">{description}</p>}
          </div>
          {actions}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Metric({ value, label, tone, hint }: {
  value: ReactNode; label: string; tone?: "good" | "warn" | "bad"; hint?: string
}) {
  return (
    <div className="fancy-card fancy-card-hover rounded-xl border bg-surface px-4 py-3" title={hint}>
      <div className={cn("num-cell text-[26px] font-semibold leading-none tracking-tight",
        tone === "good" && "text-emerald-600 dark:text-emerald-400",
        tone === "warn" && "text-amber-600 dark:text-amber-400",
        tone === "bad" && "text-rose-600 dark:text-rose-400")}>{value}</div>
      <div className="mt-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  )
}

export function Metrics({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">{children}</div>
}

export function Callout({ tone = "info", title, children }: {
  tone?: "info" | "good" | "warn" | "bad"; title?: ReactNode; children?: ReactNode
}) {
  return (
    <div className={cn(
      "rounded-xl border px-4 py-3 text-[13px]",
      tone === "good" && "border-emerald-600/25 bg-emerald-500/8",
      tone === "warn" && "border-amber-600/25 bg-amber-500/8",
      tone === "bad" && "border-rose-600/25 bg-rose-500/8",
      tone === "info" && "bg-muted/40")}>
      {title && <div className="mb-1 font-medium">{title}</div>}
      {children && <div className="text-muted-foreground [&_code]:text-foreground">{children}</div>}
    </div>
  )
}

export function EmptyState({ icon, title, children, action }: {
  icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode
}) {
  return (
    <div className="rounded-xl border border-dashed bg-surface/60 px-6 py-14 text-center">
      {icon && <div className="mb-3 flex justify-center text-muted-foreground/60">{icon}</div>}
      <h3 className="text-[15px] font-medium">{title}</h3>
      {children && <p className="mx-auto mt-1.5 max-w-md text-[13px] text-muted-foreground">{children}</p>}
      {action && <div className="mt-4 flex justify-center gap-2">{action}</div>}
    </div>
  )
}
