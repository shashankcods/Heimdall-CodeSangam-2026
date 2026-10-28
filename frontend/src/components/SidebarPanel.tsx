import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'

// Layout owns an empty spot in the sidebar and shares it through this context.
export const SidebarSlotContext = createContext<HTMLElement | null>(null)

// A page's filters, drawn in the sidebar under the nav instead of on the page.
// The page still owns the state; a portal only changes WHERE the markup appears.
export default function SidebarPanel({ title, children }: { title: string; children: ReactNode }) {
  const slot = useContext(SidebarSlotContext)
  if (!slot) return null
  return createPortal(
    <section className="sidebar-panel" aria-label={title}>
      <h2>{title}</h2>
      {children}
    </section>,
    slot,
  )
}
