import { useEffect, useState, type FormEvent } from 'react'
import { Button } from '@/components/ui/button'
import { outcomeLabels, qcError, qcRequest, ticketLabels, type AnalyticsReport, type Ticket } from './api'

const field = 'rounded-md border bg-background px-3 py-2 text-sm'
const pageSize = 20

export function AnalyticsView({ token, areas }: { token: string; areas: string[] }) {
  const [query, setQuery] = useState('days=30')
  const [page, setPage] = useState(0)
  const [updates, setUpdates] = useState(0)
  const [loading, setLoading] = useState(true)
  const [report, setReport] = useState<AnalyticsReport>()
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [pending, setPending] = useState('')
  const [created, setCreated] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const url = '/admin/analytics?' + query + '&limit=' + (pageSize + 1) + '&offset=' + page * pageSize
    void qcRequest<AnalyticsReport>(token, url, { signal: controller.signal }).then((response) => {
      if (controller.signal.aborted) return
      setReport(response)
      setLoadError('')
      setLoading(false)
    }).catch((problem: unknown) => {
      if (!controller.signal.aborted) {
        setLoadError(qcError(problem))
        setLoading(false)
      }
    })
    return () => controller.abort()
  }, [token, query, page, updates])

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const params = new URLSearchParams()
    new FormData(event.currentTarget).forEach((value, key) => {
      if (typeof value === 'string' && value) params.set(key, value)
    })
    setQuery(params.toString())
    setPage(0)
    setLoading(true)
    setUpdates((count) => count + 1)
  }

  async function createTicket(runId: string) {
    if (pending) return
    setPending(runId)
    setCreated('')
    setActionError('')
    try {
      await qcRequest<Ticket>(token, '/admin/analytics/' + runId + '/ticket', { post: {} })
      setCreated(runId)
      setUpdates((count) => count + 1)
    } catch (problem) {
      setActionError(qcError(problem))
    } finally {
      setPending('')
    }
  }

  return (
    <section className="space-y-5">
      <form onSubmit={apply} className="flex flex-wrap items-end gap-3">
        <label className="grid gap-1 text-sm font-medium">
          Период, дней
          <input className={field + ' w-28'} type="number" name="days" min={1} max={365} defaultValue={30} required />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          Область
          <select className={field} name="area">
            <option value="">Все области</option>
            {areas.map((area) => <option key={area} value={area}>{area}</option>)}
          </select>
        </label>
        <label className="grid gap-1 text-sm font-medium">
          Исход
          <select className={field} name="outcome">
            <option value="">Все исходы</option>
            {Object.entries(outcomeLabels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>
        <label className="grid gap-1 text-sm font-medium">
          Статус заявки
          <select className={field} name="ticket_status">
            <option value="">Все статусы</option>
            {Object.entries(ticketLabels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>
        <Button type="submit">Применить</Button>
      </form>
      {loading && <p role="status" className="text-sm text-muted-foreground">Загружается сводка…</p>}
      {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
      {report && !loading && !loadError && (
        <>
          <dl className="flex flex-wrap gap-x-7 gap-y-3 rounded-lg border p-4 text-sm">
            <div><dt className="text-muted-foreground">Всего ходов</dt><dd className="font-semibold">{report.total}</dd></div>
            <div><dt className="text-muted-foreground">С использованием практики</dt><dd className="font-semibold">{report.used_practice}</dd></div>
            {Object.entries(report.by_outcome).map(([id, count]) => (
              <div key={id}><dt className="text-muted-foreground">{outcomeLabels[id as keyof typeof outcomeLabels]}</dt>
                <dd className="font-semibold">{count}</dd></div>
            ))}
          </dl>
          <h3 className="font-semibold">Неотвеченные вопросы</h3>
          {actionError && <p role="alert" className="text-sm text-destructive">{actionError}</p>}
          {report.groups.length === 0 && <p className="text-sm text-muted-foreground">Вопросов за этот период нет.</p>}
          <ul className="divide-y rounded-lg border">
            {report.groups.slice(0, pageSize).map((item) => (
              <li key={item.run_id} className="flex flex-wrap items-start justify-between gap-3 p-4">
                <div className="min-w-0 space-y-1">
                  <p className="whitespace-pre-wrap break-words text-sm font-medium">{item.question}</p>
                  <p className="text-sm text-muted-foreground">
                    {item.area ?? 'Область не определена'} · {item.count} повт. · {outcomeLabels[item.outcome]}
                    {item.used_practice && ' · использована практика'}
                    {item.ticket_status && ' · ' + ticketLabels[item.ticket_status]}
                  </p>
                  {item.fragment_ids.length > 0 && (
                    <details className="text-xs text-muted-foreground">
                      <summary className="cursor-pointer">Просмотренные фрагменты</summary>
                      <ul>{item.fragment_ids.map((fragment) => <li key={fragment} className="break-all">{fragment}</li>)}</ul>
                    </details>
                  )}
                </div>
                {created === item.run_id
                  ? <p role="status" className="text-sm">Заявка создана</p>
                  : <Button variant="outline" size="sm" disabled={Boolean(pending)} onClick={() => void createTicket(item.run_id)}>Создать заявку</Button>}
              </li>
            ))}
          </ul>
          {(page > 0 || report.groups.length > pageSize) &&
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" disabled={loading || page === 0} onClick={() => {
                setLoading(true)
                setPage(page - 1)
              }}>Назад</Button>
              <Button size="sm" variant="outline" disabled={loading || report.groups.length <= pageSize} onClick={() => {
                setLoading(true)
                setPage(page + 1)
              }}>Дальше</Button>
            </div>}
        </>
      )}
    </section>
  )
}
