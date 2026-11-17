/**
 * Keep a desktop e2e sandbox gateway from outliving the Playwright worker.
 *
 * A custom HERMES_E2E_CORE_ROOT (the call demo uses S:\\ForceAgents\\...\\scratch)
 * is not under the machine temp dir or an agent's tmp folder, so the default
 * reap never sees it once that worker is gone. The gateway also breaks away
 * from the worker, so a hard kill — which never runs cleanup() — leaves it up.
 *
 * Recording the sandbox root lets a later reap find leftovers whose owner pid
 * is already dead, without sweeping a still-running demo (its gateway's own
 * parent, the ensure client, is already dead while Playwright is still going).
 * The Windows job kills non-breakaway children when the worker dies, including
 * a hard kill. The live home and a real profiles\\<name> home are never recorded.
 */
import { spawn } from 'node:child_process'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import { pathUnder } from './process-environment'

const here = path.dirname(fileURLToPath(import.meta.url))

export function sandboxRegistryDir(): string {
  return path.join(os.tmpdir(), 'hermes-e2e-sandbox-roots')
}

export function liveHermesHome(): string | null {
  const local = process.env.LOCALAPPDATA

  if (!local) {
    return null
  }

  return path.resolve(local, 'hermes')
}

/** A root that is, or contains, the live install or its profiles directory. */
export function rootContainsProtectedHome(root: string): boolean {
  const live = liveHermesHome()

  if (!live) {
    return false
  }

  const resolved = path.resolve(root)
  const profiles = path.join(live, 'profiles')

  return pathUnder(live, resolved) || pathUnder(profiles, resolved)
    || path.resolve(live) === resolved
    || path.resolve(profiles) === resolved
}

function pidAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) {
    return false
  }

  try {
    process.kill(pid, 0)

    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM'
  }
}

function recordPath(root: string, ownerPid: number): string {
  const slug = Buffer.from(path.resolve(root)).toString('hex').slice(0, 24)

  return path.join(sandboxRegistryDir(), `${ownerPid}-${slug}.json`)
}

/** Remember a sandbox root so a later reap can find it after this worker dies. */
export function rememberSandboxRoot(root: string, ownerPid = process.pid): void {
  if (rootContainsProtectedHome(root)) {
    throw new Error(`refusing to record a protected hermes root: ${root}`)
  }

  const dir = sandboxRegistryDir()
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(recordPath(root, ownerPid), JSON.stringify({
    root: path.resolve(root),
    ownerPid,
  }))
}

/** Recorded roots whose owner is already dead. A live owner is left alone. */
export function recordedDeadOwnerRoots(): string[] {
  const dir = sandboxRegistryDir()

  if (!fs.existsSync(dir)) {
    return []
  }

  const roots: string[] = []

  for (const name of fs.readdirSync(dir)) {
    if (!name.endsWith('.json')) {
      continue
    }

    try {
      const record = JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8')) as {
        root?: string
        ownerPid?: number
      }

      if (!record.root || rootContainsProtectedHome(record.root)) {
        continue
      }

      if (pidAlive(record.ownerPid ?? 0)) {
        continue
      }

      roots.push(path.resolve(record.root))
    } catch {
      // A torn write is not a root.
    }
  }

  return roots
}

function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms)
}

let jobReady: 'ok' | 'fail' | 'skip' | null = null

/**
 * Assign this process to a kill-on-close job so a hard-killed worker takes its
 * sandbox gateway with it. Once per process. Never assigns the live gateway:
 * the job only contains this process and children that do not break away.
 */
export function ensureSandboxJob(): 'ok' | 'fail' | 'skip' {
  if (jobReady) {
    return jobReady
  }

  if (process.platform !== 'win32') {
    jobReady = 'skip'

    return jobReady
  }

  const ready = path.join(os.tmpdir(), `hermes-e2e-job-${process.pid}-${Date.now()}.ready`)
  const python = process.env.HERMES_E2E_PYTHON || process.env.HERMES_DESKTOP_PYTHON || 'python'
  const child = spawn(python, [
    path.join(here, 'sandbox-job-holder.py'),
    String(process.pid),
    ready,
  ], {
    stdio: 'ignore',
    windowsHide: true,
  })

  child.unref()

  for (const deadline = Date.now() + 5_000; Date.now() < deadline; ) {
    if (fs.existsSync(ready)) {
      const text = fs.readFileSync(ready, 'utf8').trim()
      jobReady = text === 'ok' || text === 'skip' ? text : 'fail'

      return jobReady
    }

    sleepSync(50)
  }

  jobReady = 'fail'

  return jobReady
}

/** Record the root and hold the job. The caller still reaps on a normal exit. */
export function bindSandboxLifetime(root: string): 'ok' | 'fail' | 'skip' {
  rememberSandboxRoot(root, process.pid)

  return ensureSandboxJob()
}
