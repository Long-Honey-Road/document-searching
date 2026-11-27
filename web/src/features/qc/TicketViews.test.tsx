import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { AdminTickets } from './TicketViews'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('reviews a ticket and sends the selected practice mode and bindings', async () => {
  const ticket = {
    id: 'ticket',
    question: 'Как согласовать заявку?',
    kind: 'employee',
    status: 'new',
    created_at: '2026-11-28T00:00:00Z',
    updated_at: '2026-11-28T00:00:00Z',
    comment: 'Нужен срок',
    draft: null,
    history: [],
    recheck: null,
  }
  const fetch = vi.fn((url: URL, options: RequestInit) => {
    if (options.method === 'POST') {
      return Promise.resolve(Response.json({ ...ticket, status: 'publishing' }))
    }
    return Promise.resolve(Response.json(url.pathname.endsWith('/tickets') ? [ticket] : ticket))
  })
  vi.stubGlobal('fetch', fetch)
  render(<AdminTickets token="reviewer-token" />)

  fireEvent.click(await screen.findByRole('button', { name: /как согласовать/i }))
  await screen.findByText('Нужен срок')
  fireEvent.change(screen.getByLabelText('Текст практики'), { target: { value: 'Согласовать за день.' } })
  fireEvent.change(screen.getByLabelText('Режим'), { target: { value: 'overrides' } })
  fireEvent.change(screen.getByLabelText('Код документа'), { target: { value: 'agreement' } })
  fireEvent.change(screen.getByLabelText('Редакция'), { target: { value: '2' } })
  fireEvent.change(screen.getByLabelText('Пункт'), { target: { value: '1.3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Принять и опубликовать' }))

  await waitFor(() => expect(screen.getByRole('article')).toHaveTextContent('Публикуется'))
  const action = fetch.mock.calls.find(([, options]) => options.method === 'POST')
  expect(action?.[0].pathname).toMatch(/\/tickets\/ticket\/publish$/)
  expect(JSON.parse(String(action?.[1].body))).toEqual({
    text: 'Согласовать за день.',
    mode: 'overrides',
    binds: [{ doc_id: 'agreement', version: 2, clause: '1.3' }],
  })
})
