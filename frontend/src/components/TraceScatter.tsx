import { useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import type { TraceSummary } from '../api/types'
import { formatDuration, formatTime } from '../lib/format'

const Y_TICKS = 4
const X_TICKS = 5
// A drag shorter than this (px) is a click, not a zoom.
const MIN_DRAG_PX = 6

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

// What part of the chart is visible: a time range (x) and a duration range (y), in ms.
interface Domain {
  t0: number
  t1: number
  d0: number
  d1: number
}

// A drag in progress, in px from the plot's top-left corner.
interface Drag {
  x0: number
  y0: number
  x1: number
  y1: number
}

interface TraceScatterProps {
  traces: TraceSummary[]
  onOpen: (traceId: string) => void
}

// Duration over time, one dot per trace, red when the trace has an error.
// Drag a box to zoom into it; double-click or "Reset zoom" to zoom back out.
// Dots are positioned in % so the chart stretches with the page.
export default function TraceScatter({ traces, onOpen }: TraceScatterProps) {
  // Hooks must run on every render, so they come before the early return below.
  const [zoom, setZoom] = useState<Domain | null>(null)
  const [drag, setDrag] = useState<Drag | null>(null)
  const plotRef = useRef<HTMLDivElement>(null)

  if (traces.length === 0) return null

  const times = traces.map((t) => t.start_ms)
  const minT = Math.min(...times)
  // One trace (or all at once) would give a zero-width axis: pad it to a second.
  const maxT = Math.max(Math.max(...times), minT + 1000)
  const full: Domain = { t0: minT, t1: maxT, d0: 0, d1: niceMax(Math.max(...traces.map((t) => t.duration_ms))) }
  const view = zoom ?? full

  const x = (ms: number) => ((ms - view.t0) / (view.t1 - view.t0)) * 100
  const y = (ms: number) => ((ms - view.d0) / (view.d1 - view.d0)) * 100

  // Only the traces inside the zoomed box get drawn.
  const visible = traces.filter(
    (t) => t.start_ms >= view.t0 && t.start_ms <= view.t1 && t.duration_ms >= view.d0 && t.duration_ms <= view.d1,
  )
  const yTicks = Array.from({ length: Y_TICKS + 1 }, (_, i) => view.d0 + ((view.d1 - view.d0) / Y_TICKS) * i)
  const xTicks = Array.from({ length: X_TICKS }, (_, i) => view.t0 + ((view.t1 - view.t0) / (X_TICKS - 1)) * i)
  const errorCount = visible.filter((t) => t.has_error).length

  // Pointer position inside the plot, clamped to its edges.
  const localPoint = (e: PointerEvent) => {
    const rect = plotRef.current!.getBoundingClientRect()
    return {
      x: Math.min(Math.max(e.clientX - rect.left, 0), rect.width),
      y: Math.min(Math.max(e.clientY - rect.top, 0), rect.height),
    }
  }

  const startDrag = (e: PointerEvent<HTMLDivElement>) => {
    // Dots are buttons: pressing one opens its trace instead of starting a zoom.
    if ((e.target as HTMLElement).closest('.scatter-dot') || e.button !== 0) return
    const p = localPoint(e)
    e.currentTarget.setPointerCapture(e.pointerId)
    setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y })
  }

  const moveDrag = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag) return
    const p = localPoint(e)
    setDrag({ ...drag, x1: p.x, y1: p.y })
  }

  const endDrag = () => {
    if (!drag) return
    setDrag(null)
    const rect = plotRef.current!.getBoundingClientRect()
    if (Math.abs(drag.x1 - drag.x0) < MIN_DRAG_PX && Math.abs(drag.y1 - drag.y0) < MIN_DRAG_PX) return
    // px -> ms. The y axis grows upward, so the top of the box is the longest duration.
    const toTime = (px: number) => view.t0 + (px / rect.width) * (view.t1 - view.t0)
    const toDuration = (py: number) => view.d0 + (1 - py / rect.height) * (view.d1 - view.d0)
    // A mostly sideways drag zooms time only, so a thin swipe doesn't squash durations.
    const timeOnly = Math.abs(drag.y1 - drag.y0) < MIN_DRAG_PX
    setZoom({
      t0: toTime(Math.min(drag.x0, drag.x1)),
      t1: toTime(Math.max(drag.x0, drag.x1)),
      d0: timeOnly ? view.d0 : toDuration(Math.max(drag.y0, drag.y1)),
      d1: timeOnly ? view.d1 : toDuration(Math.min(drag.y0, drag.y1)),
    })
  }

  return (
    <section className="card scatter" aria-label="Trace duration over time">
      <div className="card-head">
        <h2>Duration over time</h2>
        <div className="legend">
          <span><span className="legend-dot ok" />OK</span>
          <span><span className="legend-dot err" />Error ({errorCount})</span>
          <span className="muted">Bigger dot = more spans</span>
          {zoom ? (
            <button className="link-button" onClick={() => setZoom(null)}>
              Reset zoom
            </button>
          ) : (
            <span className="muted">Drag to zoom</span>
          )}
        </div>
      </div>
      <div className="scatter-body">
        <div className="scatter-y" aria-hidden="true">
          {yTicks.map((v) => (
            <span key={v} style={{ bottom: `${y(v)}%` }}>{formatDuration(v)}</span>
          ))}
        </div>
        <div
          ref={plotRef}
          className="scatter-plot"
          onPointerDown={startDrag}
          onPointerMove={moveDrag}
          onPointerUp={endDrag}
          onPointerCancel={() => setDrag(null)}
          onDoubleClick={() => setZoom(null)}
        >
          {yTicks.slice(1).map((v) => (
            <span key={v} className="scatter-grid" style={{ bottom: `${y(v)}%` }} />
          ))}
          {/* Oldest first, so the newest dots paint on top. */}
          {[...visible].reverse().map((t) => {
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
          {drag && (
            <span
              className="scatter-brush"
              style={{
                left: Math.min(drag.x0, drag.x1),
                top: Math.min(drag.y0, drag.y1),
                width: Math.abs(drag.x1 - drag.x0),
                height: Math.abs(drag.y1 - drag.y0),
              }}
            />
          )}
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
