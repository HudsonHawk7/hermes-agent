/**
 * UI harness for the Fulcrum disk plugin (t_7f07258e).
 * Drives the real plugin.js through the same host.kanbanBoard door the
 * page uses, so we prove cards render without a live gateway WebSocket.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import plugin from '../plugins/fulcrum-board-harness/plugin.js'

const PROOF_TITLE = '#26488 HECO - RCAL - Timebox (9/9) Sprint'
const BOARD_SLUG = 'fulcrum-mymeter'

const hostMocks = vi.hoisted(() => {
  const gateway = { get: () => 'idle', subscribe: () => () => undefined }
  return {
    kanbanBoard: vi.fn(),
    request: vi.fn(),
    requestProfile: vi.fn(),
    profileRoutes: vi.fn(async () => []),
    navigate: vi.fn(),
    state: { gateway }
  }
})

vi.mock('@hermes/plugin-sdk', async () => {
  const rq = await import('@tanstack/react-query')
  return {
    host: hostMocks,
    useQuery: rq.useQuery,
    useValue: (store: { get: () => unknown }) => store.get(),
    ROUTES_AREA: 'routes',
    SIDEBAR_NAV_AREA: 'sidebar-nav',
    PALETTE_AREA: 'palette'
  }
})

type Contribution = {
  area: string
  data?: { path?: string }
  render?: () => React.ReactNode
}

function mountPage(renderFn: () => React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } }
  })
  return render(React.createElement(QueryClientProvider, { client }, renderFn()))
}

describe('Fulcrum page plugin board load (t_7f07258e)', () => {
  beforeEach(() => {
    hostMocks.kanbanBoard.mockReset()
    hostMocks.request.mockReset()
    hostMocks.requestProfile.mockReset()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('lists a real fulcrum-mymeter card title via host.kanbanBoard when gateway is idle', async () => {
    hostMocks.kanbanBoard.mockResolvedValue({
      columns: [
        {
          name: 'scheduled',
          tasks: [
            {
              id: 't_acf0ba58',
              title: PROOF_TITLE,
              status: 'scheduled',
              body: 'type: Support',
              latest_summary: 'scheduled sprint work'
            }
          ]
        }
      ],
      tenants: [],
      assignees: [],
      latest_event_id: 1,
      now: Date.now()
    })

    const contributions: Contribution[] = []
    const api = (plugin as { default?: { register: (ctx: unknown) => void }; register?: (ctx: unknown) => void })
      .default ?? (plugin as { register: (ctx: unknown) => void })
    api.register({
      registerMany: (items: Contribution[]) => {
        contributions.push(...items)
      }
    })

    const page = contributions.find(c => c.area === 'routes' && c.data?.path === '/fulcrum')
    expect(page?.render).toBeTypeOf('function')

    mountPage(() => page!.render!())

    await waitFor(() => {
      expect(screen.getByText(PROOF_TITLE)).toBeTruthy()
    })

    expect(hostMocks.kanbanBoard).toHaveBeenCalledWith({ board: BOARD_SLUG })
    expect(hostMocks.request).not.toHaveBeenCalled()
    expect(screen.getByText(BOARD_SLUG)).toBeTruthy()
    expect(screen.queryByText('Fulcrum could not read the board.')).toBeNull()
  })

  it('surfaces every attempt error when both board doors fail', async () => {
    const restFail = Object.assign(new Error('REST down'), {
      attempts: ['host.kanbanBoard -> REST down']
    })
    hostMocks.kanbanBoard.mockRejectedValue(restFail)
    hostMocks.request.mockRejectedValue(new Error('Hermes gateway unavailable'))

    const contributions: Contribution[] = []
    const api = (plugin as { default?: { register: (ctx: unknown) => void }; register?: (ctx: unknown) => void })
      .default ?? (plugin as { register: (ctx: unknown) => void })
    api.register({
      registerMany: (items: Contribution[]) => {
        contributions.push(...items)
      }
    })
    const page = contributions.find(c => c.area === 'routes' && c.data?.path === '/fulcrum')
    mountPage(() => page!.render!())

    await waitFor(() => {
      expect(screen.getByText('Fulcrum could not read the board.')).toBeTruthy()
    })

    expect(screen.getByText(/Attempts \(/)).toBeTruthy()
    expect(screen.getByText(/host\.kanbanBoard/i)).toBeTruthy()
    expect(screen.getByText(/gateway idle/i)).toBeTruthy()
  })
})
