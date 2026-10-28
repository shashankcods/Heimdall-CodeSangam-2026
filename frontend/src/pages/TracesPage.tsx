import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router'
import { fetchServices, fetchTraces } from '../api/client'
import type { TraceFilters, TraceSummary } from '../api/types'
import { formatDuration, formatTime } from '../lib/format'
import { usePageTitle } from '../lib/usePageTitle'
import { EmptyState, ErrorState, LoadingState } from '../components/StatusMessage'
import SidebarPanel from '../components/SidebarPanel'
import TraceScatter from '../components/TraceScatter'
import { serviceColor } from '../lib/serviceColor'

const AUTO_REFRESH_MS = 5000

const MINUTE = 60 * 1000
// URL value -> milliseconds. No ?lookback= means the backend's default, the last hour.
const LOOKBACKS: { value: string; label: string; ms: number }[] = [
  { value: '5m', label: 'Last 5 minutes', ms: 5 * MINUTE },
  { value: '15m', label: 'Last 15 minutes', ms: 15 * MINUTE },
  { value: '', label: 'Last hour', ms: 60 * MINUTE },
  { value: '6h', label: 'Last 6 hours', ms: 6 * 60 * MINUTE },
  { value: '24h', label: 'Last 24 hours', ms: 24 * 60 * MINUTE },
]

// p95: the duration 95% of traces are faster than.
function percentile95(values: number[]): number {
  if (values.length === 0) return 0
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.ceil(sorted.length * 0.95) - 1]
}

// Filters live in the URL (/traces?service=x&errors=1&min_ms=40), so they survive
// opening a trace and coming back, a page reload, or sharing the link.
function filtersFromUrl(params: URLSearchParams): TraceFilters {
  const minMs = params.get('min_ms')
  return {
    service: params.get('service'),
    errorsOnly: params.get('errors') === '1',
    minDurationMs: minMs !== null && minMs !== '' && !Number.isNaN(Number(minMs)) ? Number(minMs) : null,
    lookbackMs: LOOKBACKS.find((l) => l.value !== '' && l.value === params.get('lookback'))?.ms ?? null,
  }
}

function filtersToUrl(filters: TraceFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.service) params.set('service', filters.service)
  if (filters.errorsOnly) params.set('errors', '1')
  if (filters.minDurationMs !== null) params.set('min_ms', String(filters.minDurationMs))
  const lookback = LOOKBACKS.find((l) => l.ms === filters.lookbackMs)
  if (filters.lookbackMs !== null && lookback?.value) params.set('lookback', lookback.value)
  return params
}

