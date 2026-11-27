import { useEffect, useState, type FormEvent } from 'react'
import { Button } from '@/components/ui/button'
import { formatDate, qcError, qcRequest, ticketLabels, type Draft, type ReviewSummary, type ReviewTicket,
  type Ticket, type TicketStatus } from './api'

const field = 'w-full rounded-md border bg-background px-3 py-2 text-sm'
const pageSize = 25

function useTicketPage<T extends Ticket>(token: string, path: string) {
  const [items, setItems] = useState<T[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updates, setUpdates] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    void qcRequest<T[]>(token, path, { signal: controller.signal }).then((response) => {
      if (controller.signal.aborted) return
      setItems(response)
      setError('')
      setLoading(false)
    }).catch((problem: unknown) => {
      if (!controller.signal.aborted) {
        setError(qcError(problem))
        setLoading(false)
      }
    })
    return () => controller.abort()
  }, [token, path, updates])
  return { items, loading, error, refresh: () => { setLoading(true); setUpdates((value) => value + 1) } }
}

function Pager({ page, more, onPage }: { page: number; more: boolean; onPage: (page: number) => void }) {
  return (
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" disabled={page === 0} onClick={() => onPage(page - 1)}>Назад</Button>
      <Button variant="outline" size="sm" disabled={!more} onClick={() => onPage(page + 1)}>Дальше</Button>
    </div>
  )
}

export function Mine({ token }: { token: string }) {
  const [page, setPage] = useState(0)
  const { items, loading, error, refresh } = useTicketPage<Ticket>(
    token, '/tickets?limit=' + (pageSize + 1) + '&offset=' + (page * pageSize),
  )
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold">Мои заявки</h2>
        <Button variant="outline" size="sm" onClick={refresh}>Обновить</Button>
      </div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {loading && <p role="status" className="text-sm text-muted-foreground">Загружаются заявки…</p>}
      {!loading && !error && items.length === 0 && <p className="text-sm text-muted-foreground">Заявок пока нет.</p>}
      <ul className="divide-y rounded-lg border">
        {items.slice(0, pageSize).map((ticket) => (
          <li key={ticket.id} className="flex flex-wrap items-start justify-between gap-3 p-4">
            <p className="whitespace-pre-wrap break-words">{ticket.question}</p>
            <div className="text-sm">
              <p>{ticketLabels[ticket.status]}</p>
              <p className="text-muted-foreground">{formatDate(ticket.updated_at)}</p>
            </div>
          </li>
        ))}
      </ul>
      {(page > 0 || items.length > pageSize) &&
        <Pager page={page} more={items.length > pageSize} onPage={setPage} />}
    </section>
  )
}

