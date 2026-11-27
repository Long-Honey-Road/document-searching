import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { EscalateQuestion } from './EscalateQuestion'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('sends a question only after confirmation and keeps the retry key', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response('{}', { status: 503 }))
    .mockResolvedValueOnce(new Response('{"id":"ticket"}', { status: 201 }))
  vi.stubGlobal('fetch', fetch)
  render(<EscalateQuestion token="employee-token" question="Порядок согласования?" submissionId="msg-1" />)

  fireEvent.click(screen.getByRole('button', { name: 'Передать в ОКК' }))
  expect(fetch).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Комментарий (необязательно)'), { target: { value: 'Срок?' } })
  fireEvent.click(screen.getByRole('button', { name: 'Подтвердить отправку' }))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: 'Подтвердить отправку' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Заявка отправлена'))
  expect(fetch).toHaveBeenCalledTimes(2)
  const sent = fetch.mock.calls.map(([, init]) => JSON.parse(init.body))
  expect(sent).toEqual(Array(2).fill({
    submission_id: 'msg-1', question: 'Порядок согласования?', comment: 'Срок?',
  }))
})
