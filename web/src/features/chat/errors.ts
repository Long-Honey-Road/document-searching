/** Ошибки загрузки и отправки в чате: каждому случаю — свой понятный текст. */

export type ChatProblem =
  | { kind: 'unauthorized'; status: 401 }
  | { kind: 'forbidden'; status: 403 }
  | { kind: 'rate_limited'; status: 429 }
  | { kind: 'unavailable'; status?: number }
  | { kind: 'server'; status: number }
  | { kind: 'http'; status: number }
  | { kind: 'no_assistant' }
  | { kind: 'many_assistants' }
  | { kind: 'unknown' }

function statusOf(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null || !('status' in error)) return
  const status = (error as { status: unknown }).status
  return typeof status === 'number' ? status : undefined
}

export function classifyError(error: unknown): ChatProblem {
  const status = statusOf(error)
  if (status === undefined) {
    // fetch не дошёл до сервера (сеть, DNS, CORS, сервис лежит) — браузер даёт TypeError
    return error instanceof TypeError ? { kind: 'unavailable' } : { kind: 'unknown' }
  }
  if (status === 401) return { kind: 'unauthorized', status }
  if (status === 403) return { kind: 'forbidden', status }
  if (status === 429) return { kind: 'rate_limited', status }
  if (status === 502 || status === 503 || status === 504) return { kind: 'unavailable', status }
  if (status >= 500) return { kind: 'server', status }
  return { kind: 'http', status }
}

export function problemText(problem: ChatProblem): string {
  switch (problem.kind) {
    case 'unauthorized':
      return 'Сервер не принял вход: сессия истекла или недействительна. Войдите снова.'
    case 'forbidden':
      return 'У вашей учётной записи нет доступа к чату. Обратитесь к администратору.'
    case 'rate_limited':
      return 'Слишком много запросов. Подождите минуту и попробуйте снова.'
    case 'unavailable':
      return problem.status
        ? `Сервер временно недоступен (код ${problem.status}). Попробуйте позже.`
        : 'Сервер недоступен: нет связи. Проверьте подключение и обновите страницу.'
    case 'server':
      return `Ошибка на сервере (код ${problem.status}). Попробуйте позже.`
    case 'http':
      return `Сервер ответил неожиданно (код ${problem.status}). Сообщите администратору.`
    case 'no_assistant':
      return 'Агент не настроен на сервере: ассистент не найден. Сообщите администратору.'
    case 'many_assistants':
      return 'На сервере несколько ассистентов, а чат не знает, какой выбрать. Сообщите администратору.'
    case 'unknown':
      return 'Непредвиденная ошибка чата. Обновите страницу.'
  }
}
