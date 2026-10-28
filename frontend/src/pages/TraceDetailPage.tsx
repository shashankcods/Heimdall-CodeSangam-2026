import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import { fetchTrace } from '../api/client'
import type { TraceDetail } from '../api/types'
import { buildSpanTree, deepestErrorSpan, flattenTree } from '../lib/spanTree'
import { formatDuration, formatTime } from '../lib/format'
import { usePageTitle } from '../lib/usePageTitle'
import { serviceColor } from '../lib/serviceColor'
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
        // Start on the span that actually broke, else the root.
        const cause = deepestErrorSpan(buildSpanTree(result.spans))
        setSelectedSpanId((cause ?? result.spans[0])?.span_id ?? null)
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
  const cause = useMemo(() => deepestErrorSpan(tree), [tree])
  // Each service once, in the order it first appears: the key for the colored dots.
  const services = useMemo(() => [...new Set(trace?.spans.map((s) => s.service) ?? [])], [trace])
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
          <div className="page-header">
            <div>
              <div className="title-row">
                <h1 className="mono">{root?.name ?? 'Trace'}</h1>
                {errorCount > 0 ? (
                  <span className="badge badge-error">
                    Error{root?.http_status != null && ` · ${root.http_status}`}
                  </span>
                ) : (
                  <span className="badge badge-ok">OK</span>
                )}
              </div>
              <p className="trace-meta">
                <span>{root?.service}</span>
                <span>{formatTime(trace.start_ms)}</span>
                <span className="strong">{formatDuration(trace.duration_ms)}</span>
                <span>{trace.spans.length} spans</span>
                {errorCount > 0 && <span className="error-text strong">{errorCount} errors</span>}
                <span className="mono">{trace.trace_id}</span>
              </p>
            </div>
            <div className="page-actions">
              <Link className="button primary" to={`/logs?trace_id=${trace.trace_id}`}>
                View logs →
              </Link>
            </div>
          </div>

          {cause && (
            <div className="cause-box" role="note">
              <div>
                <strong>
                  Deepest failing span: <span className="mono">{cause.name}</span> on {cause.service}
                </strong>
                <span>
                  {cause.http_status !== null && `${cause.http_status} after `}
                  {formatDuration(cause.duration_ms)}
                  {cause.status_msg && ` · ${cause.status_msg}`}
                </span>
              </div>
              {cause.span_id !== selectedSpanId && (
                <button className="button" onClick={() => setSelectedSpanId(cause.span_id)}>
                  Show span
                </button>
              )}
            </div>
          )}

          <div className="trace-columns">
            <div className="trace-main">
              <div className="service-legend" aria-label="Services in this trace">
                {services.map((name) => (
                  <span key={name} className="service-chip">
                    <span className="service-dot" style={{ background: serviceColor(name) }} />
                    {name}
                  </span>
                ))}
              </div>
              <Waterfall
                trace={trace}
                rows={rows}
                collapsed={collapsed}
                selectedSpanId={selectedSpanId}
                onToggle={toggle}
                onSelect={setSelectedSpanId}
              />
            </div>
            {selectedSpan && <SpanDetails span={selectedSpan} />}
          </div>
        </>
      )}
    </section>
  )
}
