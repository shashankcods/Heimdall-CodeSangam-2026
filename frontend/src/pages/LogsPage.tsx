import { Fragment, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { fetchLogs, fetchServices } from '../api/client'
import { SEVERITIES } from '../api/types'
import type { LogEntry, LogFilters, Severity } from '../api/types'
import { formatTimeMs } from '../lib/format'
import { useDebouncedValue } from '../lib/useDebouncedValue'
import { usePageTitle } from '../lib/usePageTitle'
import { EmptyState, ErrorState, LoadingState } from '../components/StatusMessage'

const LIMIT = 100

// "env:prod  retry:0" -> ["env:prod", "retry:0"]
function parseTags(text: string): string[] {
  return text.split(/\s+/).filter(Boolean)
}

function logKey(log: LogEntry, index: number): string {
  return `${log.timestamp_ms}-${log.span_id ?? ''}-${index}`
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
      </div>

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
              {logs.map((log, index) => {
                const key = logKey(log, index)
                const expanded = key === expandedKey
                return (
                  // Fragment groups the row and its optional detail row under one key.
                  <Fragment key={key}>
                    <tr onClick={() => setExpandedKey(expanded ? null : key)}>
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

      {logs.length === LIMIT && (
        <p className="muted">Showing the newest {LIMIT}. Narrow the filters to see older ones.</p>
      )}
    </section>
  )
}
