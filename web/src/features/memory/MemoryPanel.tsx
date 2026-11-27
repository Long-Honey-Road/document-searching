import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { classifyError, problemText, type ChatProblem } from '../chat/errors'

type MemoryRecord = { key: string; value: string; confirmed_at: string }
type Signal = { area: string; at: string }
type Progress = {
  current: string | null
  current_confirmed_at: string | null
  days_until_stale: number | null
  pending_area: string | null
  pending_count: number
  pending_needed: number
}
type Memory = { records: MemoryRecord[]; signals: Signal[]; progress: Progress }

class HttpStatusError extends Error {
  status: number
  constructor(status: number) {
    super(`HTTP ${status}`)
    this.status = status
  }
}

const time = (value: string) =>
  new Date(value).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })

/** Что агент помнит об области и сколько прямых указаний осталось до записи или смены (D-55). */
export function MemoryPanel({ apiUrl, token, refreshKey }: {
  apiUrl: string
  token: string
  refreshKey: number
}) {
  const [memory, setMemory] = useState<Memory | null>(null)
  const [problem, setProblem] = useState<ChatProblem | null>(null)
  const [change, setChange] = useState<string | null>(null)
  const previous = useRef<string | null | undefined>(undefined)
  const base = apiUrl.replace(/\/$/, '') + '/me/memory'

  const request = useCallback(async (url: string, method = 'GET') => {
    const response = await fetch(url, { method, headers: { Authorization: 'Bearer ' + token } })
    if (!response.ok) throw new HttpStatusError(response.status)
    return response.status === 204 ? null : (await response.json() as Memory)
  }, [token])

  const load = useCallback(async () => {
    try {
      const next = await request(base)
      if (!next) return
      const current = next.progress.current
      if (previous.current !== undefined && previous.current !== current) {
        setChange(`Память изменилась: ${previous.current ?? 'нет области'} → ${current ?? 'нет области'}`)
      }
      previous.current = current
      setMemory(next)
      setProblem(null)
    } catch (error) {
      setProblem(classifyError(error))
    }
  }, [base, request])

  useEffect(() => { void load() }, [load, refreshKey])

  async function remove(key: string) {
    try {
      await request(base + '/' + encodeURIComponent(key), 'DELETE')
      await load()
    } catch (error) {
      setProblem(classifyError(error))
    }
  }

  if (problem) {
    return <p role="alert" className="text-sm text-destructive">{problemText(problem)}</p>
  }
  if (!memory) return <p className="text-sm text-muted-foreground">Загружаю память…</p>
  const { progress } = memory
  const currentKey = memory.records.find((record) => record.value === progress.current)?.key

  return (
    <section aria-label="Память" className="space-y-3 rounded-lg border p-3 text-sm">
      {change && <p role="status" className="font-medium text-primary">{change}</p>}
      {progress.current ? (
        <p>
          Текущая область: <strong>{progress.current}</strong>
          {progress.days_until_stale !== null && ` (ещё ${progress.days_until_stale} дн.)`}
        </p>
      ) : (
        <p className="text-muted-foreground">
          Области в памяти нет: агент ищет по всем областям и уточняет при расхождении.
        </p>
      )}
      {progress.pending_area && (
        <p>
          {progress.pending_area}: {progress.pending_count} из {progress.pending_needed}{' '}
          {progress.current ? 'до смены области' : 'до запоминания'}
        </p>
      )}
      {memory.signals.length > 0 && (
        <details>
          <summary className="cursor-pointer text-muted-foreground">Ваши указания области</summary>
          <ul className="mt-1 space-y-0.5">
            {memory.signals.slice().reverse().map((signal) => (
              <li key={signal.at + signal.area}>{time(signal.at)} — {signal.area}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="flex flex-wrap gap-2">
        {currentKey && (
          <Button variant="outline" onClick={() => void remove(currentKey)}>Забыть область</Button>
        )}
        {memory.signals.length > 0 && (
          <Button variant="ghost" onClick={() => void remove('signals:area')}>Сбросить счёт</Button>
        )}
      </div>
    </section>
  )
}