export function AdminTickets({ token }: { token: string }) {
  const [status, setStatus] = useState<TicketStatus | ''>('')
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<string>()
  const [revision, setRevision] = useState(0)
  const { items, loading, error, refresh } = useTicketPage<ReviewSummary>(
    token, '/admin/tickets?' + new URLSearchParams({
      limit: String(pageSize + 1), offset: String(page * pageSize), ...(status ? { status } : {}),
    }),
  )

  function refreshReview() {
    refresh()
    setRevision((count) => count + 1)
  }

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="grid gap-1 text-sm font-medium">
          Статус заявки
          <select className={field} value={status} onChange={(event) => {
            setStatus(event.target.value as TicketStatus | '')
            setPage(0)
          }}>
            <option value="">Все статусы</option>
            {Object.entries(ticketLabels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>
        <Button variant="outline" size="sm" onClick={refreshReview}>Обновить</Button>
      </div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {loading && <p role="status" className="text-sm text-muted-foreground">Загружаются заявки…</p>}
      {!loading && !error && items.length === 0 && <p className="text-sm text-muted-foreground">Заявок не найдено.</p>}
      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className="min-w-0 space-y-3">
          <ul className="divide-y rounded-lg border">
            {items.slice(0, pageSize).map((ticket) => (
              <li key={ticket.id}>
                <button type="button" onClick={() => setSelected(ticket.id)}
                  aria-pressed={selected === ticket.id}
                  className="block w-full space-y-1 p-3 text-left hover:bg-muted aria-pressed:bg-muted">
                  <span className="block break-words text-sm font-medium">{ticket.question}</span>
                  <span className="block text-xs text-muted-foreground">
                    {ticket.kind === 'recheck' && 'Перепроверка · '}{ticketLabels[ticket.status]}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {(page > 0 || items.length > pageSize) &&
            <Pager page={page} more={items.length > pageSize} onPage={setPage} />}
        </div>
        {selected && <ReviewDetails key={selected} token={token} ticketId={selected}
          revision={revision} onChange={refresh} />}
      </div>
    </section>
  )
}

function ReviewDetails({ token, ticketId, revision, onChange }: {
  token: string
  ticketId: string
  revision: number
  onChange: () => void
}) {
  const [ticket, setTicket] = useState<ReviewTicket>()
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    void qcRequest<ReviewTicket>(token, '/admin/tickets/' + ticketId, { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted) return
        setTicket(response)
        setError('')
      }).catch((problem: unknown) => {
        if (controller.signal.aborted) return
        setTicket(undefined)
        setError(qcError(problem))
      })
    return () => controller.abort()
  }, [token, ticketId, revision])

  if (error) return <p role="alert" className="text-sm text-destructive">{error}</p>
  if (!ticket) return <p role="status" className="text-sm text-muted-foreground">Открывается заявка…</p>
  return <Review token={token} ticket={ticket} onChange={(next) => {
    setTicket(next)
    onChange()
  }} />
}

type InputBinding = { key: string; doc_id: string; version: string; clause: string }
const recheckLabels = {
  new_version: 'Новая редакция',
  access_changed: 'Изменился доступ',
  deleted: 'Документ удалён',
}

function Review({ token, ticket, onChange }: {
  token: string
  ticket: ReviewTicket
  onChange: (ticket: ReviewTicket) => void
}) {
  const [text, setText] = useState(ticket.draft?.text ?? '')
  const [mode, setMode] = useState<Draft['mode']>(ticket.draft?.mode ?? 'clarifies')
  const [binds, setBinds] = useState<InputBinding[]>(() => ticket.draft?.binds.map((item) => ({
    ...item, key: crypto.randomUUID(), version: String(item.version),
  })) ?? [{ key: crypto.randomUUID(), doc_id: '', version: '', clause: '' }])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function act(action: 'publish' | 'reject') {
    if (saving) return
    setError('')
    setSaving(true)
    try {
      const updated = await qcRequest<ReviewTicket>(token, '/admin/tickets/' + ticket.id + '/' + action, {
        post: action === 'publish' ? {
          text, mode, binds: binds.map(({ doc_id, version, clause }) => ({ doc_id, version: Number(version), clause })),
        } : {},
      })
      onChange(updated)
    } catch (problem) {
      setError(qcError(problem))
    } finally {
      setSaving(false)
    }
  }

  function changeBind(key: string, property: 'doc_id' | 'version' | 'clause', value: string) {
    setBinds((items) => items.map((item) => item.key === key ? { ...item, [property]: value } : item))
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void act('publish')
  }

  return (
    <article className="min-w-0 space-y-4 rounded-lg border p-4">
      <h3 className="whitespace-pre-wrap break-words font-semibold">{ticket.question}</h3>
      {ticket.comment && <p className="whitespace-pre-wrap break-words text-sm">{ticket.comment}</p>}
      {ticket.recheck && <p className="break-words text-sm">
        {recheckLabels[ticket.recheck.reason]} · {ticket.recheck.doc_id}
        {ticket.recheck.version != null && ' · редакция ' + ticket.recheck.version}
      </p>}
      <p className="text-sm font-medium">{ticketLabels[ticket.status]}</p>
      <details className="space-y-2 text-sm">
        <summary className="cursor-pointer">История</summary>
        <ol className="space-y-1 text-muted-foreground">
          {ticket.history.map((entry) => (
            <li key={entry.status} className="break-all">
              {formatDate(entry.at)} · {ticketLabels[entry.status]} · {entry.actor_id}
              {entry.error_code && ' · ' + entry.error_code}
            </li>
          ))}
        </ol>
      </details>
      {ticket.status === 'new' ? (
        <form onSubmit={submit} className="space-y-3">
          <label className="grid gap-1 text-sm font-medium">
            Текст практики
            <textarea className={field} value={text} required rows={4} maxLength={12000}
              onChange={(event) => setText(event.target.value)} />
          </label>
          <label className="grid gap-1 text-sm font-medium">
            Режим
            <select className={field} value={mode} onChange={(event) => setMode(event.target.value as Draft['mode'])}>
              <option value="clarifies">Уточняет регламент</option>
              <option value="overrides">Имеет приоритет над регламентом</option>
            </select>
          </label>
          <fieldset className="space-y-3">
            <legend className="mb-1 text-sm font-medium">Привязки к регламентам</legend>
            {binds.map((binding) => (
              <div key={binding.key} className="grid gap-2 rounded-md border p-3 sm:grid-cols-3">
                <label className="grid gap-1 text-sm">Код документа
                  <input className={field} required maxLength={160} value={binding.doc_id}
                    onChange={(event) => changeBind(binding.key, 'doc_id', event.target.value)} />
                </label>
                <label className="grid gap-1 text-sm">Редакция
                  <input className={field} type="number" required min={1} step={1} value={binding.version}
                    onChange={(event) => changeBind(binding.key, 'version', event.target.value)} />
                </label>
                <label className="grid gap-1 text-sm">Пункт
                  <input className={field} required maxLength={160} value={binding.clause}
                    onChange={(event) => changeBind(binding.key, 'clause', event.target.value)} />
                </label>
                {binds.length > 1 && <Button type="button" variant="ghost" size="sm"
                  onClick={() => setBinds((items) => items.filter((item) => item.key !== binding.key))}>
                  Удалить привязку
                </Button>}
              </div>
            ))}
            <Button type="button" variant="outline" size="sm" disabled={binds.length >= 20}
              onClick={() => setBinds((items) => [...items, { key: crypto.randomUUID(), doc_id: '', version: '', clause: '' }])}>
              Добавить привязку
            </Button>
          </fieldset>
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={saving || !text.trim()}>Принять и опубликовать</Button>
            <Button type="button" variant="outline" disabled={saving} onClick={() => void act('reject')}>Отклонить</Button>
          </div>
        </form>
      ) : ticket.draft && (
        <div className="space-y-2 text-sm">
          <p className="whitespace-pre-wrap break-words">{ticket.draft.text}</p>
          <ul className="space-y-1 text-muted-foreground">
            {ticket.draft.binds.map((item) => (
              <li key={JSON.stringify(item)}>{item.doc_id} · редакция {item.version} · пункт {item.clause}</li>
            ))}
          </ul>
        </div>
      )}
    </article>
  )
}
