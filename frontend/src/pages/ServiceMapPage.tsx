import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { fetchServiceMap } from '../api/client'
import type { ServiceEdge, ServiceMapResponse, ServiceNode } from '../api/types'
import { formatDuration, formatPercentage, isHighError } from '../lib/format'

const TIME_PRESETS = [
  { label: 'Last 15 minutes', minutes: 15 },
  { label: 'Last 1 hour (Default)', minutes: 60 },
  { label: 'Last 6 hours', minutes: 360 },
  { label: 'Last 24 hours', minutes: 1440 },
]

// ---------------------------------------------------------------------------
// Custom Service Node Component
// ---------------------------------------------------------------------------

interface ServiceNodeData extends Record<string, unknown> {
  service: string
  inboundCount: number
  outboundCount: number
  hasError: boolean
}

function ServiceNodeComponent({ data, selected }: NodeProps<Node<ServiceNodeData>>) {
  return (
    <div className={`service-map-node ${selected ? 'node-selected' : ''} ${data.hasError ? 'node-has-error' : ''}`}>
      <Handle type="target" position={Position.Left} className="flow-handle" />
      <div className="node-header">
        <span className="node-icon">⚙</span>
        <span className="node-title">{data.service}</span>
      </div>
      <div className="node-meta">
        <span className="node-stat">{data.inboundCount} in</span>
        <span className="node-stat-sep">•</span>
        <span className="node-stat">{data.outboundCount} out</span>
      </div>
      <Handle type="source" position={Position.Right} className="flow-handle" />
    </div>
  )
}

const nodeTypes = {
  serviceNode: ServiceNodeComponent,
}

// ---------------------------------------------------------------------------
// Graph Layout Helper: Deterministic Layered Positioning
// ---------------------------------------------------------------------------

export function computeGraphLayout(
  nodes: ServiceNode[],
  edges: ServiceEdge[],
): { nodes: Node<ServiceNodeData>[]; edges: Edge[] } {
  // Extract clean service names
  const serviceNames = Array.from(
    new Set([
      ...nodes.map((n) => n.service || n.name || n.id || 'unknown'),
      ...edges.map((e) => e.source),
      ...edges.map((e) => e.target),
    ]),
  ).sort()

  // Calculate in-degree, out-degree, and adjacency
  const inDegree: Record<string, number> = {}
  const outDegree: Record<string, number> = {}
  const adj: Record<string, string[]> = {}

  for (const s of serviceNames) {
    inDegree[s] = 0
    outDegree[s] = 0
    adj[s] = []
  }

  for (const e of edges) {
    if (adj[e.source]) adj[e.source].push(e.target)
    outDegree[e.source] = (outDegree[e.source] || 0) + 1
    inDegree[e.target] = (inDegree[e.target] || 0) + 1
  }

  // Calculate depth level using BFS from root nodes (inDegree === 0)
  const depth: Record<string, number> = {}
  const roots = serviceNames.filter((s) => inDegree[s] === 0)
  const queue = roots.length > 0 ? [...roots] : [...serviceNames]

  for (const r of queue) {
    depth[r] = 0
  }

  while (queue.length > 0) {
    const curr = queue.shift()!
    const currDepth = depth[curr] || 0
    for (const neighbor of adj[curr] || []) {
      if (depth[neighbor] === undefined || depth[neighbor] < currDepth + 1) {
        depth[neighbor] = currDepth + 1
        queue.push(neighbor)
      }
    }
  }

  // Group services by depth level
  const levels: Record<number, string[]> = {}
  for (const s of serviceNames) {
    const d = depth[s] !== undefined ? depth[s] : 0
    if (!levels[d]) levels[d] = []
    levels[d].push(s)
  }

  // Assign deterministic positions
  const X_SPACING = 280
  const Y_SPACING = 120
  const flowNodes: Node<ServiceNodeData>[] = []

  const sortedLevels = Object.keys(levels)
    .map(Number)
    .sort((a, b) => a - b)

  for (const lvl of sortedLevels) {
    const svcs = levels[lvl].sort()
    svcs.forEach((svc, idx) => {
      // Check if any outgoing or incoming edge has an error rate > 5%
      const hasError = edges.some(
        (e) => (e.source === svc || e.target === svc) && isHighError(e.error_rate),
      )

      flowNodes.push({
        id: svc,
        type: 'serviceNode',
        data: {
          service: svc,
          inboundCount: inDegree[svc] || 0,
          outboundCount: outDegree[svc] || 0,
          hasError,
        },
        position: {
          x: 60 + lvl * X_SPACING,
          y: 80 + idx * Y_SPACING,
        },
      })
    })
  }

  // Create flow edges with labels and high-error highlighting
  const flowEdges: Edge[] = edges.map((e, idx) => {
    const highErr = isHighError(e.error_rate)
    const errText = highErr ? ` (${formatPercentage(e.error_rate)} err)` : ''
    const edgeColor = highErr ? '#cf222e' : '#57606a'

    return {
      id: `e-${e.source}-${e.target}-${idx}`,
      source: e.source,
      target: e.target,
      type: 'default',
      label: `${e.call_count} calls • ${formatDuration(e.avg_latency_ms)}${errText}`,
      animated: highErr,
      style: {
        stroke: edgeColor,
        strokeWidth: highErr ? 2.5 : 1.5,
      },
      labelStyle: {
        fill: highErr ? '#cf222e' : '#24292f',
        fontSize: 11,
        fontWeight: highErr ? 600 : 500,
      },
      labelBgStyle: {
        fill: highErr ? '#ffebe9' : '#ffffff',
        fillOpacity: 0.95,
        stroke: highErr ? '#ff8182' : '#d0d7de',
        strokeWidth: 1,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: edgeColor,
        width: 16,
        height: 16,
      },
      data: {
        ...e,
      },
    }
  })

  return { nodes: flowNodes, edges: flowEdges }
}

