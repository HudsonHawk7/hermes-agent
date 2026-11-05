// The voice door (`host.voice`) is the only way a desktop plugin can see or
// steer a voice call. Four promises are load-bearing and each one is a test
// here: the status light is READ-ONLY, the mic can only be TOGGLED (never set),
// the transcript line is always a SYSTEM line on a session the caller owns, and
// the per-sentence mouth track is READ-ONLY and cleared when it stops being true.
//
// Every verb fails closed — an address with no live composer answers null
// rather than broadcasting to whichever composer happens to be mounted.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { activeComposerTarget, requestComposerVoiceMute } from '@/app/chat/composer/focus'
import { requestForOwnedSession } from '@/store/session-states'
import {
  publishVoiceConversation,
  resetVoiceConversations,
  type VoiceConversationChange
} from '@/store/voice-conversation'
import {
  clearVoiceEnvelope,
  publishVoiceEnvelope,
  resetVoiceEnvelope,
  type VoicePlaybackEnvelope
} from '@/store/voice-playback'

import { resolveComposerAddress } from './composer'
import type * as ComposerModule from './composer'
import { createVoiceHost } from './voice'

vi.mock('@/app/chat/composer/focus', () => ({
  activeComposerTarget: vi.fn(() => 'main'),
  requestComposerVoiceMute: vi.fn()
}))
vi.mock('@/store/session-states', () => ({
  $sessionTiles: { get: () => [] },
  requestForOwnedSession: vi.fn()
}))
vi.mock('./composer', () => ({ resolveComposerAddress: vi.fn() }))

const address = (target: 'active' | null | string) => ({ ids: [], stored: '', target }) as never

const ambientRequest = vi.fn()
const door = createVoiceHost(ambientRequest)

// A published sentence: closed head/tail, a peak, 25 fps — the exact shape the
// door hands a plugin.
const track: VoicePlaybackEnvelope = {
  durationMs: 1200,
  fps: 25,
  rms: [0, 0.4, 0.8, 0.8, 0.4, 0],
  startedAtMs: 1_790_000_000_000
}

beforeEach(() => {
  resetVoiceEnvelope()
  resetVoiceConversations()
  vi.mocked(resolveComposerAddress).mockReset()
  vi.mocked(activeComposerTarget).mockReset()
  vi.mocked(activeComposerTarget).mockReturnValue('main')
  vi.mocked(requestComposerVoiceMute).mockReset()
  vi.mocked(requestForOwnedSession).mockReset()
})

afterEach(() => {
  resetVoiceEnvelope()
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

  it('returns the snapshot the active composer published when no session id is given', async () => {
    // The real empty-id address is `{ ids: [], target: 'active' }`. A mock that
    // invents a session id never hits the path a plugin's `conversation()` uses.
    const actual = await vi.importActual<typeof ComposerModule>('./composer')
    const empty = actual.resolveComposerAddress(null)

    expect(empty).toEqual({ ids: [], stored: '', target: 'active' })
    vi.mocked(resolveComposerAddress).mockImplementation(actual.resolveComposerAddress)
    vi.mocked(activeComposerTarget).mockReturnValue('tile:call')

    publishVoiceConversation({
      active: true,
      muted: false,
      sessionId: 'runtime-1',
      status: 'speaking',
      storedSessionId: 'stored-1',
      target: 'tile:call'
    })
    // Another surface is also live. The no-id read must not pick it.
    publishVoiceConversation({
      active: false,
      muted: true,
      sessionId: 'runtime-other',
      status: 'idle',
      storedSessionId: 'stored-other',
      target: 'main'
    })

    expect(door.conversation()).toMatchObject({
      sessionId: 'runtime-1',
      status: 'speaking',
      storedSessionId: 'stored-1'
    })
  })

  it('returns null when the active composer has not published, even if another surface has', async () => {
    const actual = await vi.importActual<typeof ComposerModule>('./composer')

    vi.mocked(resolveComposerAddress).mockImplementation(actual.resolveComposerAddress)
    vi.mocked(activeComposerTarget).mockReturnValue('main')

    publishVoiceConversation({
      active: true,
      muted: false,
      sessionId: 'runtime-other',
      status: 'speaking',
      storedSessionId: 'stored-other',
      target: 'tile:other'
    })

    expect(door.conversation()).toBeNull()
  })

  it('keys a dropped speaking light on storedSessionId, and a null snapshot means it dropped', () => {
    const seen: VoiceConversationChange[] = []
    const off = door.subscribe(change => seen.push(change))

    publishVoiceConversation({
      active: true,
      muted: false,
      sessionId: 'runtime-1',
      status: 'speaking',
      storedSessionId: 'stored-1',
      target: 'main'
    })
    // Session switch on the same surface: the old light clears before the new one publishes.
    publishVoiceConversation({
      active: true,
      muted: false,
      sessionId: 'runtime-2',
      status: 'listening',
      storedSessionId: 'stored-2',
      target: 'main'
    })

    const dropped = seen.filter(change => change.storedSessionId === 'stored-1' && change.snapshot === null)

    expect(dropped).toEqual([{ snapshot: null, storedSessionId: 'stored-1' }])
    // snapshot.sessionId is undefined on a clear, so that filter keeps the old light.
    expect(seen.filter(change => change.snapshot?.sessionId === 'runtime-1').every(change => change.snapshot !== null)).toBe(true)

    off()
  })

  it('exposes no setter and no writer for a read-only surface', () => {
    expect(Object.keys(door).sort()).toEqual([
      'conversation',
      'note',
      'playback',
      'subscribe',
      'subscribePlayback',
      'toggleMic'
    ])
  })
})

describe('host.voice.playback — one envelope per spoken sentence', () => {
  it('answers null until a sentence is publishing', () => {
    expect(door.playback()).toBeNull()
  })

  it('answers the published sentence envelope the overlay clocks to', () => {
    publishVoiceEnvelope(track)

    expect(door.playback()).toEqual(track)
  })

  it('gives a subscriber each publish and the clear, so no stale track is kept', () => {
    const seen: (null | VoicePlaybackEnvelope)[] = []
    const off = door.subscribePlayback(next => seen.push(next))

    publishVoiceEnvelope(track)
    clearVoiceEnvelope()
    off()

    expect(seen).toEqual([track, null])
    expect(door.playback()).toBeNull()
  })

  it('cannot be written through the answer — the track is frozen', () => {
    publishVoiceEnvelope(track)

    const answer = door.playback()

    expect(() => {
      answer!.rms[0] = 99
    }).toThrow()

    expect(door.playback()?.rms[0]).toBe(0)
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
