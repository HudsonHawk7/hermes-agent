/**
 * A custom HERMES_E2E_CORE_ROOT (the call demo's scratch folder) is not under tmp.
 * The reap must still find a leftover gateway there after the worker is dead, and
 * must not touch that gateway while the worker is still alive. A hard kill of the
 * worker — no exit handler — must take a non-breakaway child with it. The live
 * home and a real profiles\<name> home are never signaled.
 */
import { spawn, spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { isSandboxHome, defaultSandboxReapRoots, reapOrphanSandboxProcesses } from './process-census'
import { pathUnder } from './process-environment'
import { rememberSandboxRoot, rootContainsProtectedHome } from './sandbox-lifetime'

const here = path.dirname(fileURLToPath(import.meta.url))
const windows = process.platform === 'win32'

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

    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50)
  }

  return predicate()
}

function deadOwnerPid(): number {
  const pidFile = path.join(os.tmpdir(), `hermes-e2e-dead-owner-${process.pid}-${Date.now()}.pid`)
  const child = spawn(
    process.execPath,
    ['-e', 'require("node:fs").writeFileSync(process.argv[1], String(process.pid))', pidFile],
    { stdio: 'ignore', windowsHide: true }
  )
  const pid = child.pid

  if (!pid || !waitFor(() => fs.existsSync(pidFile) && !alive(pid), 5_000)) {
    throw new Error(`owner probe ${pid ?? 'missing'} did not exit`)
  }

  const recorded = Number(fs.readFileSync(pidFile, 'utf8'))
  fs.rmSync(pidFile, { force: true })

  return recorded
}

function orphan(home: string, pidFile: string): number {
  const launcher = spawn(process.execPath, ['-e', LAUNCHER, home, pidFile], {
    stdio: 'ignore',
    windowsHide: true,
  })

  if (!waitFor(() => fs.existsSync(pidFile), 10_000)) {
    throw new Error('orphan launcher did not write a pid')
  }

  const pid = Number(fs.readFileSync(pidFile, 'utf8'))

  if (!waitFor(() => !alive(launcher.pid ?? 0) && alive(pid), 10_000)) {
    throw new Error(`orphan ${pid} did not outlive its launcher`)
  }

  return pid
}

function scratchRoot(): string {
  const base = path.join('S:', 'ForceAgents', 'hera', 'scratch', 'reap-t_76d369d9')
  fs.mkdirSync(base, { recursive: true })

  return fs.mkdtempSync(path.join(base, 'core-'))
}

describe('custom core-root sandbox lifetime', () => {
  it('reaps a dead-owner scratch root and leaves a live owner alone', () => {
    const root = scratchRoot()
    const home = path.join(root, 'home', '.hermes')
    fs.mkdirSync(home, { recursive: true })
    const savedCore = process.env.HERMES_E2E_CORE_ROOT
    const savedTmp = process.env.HERMES_E2E_RUN_TMP
    delete process.env.HERMES_E2E_CORE_ROOT
    delete process.env.HERMES_E2E_RUN_TMP

    const pidFile = path.join(root, 'orphan.pid')
    let pid = 0

    try {
      expect(defaultSandboxReapRoots().some(candidate => pathUnder(home, candidate))).toBe(false)

      rememberSandboxRoot(root, process.pid)
      expect(defaultSandboxReapRoots().some(candidate => pathUnder(home, candidate))).toBe(false)

      rememberSandboxRoot(root, deadOwnerPid())
      expect(defaultSandboxReapRoots().some(candidate => pathUnder(home, candidate))).toBe(true)

      pid = orphan(home, pidFile)
      reapOrphanSandboxProcesses([root])
      expect(waitFor(() => !alive(pid), 15_000)).toBe(true)
    } finally {
      if (savedCore === undefined) {
        delete process.env.HERMES_E2E_CORE_ROOT
      } else {
        process.env.HERMES_E2E_CORE_ROOT = savedCore
      }

      if (savedTmp === undefined) {
        delete process.env.HERMES_E2E_RUN_TMP
      } else {
        process.env.HERMES_E2E_RUN_TMP = savedTmp
      }

      if (pid && alive(pid)) {
        spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true })
      }

      fs.rmSync(root, { recursive: true, force: true })
    }
  })

  it('does not signal a real profile home even when the recorded root contains it', () => {
    const root = scratchRoot()
    const profileHome = path.join(root, 'profiles', 'hera')
    fs.mkdirSync(profileHome, { recursive: true })
    const pidFile = path.join(root, 'profile.pid')
    let pid = 0

    try {
      rememberSandboxRoot(root, deadOwnerPid())
      expect(isSandboxHome(profileHome, [root])).toBe(false)
      pid = orphan(profileHome, pidFile)
      reapOrphanSandboxProcesses([root])
      expect(waitFor(() => alive(pid), 2_000)).toBe(true)
      expect(alive(pid)).toBe(true)
    } finally {
      if (pid) {
        spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true })
      }

      fs.rmSync(root, { recursive: true, force: true })
    }
  })

  it('refuses to record the live home', () => {
    const local = process.env.LOCALAPPDATA

    if (!local) {
      return
    }

    const live = path.join(local, 'hermes')

    expect(rootContainsProtectedHome(live)).toBe(true)
    expect(rootContainsProtectedHome(path.join(live, 'profiles'))).toBe(true)
    expect(() => rememberSandboxRoot(live)).toThrow(/protected/)
    expect(isSandboxHome(live, [path.dirname(live)])).toBe(false)
    expect(isSandboxHome(path.join(live, 'profiles', 'hera'), [live])).toBe(false)
  })

  it('kills a non-breakaway child when its owner is hard-killed', () => {
    if (!windows) {
      return
    }

    const stamp = `${process.pid}-${Date.now()}`
    const ready = path.join(os.tmpdir(), `hermes-e2e-job-${stamp}.ready`)
    const pidFile = path.join(os.tmpdir(), `hermes-e2e-job-${stamp}.pid`)
    const holder = path.join(here, 'sandbox-job-holder.py')
    const ownerScript = path.join(here, 'sandbox-job-owner.mjs')
    const python = process.env.HERMES_E2E_PYTHON || 'py'
    const owner = spawn(process.execPath, [ownerScript, python, holder, ready, pidFile], {
      stdio: 'ignore',
      windowsHide: true,
    })
    let childPid = 0

    try {
      expect(waitFor(() => fs.existsSync(pidFile), 15_000)).toBe(true)
      childPid = Number(fs.readFileSync(pidFile, 'utf8'))
      expect(alive(childPid)).toBe(true)
      const killed = spawnSync('taskkill', ['/F', '/PID', String(owner.pid)], {
        stdio: 'ignore',
        windowsHide: true,
      })
      expect(killed.status).toBe(0)
      expect(waitFor(() => !alive(childPid), 15_000)).toBe(true)
    } finally {
      if (owner.pid && alive(owner.pid)) {
        spawnSync('taskkill', ['/F', '/PID', String(owner.pid)], { stdio: 'ignore', windowsHide: true })
      }

      if (childPid && alive(childPid)) {
        spawnSync('taskkill', ['/F', '/PID', String(childPid)], { stdio: 'ignore', windowsHide: true })
      }

      fs.rmSync(ready, { force: true })
      fs.rmSync(pidFile, { force: true })
    }
  })
})
