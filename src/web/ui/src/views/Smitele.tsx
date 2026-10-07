import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'

type Ability = { name: string; icon: string }

type God = {
  id: number
  name: string
  title: string
  pantheon: string
  role: string
  damage: string
  range: string
  card: string
  icon: string
  abilities: Ability[]
}

type Catalogue = { version: number; gods: God[] }
type SavedGame = { day: string; guesses: string[] }

const MAX_GUESSES = 6
const PUZZLE_EPOCH = Date.UTC(2026, 9, 7)
const DAY_MS = 24 * 60 * 60 * 1000
const SITE_URL = 'https://smite.diemer.codes/smitele'

function utcDay(now = new Date()) {
  return now.toISOString().slice(0, 10)
}

function puzzleNumber(now = new Date()) {
  const midnight = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  return Math.floor((midnight - PUZZLE_EPOCH) / DAY_MS) + 1
}

function hash(text: string) {
  let value = 2166136261
  for (let index = 0; index < text.length; index += 1) {
    value ^= text.charCodeAt(index)
    value = Math.imul(value, 16777619)
  }
  return value >>> 0
}

function dailyGod(gods: God[], day: string) {
  return gods[hash(`smitele-v1:${day}`) % gods.length]
}

function normalized(value: string) {
  return value
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[’']/g, '')
    .trim()
    .toLowerCase()
}

function timeUntilTomorrow(now = new Date()) {
  const next = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1)
  const seconds = Math.max(0, Math.floor((next - now.getTime()) / 1000))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60
  return [hours, minutes, remainder].map((part) => String(part).padStart(2, '0')).join(':')
}

function readSaved(day: string): SavedGame {
  try {
    const parsed = JSON.parse(localStorage.getItem(`smitele:${day}`) || '{}') as SavedGame
    if (parsed.day === day && Array.isArray(parsed.guesses)) {
      return { day, guesses: parsed.guesses.slice(0, MAX_GUESSES) }
    }
  } catch {
    // A stale or hand-edited value should start a fresh game, not break the page.
  }
  return { day, guesses: [] }
}

function Result({ answer, guesses }: { answer: God; guesses: string[] }) {
  const won = guesses.some((guess) => normalized(guess) === normalized(answer.name))
  const finished = won || guesses.length === MAX_GUESSES
  if (!finished) return null

  return (
    <div className={`smitele-result ${won ? 'smitele-win' : 'smitele-loss'}`} aria-live="polite">
      <img src={answer.icon} alt="" />
      <div>
        <span className="smitele-result-label">{won ? 'Divinity identified' : 'The god was'}</span>
        <strong>{answer.name}</strong>
        <span>{answer.title}</span>
      </div>
    </div>
  )
}

function Clues({ answer, count, finished }: { answer: God; count: number; finished: boolean }) {
  const abilityIndex = hash(`ability:${answer.id}`) % answer.abilities.length
  const ability = answer.abilities[abilityIndex]
  const secondAbility = answer.abilities[
    (abilityIndex + 1 + (hash(`ability-name:${answer.id}`) % (answer.abilities.length - 1))) % answer.abilities.length
  ]
  const visible = finished ? MAX_GUESSES : Math.min(count + 1, MAX_GUESSES)
  const cropX = 25 + (answer.id % 51)

  return (
    <ol className="smitele-clues" aria-label={`${visible} of ${MAX_GUESSES} clues revealed`}>
      <li className={visible >= 1 ? 'revealed' : ''}>
        <span className="clue-number">01</span>
        {visible >= 1 ? (
          <div className="ability-clue">
            <img src={ability.icon} alt="A mystery ability" />
            <span>One of this god’s abilities</span>
          </div>
        ) : <span className="clue-locked">locked</span>}
      </li>
      <li className={visible >= 2 ? 'revealed' : ''}>
        <span className="clue-number">02</span>
        {visible >= 2 ? <p>Pantheon: <strong>{answer.pantheon}</strong></p> : <span className="clue-locked">locked</span>}
      </li>
      <li className={visible >= 3 ? 'revealed' : ''}>
        <span className="clue-number">03</span>
        {visible >= 3 ? <p><strong>{answer.role}</strong> · {answer.damage} · {answer.range}</p> : <span className="clue-locked">locked</span>}
      </li>
      <li className={visible >= 4 ? 'revealed' : ''}>
        <span className="clue-number">04</span>
        {visible >= 4 ? <p>Ability: <strong>{secondAbility.name}</strong></p> : <span className="clue-locked">locked</span>}
      </li>
      <li className={visible >= 5 ? 'revealed' : ''}>
        <span className="clue-number">05</span>
        {visible >= 5 ? <p>Title: <strong>{answer.title}</strong></p> : <span className="clue-locked">locked</span>}
      </li>
      <li className={visible >= 6 ? 'revealed' : ''}>
        <span className="clue-number">06</span>
        {visible >= 6 ? (
          <div className="card-crop" aria-label="A close crop of the god’s card art">
            <img src={answer.card} alt="A close crop of the mystery god" style={{ objectPosition: `${cropX}% 32%` }} />
          </div>
        ) : <span className="clue-locked">locked</span>}
      </li>
    </ol>
  )
}

