import { beforeEach, describe, expect, it, vi } from 'vitest'

import { host } from '@/sdk'
import { $gateway } from '@/store/gateway'

const restMocks = vi.hoisted(() => ({
  pluginRest: vi.fn(async (_pluginId: string, _path: string) => ({ columns: [] })),
  ensureActiveGatewayOpen: vi.fn(async () => null)
}))

vi.mock('@/hermes', async importOriginal => {
  const actual = (await importOriginal()) as Record<string, unknown>

  return {
    ...actual,
    pluginRest: restMocks.pluginRest
  }
})

vi.mock('@/store/gateway', async importOriginal => {
  const actual = (await importOriginal()) as Record<string, unknown>

  return {
    ...actual,
    ensureActiveGatewayOpen: restMocks.ensureActiveGatewayOpen
  }
})

describe('host.kanbanBoard', () => {
  beforeEach(() => {
    restMocks.pluginRest.mockClear()
    restMocks.pluginRest.mockResolvedValue({ columns: [] })
  })

  it('reads GET /api/plugins/kanban/board through the kanban plugin REST door', async () => {
    await host.kanbanBoard({ board: 'fulcrum-mymeter' })

    expect(restMocks.pluginRest).toHaveBeenCalledWith('kanban', '/board?board=fulcrum-mymeter')
  })

  it('omits the board query when no slug is given', async () => {
    await host.kanbanBoard()

    expect(restMocks.pluginRest).toHaveBeenCalledWith('kanban', '/board')
  })

  it('trims empty board slugs to the bare board path', async () => {
    await host.kanbanBoard({ board: '   ' })

    expect(restMocks.pluginRest).toHaveBeenCalledWith('kanban', '/board')
  })
})

describe('host.request page-route door', () => {
  beforeEach(() => {
    restMocks.ensureActiveGatewayOpen.mockClear()
    restMocks.ensureActiveGatewayOpen.mockResolvedValue(null)
  })

  it('reopens the active gateway before refusing when $gateway is null', async () => {
    $gateway.set(null)
    restMocks.ensureActiveGatewayOpen.mockResolvedValue(null)

    await expect(host.request('slash.exec', { command: '/kanban list' })).rejects.toThrow(
      'Hermes gateway unavailable'
    )

    expect(restMocks.ensureActiveGatewayOpen).toHaveBeenCalledWith({ explicit: true })
  })

  it('uses the reopened gateway for the RPC', async () => {
    const request = vi.fn(async () => ({ ok: true }))

    $gateway.set(null)
    restMocks.ensureActiveGatewayOpen.mockResolvedValue({ request } as never)

    await expect(host.request('config.get', {})).resolves.toEqual({ ok: true })
    expect(request).toHaveBeenCalledWith('config.get', {})
  })
})
