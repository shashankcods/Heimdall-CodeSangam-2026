import { useEffect, useState } from 'react'

// Returns `value`, but only after it has stopped changing for `delayMs`.
// Used for text boxes so we search once you pause typing, not on every key.
export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs)
    // A new keystroke cancels the previous timer before it fires.
    return () => clearTimeout(timer)
  }, [value, delayMs])

  return debounced
}
