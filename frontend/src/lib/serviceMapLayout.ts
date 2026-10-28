import type { ServiceEdge, ServiceNode } from '../api/types'

// An edge or service turns red above this share of failed calls (5%).
export const ERROR_RATE_THRESHOLD = 0.05

const COLUMN_GAP = 300
const ROW_GAP = 150

// Place services in columns, left to right, in call order: services nobody calls
// (like the gateway) on the left, then whatever they call, and so on.
// Same input always gives the same positions, so the map doesn't jump on refresh.
export function layoutServiceMap(
  nodes: ServiceNode[],
  edges: ServiceEdge[],
): Map<string, { x: number; y: number }> {
  const ids = nodes.map((n) => n.id).sort()
  const callees = new Map<string, string[]>(ids.map((id) => [id, []]))
  const hasCaller = new Set<string>()
  for (const e of edges) {
    callees.get(e.source)?.push(e.target)
    hasCaller.add(e.target)
  }

  // Breadth-first from the entry services: column = number of hops from an entry.
  const column = new Map<string, number>()
  const visit = (start: string) => {
    column.set(start, 0)
    const queue = [start]
    while (queue.length > 0) {
      const id = queue.shift()!
      for (const next of callees.get(id) ?? []) {
        if (!column.has(next)) {
          column.set(next, column.get(id)! + 1)
          queue.push(next)
        }
      }
    }
  }
  for (const id of ids) if (!hasCaller.has(id) && !column.has(id)) visit(id)
  // Services only reachable through a loop (A calls B calls A) have no entry: start from them.
  for (const id of ids) if (!column.has(id)) visit(id)

  const byColumn = new Map<number, string[]>()
  for (const id of ids) {
    const c = column.get(id)!
    byColumn.set(c, [...(byColumn.get(c) ?? []), id])
  }

  const positions = new Map<string, { x: number; y: number }>()
  for (const [c, members] of byColumn) {
    // Center each column vertically around y = 0.
    const top = -((members.length - 1) * ROW_GAP) / 2
    members.forEach((id, row) => positions.set(id, { x: c * COLUMN_GAP, y: top + row * ROW_GAP }))
  }
  return positions
}
