export function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`
}

export function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString()
}

// 24-hour time with milliseconds (14:03:07.042), since many logs land in the same second.
export function formatTimeMs(ms: number): string {
  const date = new Date(ms)
  const pad = (n: number, width = 2) => String(n).padStart(width, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`
}
