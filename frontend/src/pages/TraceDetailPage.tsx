import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import { fetchTrace } from '../api/client'
import type { TraceDetail } from '../api/types'
import { buildSpanTree, flattenTree } from '../lib/spanTree'
import { formatDuration, formatTime } from '../lib/format'
import { usePageTitle } from '../lib/usePageTitle'
import Waterfall from '../components/Waterfall'
import SpanDetails from '../components/SpanDetails'
import { ErrorState, LoadingState } from '../components/StatusMessage'

export default function TraceDetailPage() {
  // The :traceId part of the URL /traces/:traceId
  const { traceId } = useParams()
  // Set by the trace list when it opens a trace, e.g. "?errors=1".
  const location = useLocation()
  const listSearch = (location.state as { listSearch?: string } | null)?.listSearch ?? ''
  const [trace, setTrace] = useState<TraceDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null)
  const [reloadCount, setReloadCount] = useState(0)

  useEffect(() => {
    if (!traceId) return
    let ignore = false
    setTrace(null)
    setError(null)
    // A different trace starts fully expanded.
    setCollapsed(new Set())
    fetchTrace(traceId)
      .then((result) => {
        if (ignore) return
        setTrace(result)
        // Start with the first error span selected, else the root.
        const firstError = result.spans.find((s) => s.status_code === 'ERROR')
        setSelectedSpanId((firstError ?? result.spans[0])?.span_id ?? null)
      })
      .catch((err: Error) => {
        if (!ignore) setError(err.message.startsWith('404') ? 'Trace not found.' : err.message)
      })
    return () => {
      ignore = true
    }
  }, [traceId, reloadCount])

  // Rebuild the tree only when the trace changes, not on every click.
  const tree = useMemo(() => (trace ? buildSpanTree(trace.spans) : []), [trace])
  const rows = useMemo(() => flattenTree(tree, collapsed), [tree, collapsed])

  const toggle = (spanId: string) => {
    // Never change state in place: copy the Set, change the copy.
    const next = new Set(collapsed)
    if (next.has(spanId)) next.delete(spanId)
    else next.add(spanId)
    setCollapsed(next)
  }

  const selectedSpan = trace?.spans.find((s) => s.span_id === selectedSpanId) ?? null
  const root = tree[0]?.span
  const errorCount = trace?.spans.filter((s) => s.status_code === 'ERROR').length ?? 0
  usePageTitle(root?.name ?? 'Trace')

  return (
    <section className="page">
      <Link to={`/traces${listSearch}`} className="back-link">
        ← All traces
      </Link>

      {error && (
        <ErrorState
          title="Could not load trace"
          message={error}
          // Retrying can't help when the trace doesn't exist.
          onRetry={error === 'Trace not found.' ? undefined : () => setReloadCount((n) => n + 1)}
        />
      )}
      {!error && !trace && <LoadingState label="Loading trace…" />}

      {trace && (
        <>
          <h1>{root?.name ?? 'Trace'}</h1>
          <p className="trace-meta">
            <span>{root?.service}</span>
            <span>{formatTime(trace.start_ms)}</span>
            <span>{formatDuration(trace.duration_ms)}</span>
            <span>{trace.spans.length} spans</span>
            {errorCount > 0 && <span className="error-text">{errorCount} errors</span>}
            <span className="mono">{trace.trace_id}</span>
            <Link to={`/logs?trace_id=${trace.trace_id}`}>View logs →</Link>
          </p>

          <Waterfall
            trace={trace}
            rows={rows}
            collapsed={collapsed}
            selectedSpanId={selectedSpanId}
            onToggle={toggle}
            onSelect={setSelectedSpanId}
          />

          {selectedSpan && <SpanDetails span={selectedSpan} />}
        </>
      )}
    </section>
  )
}
