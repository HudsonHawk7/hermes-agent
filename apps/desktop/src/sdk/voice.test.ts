// The voice door (`host.voice`) is the only way a desktop plugin can see or
// steer a voice call. Three promises are load-bearing and each one is a test
// here: the status light is READ-ONLY, the mic can only be TOGGLED (never set),
// and the transcript line is always a SYSTEM line on a session the caller owns.
//
// Every verb fails closed — an address with no live composer answers null
// rather than broadcasting to whichever composer happens to be mounted.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { requestComposerVoiceMute } from '@/app/chat/composer/focus'
import { requestForOwnedSession } from '@/store/session-states'
import { publishVoiceConversation, resetVoiceConversations } from '@/store/voice-conversation'

import { resolveComposerAddress } from './composer'
import { createVoiceHost } from './voice'

vi.mock('@/app/chat/composer/focus', () => ({ requestComposerVoiceMute: vi.fn() }))
vi.mock('@/store/session-states', () => ({
  $sessionTiles: { get: () => [] },
  requestForOwnedSession: vi.fn()
}))
vi.mock('./composer', () => ({ resolveComposerAddress: vi.fn() }))

const address = (target: 'active' | null | string) => ({ ids: [], stored: '', target }) as never

const ambientRequest = vi.fn()
const door = createVoiceHost(ambientRequest)

beforeEach(() => {
  resetVoiceConversations()
  vi.mocked(resolveComposerAddress).mockReset()
  vi.mocked(requestComposerVoiceMute).mockReset()
  vi.mocked(requestForOwnedSession).mockReset()
})

afterEach(() => {
  resetVoiceConversations()
})

describe('host.voice.conversation — the call-status light', () => {
  it('answers with the published snapshot for the addressed session', () => {
    vi.mocked(resolveComposerAddress).mockReturnValue({ ids: ['stored-1'], stored: 'stored-1', target: 'main' })
    publishVoiceConversation({
      active: true,
      muted: false,
      sessionId: 'runtime-1',
      status: 'speaking',
      storedSessionId: 'stored-1',
      target: 'main'
    })

    expect(door.conversation('stored-1')).toMatchObject({ sessionId: 'runtime-1', status: 'speaking' })
  })

  it('answers null when no composer holds that conversation — never a guess', () => {
    vi.mocked(resolveComposerAddress).mockReturnValue({ ids: ['stored-2'], stored: 'stored-2', target: 'main' })

    expect(door.conversation('stored-2')).toBeNull()
  })

  it('exposes no setter for the status', () => {
    expect(Object.keys(door).sort()).toEqual(['conversation', 'note', 'subscribe', 'toggleMic'])
  })
})

describe('host.voice.toggleMic — the mic button', () => {
  it('routes the toggle to the addressed composer and reports the state it reached', async () => {
    vi.mocked(resolveComposerAddress).mockReturnValue({ ids: ['stored-1'], stored: 'stored-1', target: 'main' })
    vi.mocked(requestComposerVoiceMute).mockResolvedValue({ muted: true })

    await expect(door.toggleMic('stored-1')).resolves.toEqual({ muted: true })
    expect(requestComposerVoiceMute).toHaveBeenCalledWith('main')
  })

  it('addresses the active composer when no session is named', async () => {
    vi.mocked(resolveComposerAddress).mockReturnValue({ ids: [], stored: '', target: 'active' })
    vi.mocked(requestComposerVoiceMute).mockResolvedValue({ muted: false })

    await expect(door.toggleMic()).resolves.toEqual({ muted: false })
    expect(requestComposerVoiceMute).toHaveBeenCalledWith('active')
  })

  it('answers null and never sends a toggle when the session has no composer', async () => {
    vi.mocked(resolveComposerAddress).mockReturnValue(address(null))

    await expect(door.toggleMic('closed-session')).resolves.toBeNull()
    expect(requestComposerVoiceMute).not.toHaveBeenCalled()
  })

  it('answers null when the mounted composer never replies', async () => {
    vi.mocked(resolveComposerAddress).mockReturnValue({ ids: ['stored-1'], stored: 'stored-1', target: 'main' })
    vi.mocked(requestComposerVoiceMute).mockResolvedValue(null)

    await expect(door.toggleMic('stored-1')).resolves.toBeNull()
  })
})

describe('host.voice.note — the "Call ended" line', () => {
  it('sends one trimmed system line through the session owner gate', async () => {
    vi.mocked(resolveComposerAddress).mockReturnValue({ ids: ['live-1'], stored: 'stored-1', target: 'main' })
    vi.mocked(requestForOwnedSession).mockResolvedValue({ ok: true, session_id: 'live-1' })

    await expect(door.note('live-1', '  Call ended · 2:14  ')).resolves.toEqual({ ok: true, session_id: 'live-1' })
    expect(requestForOwnedSession).toHaveBeenCalledWith('live-1', ambientRequest, 'session.note', {
      session_id: 'live-1',
      text: 'Call ended · 2:14'
    })
  })

  it('refuses an empty line before it reaches the gateway', async () => {
    await expect(door.note('live-1', '   ')).rejects.toThrow(/needs text/)
    expect(requestForOwnedSession).not.toHaveBeenCalled()
  })

  it('refuses a call with no session id', async () => {
    await expect(door.note('', 'Call ended')).rejects.toThrow(/needs a session id/)
    expect(requestForOwnedSession).not.toHaveBeenCalled()
  })

  it('carries no role, so a plugin can never write as the user or the assistant', async () => {
    vi.mocked(resolveComposerAddress).mockReturnValue({ ids: ['live-1'], stored: 'stored-1', target: 'main' })
    vi.mocked(requestForOwnedSession).mockResolvedValue({ ok: true, session_id: 'live-1' })

    await door.note('live-1', 'Call ended')

    const params = vi.mocked(requestForOwnedSession).mock.calls[0][3]

    expect(Object.keys(params ?? {}).sort()).toEqual(['session_id', 'text'])
  })
})
