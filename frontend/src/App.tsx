import { Navigate, Route, Routes } from 'react-router'
import Layout from './components/Layout'
import TracesPage from './pages/TracesPage'
import TraceDetailPage from './pages/TraceDetailPage'
import LogsPage from './pages/LogsPage'
import ServiceMapPage from './pages/ServiceMapPage'

// URL -> page. Every page renders inside Layout (the sidebar), at its <Outlet />.
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Navigate to="/traces" replace />} />
        <Route path="/traces" element={<TracesPage />} />
        <Route path="/traces/:traceId" element={<TraceDetailPage />} />
        <Route path="/logs" element={<LogsPage />} />
        <Route path="/service-map" element={<ServiceMapPage />} />
        <Route path="*" element={<p className="page">Page not found.</p>} />
      </Route>
    </Routes>
  )
}
