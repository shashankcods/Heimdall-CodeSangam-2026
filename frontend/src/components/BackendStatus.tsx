import { useEffect, useState } from 'react'

type Health = 'checking' | 'ok' | 'down'

const POLL_MS = 5000

const LABELS: Record<Health, string> = {
  checking: 'Checking backend…',
  ok: 'Backend connected',
  down: 'Backend unreachable',
}

// Card at the bottom of the sidebar showing whether GET /api/health answers.
// Re-checks every few seconds so it turns green once the backend starts.
export default function BackendStatus() {
  const [health, setHealth] = useState<Health>('checking')

  useEffect(() => {
    const check = () =>
      fetch('/api/health')
        .then((res) => res.json())
        .then((data: { status: string }) => setHealth(data.status === 'ok' ? 'ok' : 'down'))
        .catch(() => setHealth('down'))

    check()
    const timer = setInterval(check, POLL_MS)
    // Cleanup: stop the timer when this component leaves the screen.
    return () => clearInterval(timer)
  }, [])

  return (
    <div className="backend-status" role="status">
      <span className={`dot dot-${health}`} />
      {LABELS[health]}
    </div>
  )
}
