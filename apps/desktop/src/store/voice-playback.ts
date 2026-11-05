import { atom } from 'nanostores'

export type VoicePlaybackSource = 'read-aloud' | 'voice-conversation'
export type VoicePlaybackStatus = 'idle' | 'preparing' | 'speaking'

export interface VoicePlaybackState {
  audioElement: HTMLAudioElement | null
  messageId: string | null
  sequence: number
  source: VoicePlaybackSource | null
  status: VoicePlaybackStatus
}

export const $voicePlayback = atom<VoicePlaybackState>({
  audioElement: null,
  messageId: null,
  sequence: 0,
  source: null,
  status: 'idle'
})

export function setVoicePlaybackState(next: VoicePlaybackState) {
  $voicePlayback.set(next)
}

/** One sentence's mouth track, as the plugin door hands it out
 *  (`host.voice.playback`). `rms[i]` is the frame at `startedAtMs + i / fps`. */
export interface VoicePlaybackEnvelope {
  durationMs: number
  fps: number
  /** 0..1 per frame; the first and last frame are always 0 (closed mouth). */
  rms: number[]
  startedAtMs: number
}

export type VoicePlaybackEnvelopeListener = (next: null | VoicePlaybackEnvelope) => void

// A single slot, not a map: only one sentence is audible at a time, and an
// overlay draws the mouth of what is playing NOW. Written only by the player
// (`lib/voice-playback`) — the door that reads it is read-only.
let currentEnvelope: null | VoicePlaybackEnvelope = null
const envelopeListeners = new Set<VoicePlaybackEnvelopeListener>()

export function voicePlaybackEnvelope(): null | VoicePlaybackEnvelope {
  return currentEnvelope
}

export function subscribePlaybackEnvelope(listener: VoicePlaybackEnvelopeListener): () => void {
  envelopeListeners.add(listener)

  return () => {
    envelopeListeners.delete(listener)
  }
}

/** Publish the sentence that is about to play. The track is frozen on the way
 *  out: a plugin holds a real reference to this object and must not be able to
 *  rewrite a frame of what the user actually heard. */
export function publishVoiceEnvelope(next: VoicePlaybackEnvelope): void {
  const snapshot: VoicePlaybackEnvelope = { ...next, rms: [...next.rms] }

  Object.freeze(snapshot.rms)
  Object.freeze(snapshot)
  currentEnvelope = snapshot

  for (const listener of [...envelopeListeners]) {
    listener(snapshot)
  }
}

/** The audio stopped being true: sentence end, barge-in, teardown. Silent when
 *  nothing was published, so a stop cannot emit a second `null`. */
export function clearVoiceEnvelope(): void {
  if (!currentEnvelope) {
    return
  }

  currentEnvelope = null

  for (const listener of [...envelopeListeners]) {
    listener(null)
  }
}

/** Test seam: drop the slot and every listener. */
export function resetVoiceEnvelope(): void {
  currentEnvelope = null
  envelopeListeners.clear()
}
