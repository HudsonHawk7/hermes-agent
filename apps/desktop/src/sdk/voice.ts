/**
 * THE voice door (#voice-doors): what a desktop plugin can see and do during a
 * voice call, without reaching into a component.
 *
 * Three verbs, one namespace, and every one of them fails closed:
 *
 *  - `conversation(sessionId?)` / `subscribe(fn)` — the call-status light:
 *    `idle` / `listening` / `transcribing` / `thinking` / `speaking`, plus the
 *    mic's muted state. READ-ONLY: the only writer is the composer that owns the
 *    conversation (`store/voice-conversation`), so a plugin cannot invent a
 *    state or make the call screen claim the other side is speaking.
 *  - `toggleMic(sessionId?)` — the mic button. Asks the mounted composer to
 *    toggle and answers with the state it actually reached. There is no
 *    `set(muted)` verb anywhere in this door: a plugin can never force the
 *    microphone open or closed.
 *  - `note(sessionId, text)` — one system line in a live session's transcript
 *    (the "Call ended · 2:14" promise). Always a system line: there is no role
 *    parameter, so a plugin cannot write as the user or as the assistant. The
 *    gateway refuses a caller that does not own the session's transport.
 *
 * Addressing matches `composerHost`: `null` = the active composer, a session id
 * (stored or runtime) = that session's composer, `'new'` = the fresh draft. An
 * address with no live surface yields `null` — never a broadcast.
 */
import { activeComposerTarget, type ComposerTarget, requestComposerVoiceMute } from '@/app/chat/composer/focus'
import { $sessionTiles, requestForOwnedSession } from '@/store/session-states'
import {
  subscribeVoiceConversation,
  type VoiceConversationChange,
  voiceConversationFor,
  voiceConversationForTarget,
  type VoiceConversationSnapshot
} from '@/store/voice-conversation'

import { resolveComposerAddress } from './composer'

export type { VoiceConversationChange, VoiceConversationSnapshot, VoiceConversationStatus } from '@/store/voice-conversation'

export interface VoiceNoteResult {
  ok: boolean
  session_id: string
}

export interface VoiceToggleMicResult {
  muted: boolean
}

type AmbientRequest = <R>(
  method: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<R>

/** The transcript RPC carries the LIVE session id; a stored id is translated
 *  through the published snapshot or the tile that holds it. Unknown ids pass
 *  through so the gateway answers `session not found` instead of the door
 *  guessing at a session the caller does not have. */
const runtimeIdForNote = (id: string): string => {
  const published = voiceConversationFor([id])

  if (published?.sessionId) {
    return published.sessionId
  }

  const tile = $sessionTiles.get().find(candidate => candidate.storedSessionId === id)

  return tile?.runtimeId || id
}

/** Build the door with the caller's ambient gateway request (the `host`
 *  aggregate passes its own `request`, so this module never imports it back). */
export const createVoiceHost = (ambientRequest: AmbientRequest) => ({
  /** The conversation light for one composer, or null when nothing holds it. */
  conversation: (sessionId: null | string = null): null | VoiceConversationSnapshot => {
    const { ids, target } = resolveComposerAddress(sessionId)

    if (ids.length) {
      return voiceConversationFor(ids)
    }

    // null/empty is the active composer — the same surface toggleMic() with no
    // id reaches. Empty ids is not "no conversation". Null only when that
    // surface has not published, or no mounted surface can own the address.
    if (target !== 'active') {
      return null
    }

    return voiceConversationForTarget(activeComposerTarget())
  },

  /** One transcript line (system role, live-only). Resolves with the gateway's
   *  result; rejects when the session's owner cannot be resolved — the same
   *  fail-closed rule every session-scoped RPC follows. */
  note: async (sessionId: null | string, text: string): Promise<VoiceNoteResult> => {
    const id = typeof sessionId === 'string' ? sessionId.trim() : ''
    const line = typeof text === 'string' ? text.trim() : ''

    if (!id) {
      throw new Error('host.voice.note needs a session id')
    }

    if (!line) {
      throw new Error('host.voice.note needs text')
    }

    const runtimeId = runtimeIdForNote(id)

    return requestForOwnedSession<VoiceNoteResult>(runtimeId, ambientRequest, 'session.note', {
      session_id: runtimeId,
      text: line
    })
  },

  /** Every conversation publish or clear in this window.
   *  Key a session's light on `change.storedSessionId`, not
   *  `change.snapshot.sessionId`: a clear (session switch or unmount) emits
   *  `{ snapshot: null, storedSessionId }`, and `snapshot === null` means that
   *  light dropped. Filtering on `snapshot.sessionId` keeps the old speaking
   *  light. Returns the unsubscribe. */
  subscribe: (listener: (change: VoiceConversationChange) => void): (() => void) =>
    subscribeVoiceConversation(listener),

  /** Flip one composer's mic and answer with the state it reached; null when no
   *  mounted composer answered (nothing on screen holds that conversation). */
  toggleMic: async (sessionId: null | string = null): Promise<null | VoiceToggleMicResult> => {
    const { target } = resolveComposerAddress(sessionId)

    if (target === null) {
      return null
    }

    return requestComposerVoiceMute(target as ComposerTarget | 'active')
  }
})

export type VoiceHost = ReturnType<typeof createVoiceHost>
