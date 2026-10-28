import type { LogEntry } from '../api/types'
import { formatTimeMs } from '../lib/format'

const BUCKETS = 40

type Level = 'info' | 'warn' | 'error'

function level(log: LogEntry): Level {
  if (log.severity === 'ERROR' || log.severity === 'FATAL') return 'error'
  if (log.severity === 'WARN') return 'warn'
  return 'info'
}

// Stacked bars of how many of the shown logs landed in each slice of time,
// split by severity, so a burst of errors stands out at a glance.
export default function LogVolume({ logs }: { logs: LogEntry[] }) {
  if (logs.length < 2) return null

  const times = logs.map((l) => l.timestamp_ms)
  const start = Math.min(...times)
  const end = Math.max(Math.max(...times), start + 1)
  const width = (end - start) / BUCKETS

  const buckets = Array.from({ length: BUCKETS }, () => ({ info: 0, warn: 0, error: 0 }))
  for (const log of logs) {
    const i = Math.min(BUCKETS - 1, Math.floor((log.timestamp_ms - start) / width))
    buckets[i][level(log)] += 1
  }
  const tallest = Math.max(1, ...buckets.map((b) => b.info + b.warn + b.error))
  const pct = (n: number) => `${(n / tallest) * 100}%`

  return (
    <section className="card log-volume" aria-label="Log volume">
      <div className="card-head">
        <h2>Log volume</h2>
        <div className="legend">
          <span><span className="legend-box info" />Info</span>
          <span><span className="legend-box warn" />Warn</span>
          <span><span className="legend-box error" />Error</span>
          <span className="muted">of the {logs.length} logs shown</span>
        </div>
      </div>
      <div className="volume-bars">
        {buckets.map((b, i) => (
          <span key={i} className="volume-bar" title={`${b.info} info, ${b.warn} warn, ${b.error} error`}>
            <span className="error" style={{ height: pct(b.error) }} />
            <span className="warn" style={{ height: pct(b.warn) }} />
            <span className="info" style={{ height: pct(b.info) }} />
          </span>
        ))}
      </div>
      <div className="volume-axis" aria-hidden="true">
        <span>{formatTimeMs(start)}</span>
        <span>{formatTimeMs(end)}</span>
      </div>
    </section>
  )
}
