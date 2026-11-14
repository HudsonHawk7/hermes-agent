import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createStreamThrottle } from './stream-throttle'

function makeTimers() {
  const pending = new Map<number, () => void>()
  let nextId = 1

  return {
    clearTimeout: (handle: unknown) => {
      pending.delete(handle as number)
    },
    fire() {
      const jobs = [...pending.values()]
      pending.clear()

      for (const job of jobs) {
        job()
      }
    },
    setTimeout: (fn: () => void, _ms: number) => {
      const id = nextId++
      pending.set(id, fn)

      return id
    }
  }
}

function makeWindow() {
  const calls: boolean[] = []
  let destroyed = false

  return {
    calls,
    isDestroyed: () => destroyed,
    webContents: {
      isDestroyed: () => destroyed,
      setBackgroundThrottling(allowed: boolean) {
        calls.push(allowed)
      }
    }
  }
}

test('hidden e2e pin keeps a never-shown window unthrottled through idle re-throttle', () => {
  const timers = makeTimers()
  const throttle = createStreamThrottle(timers, 5_000, { pinUnthrottled: true })
  const win = makeWindow()

  throttle.register(win)

  // Register while idle must not allow throttling. On a never-shown window,
  // setBackgroundThrottling(true) hides the page and Chromium pauses muted video.
  assert.deepEqual(win.calls, [false])

  throttle.update(true)
  throttle.update(false)
  timers.fire()

  assert.equal(win.calls.includes(true), false)
  assert.deepEqual(win.calls.at(-1), false)
})

test('a visible run without the pin still throttles at idle and restores it after the trailing delay', () => {
  const timers = makeTimers()
  const throttle = createStreamThrottle(timers, 5_000, { pinUnthrottled: false })
  const win = makeWindow()

  throttle.register(win)
  assert.deepEqual(win.calls, [true])

  throttle.update(true)
  throttle.update(false)
  assert.deepEqual(win.calls, [true, false])

  timers.fire()
  assert.deepEqual(win.calls, [true, false, true])
  assert.equal(throttle.isUnthrottled(), false)
})
