import { execFile, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test, vi } from 'vitest'

vi.mock('node:child_process', async () => {
  const actual = await vi.importActual('node:child_process')

  return {
    ...actual,
    execFile: vi.fn((_cmd: string, _args: string[], _opts: unknown, cb?: (err: null, stdout: string) => void) => {
      const callback = typeof _opts === 'function' ? (_opts as (err: null, stdout: string) => void) : cb

      callback?.(null, 'Install directory: C:\\fake\\hermes\n')

      return undefined
    }),
    spawnSync: vi.fn(() => {
      throw new Error('spawnSync must not run on the startup path')
    })
  }
})

import { userLauncherInstallRootAsync } from './updater-process'

test('the startup resolve does not call spawnSync', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-launcher-'))
  const bin = path.join(home, 'bin')

  fs.mkdirSync(bin, { recursive: true })
  fs.writeFileSync(path.join(bin, process.platform === 'win32' ? 'hermes.cmd' : 'hermes'), '')

  const found = await userLauncherInstallRootAsync(process.platform === 'win32', home)

  expect(spawnSync).not.toHaveBeenCalled()
  expect(execFile).toHaveBeenCalled()
  expect(found).toBeNull()
  fs.rmSync(home, { recursive: true, force: true })
})
