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

test('marks a fallback as a technical message', () => {
  render(<Answer text="Не удалось надёжно ответить по регламентам." meta={{ ...meta, outcome: 'fallback', sources: [] }} />)
  expect(screen.getByText('Техническое сообщение')).toBeInTheDocument()
})

test('replaces the draft and only links verified citations', () => {
  const { rerender } = render(<Answer text="Черновик [f:dib@3#2.7]." />)
  expect(screen.getByText(/Черновик/)).toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  rerender(<Answer text="Проверенный ответ [f:dib@3#2.7]." meta={meta} />)
  expect(screen.queryByText(/Черновик/)).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: '1' })).toBeInTheDocument()
})

test('omits edition for a practice source without a version', () => {
  const practice = {
    ...meta,
    sources: [{ ...meta.sources[0], fragment_id: 'practice:14', title: 'dib-project-activity', kind: 'practice', version: null }],
  } as const
  render(<Answer text="Итог [f:practice:14]." meta={practice} />)
  const target = screen.getByRole('link', { name: '1' }).getAttribute('href')?.slice(1)
  expect(document.getElementById(target ?? '')).toHaveTextContent('Практика ОКК')
  expect(document.getElementById(target ?? '')).toHaveTextContent('к п. 2.7')
  expect(document.getElementById(target ?? '')).not.toHaveTextContent('dib-project-activity')
  expect(document.getElementById(target ?? '')).not.toHaveTextContent('ред.')
})
