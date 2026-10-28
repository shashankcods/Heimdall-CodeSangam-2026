// Response shapes of the backend's /api endpoints, GENERATED from its OpenAPI schema
// into schema.d.ts (run `npm run gen:types` with the backend running after it changes).
// This file only gives them short names; never edit schema.d.ts by hand.
// Times are milliseconds since the Unix epoch.
import type { components } from './schema'

type Schemas = components['schemas']

export type TraceSummary = Schemas['TraceSummary']
export type TraceList = Schemas['TraceList']
// One span of GET /api/traces/{id}; the backend calls it SpanDetail.
export type Span = Schemas['SpanDetail']
// GET /api/traces/{id}: every span of one trace as a flat list, sorted by start.
export type TraceDetail = Schemas['TraceDetail']
export type LogEntry = Schemas['LogEntry']
export type LogList = Schemas['LogList']
export type ServiceList = Schemas['ServiceList']
// GET /api/service-map: services, and who calls whom (an edge's numbers describe
// the target's side of those calls). A node only counts "entry" spans.
export type ServiceNode = Schemas['ServiceNode']
export type ServiceEdge = Schemas['ServiceEdge']
export type ServiceMap = Schemas['ServiceMap']

// --- Frontend-only types (not in the API) ----------------------------------

export interface TraceFilters {
  service: string | null
  errorsOnly: boolean
  minDurationMs: number | null
  // How far back to look, in ms. null = the backend default (last hour).
  lookbackMs: number | null
}

// The levels the severity filter offers. The API types severity as any string,
// since an SDK can send its own level names.
export const SEVERITIES = ['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'] as const
export type Severity = (typeof SEVERITIES)[number]

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
