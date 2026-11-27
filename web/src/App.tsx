import { lazy, Suspense, useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { Button } from '@/components/ui/button'
import { qcRequest, type Capabilities } from './features/qc/api'
import { AdminTickets, Mine } from './features/qc/TicketViews'
import { AnalyticsView } from './features/qc/AnalyticsView'

type Page = 'chat' | 'mine' | 'admin' | 'analytics'
const navigation: { id: Page; label: string; admin?: boolean }[] = [
  { id: 'chat', label: 'Чат' },
  { id: 'mine', label: 'Мои заявки' },
  { id: 'admin', label: 'Очередь ОКК', admin: true },
  { id: 'analytics', label: 'Аналитика', admin: true },
]
const Chat = lazy(async () => ({ default: (await import('./features/chat/Chat')).Chat }))

export default function App() {
  const auth = useAuth()
  const [actionError, setActionError] = useState<string>()

  if (auth.isLoading || auth.activeNavigator) {
    return <main className="mx-auto max-w-lg px-6 py-16"><p role="status">Проверяем вход…</p></main>
  }

  if (!auth.isAuthenticated) {
    return (
      <main className="grid min-h-dvh place-items-center bg-muted/40 px-6 py-16">
        <section className="w-full max-w-sm rounded-2xl border bg-card p-8 shadow-sm">
          <h1 className="text-3xl font-semibold tracking-tight">RegAgent</h1>
          <p className="mt-3 text-sm text-muted-foreground">
            Войдите в рабочую учётную запись, чтобы искать ответы в регламентах.
          </p>
          {(auth.error || actionError) && (
            <p className="mt-4 text-sm text-destructive" role="alert">
              {actionError ?? 'Не удалось войти. Попробуйте ещё раз.'}
            </p>
          )}
          <Button className="mt-6 w-full" onClick={() => {
            setActionError(undefined)
            void auth.signinRedirect().catch(() => setActionError('Не удалось начать вход.'))
          }}>Войти</Button>
        </section>
      </main>
    )
  }

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-5xl flex-col gap-10 px-6 py-8">
      <header className="flex items-center justify-between gap-4 border-b pb-4">
        <h1 className="text-xl font-semibold tracking-tight">RegAgent</h1>
        <Button variant="outline" onClick={() => {
          void auth.signoutRedirect().catch(() => setActionError('Не удалось выйти.'))
        }}>Выйти</Button>
      </header>
      {actionError && <p className="text-sm text-destructive" role="alert">{actionError}</p>}
      {auth.user?.access_token && (
        <Workspace key={auth.user.profile?.sub} token={auth.user.access_token} onUnauthorized={() => {
          setActionError(undefined)
          void auth.signinRedirect().catch(() => setActionError('Не удалось начать вход.'))
        }} />
      )}
    </main>
  )
}


function Workspace({ token, onUnauthorized }: { token: string; onUnauthorized: () => void }) {
  const [page, setPage] = useState<Page>('chat')
  const [access, setAccess] = useState<{ token: string; permissions: Capabilities }>()
  const permissions = access?.token === token ? access.permissions : undefined
  useEffect(() => {
    const controller = new AbortController()
    void qcRequest<Capabilities>(token, '/me', { signal: controller.signal }).then((result) => {
      if (!controller.signal.aborted) setAccess({ token, permissions: result })
    }).catch(() => {
      if (!controller.signal.aborted) setAccess(undefined)
    })
    return () => controller.abort()
  }, [token])

  const activePage = (page === 'admin' || page === 'analytics') && !permissions?.can_review ? 'chat' : page
  return (
    <div className="space-y-6">
      <nav aria-label="Разделы" className="flex flex-wrap gap-2">
        {navigation.filter((item) => !item.admin || permissions?.can_review).map((item) => (
          <Button key={item.id} variant={activePage === item.id ? 'secondary' : 'ghost'}
            aria-current={activePage === item.id ? 'page' : undefined} onClick={() => setPage(item.id)}>
            {item.label}
          </Button>
        ))}
      </nav>
      <div hidden={activePage !== 'chat'}>
        <Suspense fallback={<p role="status">Загружаю чат…</p>}>
          <Chat token={token} onUnauthorized={onUnauthorized} />
        </Suspense>
      </div>
      {activePage === 'mine' && <Mine token={token} />}
      {activePage === 'admin' && <AdminTickets token={token} />}
      {activePage === 'analytics' && <AnalyticsView token={token} areas={permissions?.areas ?? []} />}
    </div>
  )
}
