/**
 * Orphaned sandbox gateways: found by their environment, killed as trees, and never the live one.
 *
 * Every case here runs real processes with a dead parent and a gateway-shaped command line that
 * names no home — the exact shape Hera found alive 35 hours and 5.2 days after their runs ended.
 * The command line cannot identify them, so these tests fail if the environment census is removed;
 * the negatives fail if the sweep ever widens to the live install or a real profile home.
 */
import { spawn, spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { describe, expect, it } from 'vitest'

import { stopSandboxGatewayDaemons } from './harness'
import { isSandboxHome, orphanSandboxProcesses, reapOrphanSandboxProcesses } from './process-census'

const windows = process.platform === 'win32'

/** Spawn a child, then die: the child is orphaned exactly as a killed Playwright worker leaves it. */
const LAUNCHER = `
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const [home, pidFile] = process.argv.slice(1)
const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 300000)', 'gateway', 'run'], {
  env: { ...process.env, HERMES_HOME: home },
  stdio: 'ignore',
  detached: true,
  windowsHide: true
})
child.unref()
fs.writeFileSync(pidFile, String(child.pid))
`

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0)

    return true
  } catch {
    return false
  }
}

function waitFor(predicate: () => boolean, timeoutMs = 20_000): boolean {
  for (const deadline = Date.now() + timeoutMs; Date.now() < deadline; ) {
    if (predicate()) {
      return true
    }

    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100)
  }

  return predicate()
}

function orphan(tmpDir: string, home: string, name: string): number {
  const pidFile = path.join(tmpDir, `${name}.pid`)
  const result = spawnSync(process.execPath, ['-e', LAUNCHER, home, pidFile], { encoding: 'utf8', timeout: 60_000 })

  expect(result.status, result.stderr).toBe(0)
  expect(waitFor(() => fs.existsSync(pidFile) && fs.readFileSync(pidFile, 'utf8').trim() !== '')).toBe(true)

  return Number(fs.readFileSync(pidFile, 'utf8').trim())
}

describe.skipIf(!windows)('orphaned sandbox gateways', () => {
  it('are found and reaped through the environment even though the command line names no home', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-reap-'))
    const root = path.join(tmpDir, 'hermes-pytest')
    const home = path.join(root, 'pw-abcd', 'hermes-home')
    const pid = orphan(tmpDir, home, 'census')

    try {
      expect(alive(pid)).toBe(true)

      const found = orphanSandboxProcesses([root]).map(proc => proc.pid)

      expect(found).toContain(pid)
      expect(reapOrphanSandboxProcesses([root])).toContain(pid)
      expect(waitFor(() => !alive(pid)), `orphan ${pid} survived the reap`).toBe(true)
    } finally {
      try {
        process.kill(pid, 'SIGKILL')
      } catch {
        // already gone
      }

      fs.rmSync(tmpDir, { recursive: true, force: true })
    }
  })

  it('are not signalled when their home is the live install or a real profile home', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-reap-'))
    const root = path.join(tmpDir, 'hermes-pytest')
    const localAppData = process.env.LOCALAPPDATA ?? path.join(os.homedir(), 'AppData', 'Local')
    const liveHome = path.join(localAppData, 'hermes')
    const profileHome = path.join(liveHome, 'profiles', 'reap-probe')

    // A dead parent is not a licence to kill: these are homes a companion or the GM really uses.
    const livePid = orphan(tmpDir, liveHome, 'live')
    const profilePid = orphan(tmpDir, profileHome, 'profile')

    try {
      expect(isSandboxHome(liveHome, [root])).toBe(false)
      expect(isSandboxHome(profileHome, [root])).toBe(false)

      const found = orphanSandboxProcesses([root]).map(proc => proc.pid)

      expect(found).not.toContain(livePid)
      expect(found).not.toContain(profilePid)
      reapOrphanSandboxProcesses([root])
      expect(alive(livePid)).toBe(true)
      expect(alive(profilePid)).toBe(true)
    } finally {
      for (const pid of [livePid, profilePid]) {
        try {
          process.kill(pid, 'SIGKILL')
        } catch {
          // already gone
        }
      }

      fs.rmSync(tmpDir, { recursive: true, force: true })
    }
  })

  it('are tree-killed by the harness even when gateway.lock names a pid that is already dead', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-reap-'))
    const root = path.join(tmpDir, 'hermes-e2e')
    const hermesHome = path.join(root, 'pw-efgh', 'hermes-home')
    fs.mkdirSync(hermesHome, { recursive: true })
    // The lock survived its daemon: the pid in it is long gone, so only the environment can help.
    fs.writeFileSync(path.join(hermesHome, 'gateway.lock'), JSON.stringify({ pid: 999_999 }))

    const pid = orphan(tmpDir, hermesHome, 'harness')

    try {
      expect(alive(pid)).toBe(true)
      stopSandboxGatewayDaemons(root, hermesHome)
      expect(waitFor(() => !alive(pid)), `sandbox daemon ${pid} survived teardown`).toBe(true)
    } finally {
      try {
        process.kill(pid, 'SIGKILL')
      } catch {
        // already gone
      }

      fs.rmSync(tmpDir, { recursive: true, force: true })
    }
  })

  it('leaves a daemon whose parent is still running alone', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-reap-'))
    const root = path.join(tmpDir, 'hermes-pytest')
    const home = path.join(root, 'pw-ijkl', 'hermes-home')
    const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 300000)', 'gateway', 'run'], {
      env: { ...process.env, HERMES_HOME: home },
      stdio: 'ignore',
      windowsHide: true,
    })

    try {
      expect(child.pid).toBeTypeOf('number')
      // A live run owns its daemon: the census must not describe it as an orphan.
      expect(orphanSandboxProcesses([root]).map(proc => proc.pid)).not.toContain(child.pid)
    } finally {
      child.kill()
      fs.rmSync(tmpDir, { recursive: true, force: true })
    }
  })
})
