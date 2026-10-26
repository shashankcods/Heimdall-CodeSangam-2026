import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { fetchServices, fetchTraces } from '../api/client'
import type { TraceFilters, TraceSummary } from '../api/types'
import { formatDuration, formatTime } from '../lib/format'

export default function TracesPage() {
  const navigate = useNavigate()
  const [filters, setFilters] = useState<TraceFilters>({
    service: null,
    errorsOnly: false,
    minDurationMs: null,
  })
  const [services, setServices] = useState<string[]>([])
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Bumped by the Refresh button; listed below so the effect re-runs.
  const [refreshCount, setRefreshCount] = useState(0)

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

  return (
    <section className="page">
      <h1>Traces</h1>

      <div className="filters">
        <label>
          Service
          <select
            value={filters.service ?? ''}
            onChange={(e) => setFilters({ ...filters, service: e.target.value || null })}
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
              setFilters({
                ...filters,
                minDurationMs: e.target.value === '' ? null : Number(e.target.value),
              })
            }
          />
        </label>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={filters.errorsOnly}
            onChange={(e) => setFilters({ ...filters, errorsOnly: e.target.checked })}
          />
          Errors only
        </label>

        <button onClick={() => setRefreshCount((n) => n + 1)}>Refresh</button>
      </div>

      {error && <p className="error-box">Could not load traces: {error}</p>}

      {!error && !loading && traces.length === 0 && (
        <p className="muted">No traces in the last hour. Send some with demo/sdk_smoke.py.</p>
      )}

      {traces.length > 0 && (
        <div className="table-scroll">
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
                <tr key={t.trace_id} onClick={() => navigate(`/traces/${t.trace_id}`)}>
                  <td>{formatTime(t.start_ms)}</td>
                  <td>{t.root_service}</td>
                  <td className="mono">
                    <Link to={`/traces/${t.trace_id}`}>{t.root_name}</Link>
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
