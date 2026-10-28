import { Handle, Position } from '@xyflow/react'
import type { Node, NodeProps } from '@xyflow/react'
import type { ServiceNode } from '../api/types'
import { formatDuration, formatPercent } from '../lib/format'
import { ERROR_RATE_THRESHOLD } from '../lib/serviceMapLayout'

// React Flow passes our data in `data`; this names its shape.
export type ServiceFlowNode = Node<{ service: ServiceNode }, 'service'>

// One box on the service map: name, requests, latency and error rate.
export default function ServiceNodeCard({ data }: NodeProps<ServiceFlowNode>) {
  const s = data.service
  const failing = s.error_rate > ERROR_RATE_THRESHOLD
  return (
    <div className={failing ? 'service-node failing' : 'service-node'}>
      {/* Connection points: calls come in on the left, go out on the right. */}
      <Handle type="target" position={Position.Left} />
      <div className="service-node-name">{s.id}</div>
      <div className="service-node-stats">
        <span>{s.request_count} req</span>
        <span>{s.avg_duration_ms === null ? '—' : `avg ${formatDuration(s.avg_duration_ms)}`}</span>
        <span className={failing ? 'error-text' : undefined}>{formatPercent(s.error_rate)} errors</span>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  )
}
