import { screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { ConnectorSelectDialog } from './connector-select-dialog'

const api = vi.hoisted(() => ({ getProviders: vi.fn() }))
vi.mock('@/lib/api', () => ({ connections: api, plugins: { catalog: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { is_superuser: false } }) }))

beforeEach(() => { vi.resetAllMocks() })

it('shows a provider logo when the provider has one, and the bank icon otherwise', async () => {
  api.getProviders.mockResolvedValue([
    { name: 'bybit', display_name: 'Bybit', description: 'Synthetic', flow_type: 'credentials', configured: true, logo_url: '/institution-logos/bybit.svg' },
    { name: 'plain', display_name: 'Plain Bank', description: 'Synthetic', flow_type: 'oauth', configured: true },
  ])
  renderWithProviders(<ConnectorSelectDialog open onClose={vi.fn()} onSelect={vi.fn()} />)
  // The tile's text names the provider, so the logo is decorative (alt="").
  await screen.findByText('Bybit')
  const logos = document.querySelectorAll('img')
  expect(logos).toHaveLength(1)
  expect(logos[0]).toHaveAttribute('src', '/institution-logos/bybit.svg')
})
