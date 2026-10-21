import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { requestGatewayForProfile } from './gateway'
import { setPetInfo } from './pet'
import { type GatewayRequest, resetPetGallery, setPetScale } from './pet-gallery'
import { $activeGatewayProfile } from './profile'
import { $activeSessionId, $selectedStoredSessionId, $sessions } from './session'

// A non-visible owner's call leaves on that profile's own socket, not the
// ambient request; record it so a test can see exactly where a write went.
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  requestGatewayForProfile: vi.fn(async () => ({ ok: true }))
}))

const routed = vi.mocked(requestGatewayForProfile)

beforeEach(() => {
  vi.useFakeTimers()
  $sessions.set([])
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  $activeGatewayProfile.set('owner-a')
  resetPetGallery()
  routed.mockClear()
  setPetInfo({ enabled: true, slug: 'same-slug', spritesheetBase64: 'A', scale: 1 })
})

afterEach(() => {
  resetPetGallery()
  vi.clearAllTimers()
  vi.useRealTimers()
  $activeGatewayProfile.set('default')
  setPetInfo({ enabled: false })
})

describe('owner switch inside the resize debounce window', () => {
  it("persists A's pending resize to A and sends nothing to B", async () => {
    const ambient = vi.fn(async () => ({ ok: true, scale: 2 })) as unknown as GatewayRequest
    setPetScale(ambient, 2)
    await vi.advanceTimersByTimeAsync(50)
    $activeGatewayProfile.set('owner-b')
    await vi.advanceTimersByTimeAsync(500)

    expect(routed).toHaveBeenCalledTimes(1)
    expect(routed).toHaveBeenCalledWith('owner-a', 'pet.scale', { scale: 2, profile: 'owner-a' }, undefined, undefined)
    // B is now the ambient owner: no scale write may reach it.
    expect(ambient).not.toHaveBeenCalled()
  })

  it('flushes only the last value of a drag, once', async () => {
    const ambient = vi.fn(async () => ({ ok: true })) as unknown as GatewayRequest
    setPetScale(ambient, 1.5)
    setPetScale(ambient, 2)
    $activeGatewayProfile.set('owner-b')
    await vi.advanceTimersByTimeAsync(500)

    expect(routed.mock.calls.map(call => call.slice(0, 3))).toEqual([
      ['owner-a', 'pet.scale', { scale: 2, profile: 'owner-a' }]
    ])
  })
})
