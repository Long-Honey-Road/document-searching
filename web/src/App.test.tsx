import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from './App'

const { auth } = vi.hoisted(() => ({
  auth: {
    isAuthenticated: false,
    isLoading: false,
    signinRedirect: vi.fn().mockResolvedValue(undefined),
  },
}))

vi.mock('react-oidc-context', () => ({ useAuth: () => auth }))

test('shows a sign-in button when unauthenticated', () => {
  render(<App />)
  screen.getByRole('button', { name: 'Войти' }).click()
  expect(auth.signinRedirect).toHaveBeenCalledOnce()
})
