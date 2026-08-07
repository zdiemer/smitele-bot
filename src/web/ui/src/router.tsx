/**
 * Routing, in about sixty lines.
 *
 * This was react-router, and it is not any more. Five static tabs and one
 * `/players/:name` detail page is not what that library is for, and it arrived
 * with a long tail of advisories — open redirects, SSR XSS, RSC deserialisation
 * — every one of them in a feature this page does not use. Carrying that to
 * avoid writing `history.pushState` is a bad trade for a public status board.
 *
 * What it does: patterns with at most one `:param`, a `<Link>` that pushes
 * state instead of reloading, and a `popstate` listener. What it deliberately
 * does not do: loaders, nested layouts, redirects, or anything that turns a
 * URL into a navigation target — the only thing this ever navigates to is a
 * path from the `ROUTES` table below.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

type Navigate = (to: string) => void

const PathContext = createContext<{ path: string; navigate: Navigate }>({
  path: '/',
  navigate: () => {},
})

export function Router({ children }: { children: ReactNode }) {
  const [path, setPath] = useState(() => window.location.pathname || '/')

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname || '/')
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const navigate = useCallback<Navigate>((to) => {
    if (to === window.location.pathname) return
    window.history.pushState(null, '', to)
    setPath(to)
    window.scrollTo(0, 0)
  }, [])

  const value = useMemo(() => ({ path, navigate }), [path, navigate])
  return <PathContext.Provider value={value}>{children}</PathContext.Provider>
}

export function usePath() {
  return useContext(PathContext)
}

export function Link({
  to,
  className,
  children,
}: {
  to: string
  className?: string
  children: ReactNode
}) {
  const { navigate } = usePath()
  return (
    <a
      href={to}
      className={className}
      onClick={(event) => {
        // Let the browser handle anything that isn't a plain left click, so
        // middle-click and cmd-click still open a tab.
        if (event.defaultPrevented || event.button !== 0) return
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
        event.preventDefault()
        navigate(to)
      }}
    >
      {children}
    </a>
  )
}

export function NavLink({ to, end, children }: { to: string; end?: boolean; children: ReactNode }) {
  const { path } = usePath()
  const active = end ? path === to : path === to || path.startsWith(`${to}/`)
  return (
    <Link to={to} className={active ? 'active' : undefined}>
      {children}
    </Link>
  )
}

/**
 * Match `path` against a pattern with at most one `:param` segment.
 * Returns the captured param, `{}` for a match with none, or null.
 */
export function match(pattern: string, path: string): Record<string, string> | null {
  const wanted = pattern.split('/').filter(Boolean)
  const actual = path.split('/').filter(Boolean)
  if (wanted.length !== actual.length) return null

  const params: Record<string, string> = {}
  for (let index = 0; index < wanted.length; index += 1) {
    const segment = wanted[index]
    if (segment.startsWith(':')) {
      params[segment.slice(1)] = decodeURIComponent(actual[index])
    } else if (segment !== actual[index]) {
      return null
    }
  }
  return params
}