export default function TracesPage() {
  usePageTitle('Traces')
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()

  // The URL string changes only when a filter changes, so it's a stable dependency.
  const filterKey = searchParams.toString()
  const filters = useMemo(() => filtersFromUrl(new URLSearchParams(filterKey)), [filterKey])
  const hasFilters = filterKey !== ''

  const updateFilters = (change: Partial<TraceFilters>) => {
    // replace: true so each keystroke doesn't add a Back-button step.
    setSearchParams(filtersToUrl({ ...filters, ...change }), { replace: true })
  }

  const [services, setServices] = useState<string[]>([])
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Bumped by Refresh / Try again; listed below so the effect re-runs.
  const [refreshCount, setRefreshCount] = useState(0)
  const refresh = () => setRefreshCount((n) => n + 1)
  const [autoRefresh, setAutoRefresh] = useState(true)

  // Service names for the dropdown: load once.
  useEffect(() => {
    fetchServices().then(setServices).catch(() => setServices([]))
  }, [])

  // Trace list: reload whenever a filter changes or Refresh is clicked.
  useEffect(() => {
    // If filters change again before this request finishes, ignore its result,
    // so an old slow response can't overwrite a newer one.
    let ignore = false
    setLoading(true)
    setError(null)
    fetchTraces(filters)
      .then((result) => {
        if (!ignore) setTraces(result)
      })
      .catch((err: Error) => {
        if (!ignore) setError(err.message)
      })
      .finally(() => {
        if (!ignore) setLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [filters, refreshCount])

  // Auto-refresh: quietly re-fetch every few seconds so new requests show up on
  // their own. No loading state, so the table doesn't fade on every tick.
  useEffect(() => {
    if (!autoRefresh) return
    let ignore = false
    const timer = setInterval(() => {
      // Skip while the browser tab is hidden: nobody is looking.
      if (document.visibilityState !== 'visible') return
      fetchTraces(filters)
        .then((result) => {
          if (!ignore) {
            setTraces(result)
            setError(null)
          }
        })
        .catch(() => {
          // Keep showing the last list; the next tick (or Refresh) tries again.
        })
    }, AUTO_REFRESH_MS)
    return () => {
      ignore = true
      clearInterval(timer)
    }
  }, [autoRefresh, filters])

  // Remember this list's URL so the trace page's "← All traces" comes back to it.
  const openTrace = (traceId: string) =>
    navigate(`/traces/${traceId}`, { state: { listSearch: location.search } })

  const firstLoad = loading && traces.length === 0
  // Duration bars are relative to the slowest trace in the list.
  const maxDuration = Math.max(1, ...traces.map((t) => t.duration_ms))
  const errorTraces = traces.filter((t) => t.has_error).length
  const errorRate = traces.length > 0 ? errorTraces / traces.length : 0

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>Traces</h1>
          <p className="muted">Every request across your services, newest first.</p>
        </div>
        <div className="page-actions">
          <label className="toggle">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            Auto-refresh
          </label>
          <button className="button" onClick={refresh}>Refresh</button>
        </div>
      </div>

      <SidebarPanel title="Search traces">
        <label className="field">
          Service
          <select
            value={filters.service ?? ''}
            onChange={(e) => updateFilters({ service: e.target.value || null })}
          >
            <option value="">All services</option>
            {services.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          Lookback
          <select
            value={LOOKBACKS.find((l) => l.ms === filters.lookbackMs)?.value ?? ''}
            onChange={(e) =>
              updateFilters({ lookbackMs: LOOKBACKS.find((l) => l.value === e.target.value && l.value !== '')?.ms ?? null })
            }
          >
            {LOOKBACKS.map((l) => (
              <option key={l.label} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          Min duration (ms)
          <input
            type="number"
            min={0}
            placeholder="any"
            value={filters.minDurationMs ?? ''}
            onChange={(e) =>
              updateFilters({ minDurationMs: e.target.value === '' ? null : Number(e.target.value) })
            }
          />
        </label>

        <label className="toggle">
          <input
            type="checkbox"
            checked={filters.errorsOnly}
            onChange={(e) => updateFilters({ errorsOnly: e.target.checked })}
          />
          Errors only
        </label>

        {hasFilters && (
          <button className="link-button" onClick={() => setSearchParams({}, { replace: true })}>
            Clear filters
          </button>
        )}
      </SidebarPanel>

      {error ? (
        <ErrorState title="Could not load traces" message={error} onRetry={refresh} />
      ) : firstLoad ? (
        <LoadingState label="Loading traces…" />
      ) : traces.length === 0 ? (
        hasFilters ? (
          <EmptyState title="No traces match these filters">Try removing a filter.</EmptyState>
        ) : (
          <EmptyState title="No traces in the last hour">
            Send some with <code>uv run demo/run_demo.py</code>, then click Refresh.
          </EmptyState>
        )
      ) : (
        <>
          <div className="stat-grid">
            <div className="card stat">
              <span className="stat-label">Traces</span>
              <span className="stat-value">{traces.length}</span>
            </div>
            <div className="card stat">
              <span className="stat-label">Error rate</span>
              <span className={errorRate > 0.05 ? 'stat-value error-text' : 'stat-value'}>
                {(errorRate * 100).toFixed(1)}%
              </span>
            </div>
            <div className="card stat">
              <span className="stat-label">p95 duration</span>
              <span className="stat-value">{formatDuration(percentile95(traces.map((t) => t.duration_ms)))}</span>
            </div>
            <div className="card stat">
              <span className="stat-label">Services</span>
              <span className="stat-value">{services.length}</span>
            </div>
          </div>
          <TraceScatter traces={traces} onOpen={openTrace} />
          <p className="result-count muted">
            {traces.length} {traces.length === 1 ? 'trace' : 'traces'} · {errorTraces} with errors
            {traces.length === 100 && ' · showing the newest 100'}
          </p>
          <div className={loading ? 'card table-scroll is-refreshing' : 'card table-scroll'}>
            <table className="trace-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Service</th>
                  <th>Operation</th>
                  <th className="num">Spans</th>
                  <th className="num">Errors</th>
                  <th className="duration-col">Duration</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {traces.map((t) => (
                  <tr key={t.trace_id} onClick={() => openTrace(t.trace_id)}>
                    <td className="mono muted">{formatTime(t.start_ms)}</td>
                    <td>
                      <span className="service-chip">
                        <span className="service-dot" style={{ background: serviceColor(t.root_service) }} />
                        {t.root_service}
                      </span>
                    </td>
                    <td className="mono">
                      <Link to={`/traces/${t.trace_id}`} state={{ listSearch: location.search }}>
                        {t.root_name}
                      </Link>
                    </td>
                    <td className="num mono">{t.span_count}</td>
                    <td className="num mono">
                      {t.error_count > 0 ? <span className="error-count">{t.error_count}</span> : '0'}
                    </td>
                    <td>
                      <span className="duration-cell">
                        <span className="duration-track">
                          <span
                            className={t.has_error ? 'duration-bar err' : 'duration-bar'}
                            style={{ width: `${Math.max(2, (t.duration_ms / maxDuration) * 100)}%` }}
                          />
                        </span>
                        <span className="mono">{formatDuration(t.duration_ms)}</span>
                      </span>
                    </td>
                    <td>
                      <span className={t.has_error ? 'badge badge-error' : 'badge badge-ok'}>
                        {t.has_error ? 'Error' : 'OK'}
                        {t.http_status !== null && ` · ${t.http_status}`}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}
