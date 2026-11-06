/**
 * Reading `HERMES_HOME` out of a live process on Windows.
 *
 * The whole point of this module is that the command line cannot answer the question: a sandbox
 * gateway runs as `hermes_cli.main --profile default gateway run --quiet`, with no path anywhere in
 * its argv. These tests spawn a real child with a distinctive home and assert the environment — and
 * only the environment — is what identifies it.
 */
import { spawn } from 'node:child_process'
import os from 'node:os'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { pathUnder, samePath, windowsProcessHomes } from './process-environment'

const windows = process.platform === 'win32'

function sleeper(home: string) {
  return spawn(process.execPath, ['-e', 'setTimeout(() => {}, 300000)'], {
    env: { ...process.env, HERMES_HOME: home },
    stdio: 'ignore',
  })
}

describe('windowsProcessHomes', () => {
  it.skipIf(!windows)('reads the home from the environment, which the command line never shows', async () => {
    const home = path.join(os.tmpdir(), 'hermes-env-probe', 'pw-probe', 'hermes-home')
    const child = sleeper(home)

    try {
      expect(child.pid).toBeTypeOf('number')
      expect(child.spawnargs.join(' ')).not.toContain(home)

      const homes = windowsProcessHomes([child.pid as number])

      expect(homes.get(child.pid as number)).toBe(home)
    } finally {
      child.kill()
    }
  })

  it.skipIf(!windows)('has no answer for a pid that is gone', () => {
    expect(windowsProcessHomes([999_999]).size).toBe(0)
  })

  it.skipIf(windows)('is a no-op away from Windows', () => {
    expect(windowsProcessHomes([process.pid]).size).toBe(0)
  })
})

describe('path helpers', () => {
  it('matches the same location written two ways', () => {
    expect(samePath(path.join(os.tmpdir(), 'a', 'b'), path.join(os.tmpdir(), 'a', '.', 'b'))).toBe(true)
  })

  it('never treats a longer sibling name as a child', () => {
    expect(pathUnder(path.join(os.tmpdir(), 'pw-abcd'), path.join(os.tmpdir(), 'pw-abc'))).toBe(false)
    expect(pathUnder(path.join(os.tmpdir(), 'pw-abc', 'home'), path.join(os.tmpdir(), 'pw-abc'))).toBe(true)
    expect(pathUnder(path.join(os.tmpdir(), 'pw-abc'), path.join(os.tmpdir(), 'pw-abc'))).toBe(true)
  })
})
