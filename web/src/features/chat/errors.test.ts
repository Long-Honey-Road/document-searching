import { expect, test } from 'vitest'
import { classifyError, problemText, type ChatProblem } from './errors'

class HTTPError extends Error {
  status: number
  constructor(status: number) {
    super(`HTTP ${status}`)
    this.status = status
  }
}

test.each([
  [new HTTPError(401), { kind: 'unauthorized', status: 401 }],
  [new HTTPError(403), { kind: 'forbidden', status: 403 }],
  [new HTTPError(429), { kind: 'rate_limited', status: 429 }],
  [new HTTPError(502), { kind: 'unavailable', status: 502 }],
  [new HTTPError(503), { kind: 'unavailable', status: 503 }],
  [new HTTPError(500), { kind: 'server', status: 500 }],
  [new HTTPError(404), { kind: 'http', status: 404 }],
  [new TypeError('Failed to fetch'), { kind: 'unavailable' }],
  ['что-то странное', { kind: 'unknown' }],
] as const)('classifies %s', (error, expected) => {
  expect(classifyError(error)).toEqual(expected)
})

test('every problem has its own text, unauthorized asks to sign in again', () => {
  const kinds: ChatProblem[] = [
    { kind: 'unauthorized', status: 401 },
    { kind: 'forbidden', status: 403 },
    { kind: 'rate_limited', status: 429 },
    { kind: 'unavailable' },
    { kind: 'server', status: 500 },
    { kind: 'http', status: 404 },
    { kind: 'no_assistant' },
    { kind: 'many_assistants' },
    { kind: 'unknown' },
  ]
  const texts = kinds.map(problemText)
  expect(new Set(texts).size).toBe(texts.length)
  expect(problemText({ kind: 'unauthorized', status: 401 })).toMatch(/войдите снова/i)
  expect(problemText({ kind: 'server', status: 500 })).toContain('500')
  expect(problemText({ kind: 'http', status: 404 })).toContain('404')
  for (const text of texts) expect(text).not.toBe('Не удалось загрузить данные чата.')
})
