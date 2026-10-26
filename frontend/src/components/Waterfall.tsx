import type { CSSProperties } from 'react'
import type { Span, TraceDetail } from '../api/types'
import type { SpanRow } from '../lib/spanTree'
import { formatDuration } from '../lib/format'

const INDENT_PX = 16
const TICKS = [0, 0.25, 0.5, 0.75, 1]

// Place a span's bar on the trace's timeline, in percent of the total duration.
function barStyle(span: Span, trace: TraceDetail): CSSProperties {
  if (trace.duration_ms <= 0) return { left: '0%', width: '100%' }
  const left = ((span.start_ms - trace.start_ms) / trace.duration_ms) * 100
  const width = (span.duration_ms / trace.duration_ms) * 100
  // max(..., 2px) keeps very short spans visible.
  return { left: `${left}%`, width: `max(${width}%, 2px)` }
}

interface WaterfallProps {
  trace: TraceDetail
  rows: SpanRow[]
  collapsed: Set<string>
  selectedSpanId: string | null
  onToggle: (spanId: string) => void
  onSelect: (spanId: string) => void
}

export default function Waterfall({
  trace,
  rows,
  collapsed,
  selectedSpanId,
  onToggle,
  onSelect,
}: WaterfallProps) {
  return (
    <div className="table-scroll">
      <div className="waterfall">
        <div className="wf-row wf-header">
          <div>Span</div>
          <div className="num">Duration</div>
          <div className="wf-timeline">
            {TICKS.map((t) => (
              <span key={t} className="wf-tick" style={{ left: `${t * 100}%` }}>
                {formatDuration(trace.duration_ms * t)}
              </span>
            ))}
          </div>
        </div>

        {rows.map(({ span, depth, hasChildren }) => {
          const isError = span.status_code === 'ERROR'
          const isSelected = span.span_id === selectedSpanId
          return (
            <div
              key={span.span_id}
              className={`wf-row wf-span${isSelected ? ' selected' : ''}`}
              onClick={() => onSelect(span.span_id)}
            >
              <div className="wf-name" style={{ paddingLeft: depth * INDENT_PX }}>
                {hasChildren ? (
                  <button
                    className="wf-toggle"
                    aria-label={collapsed.has(span.span_id) ? 'Expand' : 'Collapse'}
                    onClick={(e) => {
                      // Don't also select the row when only toggling.
                      e.stopPropagation()
                      onToggle(span.span_id)
                    }}
                  >
                    {collapsed.has(span.span_id) ? '▸' : '▾'}
                  </button>
                ) : (
                  <span className="wf-toggle-spacer" />
                )}
                <span className={isError ? 'wf-label error-text' : 'wf-label'}>{span.name}</span>
                {span.http_status !== null && (
                  <span className={span.http_status >= 500 ? 'badge badge-error' : 'badge badge-ok'}>
                    {span.http_status}
                  </span>
                )}
                <span className="wf-service">{span.service}</span>
              </div>
              <div className="num">{formatDuration(span.duration_ms)}</div>
              <div className="wf-timeline">
                <div
                  className={isError ? 'wf-bar wf-bar-error' : 'wf-bar'}
                  style={barStyle(span, trace)}
                />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
