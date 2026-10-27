import type {
  LogFilters,
  LogRecord,
  ServiceMapResponse,
  TraceDetail,
  TraceFilters,
  TraceSummary,
} from './types'

// GET a relative /api path and return the parsed JSON, or throw on a non-2xx status.
async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    let errorDetail = `${res.status} ${res.statusText}`
    try {
      const errJson = await res.json()
      if (errJson && errJson.detail) {
        errorDetail = typeof errJson.detail === 'string' ? errJson.detail : JSON.stringify(errJson.detail)
      }
    } catch {
      // Body is not JSON
    }
    throw new Error(errorDetail)
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

export async function fetchLogs(filters: LogFilters): Promise<LogRecord[]> {
  const params = new URLSearchParams()
  if (filters.query?.trim()) params.set('query', filters.query.trim())
  if (filters.regex?.trim()) params.set('regex', filters.regex.trim())
  if (filters.service?.trim()) params.set('service', filters.service.trim())
  if (filters.severity?.trim() && filters.severity.toUpperCase() !== 'ALL') {
    params.set('severity', filters.severity.trim())
  }
  if (filters.traceId?.trim()) params.set('trace_id', filters.traceId.trim())
  if (filters.fromMs != null) params.set('from_ms', String(filters.fromMs))
  if (filters.toMs != null) params.set('to_ms', String(filters.toMs))
  if (filters.limit != null && filters.limit > 0) params.set('limit', String(filters.limit))

  const qs = params.toString()
  const data = await getJson<{ logs: LogRecord[] }>(qs ? `/api/logs?${qs}` : '/api/logs')
  return data.logs
}

export async function fetchServiceMap(fromMs?: number | null, toMs?: number | null): Promise<ServiceMapResponse> {
  const params = new URLSearchParams()
  if (fromMs != null) params.set('from_ms', String(fromMs))
  if (toMs != null) params.set('to_ms', String(toMs))

  const qs = params.toString()
  return getJson<ServiceMapResponse>(qs ? `/api/service-map?${qs}` : '/api/service-map')
}

export function subscribeLogsStream(
  onLog: (record: LogRecord) => void,
  onError?: (err: Event) => void,
  onOpen?: () => void,
): () => void {
  const es = new EventSource('/api/logs/stream')

  es.addEventListener('log', (event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data) as LogRecord
      onLog(data)
    } catch (e) {
      console.error('Failed to parse SSE log payload:', e)
    }
  })

  if (onOpen) {
    es.onopen = onOpen
  }

  if (onError) {
    es.onerror = onError
  }

  return () => {
    es.close()
  }
}
