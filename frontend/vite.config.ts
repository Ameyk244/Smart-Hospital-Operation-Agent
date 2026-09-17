import react from '@vitejs/plugin-react'
// vitest/config re-exports vite's defineConfig merged with the `test` key's
// types — importing from plain 'vite' would make TypeScript reject `test`.
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    globals: true,
  },
})
