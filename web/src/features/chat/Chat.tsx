import { Client, type Message, type Thread } from '@langchain/langgraph-sdk'
import { useStream } from '@langchain/langgraph-sdk/react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Button } from '@/components/ui/button'
import { Answer, type AnswerMeta } from './Answer'

type ChatState = { messages: Message[] }

function getText(message: Message) {
  if (typeof message.content === 'string') return message.content
  return message.content.map((part) => part.type === 'text' ? part.text : '').join('')
}

function getAnswerMeta(message: Message): AnswerMeta | undefined {
  const metadata = message.additional_kwargs
  const candidate = (metadata?.answer_meta ?? metadata) as Partial<AnswerMeta> | undefined
  if (candidate?.verified !== true || !Array.isArray(candidate.sources)) return
  return candidate as AnswerMeta
}

function Conversation({
  client, assistantId, threadId, setThreadId, reloadThreads,
}: {
  client: Client
  assistantId: string
  threadId: string | null
  setThreadId: (id: string) => void
  reloadThreads: () => void
}) {
  const stream = useStream<ChatState>({
    client,
    assistantId,
    threadId,
    onThreadId: setThreadId,
    fetchStateHistory: false,
    onFinish: reloadThreads,
  })
  const [question, setQuestion] = useState('')
  const [sendError, setSendError] = useState(false)
  const messages = stream.messages.filter((message) =>
    message.type === 'human' ||
    (message.type === 'ai' && Boolean(getAnswerMeta(message))),
  )
  const latest = stream.messages.at(-1)
  const draft = stream.isLoading && latest?.type === 'ai' && !latest.tool_calls?.length &&
    !getAnswerMeta(latest) ? getText(latest) : ''

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const content = question.trim()
    if (!content || stream.isLoading) return
    setSendError(false)
    setQuestion('')
    try {
      await stream.submit({ messages: [{ type: 'human', content }] })
    } catch {
      setQuestion(content)
      setSendError(true)
    }
  }

  return (
    <section aria-label="Чат" className="min-w-0 space-y-5">
      {messages.length === 0 && !stream.isLoading && (
        <p className="text-sm text-muted-foreground">Задайте вопрос по регламентам.</p>
      )}
      <div className="space-y-4" aria-live="polite">
        {messages.map((message, index) => message.type === 'human' ? (
          <p key={message.id ?? index} className="ml-auto max-w-prose whitespace-pre-wrap rounded-xl bg-primary px-4 py-3 text-primary-foreground">
            {getText(message)}
          </p>
        ) : (
          <Answer key={message.id ?? index} text={getText(message)} meta={getAnswerMeta(message)} />
        ))}
      </div>
      {draft && <Answer text={draft} />}
      {stream.isLoading && <p role="status" className="text-sm text-muted-foreground">Ищу в регламентах…</p>}
      {(sendError || Boolean(stream.error)) && (
        <p role="alert" className="text-sm text-destructive">Не удалось получить ответ. Попробуйте ещё раз.</p>
      )}
      <form onSubmit={(event) => void submit(event)} className="flex flex-col items-start gap-3 sm:flex-row sm:items-end">
        <label className="grid w-full gap-1.5 text-sm font-medium">
          Вопрос по регламентам
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            rows={2}
            className="w-full resize-y rounded-lg border bg-background px-3 py-2"
          />
        </label>
        <Button type="submit" disabled={!question.trim() || stream.isLoading}>Отправить</Button>
      </form>
    </section>
  )
}

export function Chat({ token }: { token: string }) {
  const client = useMemo(() => new Client({
    apiUrl: new URL(import.meta.env.VITE_API_URL || '/api', window.location.origin).href,
    defaultHeaders: { Authorization: 'Bearer ' + token },
  }), [token])
  const [assistantId, setAssistantId] = useState<string>(import.meta.env.VITE_ASSISTANT_ID ?? '')
  const [threadId, setThreadId] = useState<string | null>(null)
  const [threads, setThreads] = useState<Thread[]>([])
  const [loadingError, setLoadingError] = useState(false)
  const [threadUpdates, setThreadUpdates] = useState(0)
  const reloadThreads = () => setThreadUpdates((count) => count + 1)

  useEffect(() => {
    if (assistantId) return
    let active = true
    void client.assistants.search({ limit: 2 }).then((assistants) => {
      if (!active) return
      if (assistants.length === 1) setAssistantId(assistants[0].assistant_id)
      else setLoadingError(true)
    }).catch(() => { if (active) setLoadingError(true) })
    return () => { active = false }
  }, [assistantId, client])

  useEffect(() => {
    let active = true
    void client.threads.search({ sortBy: 'updated_at', sortOrder: 'desc' }).then((items) => {
      if (active) setThreads(items)
    }).catch(() => { if (active) setLoadingError(true) })
    return () => { active = false }
  }, [client, threadUpdates])

  return (
    <div className="grid gap-8 sm:grid-cols-[12rem_minmax(0,1fr)]">
      <nav aria-label="Диалоги" className="space-y-3">
        <Button variant="outline" onClick={() => setThreadId(null)}>Новый диалог</Button>
        <ul className="space-y-1">
          {threads.map((thread) => (
            <li key={thread.thread_id}>
              <Button variant={thread.thread_id === threadId ? 'secondary' : 'ghost'} onClick={() => setThreadId(thread.thread_id)}>
                {new Date(thread.created_at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })}
              </Button>
            </li>
          ))}
        </ul>
      </nav>
      <div className="min-w-0 space-y-3">
        {loadingError && <p role="alert" className="text-sm text-destructive">Не удалось загрузить данные чата.</p>}
        {assistantId ? (
          <Conversation {...{ client, assistantId, threadId, setThreadId, reloadThreads }} />
        ) : !loadingError && (
          <p role="status" className="text-sm text-muted-foreground">Подключаюсь к агенту…</p>
        )}
      </div>
    </div>
  )
}
