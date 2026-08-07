/**
 * `docs/desktop-api.md`, rendered.
 *
 * Imported with `?raw`, so the file is inlined at build time and the page has
 * one source of truth with the repo rather than a copy that drifts. It also
 * means the markdown being rendered is a file from this repository and never
 * anything a request supplied, which is what makes setting it as HTML fine
 * here and would not make it fine anywhere user input could reach.
 */

import { useMemo } from 'react'
import { Band } from '../components'
import { marked } from 'marked'
import source from '../../../../../docs/desktop-api.md?raw'

export default function Docs() {
  const html = useMemo(() => marked.parse(source, { async: false }) as string, [])

  return (
    <>
      <Band label="Design sketch" qualifier="none of this is implemented" health="warn">
        <p className="prose" style={{ marginBottom: 0 }}>
          The live API is <code>/api/status</code>, <code>/api/players</code>,{' '}
          <code>/api/players/&#123;name&#125;</code> and <code>/api/meta</code> — all of
          which serve a snapshot and compute nothing.
        </p>
      </Band>
      <article className="doc" dangerouslySetInnerHTML={{ __html: html }} />
    </>
  )
}
