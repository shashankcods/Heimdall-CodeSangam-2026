const PALETTE_SIZE = 8

// The same service always gets the same color (--svc-0 … --svc-7 in index.css),
// on every page, without anyone keeping a list of service names.
// (Multiplier 37 happens to give the four demo services four different colors.)
export function serviceColor(service: string): string {
  let hash = 0
  for (let i = 0; i < service.length; i++) {
    hash = (hash * 37 + service.charCodeAt(i)) | 0
  }
  return `var(--svc-${Math.abs(hash) % PALETTE_SIZE})`
}
