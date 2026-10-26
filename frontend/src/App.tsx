import { useEffect, useState } from 'react'

type Health = 'checking' | 'ok' | 'down'

// Placeholder page. Calls GET /api/health through the Vite proxy to prove
// the frontend can reach the backend. Replaced by the app shell in the next task.
export default function App() {
  const [health, setHealth] = useState<Health>('checking')

  useEffect(() => {
    fetch('/api/health')
      .then((res) => res.json())
      .then((data: { status: string }) => setHealth(data.status === 'ok' ? 'ok' : 'down'))
      .catch(() => setHealth('down'))
  }, [])

  return (
    <main className="placeholder">
      <h1>Heimdall</h1>
      <p>An OpenTelemetry-native observability engine.</p>
      <p>
        Backend: <strong className={`health health-${health}`}>{health}</strong>
      </p>
    </main>
  )
}
