import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router'
import { fetchServices, fetchTraces } from '../api/client'
import type { TraceFilters, TraceSummary } from '../api/types'
import { formatDuration, formatTime } from '../lib/format'
import { usePageTitle } from '../lib/usePageTitle'
import { EmptyState, ErrorState, LoadingState } from '../components/StatusMessage'

// Filters live in the URL (/traces?service=x&errors=1&min_ms=40), so they survive
// opening a trace and coming back, a page reload, or sharing the link.
function filtersFromUrl(params: URLSearchParams): TraceFilters {
  const minMs = params.get('min_ms')
  return {
    service: params.get('service'),
    errorsOnly: params.get('errors') === '1',
    minDurationMs: minMs !== null && minMs !== '' && !Number.isNaN(Number(minMs)) ? Number(minMs) : null,
  }
}

function filtersToUrl(filters: TraceFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.service) params.set('service', filters.service)
  if (filters.errorsOnly) params.set('errors', '1')
  if (filters.minDurationMs !== null) params.set('min_ms', String(filters.minDurationMs))
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

  // Remember this list's URL so the trace page's "← All traces" comes back to it.
  const openTrace = (traceId: string) =>
    navigate(`/traces/${traceId}`, { state: { listSearch: location.search } })

  const firstLoad = loading && traces.length === 0

  return (
    <section className="page">
      <h1>Traces</h1>

      <div className="filters">
        <label>
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

        <label>
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

        <label className="checkbox">
          <input
            type="checkbox"
            checked={filters.errorsOnly}
            onChange={(e) => updateFilters({ errorsOnly: e.target.checked })}
          />
          Errors only
        </label>

        <button onClick={refresh}>Refresh</button>
        {hasFilters && (
          <button className="link-button" onClick={() => setSearchParams({}, { replace: true })}>
            Clear filters
          </button>
        )}
      </div>

      {error ? (
        <ErrorState title="Could not load traces" message={error} onRetry={refresh} />
      ) : firstLoad ? (
        <LoadingState label="Loading traces…" />
      ) : traces.length === 0 ? (
        hasFilters ? (
          <EmptyState title="No traces match these filters">Try removing a filter.</EmptyState>
        ) : (
          <EmptyState title="No traces in the last hour">
            Send some with <code>uv run demo/sdk_smoke.py</code>, then click Refresh.
          </EmptyState>
        )
      ) : (
        <div className={loading ? 'table-scroll is-refreshing' : 'table-scroll'}>
          <table className="trace-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Service</th>
                <th>Operation</th>
                <th className="num">Duration</th>
                <th className="num">Spans</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {traces.map((t) => (
                <tr key={t.trace_id} onClick={() => openTrace(t.trace_id)}>
                  <td>{formatTime(t.start_ms)}</td>
                  <td>{t.root_service}</td>
                  <td className="mono">
                    <Link to={`/traces/${t.trace_id}`} state={{ listSearch: location.search }}>
                      {t.root_name}
                    </Link>
                  </td>
                  <td className="num">{formatDuration(t.duration_ms)}</td>
                  <td className="num">{t.span_count}</td>
                  <td>
                    <span className={t.has_error ? 'badge badge-error' : 'badge badge-ok'}>
                      {t.has_error ? 'error' : 'ok'}
                      {t.http_status !== null && ` · ${t.http_status}`}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
