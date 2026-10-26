export function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`
}

export function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString()
}
