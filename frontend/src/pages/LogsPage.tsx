import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { fetchLogs, fetchServices, subscribeLogsStream } from '../api/client'
import type { LogFilters, LogRecord } from '../api/types'
import { formatDateTime } from '../lib/format'

const TIME_PRESETS = [
  { label: 'All time', minutes: null },
  { label: 'Last 15 minutes', minutes: 15 },
  { label: 'Last 1 hour', minutes: 60 },
  { label: 'Last 24 hours', minutes: 1440 },
]

const LIMIT_OPTIONS = [50, 100, 250, 500]
const SEVERITIES = ['ALL', 'ERROR', 'WARN', 'INFO', 'DEBUG', 'TRACE']
const MAX_LIVE_RECORDS = 500

export default function LogsPage() {
  const [searchParams, setSearchParams] = useSearchParams()

  // Filter state initialized from URL search params if present
  const [query, setQuery] = useState(searchParams.get('q') || '')
  const [isRegex, setIsRegex] = useState(searchParams.get('regex') === 'true')
  const [selectedService, setSelectedService] = useState(searchParams.get('service') || '')
  const [selectedSeverity, setSelectedSeverity] = useState(searchParams.get('severity') || 'ALL')
  const [traceIdFilter, setTraceIdFilter] = useState(searchParams.get('trace_id') || '')
  const [selectedPreset, setSelectedPreset] = useState<number | null>(60) // Default 1 hour
  const [limit, setLimit] = useState(100)

  // Data & loading state
  const [logs, setLogs] = useState<LogRecord[]>([])
  const [services, setServices] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({})

  // Live tail state
  const [isLiveTail, setIsLiveTail] = useState(false)
  const [isStreamPaused, setIsStreamPaused] = useState(false)
  const [streamStatus, setStreamStatus] = useState<'idle' | 'connecting' | 'connected' | 'error'>('idle')
  const isStreamPausedRef = useRef(isStreamPaused)
  isStreamPausedRef.current = isStreamPaused

  // 1. Fetch available services on mount
  useEffect(() => {
    fetchServices()
      .then(setServices)
      .catch((err) => {
        console.warn('Could not load service list for filter dropdown:', err)
      })
  }, [])

  // 2. Compute timestamp range based on preset
  const getTimeRange = useCallback((minutes: number | null): { fromMs: number | null; toMs: number | null } => {
    if (minutes === null) return { fromMs: null, toMs: null }
    const now = Date.now()
    return {
      fromMs: now - minutes * 60 * 1000,
      toMs: now,
    }
  }, [])

  // 3. Search logs function
  const loadLogs = useCallback(async () => {
    setLoading(true)
    setError(null)

    const { fromMs, toMs } = getTimeRange(selectedPreset)
    const filters: LogFilters = {
      query: isRegex ? undefined : query || undefined,
      regex: isRegex ? query || undefined : undefined,
      service: selectedService || undefined,
      severity: selectedSeverity !== 'ALL' ? selectedSeverity : undefined,
      traceId: traceIdFilter || undefined,
      fromMs,
      toMs,
      limit,
    }

    try {
      const records = await fetchLogs(filters)
      setLogs(records)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [query, isRegex, selectedService, selectedSeverity, traceIdFilter, selectedPreset, limit, getTimeRange])

  // Load logs on mount or when explicit search trigger changes
  useEffect(() => {
    if (!isLiveTail) {
      loadLogs()
    }
  }, [loadLogs, isLiveTail])

  // 4. Live tail subscription
  useEffect(() => {
    if (!isLiveTail) {
      setStreamStatus('idle')
      return
    }

    setStreamStatus('connecting')
    const cleanup = subscribeLogsStream(
      (newRecord: LogRecord) => {
        if (isStreamPausedRef.current) return

        // Client-side filter match for incoming stream records
        if (selectedService && newRecord.service !== selectedService) return
        if (selectedSeverity !== 'ALL' && newRecord.severity?.toUpperCase() !== selectedSeverity) return
        if (traceIdFilter && newRecord.trace_id?.toLowerCase() !== traceIdFilter.toLowerCase()) return
        if (query.trim()) {
          if (isRegex) {
            try {
              const re = new RegExp(query, 'i')
              if (!re.test(newRecord.body || '')) return
            } catch {
              return
            }
          } else {
            if (!newRecord.body?.toLowerCase().includes(query.toLowerCase())) return
          }
        }

        setLogs((prev) => [newRecord, ...prev].slice(0, MAX_LIVE_RECORDS))
      },
      () => {
        setStreamStatus('error')
      },
      () => {
        setStreamStatus('connected')
      },
    )

    return () => {
      cleanup()
    }
  }, [isLiveTail, selectedService, selectedSeverity, traceIdFilter, query, isRegex])

  // Reset all filters to default
  const handleReset = () => {
    setQuery('')
    setIsRegex(false)
    setSelectedService('')
    setSelectedSeverity('ALL')
    setTraceIdFilter('')
    setSelectedPreset(60)
    setLimit(100)
    setSearchParams({})
  }

  // Row expand toggle
  const toggleRow = (id: string) => {
    setExpandedRows((prev) => ({ ...prev, [id]: !prev[id] }))
  }

  const getSeverityBadgeClass = (severity: string | null) => {
    const sev = (severity || '').toUpperCase()
    if (sev === 'ERROR' || sev === 'FATAL') return 'badge-error'
    if (sev === 'WARN' || sev === 'WARNING') return 'badge-warn'
    if (sev === 'INFO') return 'badge-info'
    return 'badge-debug'
  }

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>Logs Explorer</h1>
          <p className="muted">Search stored logs and monitor live log streams across services.</p>
        </div>

        {/* Live Tail Toggle */}
        <div className="live-tail-control">
          <div className="live-tail-status">
            <span className={`live-dot live-dot-${streamStatus}`} />
            <span className="live-tail-label">
              {isLiveTail ? (isStreamPaused ? 'Tail Paused' : 'Tail Streaming') : 'Live Tail Off'}
            </span>
          </div>

          <button
            type="button"
            className={`btn-toggle ${isLiveTail ? 'btn-toggle-active' : ''}`}
            onClick={() => {
              setIsLiveTail((prev) => !prev)
              setIsStreamPaused(false)
            }}
          >
            {isLiveTail ? 'Stop Tail' : 'Start Live Tail'}
          </button>

          {isLiveTail && (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setIsStreamPaused((prev) => !prev)}
            >
              {isStreamPaused ? 'Resume' : 'Pause'}
            </button>
          )}
        </div>
      </div>

      {/* Filter Toolbar */}
      <form
        className="filters log-filters"
        onSubmit={(e) => {
          e.preventDefault()
          loadLogs()
        }}
      >
        {/* Search input with regex toggle */}
        <div className="filter-group search-input-group">
          <label htmlFor="log-query">Search logs</label>
          <div className="search-box-wrapper">
            <input
              id="log-query"
              type="text"
              placeholder={isRegex ? 'Enter regular expression...' : 'Search message text...'}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <label className="checkbox inline-checkbox">
              <input
                type="checkbox"
                checked={isRegex}
                onChange={(e) => setIsRegex(e.target.checked)}
              />
              Regex
            </label>
          </div>
        </div>

        {/* Service filter */}
        <div className="filter-group">
          <label htmlFor="log-service">Service</label>
          <select
            id="log-service"
            value={selectedService}
            onChange={(e) => setSelectedService(e.target.value)}
          >
            <option value="">All Services</option>
            {services.map((svc) => (
              <option key={svc} value={svc}>
                {svc}
              </option>
            ))}
          </select>
        </div>

        {/* Severity filter */}
        <div className="filter-group">
          <label htmlFor="log-severity">Severity</label>
          <select
            id="log-severity"
            value={selectedSeverity}
            onChange={(e) => setSelectedSeverity(e.target.value)}
          >
            {SEVERITIES.map((sev) => (
              <option key={sev} value={sev}>
                {sev === 'ALL' ? 'All Severities' : sev}
              </option>
            ))}
          </select>
        </div>

        {/* Trace ID filter */}
        <div className="filter-group">
          <label htmlFor="log-trace-id">Trace ID</label>
          <input
            id="log-trace-id"
            type="text"
            placeholder="Filter by Trace ID..."
            value={traceIdFilter}
            onChange={(e) => setTraceIdFilter(e.target.value)}
          />
        </div>

        {/* Time range preset */}
        <div className="filter-group">
          <label htmlFor="log-time-range">Time Range</label>
          <select
            id="log-time-range"
            value={selectedPreset !== null ? String(selectedPreset) : 'all'}
            onChange={(e) => {
              const val = e.target.value
              setSelectedPreset(val === 'all' ? null : Number(val))
            }}
            disabled={isLiveTail}
          >
            {TIME_PRESETS.map((p) => (
              <option key={p.label} value={p.minutes !== null ? String(p.minutes) : 'all'}>
                {p.label}
              </option>
            ))}
          </select>
        </div>

        {/* Limit */}
        <div className="filter-group">
          <label htmlFor="log-limit">Limit</label>
          <select
            id="log-limit"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            disabled={isLiveTail}
          >
            {LIMIT_OPTIONS.map((lim) => (
              <option key={lim} value={lim}>
                {lim}
              </option>
            ))}
          </select>
        </div>

        {/* Action buttons */}
        <div className="filter-actions">
          <button type="submit" className="btn-primary" disabled={loading || isLiveTail}>
            {loading ? 'Searching...' : 'Search'}
          </button>
          <button type="button" className="btn-secondary" onClick={handleReset}>
            Reset
          </button>
        </div>
      </form>

      {/* Error state */}
      {error && (
        <div className="error-box log-error-box">
          <strong>Query Error:</strong> {error}
        </div>
      )}

      {/* Results summary */}
      <div className="results-header">
        <span className="results-count">
          {loading ? 'Loading records...' : `${logs.length} ${logs.length === 1 ? 'record' : 'records'}`}
          {isLiveTail && ' (live streaming)'}
        </span>
      </div>

      {/* Log Table */}
      <div className="table-scroll">
        <table className="trace-table log-table">
          <thead>
            <tr>
              <th style={{ width: '32px' }} />
              <th style={{ width: '180px' }}>Timestamp</th>
              <th style={{ width: '130px' }}>Service</th>
              <th style={{ width: '100px' }}>Severity</th>
              <th>Message</th>
              <th style={{ width: '150px' }}>Trace ID</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((record, idx) => {
              const rowKey = `${record.timestamp_ms}-${record.span_id || idx}-${idx}`
              const isExpanded = !!expandedRows[rowKey]
              const hasAttributes = record.attributes && Object.keys(record.attributes).length > 0

              return (
                <tr key={rowKey} className={isExpanded ? 'row-expanded' : ''}>
                  <td style={{ textAlign: 'center' }}>
                    <button
                      type="button"
                      className="btn-expand"
                      title={isExpanded ? 'Collapse details' : 'Expand details'}
                      onClick={() => toggleRow(rowKey)}
                    >
                      {isExpanded ? '▼' : '▶'}
                    </button>
                  </td>
                  <td className="mono" style={{ fontSize: '12px' }}>
                    {formatDateTime(record.timestamp_ms)}
                  </td>
                  <td>
                    <span className="badge badge-service">{record.service}</span>
                  </td>
                  <td>
                    <span className={`badge ${getSeverityBadgeClass(record.severity)}`}>
                      {record.severity || 'UNKNOWN'}
                    </span>
                  </td>
                  <td className="log-body-cell">
                    <div className="log-body-text">{record.body || '<empty body>'}</div>
                    {isExpanded && (
                      <div className="log-detail-panel">
                        <div className="log-detail-field">
                          <strong>Span ID:</strong>{' '}
                          <span className="mono">{record.span_id || 'None'}</span>
                        </div>
                        {hasAttributes ? (
                          <div className="log-detail-field">
                            <strong>Attributes:</strong>
                            <pre className="attributes-json">
                              {JSON.stringify(record.attributes, null, 2)}
                            </pre>
                          </div>
                        ) : (
                          <div className="log-detail-field muted">No additional attributes</div>
                        )}
                      </div>
                    )}
                  </td>
                  <td>
                    {record.trace_id ? (
                      <Link
                        to={`/traces/${encodeURIComponent(record.trace_id)}`}
                        className="trace-link mono"
                        title={`View trace ${record.trace_id}`}
                      >
                        {record.trace_id.slice(0, 8)}...
                      </Link>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Empty State */}
      {!loading && logs.length === 0 && !error && (
        <div className="empty-box">
          <p className="empty-title">No log records found</p>
          <p className="muted">
            {isLiveTail
              ? 'Waiting for new log events from the live tail stream...'
              : 'No logs match your current query and filters. Try widening the time range or resetting filters.'}
          </p>
          {!isLiveTail && (
            <button type="button" className="btn-secondary" onClick={handleReset}>
              Reset Filters
            </button>
          )}
        </div>
      )}
    </section>
  )
}
