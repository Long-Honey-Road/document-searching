export const ticketLabels = {
  new: 'Новая',
  publishing: 'Публикуется',
  published: 'Опубликована',
  rejected: 'Отклонена',
  publish_failed: 'Ошибка публикации',
}
export type TicketStatus = keyof typeof ticketLabels

export const outcomeLabels = {
  answer: 'Есть ответ',
  empty_search: 'Пустой поиск',
  insufficient_evidence: 'Мало подтверждений',
  clarification: 'Нужно уточнение',
  access_limited: 'Ограничен доступ',
  search_unavailable: 'Поиск недоступен',
  model_timeout: 'Таймаут модели',
  refusal: 'Отказ',
  error: 'Сбой',
}
export type Outcome = keyof typeof outcomeLabels

export type Ticket = {
  id: string
  question: string
  status: TicketStatus
  created_at: string
  updated_at: string
}
export type Binding = { doc_id: string; version: number; clause: string }
export type Draft = { text: string; mode: 'clarifies' | 'overrides'; binds: Binding[] }
export type ReviewSummary = Ticket & { kind: 'employee' | 'analytics' | 'recheck' }
export type ReviewTicket = ReviewSummary & {
  comment: string | null
  draft: Draft | null
  history: { status: TicketStatus; actor_id: string; at: string; error_code: string | null }[]
  recheck: { reason: 'new_version' | 'access_changed' | 'deleted' | 'content_changed'; doc_id: string; version: number | null } | null
}
export type Capabilities = { can_review: boolean; areas: string[] }
export type AnalyticsReport = {
  total: number
  used_practice: number
  by_outcome: Partial<Record<Outcome, number>>
  groups: {
    run_id: string
    question: string
    area: string | null
    count: number
    outcome: Outcome
    reason: string | null
    fragment_ids: string[]
    used_practice: boolean
    ticket_status: TicketStatus | null
  }[]
}

export async function qcRequest<T>(token: string, path: string, options?: {
  post?: object
  signal?: AbortSignal
}): Promise<T> {
  const root = (import.meta.env.VITE_API_URL || '/api').replace(/\/$/, '')
  const headers: Record<string, string> = { Authorization: 'Bearer ' + token }
  if (options?.post) headers['Content-Type'] = 'application/json'
  const response = await fetch(new URL(root + '/qc' + path, window.location.origin), {
    method: options?.post ? 'POST' : 'GET',
    body: options?.post ? JSON.stringify(options.post) : undefined,
    signal: options?.signal,
    headers,
    cache: 'no-store',
  })
  if (!response.ok) throw Object.assign(new Error('qc request failed'), { status: response.status })
  return await response.json() as T
}

export function qcError(error: unknown): string {
  const status = typeof error === 'object' && error != null && 'status' in error ? error.status : 0
  switch (status) {
    case 401: return 'Сессия истекла. Войдите снова.'
    case 403: return 'У вашей учётной записи нет доступа.'
    case 404: return 'Запись недоступна.'
    case 409: return 'Статус уже изменился. Обновите список.'
    case 422: return 'Проверьте текст и данные привязок.'
    default: return 'Сервис ОКК недоступен. Попробуйте ещё раз.'
  }
}

export function formatDate(value: string): string {
  return new Date(value).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })
}
