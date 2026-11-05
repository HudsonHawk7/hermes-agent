// The plugin voice door reads its state from here (`host.voice.conversation`),
// and the only writer is the composer that owns the conversation. These tests
// pin the two properties the door relies on: a reader can never invent a state,
// and a composer that goes away stops answering instead of leaving a stale
// "listening" light on a call screen.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearVoiceConversation,
  publishVoiceConversation,
  resetVoiceConversations,
  subscribeVoiceConversation,
  voiceConversationFor,
  voiceConversationForTarget,
  voiceConversations
} from './voice-conversation'

const patch = (overrides: Partial<Parameters<typeof publishVoiceConversation>[0]> = {}) => ({
  active: true,
  muted: false,
  sessionId: 'runtime-1',
  status: 'listening' as const,
  storedSessionId: 'stored-1',
  target: 'main',
  ...overrides
})

beforeEach(() => {
  resetVoiceConversations()
})

afterEach(() => {
  resetVoiceConversations()
  vi.useRealTimers()
})

describe('voice-conversation mirror', () => {
  it('answers by stored id, by runtime id, and by composer target', () => {
    publishVoiceConversation(patch())

    expect(voiceConversationFor(['stored-1'])?.status).toBe('listening')
    expect(voiceConversationFor(['runtime-1'])?.status).toBe('listening')
    expect(voiceConversationForTarget('main')?.storedSessionId).toBe('stored-1')
  })

  it('fails closed for a session no composer owns', () => {
    publishVoiceConversation(patch())

    expect(voiceConversationFor(['someone-elses-session'])).toBeNull()
    expect(voiceConversationFor([])).toBeNull()
    expect(voiceConversationForTarget(null)).toBeNull()
    expect(voiceConversationForTarget('tile:other')).toBeNull()
  })

  it('keeps `since` while the status holds and restamps when it changes', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000)

    publishVoiceConversation(patch())
    vi.setSystemTime(2_000)
    publishVoiceConversation(patch({ muted: true }))
    const held = voiceConversationFor(['stored-1'])

    expect(held?.since).toBe(1_000)
    expect(held?.muted).toBe(true)

    vi.setSystemTime(3_000)
    publishVoiceConversation(patch({ status: 'speaking' }))

    expect(voiceConversationFor(['stored-1'])?.since).toBe(3_000)
  })

  it('notifies a subscriber that attaches BEFORE the composer publishes', () => {
    const seen: (null | string)[] = []

    subscribeVoiceConversation(change => seen.push(change.snapshot?.status ?? null))
    publishVoiceConversation(patch())
    publishVoiceConversation(patch({ status: 'thinking' }))

    expect(seen).toEqual(['listening', 'thinking'])
  })

  it('tells subscribers a conversation is gone when the composer unmounts', () => {
    const seen: (null | string)[] = []

    publishVoiceConversation(patch())
    subscribeVoiceConversation(change => seen.push(change.snapshot?.status ?? null))
    clearVoiceConversation('main')

    expect(seen).toEqual([null])
    expect(voiceConversationFor(['stored-1'])).toBeNull()
    expect(voiceConversations()).toHaveLength(0)
  })

  it('never lets one plugin listener break the composer or other listeners', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const healthy = vi.fn()

    subscribeVoiceConversation(() => {
      throw new Error('plugin blew up')
    })
    subscribeVoiceConversation(healthy)

    expect(() => publishVoiceConversation(patch())).not.toThrow()
    expect(healthy).toHaveBeenCalledOnce()
    expect(error).toHaveBeenCalled()

    error.mockRestore()
  })

  it('unsubscribes cleanly', () => {
    const listener = vi.fn()
    const off = subscribeVoiceConversation(listener)

    off()
    publishVoiceConversation(patch())

    expect(listener).not.toHaveBeenCalled()
  })

  it('publishes the fresh draft under its own key without a runtime session', () => {
    publishVoiceConversation(patch({ sessionId: null, status: 'idle', storedSessionId: 'draft-scope', target: 'main' }))

    const draft = voiceConversationFor(['draft-scope'])

    expect(draft?.sessionId).toBeNull()
    expect(draft?.status).toBe('idle')
  })

  it('drops the previous session index when the same surface changes session', () => {
    const heard: Array<{ snapshot: null | { status: string }; storedSessionId: string }> = []

    subscribeVoiceConversation(change => heard.push(change))
    publishVoiceConversation(patch({ sessionId: 'live-1', status: 'speaking', storedSessionId: 'stored-1' }))
    publishVoiceConversation(patch({ sessionId: 'live-2', status: 'listening', storedSessionId: 'stored-2' }))

    expect(voiceConversationFor(['stored-1'])).toBeNull()
    expect(voiceConversationFor(['stored-2'])?.status).toBe('listening')
    expect(voiceConversations()).toHaveLength(1)
    expect(heard.some(change => change.storedSessionId === 'stored-1' && change.snapshot === null)).toBe(true)
  })
})
