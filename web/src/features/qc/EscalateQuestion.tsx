import { useRef, useState, type FormEvent } from 'react'
import { Button } from '@/components/ui/button'
import { qcError, qcRequest, type Ticket } from './api'

export function EscalateQuestion({ token, question, submissionId }: {
  token: string
  question: string
  submissionId: string
}) {
  const [opened, setOpened] = useState(false)
  const [comment, setComment] = useState('')
  const [sent, setSent] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending.current) return
    pending.current = true
    setSending(true)
    setError('')
    try {
      await qcRequest<Ticket>(token, '/tickets', { post: {
        submission_id: submissionId,
        question,
        ...(comment.trim() ? { comment: comment.trim() } : {}),
      } })
      setOpened(false)
      setSent(true)
    } catch (problem) {
      setError(qcError(problem))
    } finally {
      pending.current = false
      setSending(false)
    }
  }

  if (sent) return <p role="status" className="text-sm text-muted-foreground">Заявка отправлена</p>
  if (!opened) return (
    <Button type="button" size="sm" variant="ghost" onClick={() => setOpened(true)}>Передать в ОКК</Button>
  )
  return (
    <form onSubmit={(event) => void submit(event)} className="space-y-3 rounded-lg border bg-card p-4 text-left">
      <p className="text-sm">Вопрос будет отправлен в отдел контроля качества.</p>
      <label className="grid gap-1 text-sm font-medium">
        Комментарий (необязательно)
        <textarea className="rounded-md border bg-background p-2" value={comment} maxLength={2000} rows={2}
          onChange={(event) => setComment(event.target.value)} />
      </label>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={sending}>Подтвердить отправку</Button>
        <Button type="button" size="sm" variant="outline" disabled={sending} onClick={() => {
          setOpened(false)
          setError('')
        }}>Отмена</Button>
      </div>
    </form>
  )
}
