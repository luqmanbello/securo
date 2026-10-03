import { screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { CredentialsConnectDialog } from './credentials-connect-dialog'

const api = vi.hoisted(() => ({ handleCallback: vi.fn(), toastError: vi.fn(), toastSuccess: vi.fn() }))
vi.mock('@/lib/api', () => ({ connections: api }))
vi.mock('sonner', () => ({ toast: { error: api.toastError, success: api.toastSuccess } }))

beforeEach(() => { vi.resetAllMocks() })

const BYBIT_FIELDS = [
  { name: 'api_key', label_key: 'accounts.credentialsConnect.bybit.apiKeyLabel', placeholder_key: 'accounts.credentialsConnect.bybit.apiKeyPlaceholder', secret: false },
  { name: 'api_secret', label_key: 'accounts.credentialsConnect.bybit.apiSecretLabel', placeholder_key: 'accounts.credentialsConnect.bybit.apiSecretPlaceholder', secret: true },
]

it('keeps the user ID + password form when a provider declares no fields', async () => {
  api.handleCallback.mockResolvedValue({})
  const { user } = renderWithProviders(<CredentialsConnectDialog open provider="accessbank" onClose={vi.fn()} />)
  const userId = screen.getByLabelText('User ID')
  const password = screen.getByLabelText('Password')
  expect(password).toHaveAttribute('type', 'password')
  await user.type(userId, ' someone ')
  await user.type(password, 'pw')
  await user.click(screen.getByRole('button', { name: 'Connect' }))
  expect(api.handleCallback).toHaveBeenCalledWith(JSON.stringify({ user_id: 'someone', password: 'pw' }), 'accessbank', undefined, undefined, undefined)
})

it('renders the provider fields, masks secrets, and sends them by name', async () => {
  api.handleCallback.mockResolvedValue({})
  const { user } = renderWithProviders(<CredentialsConnectDialog open provider="bybit" fields={BYBIT_FIELDS} onClose={vi.fn()} />)
  const key = screen.getByLabelText('API key')
  const secret = screen.getByLabelText('API secret')
  expect(key).toHaveAttribute('type', 'text')
  expect(secret).toHaveAttribute('type', 'password')
  expect(secret).toHaveAttribute('autocomplete', 'new-password')
  expect(screen.getByText(/read-only API key/i)).toBeInTheDocument()
  const connect = screen.getByRole('button', { name: 'Connect' })
  await user.type(key, ' k-123 ')
  expect(connect).toBeDisabled()
  await user.type(secret, 's-456')
  await user.click(connect)
  expect(api.handleCallback).toHaveBeenCalledWith(JSON.stringify({ api_key: 'k-123', api_secret: 's-456' }), 'bybit', undefined, undefined, undefined)
})
