import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { fetchLogs, fetchServices, streamLogs } from '../api/client'
import type { LiveStatus } from '../api/client'
import { SEVERITIES } from '../api/types'
import type { LogEntry, LogFilters, Severity } from '../api/types'
import { formatTimeMs } from '../lib/format'
import { useDebouncedValue } from '../lib/useDebouncedValue'
import { usePageTitle } from '../lib/usePageTitle'
import { EmptyState, ErrorState, LoadingState } from '../components/StatusMessage'

const LIMIT = 100
// Live tail keeps adding rows; drop the oldest past this so the page stays fast.
const MAX_LIVE_ROWS = 500

const LIVE_LABELS: Record<LiveStatus, string> = {
  connecting: 'Connecting…',
  live: 'Live: new logs appear at the top',
  reconnecting: 'Connection lost, reconnecting…',
  stopped: 'Live tail stopped (backend refused or is down)',
}

// "env:prod  retry:0" -> ["env:prod", "retry:0"]
function parseTags(text: string): string[] {
  return text.split(/\s+/).filter(Boolean)
}

// Built from the log itself (not its position), so keys stay the same when
// live tail inserts new rows above. Also used to skip duplicates.
function logKey(log: LogEntry): string {
  return `${log.timestamp_ms}|${log.service}|${log.span_id ?? ''}|${log.body ?? ''}`
}

