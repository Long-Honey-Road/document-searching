import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { InMemoryWebStorage, WebStorageStateStore } from 'oidc-client-ts'
import { AuthProvider } from 'react-oidc-context'
import App from './App'
import './index.css'

const authority = import.meta.env.VITE_OIDC_AUTHORITY
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID
const appUrl = new URL('/', window.location.href).href

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {authority && clientId ? (
      <AuthProvider
        authority={authority}
        client_id={clientId}
        redirect_uri={appUrl}
        post_logout_redirect_uri={appUrl}
        response_type="code"
        scope="openid profile"
        userStore={new WebStorageStateStore({ store: new InMemoryWebStorage() })}
        stateStore={new WebStorageStateStore({ store: window.sessionStorage })}
        onSigninCallback={() => window.history.replaceState({}, '', appUrl)}
      >
        <App />
      </AuthProvider>
    ) : (
      <main className="mx-auto max-w-lg px-6 py-16">
        <p role="alert">Не заданы настройки входа.</p>
      </main>
    )}
  </StrictMode>,
)
