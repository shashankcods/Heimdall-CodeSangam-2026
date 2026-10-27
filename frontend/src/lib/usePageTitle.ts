import { useEffect } from 'react'

// Sets the browser tab title, e.g. "Logs · Heimdall".
export function usePageTitle(title: string) {
  useEffect(() => {
    document.title = `${title} · Heimdall`
  }, [title])
}
