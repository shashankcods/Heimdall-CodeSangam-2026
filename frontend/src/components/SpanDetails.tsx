import { Link } from 'react-router'
import type { LogEntry, Span } from '../api/types'
import { formatDuration, formatTimeMs } from '../lib/format'

interface SpanDetailsProps {
  span: Span
  // Logs written while this span ran; null while they're still loading.
  logs: LogEntry[] | null
  traceId: string
}

// Everything we know about one span: the main fields, its attributes, and its logs.
export default function SpanDetails({ span, logs, traceId }: SpanDetailsProps) {
  const fields: [string, string][] = [
    ['Service', span.service],
    ['Kind', span.kind ?? '—'],
    ['Duration', formatDuration(span.duration_ms)],
    ['Status', span.status_code ?? '—'],
    ['Status message', span.status_msg ?? '—'],
    ['HTTP status', span.http_status?.toString() ?? '—'],
    ['Route', span.route ?? '—'],
    ['Span ID', span.span_id],
    ['Parent span ID', span.parent_span_id ?? '— (root)'],
  ]
  const attributes = Object.entries(span.attributes).sort(([a], [b]) => a.localeCompare(b))

  return (
    <aside className="span-details" aria-label="Span details">
      <h2>{span.name}</h2>
      <dl>
        {fields.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd className={label.includes('ID') ? 'mono' : undefined}>{value}</dd>
          </div>
        ))}
      </dl>

      <h3>Attributes</h3>
      {attributes.length === 0 ? (
        <p className="muted">No attributes.</p>
      ) : (
        <dl>
          {attributes.map(([key, value]) => (
            <div key={key}>
              <dt className="mono">{key}</dt>
              <dd className="mono">{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}

      <div className="span-logs-head">
        <h3>Logs from this span</h3>
        <Link to={`/logs?trace_id=${traceId}`}>Open in Logs</Link>
      </div>
      {logs === null ? (
        <p className="muted">Loading logs…</p>
      ) : logs.length === 0 ? (
        <p className="muted">This span wrote no logs.</p>
      ) : (
        <ul className="span-logs">
          {logs.map((log, i) => (
            <li key={`${log.timestamp_ms}-${i}`} className={log.severity === 'ERROR' || log.severity === 'FATAL' ? 'err' : undefined}>
              <span className="muted">{formatTimeMs(log.timestamp_ms)}</span>
              <span className={`sev sev-${(log.severity ?? 'none').toLowerCase()}`}>{log.severity ?? '—'}</span>
              <span>{log.body ?? '—'}</span>
            </li>
          ))}
        </ul>
      )}
    </aside>
  )
}