export default function LogsPage() {
  usePageTitle('Logs')
  // ?trace_id=... in the URL, so the waterfall can link straight to a trace's logs.
  const [searchParams, setSearchParams] = useSearchParams()
  const traceId = searchParams.get('trace_id')

  const [query, setQuery] = useState('')
  const [regex, setRegex] = useState(false)
  const [service, setService] = useState<string | null>(null)
  const [severity, setSeverity] = useState<Severity | null>(null)
  const [tagsText, setTagsText] = useState('')
  const [refreshCount, setRefreshCount] = useState(0)

  const [services, setServices] = useState<string[]>([])
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  const [live, setLive] = useState(false)
  const [liveStatus, setLiveStatus] = useState<LiveStatus>('connecting')
  // Logs that arrived over live tail, so only those rows get the highlight.
  // A ref, not state: changing it shouldn't cause a re-render by itself.
  const arrivedLive = useRef(new WeakSet<LogEntry>())

  const debouncedQuery = useDebouncedValue(query)
  const debouncedTags = useDebouncedValue(tagsText)

  const filters = useMemo<LogFilters>(
    () => ({
      q: debouncedQuery.trim(),
      regex,
      service,
      severity,
      traceId,
      tags: parseTags(debouncedTags),
    }),
    [debouncedQuery, regex, service, severity, traceId, debouncedTags],
  )

  const hasFilters =
    filters.q !== '' || filters.service !== null || filters.severity !== null ||
    filters.traceId !== null || filters.tags.length > 0
  const firstLoad = loading && logs.length === 0
  const refresh = () => setRefreshCount((n) => n + 1)

  useEffect(() => {
    fetchServices().then(setServices).catch(() => setServices([]))
  }, [])

  useEffect(() => {
    let ignore = false
    setLoading(true)
    setError(null)
    fetchLogs(filters)
      .then((result) => {
        if (!ignore) setLogs(result)
      })
      .catch((err: Error) => {
        if (!ignore) {
          setLogs([])
          setError(err.message)
        }
      })
      .finally(() => {
        if (!ignore) setLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [filters, refreshCount])

  // While Live is on, keep one stream open with the current filters.
  // Changing a filter closes the old stream (cleanup) and opens a new one.
  useEffect(() => {
    if (!live) return
    const close = streamLogs(
      filters,
      (log) => {
        arrivedLive.current.add(log)
        setLogs((prev) => {
          const key = logKey(log)
          if (prev.some((p) => logKey(p) === key)) return prev
          // The backend flushes logs in batches grouped by service, so they can
          // arrive slightly out of order: keep the list newest first by time.
          return [log, ...prev]
            .sort((a, b) => b.timestamp_ms - a.timestamp_ms)
            .slice(0, MAX_LIVE_ROWS)
        })
      },
      setLiveStatus,
    )
    return close
  }, [live, filters])

  return (
    <section className="page">
      <h1>Logs</h1>

      <div className="filters">
        <label className="grow">
          Search
          <input
            type="search"
            placeholder={regex ? 'regex, e.g. card (declined|failed)' : 'text in the message'}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>

        <label className="checkbox">
          <input type="checkbox" checked={regex} onChange={(e) => setRegex(e.target.checked)} />
          Regex
        </label>

        <label>
          Service
          <select value={service ?? ''} onChange={(e) => setService(e.target.value || null)}>
            <option value="">All services</option>
            {services.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label>
          Min severity
          <select
            value={severity ?? ''}
            onChange={(e) => setSeverity((e.target.value || null) as Severity | null)}
          >
            <option value="">Any</option>
            {SEVERITIES.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
        </label>

        <label>
          Tags
          <input
            type="text"
            placeholder="key:value key2:value2"
            value={tagsText}
            onChange={(e) => setTagsText(e.target.value)}
          />
        </label>

        <button onClick={refresh}>Refresh</button>
        <button
          className={live ? 'live-toggle on' : 'live-toggle'}
          aria-pressed={live}
          onClick={() => setLive(!live)}
        >
          <span className="live-dot" aria-hidden="true" />
          {live ? 'Live' : 'Go live'}
        </button>
      </div>

      {live && (
        <p className={`live-status live-${liveStatus}`} role="status">
          {LIVE_LABELS[liveStatus]}
        </p>
      )}

      {traceId && (
        <p className="active-filter">
          Showing logs for trace <Link to={`/traces/${traceId}`} className="mono">{traceId}</Link>
          <button onClick={() => setSearchParams({})} aria-label="Clear trace filter">
            ✕
          </button>
        </p>
      )}

      {error ? (
        <ErrorState
          title="Could not load logs"
          message={error}
          // A 400 is a bad search (e.g. broken regex): fixing the input helps, retrying doesn't.
          onRetry={error.startsWith('400') ? undefined : refresh}
        />
      ) : firstLoad ? (
        <LoadingState label="Loading logs…" />
      ) : logs.length === 0 ? (
        hasFilters ? (
          <EmptyState title="No logs match these filters">Try a shorter search or remove a filter.</EmptyState>
        ) : (
          <EmptyState title="No logs in the last hour">
            Send some with <code>uv run demo/sdk_smoke.py</code>, then click Refresh.
          </EmptyState>
        )
      ) : null}

      {!error && logs.length > 0 && (
        <div className={loading ? 'table-scroll is-refreshing' : 'table-scroll'}>
          <table className="trace-table log-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Level</th>
                <th>Service</th>
                <th>Message</th>
                <th>Trace</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => {
                const key = logKey(log)
                const expanded = key === expandedKey
                return (
                  // Fragment groups the row and its optional detail row under one key.
                  <Fragment key={key}>
                    <tr
                      className={arrivedLive.current.has(log) ? 'log-new' : undefined}
                      onClick={() => setExpandedKey(expanded ? null : key)}
                    >
                      <td className="mono">{formatTimeMs(log.timestamp_ms)}</td>
                      <td>
                        <span className={`badge sev-${(log.severity ?? 'none').toLowerCase()}`}>
                          {log.severity ?? '—'}
                        </span>
                      </td>
                      <td>{log.service}</td>
                      <td className="log-body">{log.body ?? '—'}</td>
                      <td className="mono">
                        {log.trace_id ? (
                          <Link
                            to={`/traces/${log.trace_id}`}
                            className="trace-link"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {log.trace_id.slice(0, 8)}
                          </Link>
                        ) : (
                          '—'
                        )}
                      </td>
                    </tr>
                    {expanded && (
                      <tr className="log-expanded">
                        <td colSpan={5}>
                          <dl className="kv-list">
                            <div>
                              <dt>Span ID</dt>
                              <dd className="mono">{log.span_id ?? '—'}</dd>
                            </div>
                            {Object.entries(log.attributes)
                              .sort(([a], [b]) => a.localeCompare(b))
                              .map(([k, v]) => (
                                <div key={k}>
                                  <dt className="mono">{k}</dt>
                                  <dd className="mono">{String(v)}</dd>
                                </div>
                              ))}
                          </dl>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {!live && logs.length === LIMIT && (
        <p className="muted">Showing the newest {LIMIT}. Narrow the filters to see older ones.</p>
      )}
    </section>
  )
}
