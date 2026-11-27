import { lazy, Suspense, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { Button } from '@/components/ui/button'
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
        <Suspense fallback={<p role="status">Загружаю чат…</p>}>
          <Chat
            token={auth.user.access_token}
            onUnauthorized={() => {
              setActionError(undefined)
              void auth.signinRedirect().catch(() => setActionError('Не удалось начать вход.'))
            }}
          />
        </Suspense>
      )}
    </main>
  )
}
