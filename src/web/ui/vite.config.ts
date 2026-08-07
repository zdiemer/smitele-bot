import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built into `dist/`, which Dockerfile.web copies next to serve.py. serve.py
// looks for it there by default, so nothing has to agree on a path twice.
//
// `npm run dev` proxies /api to a locally-running serve.py rather than mocking
// it: the API is same-origin in production, and a dev server that invents its
// own data is a dev server that can be wrong in ways production never is.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: {
      '/api': 'http://localhost:8080',
    },
    // The docs view imports `docs/desktop-api.md?raw` so the page and the repo
    // cannot drift. That path is above this project root, which the dev server
    // refuses by default; the build resolves it either way.
    fs: { allow: ['..', '../../..'] },
  },
})
