// The mic door rides a small tokened bus: the plugin asks, the mounted composer
// answers with the state it actually reached. These tests pin the three ways it
// can fail, because every one of them must fail CLOSED — a plugin request must
// never reach a composer that was not addressed, and silence must never look
// like success.
import { afterEach, describe, expect, it, vi } from 'vitest'

import { onComposerVoiceMuteRequest, requestComposerVoiceMute } from './focus'

const disposers: (() => void)[] = []

afterEach(() => {
  for (const off of disposers.splice(0)) {
    off()
  }
})

const answer = (target: Parameters<typeof onComposerVoiceMuteRequest>[0], muted: boolean) => {
  const handler = vi.fn(() => muted)

  disposers.push(onComposerVoiceMuteRequest(target, handler))

  return handler
}

describe('requestComposerVoiceMute', () => {
  it('reports the state the addressed composer reached', async () => {
    const handler = answer('main', true)

    await expect(requestComposerVoiceMute('main')).resolves.toEqual({ muted: true })
    expect(handler).toHaveBeenCalledOnce()
  })

  it('never asks a composer it was not addressed to', async () => {
    const tile = answer('tile:stored-2', true)

    await expect(requestComposerVoiceMute('main')).resolves.toBeNull()
    expect(tile).not.toHaveBeenCalled()
  })

  it('routes an `active` request to the focused composer only', async () => {
    const tile = answer('tile:stored-2', true)

    await expect(requestComposerVoiceMute('active')).resolves.toBeNull()
    expect(tile).not.toHaveBeenCalled()
  })

  it('answers null when nothing on screen holds the conversation', async () => {
    await expect(requestComposerVoiceMute('main')).resolves.toBeNull()
  })

  it('settles every caller exactly once — no dangling promise after the timeout', async () => {
    vi.useFakeTimers()

    const pending = requestComposerVoiceMute('main')

    vi.advanceTimersByTime(60)
    await expect(pending).resolves.toBeNull()

    // A late reply for the expired token must not resurrect the promise.
    window.dispatchEvent(new CustomEvent('hermes:composer-voice-mute-reply', { detail: { muted: true, token: 1 } }))
    await expect(pending).resolves.toBeNull()

    vi.useRealTimers()
  })
})
