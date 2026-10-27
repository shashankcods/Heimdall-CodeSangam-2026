export function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`
}

export function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString()
}

export function formatDateTime(ms: number): string {
  const d = new Date(ms)
  const time = d.toLocaleTimeString([], { hour12: false })
  const millis = String(d.getMilliseconds()).padStart(3, '0')
  return `${d.toLocaleDateString()} ${time}.${millis}`
}

export function formatPercentage(fraction: number): string {
  if (fraction <= 0) return '0.0%'
  return `${(fraction * 100).toFixed(1)}%`
}

export function isHighError(errorRate: number, threshold = 0.05): boolean {
  return errorRate > threshold
}
