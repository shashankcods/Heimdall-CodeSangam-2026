import { NavLink, Outlet } from 'react-router'
import BackendStatus from './BackendStatus'

const NAV_ITEMS = [
  { to: '/traces', label: 'Traces' },
  { to: '/logs', label: 'Logs' },
  { to: '/service-map', label: 'Service Map' },
]

// App shell: sidebar on the left, the current page on the right.
export default function Layout() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">Heimdall</div>
        <nav>
          {NAV_ITEMS.map((item) => (
            // NavLink adds the "active" class when its URL is the current one.
            <NavLink key={item.to} to={item.to} className="nav-link">
              {item.label}
            </NavLink>
          ))}
        </nav>
        <BackendStatus />
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}
