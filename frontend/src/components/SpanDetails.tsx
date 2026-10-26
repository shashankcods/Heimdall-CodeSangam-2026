import type { Span } from '../api/types'
import { formatDuration } from '../lib/format'

// Everything we know about one span: the main fields, then all raw attributes.
export default function SpanDetails({ span }: { span: Span }) {
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
    <div className="span-details">
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
    </div>
  )
}
