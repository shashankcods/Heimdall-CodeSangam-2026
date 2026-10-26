import type { TraceFilters, TraceSummary } from './types'

// GET a relative /api path and return the parsed JSON, or throw on a non-2xx status.
async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    throw new Error(`${path} failed: ${res.status} ${res.statusText}`)
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

export async function fetchServices(): Promise<string[]> {
  const data = await getJson<{ services: string[] }>('/api/services')
  return data.services
}
