import { describe, expect, it, vi } from 'vitest'
import { fetchLogs, fetchServiceMap } from '../api/client'
import { formatDuration, formatPercentage, isHighError } from '../lib/format'
import { computeGraphLayout } from '../pages/ServiceMapPage'

describe('Formatting utilities', () => {
  it('formats fractional error rates as percentages correctly', () => {
    expect(formatPercentage(0.0)).toBe('0.0%')
    expect(formatPercentage(0.1)).toBe('10.0%')
    expect(formatPercentage(0.05)).toBe('5.0%')
    expect(formatPercentage(0.254)).toBe('25.4%')
    expect(formatPercentage(1.0)).toBe('100.0%')
  })

  it('detects high error rate threshold (> 5%)', () => {
    expect(isHighError(0.0)).toBe(false)
    expect(isHighError(0.05)).toBe(false)
    expect(isHighError(0.051)).toBe(true)
    expect(isHighError(0.1)).toBe(true)
  })

  it('formats duration correctly', () => {
    expect(formatDuration(42.5)).toBe('42.5 ms')
    expect(formatDuration(1500)).toBe('1.50 s')
  })
})

describe('Service Map layout generation', () => {
  it('computes deterministic positions and assigns layers for gateway -> order -> payment', () => {
    const rawNodes = [{ service: 'gateway' }, { service: 'order' }, { service: 'payment' }]
    const rawEdges = [
      {
        source: 'gateway',
        target: 'order',
        call_count: 10,
        avg_latency_ms: 25.0,
        error_count: 0,
        error_rate: 0.0,
      },
      {
        source: 'order',
        target: 'payment',
        call_count: 10,
        avg_latency_ms: 10.0,
        error_count: 1,
        error_rate: 0.1, // 10% error
      },
    ]

    const { nodes, edges } = computeGraphLayout(rawNodes, rawEdges)

    expect(nodes).toHaveLength(3)
    expect(edges).toHaveLength(2)

    const gw = nodes.find((n) => n.id === 'gateway')
    const ord = nodes.find((n) => n.id === 'order')
    const pay = nodes.find((n) => n.id === 'payment')

    expect(gw).toBeDefined()
    expect(ord).toBeDefined()
    expect(pay).toBeDefined()

    // Layered X-coordinates: gateway (left) -> order (middle) -> payment (right)
    expect(gw!.position.x).toBeLessThan(ord!.position.x)
    expect(ord!.position.x).toBeLessThan(pay!.position.x)

    // Edge highlighting for order -> payment with 10% error rate
    const errEdge = edges.find((e) => e.source === 'order' && e.target === 'payment')
    expect(errEdge).toBeDefined()
    expect(errEdge!.label).toContain('10.0% err')
    expect(errEdge!.animated).toBe(true)
    expect(errEdge!.style?.stroke).toBe('#cf222e') // Red error stroke
  })

  it('handles isolated disconnected services gracefully', () => {
    const rawNodes = [{ service: 'service-a' }, { service: 'service-b' }]
    const rawEdges: any[] = []

    const { nodes, edges } = computeGraphLayout(rawNodes, rawEdges)
    expect(nodes).toHaveLength(2)
    expect(edges).toHaveLength(0)
    expect(nodes[0].data.inboundCount).toBe(0)
    expect(nodes[0].data.outboundCount).toBe(0)
  })
})

describe('API Client query construction', () => {
  it('builds query parameters for fetchLogs properly', async () => {
    let requestedUrl = ''
    global.fetch = vi.fn().mockImplementation((url: string) => {
      requestedUrl = url
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ logs: [] }),
      })
    })

    await fetchLogs({
      query: 'order created',
      service: 'order',
      severity: 'ERROR',
      limit: 50,
      fromMs: 1000,
      toMs: 2000,
    })

    expect(requestedUrl).toContain('/api/logs?')
    expect(requestedUrl).toContain('query=order+created')
    expect(requestedUrl).toContain('service=order')
    expect(requestedUrl).toContain('severity=ERROR')
    expect(requestedUrl).toContain('limit=50')
    expect(requestedUrl).toContain('from_ms=1000')
    expect(requestedUrl).toContain('to_ms=2000')
  })

  it('extracts structured error detail from HTTP 422 responses', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      statusText: 'Unprocessable Entity',
      json: () => Promise.resolve({ detail: 'Invalid regular expression: unterminated subpattern' }),
    })

    await expect(fetchLogs({ regex: '(abc' })).rejects.toThrow(
      'Invalid regular expression: unterminated subpattern',
    )
  })

  it('builds query parameters for fetchServiceMap properly', async () => {
    let requestedUrl = ''
    global.fetch = vi.fn().mockImplementation((url: string) => {
      requestedUrl = url
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ nodes: [], edges: [] }),
      })
    })

    await fetchServiceMap(1000, 5000)
    expect(requestedUrl).toBe('/api/service-map?from_ms=1000&to_ms=5000')
  })
})
