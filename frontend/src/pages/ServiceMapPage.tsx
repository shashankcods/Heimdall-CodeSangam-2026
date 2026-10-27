import { usePageTitle } from '../lib/usePageTitle'
import { EmptyState } from '../components/StatusMessage'

export default function ServiceMapPage() {
  usePageTitle('Service Map')
  return (
    <section className="page">
      <h1>Service Map</h1>
      <EmptyState title="Coming soon">
        A graph of which services call which, with latency and error rates. Waiting on the
        service map API (#7).
      </EmptyState>
    </section>
  )
}
