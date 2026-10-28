// The Heimdall "Span-H" mark: two tall spans and a short one between them, like
// bars in a trace waterfall forming an H. Fixed brand colors (Blue eclipse), the
// same in light and dark mode. public/favicon.svg is the same drawing.
export default function LogoMark({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" role="img" aria-label="Heimdall logo">
      <rect x="4" y="4" width="4" height="16" rx="2" fill="#8686AC" />
      <rect x="10" y="8" width="4" height="8" rx="2" fill="#ffffff" />
      <rect x="16" y="4" width="4" height="16" rx="2" fill="#505081" />
    </svg>
  )
}
