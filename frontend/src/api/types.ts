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
