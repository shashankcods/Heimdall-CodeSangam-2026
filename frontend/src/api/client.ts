import type {
  LogEntry,
  LogFilters,
  ServiceMap,
  TraceDetail,
  TraceFilters,
  TraceSummary,
} from './types'

export const BACKEND_DOWN = "Can't reach the backend. Is it running?"

// GET a relative /api path and return the parsed JSON, or throw on a non-2xx status.
// FastAPI errors look like {"detail": "..."}; that message is used when present.
async function getJson<T>(path: string): Promise<T> {
  let res: Response
  try {
    res = await fetch(path)
  } catch {
    throw new Error(BACKEND_DOWN)
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    // The Vite dev proxy answers with an empty 5xx when the backend isn't running.
    if (res.status >= 500 && text === '') throw new Error(BACKEND_DOWN)
    let detail = res.statusText
    try {
      const body = JSON.parse(text)
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      // Not JSON: keep the status text.
    }
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

// Service map for the last `rangeMs` milliseconds.
export async function fetchServiceMap(rangeMs: number): Promise<ServiceMap> {
  const params = new URLSearchParams({ from_ms: String(Date.now() - rangeMs) })
  return getJson<ServiceMap>(`/api/service-map?${params}`)
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
