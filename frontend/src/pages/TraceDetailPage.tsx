import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'
import { fetchTrace } from '../api/client'
import type { TraceDetail } from '../api/types'
import { buildSpanTree, flattenTree } from '../lib/spanTree'
import { formatDuration, formatTime } from '../lib/format'
import Waterfall from '../components/Waterfall'
import SpanDetails from '../components/SpanDetails'

export default function TraceDetailPage() {
  // The :traceId part of the URL /traces/:traceId
  const { traceId } = useParams()
  const [trace, setTrace] = useState<TraceDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null)

  useEffect(() => {
    if (!traceId) return
    let ignore = false
    setTrace(null)
    setError(null)
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
  }, [traceId])

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

  return (
    <section className="page">
      <Link to="/traces" className="back-link">
        ← All traces
      </Link>

      {error && <p className="error-box">Could not load trace: {error}</p>}
      {!error && !trace && <p className="muted">Loading trace…</p>}

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
