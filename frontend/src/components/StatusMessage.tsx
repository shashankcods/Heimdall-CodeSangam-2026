import type { ReactNode } from 'react'

// The three "no table to show" situations every page has, so they look the same everywhere.

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="state" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  )
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty">
      <strong>{title}</strong>
      {children && <span className="muted">{children}</span>}
    </div>
  )
}

export function ErrorState({
  title,
  message,
  onRetry,
}: {
  title: string
  message: string
  onRetry?: () => void
}) {
  return (
    <div className="state state-error" role="alert">
      <strong>{title}</strong>
      <span>{message}</span>
      {onRetry && <button onClick={onRetry}>Try again</button>}
    </div>
  )
}
