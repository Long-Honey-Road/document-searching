import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import { Answer } from './Answer'

afterEach(cleanup)

const meta = {
  verified: true,
  area_used: 'ДИБ',
  area_source: 'memory',
  sources: [
    { n: 1, fragment_id: 'dib@3#2.7', title: 'Проектная деятельность ДИБ', clause: '2.7', version: 3, kind: 'regulation' },
  ],
  outcome: 'answer',
} as const

test('links citations to source cards', () => {
  render(<Answer text="РП назначает координатор [f:dib@3#2.7]." meta={meta} />)
  const link = screen.getByRole('link', { name: '1' })
  const target = link.getAttribute('href')?.slice(1)
  expect(document.getElementById(target ?? '')).toHaveTextContent('Проектная деятельность ДИБ')
  expect(document.getElementById(target ?? '')).toHaveTextContent('п. 2.7')
})

test('shows the chosen area', () => {
  render(<Answer text="Координатор назначает РП." meta={meta} />)
  expect(screen.getByText('Область: ДИБ')).toBeInTheDocument()
})

test('replaces the draft and only links verified citations', () => {
  const { rerender } = render(<Answer text="Черновик [f:dib@3#2.7]." />)
  expect(screen.getByText(/Черновик/)).toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  rerender(<Answer text="Проверенный ответ [f:dib@3#2.7]." meta={meta} />)
  expect(screen.queryByText(/Черновик/)).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: '1' })).toBeInTheDocument()
})
