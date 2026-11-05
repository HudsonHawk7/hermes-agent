/**
 * Conversation-status mirror for the plugin voice doors (`host.voice`).
 *
 * The truth lives in `useVoiceConversation` inside the composer that owns the
 * conversation. This module is the ONE place that state is mirrored so a plugin
 * can read it without reaching into a component: only the owning composer
 * publishes, the map is write-private to the app, and every reader is a
 * subscriber — the SDK door has no setter, so a plugin cannot invent a state.
 *
 * Keyed two ways on purpose: by durable session id (what a plugin usually
 * holds) and by composer target (so `conversation()` with no argument answers
 * for the composer the focus bus routes to).
 */

export type VoiceConversationStatus = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

export interface VoiceConversationSnapshot {
  /** The hands-free conversation is armed (mic open or waiting for the next turn). */
  active: boolean
  muted: boolean
  /** Runtime session id the publishing composer is showing; null while the fresh draft has no session. */
  sessionId: null | string
  /** Epoch ms the current `status` began. Stamped here, so re-publishing the same status keeps it. */
  since: number
  status: VoiceConversationStatus
  /** Durable id the snapshot is keyed by (`freshDraftScope()` for the not-yet-created session). */
  storedSessionId: string
  /** This composer's focus-bus key (`main` / `tile:<stored>`). */
  target: string
}

export type VoiceConversationPatch = Omit<VoiceConversationSnapshot, 'since'>

export interface VoiceConversationChange {
  snapshot: null | VoiceConversationSnapshot
  storedSessionId: string
}

type Listener = (change: VoiceConversationChange) => void

const bySession = new Map<string, VoiceConversationSnapshot>()
const byTarget = new Map<string, VoiceConversationSnapshot>()
const listeners = new Set<Listener>()

const emit = (change: VoiceConversationChange) => {
  for (const listener of [...listeners]) {
    try {
      listener(change)
    } catch (error) {
      // A plugin listener must never break the composer that publishes.
      console.error('[voice] conversation-status listener failed', error)
    }
  }
}

const publish = (snapshot: VoiceConversationSnapshot) => {
  bySession.set(snapshot.storedSessionId, snapshot)
  byTarget.set(snapshot.target, snapshot)
  emit({ snapshot, storedSessionId: snapshot.storedSessionId })
}

/** Mirror one composer's conversation state (called on every change and on mount). */
export const publishVoiceConversation = (patch: VoiceConversationPatch): VoiceConversationSnapshot => {
  const onTarget = byTarget.get(patch.target)

  // The same surface can move to a new session (fresh draft becomes a real
  // chat, or the composer switches). Drop the old index and tell subscribers
  // that id is gone — otherwise a call screen keyed on the old id keeps a
  // stale "speaking" light after the surface has moved on.
  if (
    onTarget &&
    onTarget.storedSessionId !== patch.storedSessionId &&
    bySession.get(onTarget.storedSessionId) === onTarget
  ) {
    bySession.delete(onTarget.storedSessionId)
    emit({ snapshot: null, storedSessionId: onTarget.storedSessionId })
  }

  const previous =
    onTarget && onTarget.storedSessionId === patch.storedSessionId ? onTarget : bySession.get(patch.storedSessionId)

  const snapshot: VoiceConversationSnapshot = {
    ...patch,
    since: previous && previous.status === patch.status ? previous.since : Date.now()
  }

  publish(snapshot)

  return snapshot
}

/** Drop a composer's snapshot (unmount / session switch) and tell subscribers it is gone. */
export const clearVoiceConversation = (target: string): void => {
  const previous = byTarget.get(target)

  if (!previous) {
    return
  }

  byTarget.delete(target)

  if (bySession.get(previous.storedSessionId) === previous) {
    bySession.delete(previous.storedSessionId)
  }

  emit({ snapshot: null, storedSessionId: previous.storedSessionId })
}

/** The snapshot for a session addressed by durable OR runtime id, or null when no composer owns it. */
export const voiceConversationFor = (ids: string[]): null | VoiceConversationSnapshot => {
  for (const raw of ids) {
    const id = raw?.trim()

    if (!id) {
      continue
    }

    const direct = bySession.get(id)

    if (direct) {
      return direct
    }

    for (const snapshot of bySession.values()) {
      if (snapshot.sessionId === id) {
        return snapshot
      }
    }
  }

  return null
}

/** The snapshot of one composer target (`main` / `tile:<stored>`), or null when it publishes none. */
export const voiceConversationForTarget = (target: null | string): null | VoiceConversationSnapshot =>
  target ? byTarget.get(target) ?? null : null

/** Every published conversation in this window (diagnostics + tests). */
export const voiceConversations = (): VoiceConversationSnapshot[] => [...bySession.values()]

/** Subscribe to every publish/clear in this window. Returns the disposer. */
export const subscribeVoiceConversation = (listener: Listener): (() => void) => {
  listeners.add(listener)

  return () => {
    listeners.delete(listener)
  }
}

/** Test seam: forget every publish and subscriber. */
export const resetVoiceConversations = (): void => {
  bySession.clear()
  byTarget.clear()
  listeners.clear()
}
