import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server runs on :5173. Any request to /api/... is forwarded to the
// backend on :4318, so app code only ever uses relative paths like '/api/traces'.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:4318',
    },
  },
})
