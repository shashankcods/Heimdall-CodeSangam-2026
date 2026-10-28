import type { Span } from '../api/types'

export interface SpanNode {
  span: Span
  children: SpanNode[]
}

// One visible line of the waterfall: a span plus how far to indent it.
export interface SpanRow {
  span: Span
  depth: number
  hasChildren: boolean
}

// Turn the flat span list into a tree using parent_span_id.
// A span whose parent is missing (not arrived yet, or dropped) becomes a root,
// so it still shows up instead of disappearing.
export function buildSpanTree(spans: Span[]): SpanNode[] {
  const nodes = new Map<string, SpanNode>()
  for (const span of spans) {
    nodes.set(span.span_id, { span, children: [] })
  }

  const roots: SpanNode[] = []
  // Spans arrive sorted by start time and a Map keeps insertion order,
  // so children end up in start order too.
  for (const node of nodes.values()) {
    const parentId = node.span.parent_span_id
    const parent = parentId ? nodes.get(parentId) : undefined
    if (parent && parent !== node) {
      parent.children.push(node)
    } else {
      roots.push(node)
    }
  }
  return roots
}

// Walk the tree top to bottom and list the rows to draw.
// Children of a collapsed span are skipped.
export function flattenTree(roots: SpanNode[], collapsed: Set<string>): SpanRow[] {
  const rows: SpanRow[] = []
  const visit = (node: SpanNode, depth: number) => {
    rows.push({ span: node.span, depth, hasChildren: node.children.length > 0 })
    if (collapsed.has(node.span.span_id)) return
    for (const child of node.children) visit(child, depth + 1)
  }
  for (const root of roots) visit(root, 0)
  return rows
}

// The error span furthest down the tree: the step that actually broke, rather than
// the callers above it that only failed because of it. Ties go to the longer span.
export function deepestErrorSpan(roots: SpanNode[]): Span | null {
  let best: Span | null = null
  let bestDepth = -1
  const stack = roots.map((node) => ({ node, depth: 0 }))
  while (stack.length > 0) {
    const { node, depth } = stack.pop()!
    const span = node.span
    if (
      span.status_code === 'ERROR' &&
      (depth > bestDepth || (depth === bestDepth && best !== null && span.duration_ms > best.duration_ms))
    ) {
      best = span
      bestDepth = depth
    }
    for (const child of node.children) stack.push({ node: child, depth: depth + 1 })
  }
  return best
}
