import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Router } from './router'
import App from './App'
import './styles.css'

// Real paths rather than hashes: serve.py serves index.html for any unmatched
// path that isn't /api, so a deep link works on a cold load.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Router>
      <App />
    </Router>
  </StrictMode>,
)
