import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router'
import { Background, Controls, MarkerType, ReactFlow } from '@xyflow/react'
import type { Edge } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { fetchServiceMap } from '../api/client'
import type { ServiceMap } from '../api/types'
import { formatDuration, formatPercent } from '../lib/format'
import { ERROR_RATE_THRESHOLD, layoutServiceMap } from '../lib/serviceMapLayout'
import { usePageTitle } from '../lib/usePageTitle'
import { EmptyState, ErrorState, LoadingState } from '../components/StatusMessage'
import ServiceNodeCard from '../components/ServiceNodeCard'
import type { ServiceFlowNode } from '../components/ServiceNodeCard'
import SidebarPanel from '../components/SidebarPanel'
import type { Theme } from '../lib/theme'

const MINUTE = 60 * 1000
const RANGES = [
  // Short window: a new failure dominates the numbers within seconds (good for demos).
  { label: 'Last minute', ms: MINUTE },
  { label: 'Last 5 minutes', ms: 5 * MINUTE },
  { label: 'Last 15 minutes', ms: 15 * MINUTE },
  { label: 'Last hour', ms: 60 * MINUTE },
  { label: 'Last 6 hours', ms: 6 * 60 * MINUTE },
]
const AUTO_REFRESH_MS = 5000

// Defined outside the component so React Flow gets the same object every render.
const NODE_TYPES = { service: ServiceNodeCard }

export default function ServiceMapPage() {
  usePageTitle('Service Map')
  const navigate = useNavigate()
  // Layout passes the theme down so React Flow can draw its own parts in dark mode too.
  const { theme } = useOutletContext<{ theme: Theme }>()
  const [rangeMs, setRangeMs] = useState(5 * MINUTE)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [map, setMap] = useState<ServiceMap | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshCount, setRefreshCount] = useState(0)
  const refresh = () => setRefreshCount((n) => n + 1)

  useEffect(() => {
    let ignore = false
    setError(null)
    fetchServiceMap(rangeMs)
      .then((result) => {
        if (!ignore) setMap(result)
      })
      .catch((err: Error) => {
        if (!ignore) setError(err.message)
      })
    return () => {
      ignore = true
    }
  }, [rangeMs, refreshCount])

  // A live map: re-fetch every few seconds so a breaking service turns red on its own.
  useEffect(() => {
    if (!autoRefresh) return
    const timer = setInterval(refresh, AUTO_REFRESH_MS)
    return () => clearInterval(timer)
  }, [autoRefresh])

  // Turn the API's nodes/edges into React Flow's format, with positions.
  const { flowNodes, flowEdges } = useMemo(() => {
    if (!map) return { flowNodes: [], flowEdges: [] }
    const positions = layoutServiceMap(map.nodes, map.edges)
    const flowNodes: ServiceFlowNode[] = map.nodes.map((service) => ({
      id: service.id,
      type: 'service',
      position: positions.get(service.id) ?? { x: 0, y: 0 },
      data: { service },
    }))
    const flowEdges: Edge[] = map.edges.map((e) => {
      const failing = e.error_rate > ERROR_RATE_THRESHOLD
      const color = failing ? 'var(--down)' : 'var(--edge)'
      return {
        id: `${e.source}->${e.target}`,
        source: e.source,
        target: e.target,
        label: `${formatDuration(e.avg_duration_ms)} · ${formatPercent(e.error_rate)} err`,
        // Moving dashes draw the eye to the failing call.
        animated: failing,
        style: { stroke: color, strokeWidth: failing ? 2.5 : 1.5 },
        labelStyle: { fill: failing ? 'var(--down)' : 'var(--muted)', fontWeight: failing ? 600 : 400 },
        markerEnd: { type: MarkerType.ArrowClosed, color },
      }
    })
    return { flowNodes, flowEdges }
  }, [map])

  const failingEdges = map?.edges.filter((e) => e.error_rate > ERROR_RATE_THRESHOLD) ?? []

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>Service Map</h1>
          <p className="muted">Who calls whom, and how those calls are doing.</p>
        </div>
        <div className="page-actions">
          <button className="button" onClick={refresh}>Refresh</button>
        </div>
      </div>

      <SidebarPanel title="Map options">
        <label className="field">
          Time range
          <select value={rangeMs} onChange={(e) => setRangeMs(Number(e.target.value))}>
            {RANGES.map((r) => (
              <option key={r.ms} value={r.ms}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />
          Auto-refresh every 5 s
        </label>
        <p className="sidebar-hint">Pick "Last minute" to see a new failure turn red within seconds.</p>
      </SidebarPanel>

      {error && !map ? (
        <ErrorState title="Could not load the service map" message={error} onRetry={refresh} />
      ) : !map ? (
        <LoadingState label="Loading service map…" />
      ) : map.nodes.length === 0 ? (
        <EmptyState title="No services in this time range">
          Start the demo with <code>uv run demo/run_demo.py</code> or pick a longer range.
        </EmptyState>
      ) : (
        <>
          {error && <p className="map-hint error-text">Refresh failed, showing the last map: {error}</p>}
          <p className="muted map-hint">
            {failingEdges.length > 0 ? (
              <span className="error-text">
                {failingEdges.length} failing {failingEdges.length === 1 ? 'call' : 'calls'} (red, over{' '}
                {formatPercent(ERROR_RATE_THRESHOLD)} errors).{' '}
              </span>
            ) : (
              'All calls healthy. '
            )}
            Click a service or an arrow to see its traces.
          </p>
          <div className="map-canvas">
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdges}
              nodeTypes={NODE_TYPES}
              colorMode={theme}
              fitView
              fitViewOptions={{ padding: 0.25 }}
              nodesDraggable={false}
              nodesConnectable={false}
              proOptions={{ hideAttribution: true }}
              onNodeClick={(_, node) => navigate(`/traces?service=${encodeURIComponent(node.id)}`)}
              onEdgeClick={(_, edge) => {
                // An arrow describes the target's side of the call: show the target's traces,
                // only the failing ones if the arrow is red.
                const data = map.edges.find((e) => `${e.source}->${e.target}` === edge.id)
                const failing = data !== undefined && data.error_rate > ERROR_RATE_THRESHOLD
                navigate(`/traces?service=${encodeURIComponent(edge.target)}${failing ? '&errors=1' : ''}`)
              }}
            >
              <Background gap={20} color="var(--grid-dot)" />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        </>
      )}
    </section>
  )
}