export default function Smitele() {
  const day = utcDay()
  const number = puzzleNumber()
  const [gods, setGods] = useState<God[]>([])
  const [loadError, setLoadError] = useState('')
  const [guesses, setGuesses] = useState<string[]>(() => readSaved(day).guesses)
  const [query, setQuery] = useState('')
  const [message, setMessage] = useState('')
  const [shareLabel, setShareLabel] = useState('Share result')
  const [remaining, setRemaining] = useState(() => timeUntilTomorrow())
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let active = true
    fetch('/smite-gods.json')
      .then((response) => {
        if (!response.ok) throw new Error(`catalogue returned ${response.status}`)
        return response.json() as Promise<Catalogue>
      })
      .then((catalogue) => { if (active) setGods(catalogue.gods) })
      .catch((error: Error) => { if (active) setLoadError(error.message) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (utcDay() !== day) window.location.reload()
      setRemaining(timeUntilTomorrow())
    }, 1000)
    return () => window.clearInterval(timer)
  }, [day])

  useEffect(() => {
    document.title = `Smite-le #${number} · Daily Smite god`
    return () => { document.title = 'Smite-le · daily Smite god game' }
  }, [number])

  const answer = gods.length ? dailyGod(gods, day) : null
  const won = answer ? guesses.some((guess) => normalized(guess) === normalized(answer.name)) : false
  const finished = won || guesses.length === MAX_GUESSES
  const guessed = useMemo(() => new Set(guesses.map(normalized)), [guesses])
  const suggestions = useMemo(() => {
    const wanted = normalized(query)
    if (!wanted) return []
    return gods
      .filter((god) => !guessed.has(normalized(god.name)) && normalized(god.name).includes(wanted))
      .slice(0, 6)
  }, [gods, guessed, query])

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!answer || finished) return
    const god = gods.find((candidate) => normalized(candidate.name) === normalized(query))
    if (!god) {
      setMessage('Choose a god from the roster.')
      return
    }
    if (guessed.has(normalized(god.name))) {
      setMessage(`${god.name} is already on the board.`)
      return
    }

    const next = [...guesses, god.name]
    setGuesses(next)
    localStorage.setItem(`smitele:${day}`, JSON.stringify({ day, guesses: next }))
    setQuery('')
    setMessage(normalized(god.name) === normalized(answer.name) ? 'Correct.' : 'Not this god. Another clue is open.')
    window.setTimeout(() => input.current?.focus(), 0)
  }

  async function share() {
    if (!answer || !finished) return
    const score = won ? `${guesses.length}/${MAX_GUESSES}` : `X/${MAX_GUESSES}`
    const row = Array.from({ length: MAX_GUESSES }, (_, index) => {
      if (won && index === guesses.length - 1) return '🟩'
      return index < guesses.length ? '⬛' : '⬜'
    }).join('')
    const text = `Smite-le #${number} ${score}\n\n${row}\n\n${SITE_URL}`

    try {
      if (navigator.share) {
        await navigator.share({ text })
        setShareLabel('Shared')
      } else {
        await navigator.clipboard.writeText(text)
        setShareLabel('Copied')
      }
    } catch (error) {
      if ((error as DOMException).name !== 'AbortError') {
        try {
          await navigator.clipboard.writeText(text)
          setShareLabel('Copied')
        } catch {
          setShareLabel('Could not copy')
        }
      }
    }
  }

  if (loadError) return <div className="smitele-load">Could not load today’s god roster: {loadError}</div>
  if (!answer) return <div className="smitele-load">Preparing today’s divinity…</div>

  return (
    <main className="smitele-game">
      <div className="smitele-heading">
        <div>
          <span className="smitele-kicker">Daily god · puzzle {number}</span>
          <h2>Smite-le</h2>
          <p>Name the Smite god. Every miss opens another clue; you get six attempts.</p>
        </div>
        <div className="smitele-clock" title="The next puzzle starts at midnight UTC">
          <span>next god</span>
          <strong>{remaining}</strong>
        </div>
      </div>

      <Clues answer={answer} count={guesses.length} finished={finished} />

      <div className="smitele-board" aria-label="Your guesses">
        {Array.from({ length: MAX_GUESSES }, (_, index) => {
          const guess = guesses[index]
          const correct = guess && normalized(guess) === normalized(answer.name)
          return (
            <div key={index} className={`guess-row ${guess ? 'filled' : ''} ${correct ? 'correct' : ''}`}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <strong>{guess || '—'}</strong>
              <b aria-label={correct ? 'correct' : guess ? 'incorrect' : 'unused'}>{correct ? '✓' : guess ? '×' : '·'}</b>
            </div>
          )
        })}
      </div>

      {!finished && (
        <form className="smitele-form" onSubmit={submit}>
          <label htmlFor="god-guess">Your guess</label>
          <div className="smitele-input-row">
            <input
              ref={input}
              id="god-guess"
              value={query}
              onChange={(event) => { setQuery(event.target.value); setMessage('') }}
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
              placeholder="Start typing a god name"
              aria-describedby="guess-message"
            />
            <button type="submit">Guess</button>
          </div>
          {suggestions.length > 0 && (
            <div className="god-suggestions" role="listbox" aria-label="Matching gods">
              {suggestions.map((god) => (
                <button key={god.id} type="button" onClick={() => { setQuery(god.name); input.current?.focus() }}>
                  <img src={god.icon} alt="" loading="lazy" />
                  <span>{god.name}</span>
                  <small>{god.pantheon} {god.role}</small>
                </button>
              ))}
            </div>
          )}
          <p id="guess-message" className="guess-message" aria-live="polite">{message || `${MAX_GUESSES - guesses.length} attempts left`}</p>
        </form>
      )}

      <Result answer={answer} guesses={guesses} />
      {finished && (
        <div className="smitele-actions">
          <button type="button" onClick={share}>{shareLabel}</button>
          <span>Come back in {remaining}</span>
        </div>
      )}
    </main>
  )
}