// ---------------------------------------------------------------------------
// Main ServiceMapPage Component
// ---------------------------------------------------------------------------

export default function ServiceMapPage() {
  const [selectedMinutes, setSelectedMinutes] = useState<number>(60)
  const [data, setData] = useState<ServiceMapResponse>({ nodes: [], edges: [] })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Selected item for inspector panel
  const [selectedService, setSelectedService] = useState<string | null>(null)
  const [selectedEdgeData, setSelectedEdgeData] = useState<ServiceEdge | null>(null)

  const loadServiceMap = useCallback(async () => {
    setLoading(true)
    setError(null)
    const now = Date.now()
    const fromMs = now - selectedMinutes * 60 * 1000
    const toMs = now

    try {
      const resp = await fetchServiceMap(fromMs, toMs)
      setData(resp)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [selectedMinutes])

  useEffect(() => {
    loadServiceMap()
  }, [loadServiceMap])

  // Compute ReactFlow graph elements
  const { nodes, edges } = useMemo(() => {
    return computeGraphLayout(data.nodes || [], data.edges || [])
  }, [data])

  // Handle graph clicks
  const onNodeClick = (_: React.MouseEvent, node: Node) => {
    setSelectedService(node.id)
    setSelectedEdgeData(null)
  }

  const onEdgeClick = (_: React.MouseEvent, edge: Edge) => {
    const rawEdge = (data.edges || []).find(
      (e) => e.source === edge.source && e.target === edge.target,
    )
    if (rawEdge) {
      setSelectedEdgeData(rawEdge)
      setSelectedService(null)
    }
  }

  const onPaneClick = () => {
    setSelectedService(null)
    setSelectedEdgeData(null)
  }

  // Summary statistics for selected service
  const serviceStats = useMemo(() => {
    if (!selectedService) return null
    const inbound = (data.edges || []).filter((e) => e.target === selectedService)
    const outbound = (data.edges || []).filter((e) => e.source === selectedService)
    const totalCalls = [...inbound, ...outbound].reduce((sum, e) => sum + e.call_count, 0)
    const totalErrors = [...inbound, ...outbound].reduce((sum, e) => sum + e.error_count, 0)
    return { inbound, outbound, totalCalls, totalErrors }
  }, [selectedService, data.edges])

  return (
    <section className="page service-map-page">
      <div className="page-header">
        <div>
          <h1>Service Map</h1>
          <p className="muted">Interactive dependency graph showing service call volume, latency, and error rates.</p>
        </div>

        {/* Time window selector and refresh */}
        <div className="map-toolbar">
          <label htmlFor="map-time-range" className="muted" style={{ fontSize: '13px' }}>
            Time Window:
          </label>
          <select
            id="map-time-range"
            value={selectedMinutes}
            onChange={(e) => setSelectedMinutes(Number(e.target.value))}
            disabled={loading}
          >
            {TIME_PRESETS.map((p) => (
              <option key={p.minutes} value={p.minutes}>
                {p.label}
              </option>
            ))}
          </select>

          <button
            type="button"
            className="btn-secondary"
            onClick={loadServiceMap}
            disabled={loading}
          >
            {loading ? 'Refreshing...' : 'Refresh Map'}
          </button>
        </div>
      </div>

      {error && (
        <div className="error-box" style={{ marginBottom: '16px' }}>
          <strong>Service Map Error:</strong> {error}
        </div>
      )}

      {/* Main Canvas & Details Inspector Container */}
      <div className="service-map-layout">
        <div className="flow-container">
          {nodes.length > 0 ? (
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodeClick={onNodeClick}
              onEdgeClick={onEdgeClick}
              onPaneClick={onPaneClick}
              fitView
              minZoom={0.2}
              maxZoom={2.0}
            >
              <Background gap={16} size={1} color="#e1e4e8" />
              <Controls showInteractive={false} />
            </ReactFlow>
          ) : (
            !loading && (
              <div className="empty-box" style={{ margin: 'auto' }}>
                <p className="empty-title">No Service Dependencies Found</p>
                <p className="muted">
                  No trace spans were found in the selected time window ({selectedMinutes}m). Send traffic
                  through the demo services to populate the graph.
                </p>
              </div>
            )
          )}
        </div>

        {/* Selected Service / Edge Details Panel */}
        {(selectedService || selectedEdgeData) && (
          <aside className="service-inspector">
            <div className="inspector-header">
              <h3>{selectedService ? `Service: ${selectedService}` : 'Dependency Edge'}</h3>
              <button
                type="button"
                className="btn-close"
                onClick={() => {
                  setSelectedService(null)
                  setSelectedEdgeData(null)
                }}
              >
                ✕
              </button>
            </div>

            {/* Service Inspector */}
            {selectedService && serviceStats && (
              <div className="inspector-content">
                <div className="inspector-card">
                  <div className="inspector-metric">
                    <span className="muted">Total Calls</span>
                    <strong>{serviceStats.totalCalls}</strong>
                  </div>
                  <div className="inspector-metric">
                    <span className="muted">Errors</span>
                    <strong className={serviceStats.totalErrors > 0 ? 'error-text' : ''}>
                      {serviceStats.totalErrors}
                    </strong>
                  </div>
                </div>

                {serviceStats.inbound.length > 0 && (
                  <div className="inspector-section">
                    <h4>Inbound Calls (Callers)</h4>
                    <ul className="dependency-list">
                      {serviceStats.inbound.map((e) => (
                        <li key={e.source} className="dependency-item">
                          <span className="mono">{e.source}</span>
                          <span className="muted">
                            {e.call_count} calls ({formatDuration(e.avg_latency_ms)})
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {serviceStats.outbound.length > 0 && (
                  <div className="inspector-section">
                    <h4>Outbound Calls (Callees)</h4>
                    <ul className="dependency-list">
                      {serviceStats.outbound.map((e) => (
                        <li key={e.target} className="dependency-item">
                          <span className="mono">{e.target}</span>
                          <span className="muted">
                            {e.call_count} calls ({formatDuration(e.avg_latency_ms)})
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div className="inspector-links">
                  <Link
                    to={`/traces?service=${encodeURIComponent(selectedService)}`}
                    className="btn-primary-link"
                  >
                    View Traces for {selectedService}
                  </Link>
                  <Link
                    to={`/logs?service=${encodeURIComponent(selectedService)}`}
                    className="btn-secondary-link"
                  >
                    View Logs for {selectedService}
                  </Link>
                </div>
              </div>
            )}

            {/* Edge Inspector */}
            {selectedEdgeData && (
              <div className="inspector-content">
                <div className="edge-title-box">
                  <span className="mono bold">{selectedEdgeData.source}</span>
                  <span className="edge-arrow"> ➔ </span>
                  <span className="mono bold">{selectedEdgeData.target}</span>
                </div>

                <div className="inspector-card">
                  <div className="inspector-metric">
                    <span className="muted">Total Calls</span>
                    <strong>{selectedEdgeData.call_count}</strong>
                  </div>
                  <div className="inspector-metric">
                    <span className="muted">Average Latency</span>
                    <strong>{formatDuration(selectedEdgeData.avg_latency_ms)}</strong>
                  </div>
                  <div className="inspector-metric">
                    <span className="muted">Error Count</span>
                    <strong className={selectedEdgeData.error_count > 0 ? 'error-text' : ''}>
                      {selectedEdgeData.error_count}
                    </strong>
                  </div>
                  <div className="inspector-metric">
                    <span className="muted">Error Rate</span>
                    <strong className={isHighError(selectedEdgeData.error_rate) ? 'error-text' : ''}>
                      {formatPercentage(selectedEdgeData.error_rate)}
                    </strong>
                  </div>
                </div>

                <div className="inspector-status-badge">
                  {isHighError(selectedEdgeData.error_rate) ? (
                    <span className="badge badge-error">High Error Rate (&gt; 5%)</span>
                  ) : (
                    <span className="badge badge-ok">Normal Operation</span>
                  )}
                </div>

                <div className="inspector-links">
                  <Link
                    to={`/traces?service=${encodeURIComponent(selectedEdgeData.source)}`}
                    className="btn-primary-link"
                  >
                    View Caller Traces ({selectedEdgeData.source})
                  </Link>
                  <Link
                    to={`/logs?service=${encodeURIComponent(selectedEdgeData.target)}`}
                    className="btn-secondary-link"
                  >
                    View Callee Logs ({selectedEdgeData.target})
                  </Link>
                </div>
              </div>
            )}
          </aside>
        )}
      </div>
    </section>
  )
}
