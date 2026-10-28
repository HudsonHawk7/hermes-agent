import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $voiceHub,
  applyVoiceHubState,
  isVoiceHubFailure,
  isVoiceHubListening,
  refreshVoiceHub,
  resetVoiceHubState,
  stopVoiceHubListening,
  stopVoiceHubPolling,
  type VoiceHubRequester
} from './voice-hub'

const requester = (impl: (method: string, params?: Record<string, unknown>) => unknown) =>
  vi.fn(async (method: string, params: Record<string, unknown> = {}) => impl(method, params)) as unknown as VoiceHubRequester

beforeEach(() => {
  resetVoiceHubState()
})

afterEach(() => {
  stopVoiceHubPolling()
  resetVoiceHubState()
})

describe('applyVoiceHubState', () => {
  it('marks listening when hub status is armed', () => {
    applyVoiceHubState({ status: 'armed', owner_surface: 'gui', stop_reason: null })
    expect(isVoiceHubListening()).toBe(true)
    expect(isVoiceHubFailure()).toBe(false)
    expect($voiceHub.get()).toMatchObject({ status: 'armed', ownerSurface: 'gui', stopReason: null })
  })

  it('shows a named failure for capture_failed and never claims listening', () => {
    applyVoiceHubState({ status: 'stopped', owner_surface: null, stop_reason: 'capture_failed' })
    expect(isVoiceHubListening()).toBe(false)
    expect(isVoiceHubFailure()).toBe(true)
    expect($voiceHub.get().stopReason).toBe('capture_failed')
  })

  it('hides the light after a clean user_stop', () => {
    applyVoiceHubState({ status: 'armed', owner_surface: 'gui', stop_reason: null })
    applyVoiceHubState({ status: 'stopped', owner_surface: null, stop_reason: 'user_stop' })
    expect(isVoiceHubListening()).toBe(false)
    expect(isVoiceHubFailure()).toBe(false)
  })
})

describe('refreshVoiceHub', () => {
  it('pulls voice_hub.state and applies the snapshot', async () => {
    const request = requester(method => {
      expect(method).toBe('voice_hub.state')
      return { status: 'armed', owner_surface: 'gui', stop_reason: null }
    })
    await refreshVoiceHub(request)
    expect($voiceHub.get()).toMatchObject({ status: 'armed', ownerSurface: 'gui' })
    expect(request).toHaveBeenCalledWith('voice_hub.state', {})
  })
})

describe('stopVoiceHubListening', () => {
  it('stops through wake.stop with user_stop + persist (Hub lease path)', async () => {
    applyVoiceHubState({ status: 'armed', owner_surface: 'gui', stop_reason: null })
    const request = requester(method => {
      if (method === 'wake.stop') return { stopped: true, disabled_persisted: true, reason: null }
      if (method === 'voice_hub.state') {
        return { status: 'idle', owner_surface: null, stop_reason: 'user_stop' }
      }
      throw new Error(`unexpected ${method}`)
    })

    await stopVoiceHubListening(request, { persist: true, reason: 'user_stop' })

    expect(request).toHaveBeenCalledWith('wake.stop', { persist: true, reason: 'user_stop' })
    expect($voiceHub.get()).toMatchObject({
      status: 'stopped',
      stopReason: 'user_stop',
      pending: false
    })
  })

  it('records capture_failed without persisting disabled by default when reason is failure', async () => {
    applyVoiceHubState({ status: 'armed', owner_surface: 'gui', stop_reason: null })
    const request = requester(method => {
      if (method === 'wake.stop') return { stopped: true, disabled_persisted: false, reason: null }
      if (method === 'voice_hub.state') {
        return { status: 'stopped', owner_surface: null, stop_reason: 'capture_failed' }
      }
      throw new Error(`unexpected ${method}`)
    })

    await stopVoiceHubListening(request, { reason: 'capture_failed', persist: false })

    expect(request).toHaveBeenCalledWith('wake.stop', { persist: false, reason: 'capture_failed' })
    expect(isVoiceHubFailure()).toBe(true)
    expect($voiceHub.get().stopReason).toBe('capture_failed')
  })
})
