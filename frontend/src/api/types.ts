// Response shapes of the backend's /api endpoints (see backend/heimdall/queries/).
// Times are milliseconds since the Unix epoch.

export interface TraceSummary {
  trace_id: string
  root_service: string
  root_name: string
  start_ms: number
  duration_ms: number
  span_count: number
  service_count: number
  error_count: number
  has_error: boolean
  http_status: number | null
}

export interface TraceFilters {
  service: string | null
  errorsOnly: boolean
  minDurationMs: number | null
}

export interface Span {
  span_id: string
  parent_span_id: string | null
  service: string
  name: string
  kind: string | null
  start_ms: number
  end_ms: number
  duration_ms: number
  status_code: 'UNSET' | 'OK' | 'ERROR' | null
  status_msg: string | null
  http_status: number | null
  route: string | null
  attributes: Record<string, unknown>
}

// GET /api/traces/{id}: every span of one trace as a flat list, sorted by start.
export interface TraceDetail {
  trace_id: string
  start_ms: number
  duration_ms: number
  spans: Span[]
}
