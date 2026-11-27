import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

class HTTPError extends Error {
  status: number
  constructor(status: number) {
    super(`HTTP ${status}`)
    this.status = status
  }
}

const api = vi.hoisted(() => ({
  assistants: vi.fn(),
  threads: vi.fn(),
}))

vi.mock('@langchain/langgraph-sdk', () => ({
  Client: class {
    assistants = { search: api.assistants }
    threads = { search: api.threads }
  },
}))
vi.mock('@langchain/langgraph-sdk/react', () => ({
  useStream: () => ({ messages: [], isLoading: false, error: undefined, submit: vi.fn() }),
}))

import { Chat } from './Chat'

afterEach(cleanup)

beforeEach(() => {
  api.assistants.mockReset()
  api.threads.mockReset()
})

test('401 asks to sign in again instead of claiming there is no chat', async () => {
  api.assistants.mockRejectedValue(new HTTPError(401))
  api.threads.mockRejectedValue(new HTTPError(401))
  const reauth = vi.fn()
  render(<Chat token="t" onUnauthorized={reauth} />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/войдите снова/i)
  screen.getByRole('button', { name: 'Войти снова' }).click()
  expect(reauth).toHaveBeenCalledOnce()
})

test('server unreachable is reported as unavailable', async () => {
  api.assistants.mockRejectedValue(new TypeError('Failed to fetch'))
  api.threads.mockRejectedValue(new TypeError('Failed to fetch'))
  render(<Chat token="t" onUnauthorized={vi.fn()} />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/недоступен/i)
  expect(screen.queryByRole('button', { name: 'Войти снова' })).not.toBeInTheDocument()
})

test('missing assistant is not reported as a network problem', async () => {
  api.assistants.mockResolvedValue([])
  api.threads.mockResolvedValue([])
  render(<Chat token="t" onUnauthorized={vi.fn()} />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/агент не настроен/i)
})

test('empty thread list is not an error', async () => {
  api.assistants.mockResolvedValue([{ assistant_id: 'a1' }])
  api.threads.mockResolvedValue([])
  render(<Chat token="t" onUnauthorized={vi.fn()} />)
  expect(await screen.findByText('Задайте вопрос по регламентам.')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test('memory panel opens on demand and loads the account memory', async () => {
  api.assistants.mockResolvedValue([{ assistant_id: 'regagent' }])
  api.threads.mockResolvedValue([])
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    records: [], signals: [],
    progress: { current: null, current_confirmed_at: null, days_until_stale: null,
      pending_area: null, pending_count: 0, pending_needed: 0 },
  }), { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  try {
    render(<Chat token="t" onUnauthorized={vi.fn()} />)
    expect(fetchMock).not.toHaveBeenCalled()
    screen.getByRole('button', { name: 'Память' }).click()
    expect(await screen.findByText(/Области в памяти нет/)).toBeInTheDocument()
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/api\/me\/memory$/)
  } finally {
    vi.unstubAllGlobals()
  }
})
