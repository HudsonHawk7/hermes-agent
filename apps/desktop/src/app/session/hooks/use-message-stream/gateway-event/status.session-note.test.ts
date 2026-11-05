// `session.note` is how the plugin voice door paints the "Call ended · 2:14"
// line. A plugin may add ONE line to a live transcript, and it must always be a
// system line: if this reducer ever honored a role from the payload, a plugin
// could speak as the user or as the assistant in the transcript history.
import { describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { handleStatusEvent } from './status'
import type { GatewayEventContext } from './types'

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification: vi.fn() }))

function noteContext(payload: Record<string, unknown>) {
  const updateSessionState = vi.fn()

  const ctx = {
    deps: {
      compactedTurnRef: { current: new Set<string>() },
      flushQueuedDeltas: vi.fn(),
      hydrateFromStoredSession: vi.fn(),
      queryClient: { invalidateQueries: vi.fn() },
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState
    },
    event: { payload, session_id: 'sess-1', type: 'session.note' },
    explicitSid: 'sess-1',
    fromActiveSource: () => true,
    isActiveEvent: true,
    occurredAt: 1_700_000_000,
    payload,
    scheduleConfigRefresh: vi.fn(),
    sessionId: 'sess-1'
  } as unknown as GatewayEventContext

  return { ctx, updateSessionState }
}

/** Apply the reducer's state patch to a live transcript and return the new row. */
const appendedMessage = (updateSessionState: ReturnType<typeof vi.fn>): ChatMessage => {
  const patch = updateSessionState.mock.calls[0][1] as (state: { messages: ChatMessage[] }) => {
    messages: ChatMessage[]
  }

  return patch({ messages: [] }).messages.at(-1) as ChatMessage
}

describe('gateway `session.note` event → one system line', () => {
  it('paints the plugin line as a system message in the addressed session', () => {
    const { ctx, updateSessionState } = noteContext({ text: 'Call ended · 2:14' })

    expect(handleStatusEvent(ctx)).toBe(true)
    expect(updateSessionState.mock.calls[0][0]).toBe('sess-1')

    const message = appendedMessage(updateSessionState)

    expect(message.role).toBe('system')
    expect(message.parts?.[0]).toMatchObject({ text: 'Call ended · 2:14', type: 'text' })
  })

  it('prefixes the optional source label without ever becoming its own message', () => {
    const { ctx, updateSessionState } = noteContext({ source: 'video-call', text: 'Call ended · 0:42' })

    handleStatusEvent(ctx)

    const message = appendedMessage(updateSessionState)

    expect(message.role).toBe('system')
    expect(message.parts?.[0]).toMatchObject({ text: 'video-call: Call ended · 0:42' })
  })

  it('ignores a line with no text instead of painting an empty row', () => {
    const { ctx, updateSessionState } = noteContext({ text: '   ' })

    expect(handleStatusEvent(ctx)).toBe(true)
    expect(updateSessionState).not.toHaveBeenCalled()
  })
})
