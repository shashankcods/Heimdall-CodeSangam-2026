import type { LogEntry, LogFilters, TraceDetail, TraceFilters, TraceSummary } from './types'

// GET a relative /api path and return the parsed JSON, or throw on a non-2xx status.
// FastAPI errors look like {"detail": "..."}; that message is used when present.
async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const detail = typeof body?.detail === 'string' ? body.detail : res.statusText
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export async function fetchTraces(filters: TraceFilters): Promise<TraceSummary[]> {
  const params = new URLSearchParams()
  if (filters.service) params.set('service', filters.service)
  if (filters.errorsOnly) params.set('errors_only', 'true')
  if (filters.minDurationMs !== null) params.set('min_duration_ms', String(filters.minDurationMs))

  const data = await getJson<{ traces: TraceSummary[] }>(`/api/traces?${params}`)
  return data.traces
}

export async function fetchTrace(traceId: string): Promise<TraceDetail> {
  return getJson<TraceDetail>(`/api/traces/${encodeURIComponent(traceId)}`)
}

export async function fetchServices(): Promise<string[]> {
  const data = await getJson<{ services: string[] }>('/api/services')
  return data.services
}

export async function fetchLogs(filters: LogFilters): Promise<LogEntry[]> {
  const params = new URLSearchParams()
  if (filters.q) params.set('q', filters.q)
  if (filters.regex) params.set('regex', 'true')
  if (filters.service) params.set('service', filters.service)
  if (filters.severity) params.set('severity', filters.severity)
  if (filters.traceId) params.set('trace_id', filters.traceId)
  // Repeated param: ?tag=a:1&tag=b:2
  for (const tag of filters.tags) params.append('tag', tag)

  const data = await getJson<{ logs: LogEntry[] }>(`/api/logs?${params}`)
  return data.logs
}
