import { useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router'
import BackendStatus from './BackendStatus'
import LogoMark from './LogoMark'
import { SidebarSlotContext } from './SidebarPanel'
import { useTheme } from '../lib/theme'

// Small stroke icons, drawn in the text color (currentColor) so they follow the theme.
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  )
}

const NAV_ITEMS = [
  { to: '/traces', label: 'Traces', icon: <><path d="M3 6h10" /><path d="M7 12h10" /><path d="M11 18h10" /></> },
  { to: '/logs', label: 'Logs', icon: <><path d="M4 6h16" /><path d="M4 10h16" /><path d="M4 14h10" /><path d="M4 18h13" /></> },
  {
    to: '/service-map',
    label: 'Service Map',
    icon: <><circle cx="5" cy="12" r="2.5" /><circle cx="19" cy="6" r="2.5" /><circle cx="19" cy="18" r="2.5" /><path d="M7.5 12h4l5-5" /><path d="M11.5 12l5 5" /></>,
  },
]

// App shell: sidebar (brand, nav, the current page's filters, backend status),
// a top bar (trace lookup, theme toggle) and the current page.
export default function Layout() {
  const navigate = useNavigate()
  const [theme, toggleTheme] = useTheme()
  // The DOM node pages portal their filters into. A callback ref (setFilterSlot)
  // stores it in state, so pages re-render once it exists.
  const [filterSlot, setFilterSlot] = useState<HTMLElement | null>(null)
  const [lookup, setLookup] = useState('')

  const openTrace = (e: FormEvent) => {
    e.preventDefault()
    const id = lookup.trim().toLowerCase()
    if (id) navigate(`/traces/${id}`)
    setLookup('')
  }

  return (
    <SidebarSlotContext.Provider value={filterSlot}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark">
              <LogoMark />
            </span>
            <span className="brand-name">heimdall</span>
          </div>
          <nav aria-label="Main">
            {NAV_ITEMS.map((item) => (
              // NavLink adds the "active" class when its URL is the current one.
              <NavLink key={item.to} to={item.to} className="nav-link">
                <Icon>{item.icon}</Icon>
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div ref={setFilterSlot} className="sidebar-slot" />
          <BackendStatus />
        </aside>

        <div className="main">
          <header className="topbar">
            <form className="trace-lookup" onSubmit={openTrace} role="search">
              <label htmlFor="trace-lookup" className="visually-hidden">Look up a trace by ID</label>
              <Icon><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></Icon>
              <input
                id="trace-lookup"
                type="search"
                placeholder="Look up a trace ID"
                value={lookup}
                onChange={(e) => setLookup(e.target.value)}
              />
            </form>
            <button
              className="icon-button"
              onClick={toggleTheme}
              aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
              title={theme === 'dark' ? 'Light mode' : 'Dark mode'}
            >
              {theme === 'dark' ? (
                <Icon><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></Icon>
              ) : (
                <Icon><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></Icon>
              )}
            </button>
          </header>
          <main className="content">
            <Outlet context={{ theme }} />
          </main>
        </div>
      </div>
    </SidebarSlotContext.Provider>
  )
}
