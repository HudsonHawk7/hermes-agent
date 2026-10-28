/**
 * Voice Hub listening state for the always-visible listening light (VH8).
 *
 * Source of truth is `voice_hub.state` on the gateway — not a second mic path.
 * One-click stop goes through `wake.stop` so the Hub lease is released.
 */
import type {
  VoiceHubStateParams,
  VoiceHubStateResult,
  WakeStopParams,
  WakeStopResult
} from '@hermes/shared/gateway-contract.generated'
import { atom } from 'nanostores'

import { $gateway } from '@/store/gateway'

export type VoiceHubRequester = (
  method: string,
  params?: Record<string, unknown>
) => Promise<unknown>

/** Hub status values from tui_gateway/voice_hub.py. */
export type VoiceHubStatus = 'idle' | 'armed' | 'stopped'

export type VoiceHubSnapshot = {
  /** Last successful poll/apply time (ms). */
  fetchedAt: number
  ownerSurface: string | null
  pending: boolean
  status: VoiceHubStatus
  stopReason: string | null
}

const INITIAL: VoiceHubSnapshot = {
  fetchedAt: 0,
  ownerSurface: null,
  pending: false,
  status: 'idle',
  stopReason: null
}

export const $voiceHub = atom<VoiceHubSnapshot>(INITIAL)

let pollTimer: ReturnType<typeof setInterval> | null = null
let pollInFlight = false

const POLL_MS = 500

export function resetVoiceHubState(): void {
  stopVoiceHubPolling()
  $voiceHub.set(INITIAL)
}

export function applyVoiceHubState(result: VoiceHubStateResult | null | undefined): void {
  if (!result) {
    return
  }

  const status = normalizeStatus(result.status)
  $voiceHub.set({
    fetchedAt: Date.now(),
    ownerSurface: result.owner_surface ?? null,
    pending: false,
    status,
    stopReason: result.stop_reason ?? null
  })
}

function normalizeStatus(raw: string | null | undefined): VoiceHubStatus {
  if (raw === 'armed' || raw === 'stopped' || raw === 'idle') {
    return raw
  }

  return 'idle'
}

const gatewayRequester: VoiceHubRequester = (method, params = {}) => {
  const gw = $gateway.get()

  if (!gw) {
    return Promise.reject(new Error('gateway not connected'))
  }

  return gw.request(method, params)
}

/** Pull hub state once. Safe to call often; concurrent calls share one pending flag. */
export async function refreshVoiceHub(
  request: VoiceHubRequester = gatewayRequester
): Promise<VoiceHubSnapshot> {
  if (pollInFlight) {
    return $voiceHub.get()
  }

  pollInFlight = true

  try {
    const result = (await request('voice_hub.state', {} satisfies VoiceHubStateParams)) as VoiceHubStateResult
    applyVoiceHubState(result)
  } catch {
    // Keep last known snapshot; a transient RPC blip must not invent "listening".
  } finally {
    pollInFlight = false
  }

  return $voiceHub.get()
}

/**
 * Start a 500 ms poll so the light turns on/off within 1 s of arm/stop (AC1).
 * Idempotent. Call when the gateway is open; stop when it closes.
 */
export function startVoiceHubPolling(request: VoiceHubRequester = gatewayRequester): void {
  if (pollTimer != null) {
    return
  }

  void refreshVoiceHub(request)
  pollTimer = setInterval(() => {
    void refreshVoiceHub(request)
  }, POLL_MS)
}

export function stopVoiceHubPolling(): void {
  if (pollTimer != null) {
    clearInterval(pollTimer)
    pollTimer = null
  }

  pollInFlight = false
}

/**
 * One-click stop: release the Hub lease via wake.stop (not a second mic path).
 * Deliberate UI stop persists disabled so restart can't re-arm from config.
 */
export async function stopVoiceHubListening(
  request: VoiceHubRequester = gatewayRequester,
  opts: { persist?: boolean; reason?: string } = {}
): Promise<void> {
  const reason = opts.reason ?? 'user_stop'
  const persist = opts.persist ?? reason === 'user_stop'
  $voiceHub.set({ ...$voiceHub.get(), pending: true })

  try {
    const params: WakeStopParams = { persist, reason }
    const result = (await request('wake.stop', params as Record<string, unknown>)) as WakeStopResult

    // Optimistic local mirror so the light drops within the same tick; then await
    // hub state so callers/tests settle on server truth (release → stopped, not idle).
    if (result?.stopped !== false) {
      $voiceHub.set({
        fetchedAt: Date.now(),
        ownerSurface: null,
        pending: false,
        status: 'stopped',
        stopReason: reason
      })
    } else {
      $voiceHub.set({ ...$voiceHub.get(), pending: false })
    }

    await refreshVoiceHub(request)
  } catch (err) {
    $voiceHub.set({
      ...$voiceHub.get(),
      pending: false,
      status: 'stopped',
      stopReason: err instanceof Error ? err.message : 'stop_failed'
    })
  }
}

/** True when the light should show the active listening glow. */
export function isVoiceHubListening(snap: VoiceHubSnapshot = $voiceHub.get()): boolean {
  return snap.status === 'armed'
}

/**
 * Failure reasons that must never paint as "listening". user_stop / paused / detected
 * are expected quiet ends — the light hides. start_failed / capture_failed / reclaimed
 * stay visible with the named reason until the next arm or a clean idle poll.
 */
const FAILURE_REASONS = new Set(['start_failed', 'capture_failed', 'reclaimed', 'stop_failed'])

export function isVoiceHubFailure(snap: VoiceHubSnapshot = $voiceHub.get()): boolean {
  if (snap.status === 'armed') {
    return false
  }

  const reason = snap.stopReason

  if (!reason) {
    return false
  }

  return FAILURE_REASONS.has(reason)
}
