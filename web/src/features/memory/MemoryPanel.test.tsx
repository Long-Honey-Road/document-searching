import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { MemoryPanel } from './MemoryPanel'

type Memory = {
  records: { key: string; value: string; confirmed_at: string }[]
  signals: { area: string; at: string }[]
  progress: {
    current: string | null
    current_confirmed_at: string | null
    days_until_stale: number | null
    pending_area: string | null
    pending_count: number
    pending_needed: number
  }
}

const empty: Memory = {
  records: [],
  signals: [],
  progress: {
    current: null, current_confirmed_at: null, days_until_stale: null,
    pending_area: null, pending_count: 0, pending_needed: 0,
  },
}

const dib: Memory = {
  records: [{ key: 'area:ДИБ', value: 'ДИБ', confirmed_at: '2026-11-28T01:00:00Z' }],
  signals: [{ area: 'ДИБ', at: '2026-11-28T00:50:00Z' }, { area: 'ДБРЦТ', at: '2026-11-28T01:10:00Z' }],
  progress: {
    current: 'ДИБ', current_confirmed_at: '2026-11-28T01:00:00Z', days_until_stale: 13,
    pending_area: 'ДБРЦТ', pending_count: 1, pending_needed: 3,
  },
}

const fetchMock = vi.fn()

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(status === 204 ? null : JSON.stringify(body), { status }))
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('shows current area, days left and progress towards a change', async () => {
  fetchMock.mockReturnValueOnce(reply(dib))
  render(<MemoryPanel apiUrl="http://stand/api" token="t" refreshKey={0} />)
  expect(await screen.findByText('ДИБ', { selector: 'strong' })).toBeInTheDocument()
  expect(screen.getByText(/ещё 13 дн/)).toBeInTheDocument()
  expect(screen.getByText(/ДБРЦТ: 1 из 3 до смены области/)).toBeInTheDocument()
  const [url, init] = fetchMock.mock.calls[0]
  expect(url).toBe('http://stand/api/me/memory')
  expect(init.headers.Authorization).toBe('Bearer t')
})

test('empty memory explains how an area gets remembered', async () => {
  fetchMock.mockReturnValueOnce(reply({ ...empty, signals: [{ area: 'ДИБ', at: '2026-11-28T01:00:00Z' }],
    progress: { ...empty.progress, pending_area: 'ДИБ', pending_count: 1, pending_needed: 2 } }))
  render(<MemoryPanel apiUrl="/api" token="t" refreshKey={0} />)
  expect(await screen.findByText(/Области в памяти нет/)).toBeInTheDocument()
  expect(screen.getByText(/ДИБ: 1 из 2 до запоминания/)).toBeInTheDocument()
})

test('refresh after an answer reports that the area changed', async () => {
  fetchMock.mockReturnValueOnce(reply(dib))
  const view = render(<MemoryPanel apiUrl="/api" token="t" refreshKey={0} />)
  await screen.findByText(/ещё 13 дн/)
  const switched: Memory = {
    ...dib,
    records: [{ key: 'area:ДБРЦТ', value: 'ДБРЦТ', confirmed_at: '2026-11-28T01:20:00Z' }],
    progress: { ...dib.progress, current: 'ДБРЦТ', days_until_stale: 14, pending_area: null, pending_count: 0 },
  }
  fetchMock.mockReturnValueOnce(reply(switched))
  view.rerender(<MemoryPanel apiUrl="/api" token="t" refreshKey={1} />)
  expect(await screen.findByRole('status')).toHaveTextContent('Память изменилась: ДИБ → ДБРЦТ')
})

test('forget area and reset counter call the API and reload', async () => {
  fetchMock.mockReturnValueOnce(reply(dib))
  render(<MemoryPanel apiUrl="/api" token="t" refreshKey={0} />)
  await screen.findByText(/ещё 13 дн/)
  const forgotten: Memory = {
    ...empty,
    signals: [dib.signals[1]],
    progress: { ...empty.progress, pending_area: 'ДБРЦТ', pending_count: 1, pending_needed: 2 },
  }
  fetchMock.mockReturnValueOnce(reply(null, 204)).mockReturnValueOnce(reply(forgotten))
  fireEvent.click(screen.getByRole('button', { name: 'Забыть область' }))
  await waitFor(() => expect(screen.getByText(/Области в памяти нет/)).toBeInTheDocument())
  expect(fetchMock.mock.calls[1][0]).toBe('/api/me/memory/' + encodeURIComponent('area:ДИБ'))
  expect(fetchMock.mock.calls[1][1].method).toBe('DELETE')

  fetchMock.mockReturnValueOnce(reply(null, 204)).mockReturnValueOnce(reply(empty))
  fireEvent.click(screen.getByRole('button', { name: 'Сбросить счёт' }))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5))
  expect(fetchMock.mock.calls[3][0]).toBe('/api/me/memory/' + encodeURIComponent('signals:area'))
})

test('401 is reported as an expired session', async () => {
  fetchMock.mockReturnValueOnce(reply({}, 401))
  render(<MemoryPanel apiUrl="/api" token="t" refreshKey={0} />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/войдите снова/i)
})
