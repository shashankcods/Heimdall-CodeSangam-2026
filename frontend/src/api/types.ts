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

// Log records from /api/logs and SSE /api/logs/stream
export interface LogRecord {
  timestamp_ms: number
  timestamp: number
  service: string
  severity: string | null
  body: string | null
  trace_id: string | null
  span_id: string | null
  attributes: Record<string, unknown>
}

export interface LogFilters {
  query?: string | null
  regex?: string | null
  service?: string | null
  severity?: string | null
  traceId?: string | null
  fromMs?: number | null
  toMs?: number | null
  limit?: number
}

// Service topology graph shapes from /api/service-map
export interface ServiceNode {
  id?: string
  service?: string
  name?: string
}

export interface ServiceEdge {
  source: string
  target: string
  call_count: number
  avg_latency_ms: number
  error_count: number
  error_rate: number // fraction in [0.0, 1.0]
}

export interface ServiceMapResponse {
  nodes: ServiceNode[]
  edges: ServiceEdge[]
}
