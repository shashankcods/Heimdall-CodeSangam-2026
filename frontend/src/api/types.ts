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
  // How far back to look, in ms. null = the backend default (last hour).
  lookbackMs: number | null
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

export const SEVERITIES = ['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'] as const
export type Severity = (typeof SEVERITIES)[number]

export interface LogEntry {
  timestamp_ms: number
  service: string
  severity: Severity | null
  body: string | null
  trace_id: string | null
  span_id: string | null
  attributes: Record<string, unknown>
}

export interface LogFilters {
  q: string
  regex: boolean
  service: string | null
  // Minimum level: WARN also returns ERROR and FATAL.
  severity: Severity | null
  traceId: string | null
  // Each one "key:value"; a log must match all of them.
  tags: string[]
}

// GET /api/service-map: services and who calls whom in the time range.
export interface ServiceNode {
  id: string
  // Only "entry" spans count: requests the service handled.
  request_count: number
  error_count: number
  error_rate: number
  // null when the service had no entry spans in the range.
  avg_duration_ms: number | null
  p95_duration_ms: number | null
}

// source calls target; the numbers describe the target's side of those calls.
export interface ServiceEdge {
  source: string
  target: string
  call_count: number
  error_count: number
  error_rate: number
  avg_duration_ms: number
  p95_duration_ms: number
}

export interface ServiceMap {
  nodes: ServiceNode[]
  edges: ServiceEdge[]
}
