import type { TraceSummary } from '../api/types'
import { formatDuration, formatTime } from '../lib/format'

const Y_TICKS = 4
const X_TICKS = 5

// Round up to a "nice" axis maximum: 1, 2, 2.5 or 5 times a power of ten.
function niceMax(value: number): number {
  if (value <= 0) return 1
  const power = 10 ** Math.floor(Math.log10(value))
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * power >= value) return step * power
  }
  return 10 * power
}

// Bigger trace (more spans) = bigger dot, like Jaeger's scatter plot.
function dotSize(spanCount: number): number {
  return Math.min(22, 8 + Math.sqrt(spanCount) * 2)
}

interface TraceScatterProps {
  traces: TraceSummary[]
  onOpen: (traceId: string) => void
}

// Duration over time, one dot per trace, red when the trace has an error.
// Dots are positioned in % so the chart stretches with the page.
export default function TraceScatter({ traces, onOpen }: TraceScatterProps) {
  if (traces.length === 0) return null

  const times = traces.map((t) => t.start_ms)
  const minT = Math.min(...times)
  // One trace (or all at once) would give a zero-width axis: pad it to a second.
  const maxT = Math.max(Math.max(...times), minT + 1000)
  const maxY = niceMax(Math.max(...traces.map((t) => t.duration_ms)))

  const x = (ms: number) => ((ms - minT) / (maxT - minT)) * 100
  const y = (ms: number) => (ms / maxY) * 100

  const yTicks = Array.from({ length: Y_TICKS + 1 }, (_, i) => (maxY / Y_TICKS) * i)
  const xTicks = Array.from({ length: X_TICKS }, (_, i) => minT + ((maxT - minT) / (X_TICKS - 1)) * i)
  const errorCount = traces.filter((t) => t.has_error).length

  return (
    <section className="card scatter" aria-label="Trace duration over time">
      <div className="card-head">
        <h2>Duration over time</h2>
        <div className="legend">
          <span><span className="legend-dot ok" />OK</span>
          <span><span className="legend-dot err" />Error ({errorCount})</span>
          <span className="muted">Bigger dot = more spans</span>
        </div>
      </div>
      <div className="scatter-body">
        <div className="scatter-y" aria-hidden="true">
          {yTicks.map((v) => (
            <span key={v} style={{ bottom: `${y(v)}%` }}>{formatDuration(v)}</span>
          ))}
        </div>
        <div className="scatter-plot">
          {yTicks.slice(1).map((v) => (
            <span key={v} className="scatter-grid" style={{ bottom: `${y(v)}%` }} />
          ))}
          {/* Oldest first, so the newest dots paint on top. */}
          {[...traces].reverse().map((t) => {
            const size = dotSize(t.span_count)
            const label = `${t.root_name}, ${formatDuration(t.duration_ms)}, ${t.span_count} spans${t.has_error ? ', error' : ''}, at ${formatTime(t.start_ms)}`
            return (
              <button
                key={t.trace_id}
                className={t.has_error ? 'scatter-dot err' : 'scatter-dot'}
                style={{ left: `${x(t.start_ms)}%`, bottom: `${y(t.duration_ms)}%`, width: size, height: size }}
                aria-label={label}
                title={label}
                onClick={() => onOpen(t.trace_id)}
              />
            )
          })}
        </div>
      </div>
      <div className="scatter-x" aria-hidden="true">
        {xTicks.map((v) => (
          <span key={v}>{formatTime(v)}</span>
        ))}
      </div>
    </section>
  )
}
