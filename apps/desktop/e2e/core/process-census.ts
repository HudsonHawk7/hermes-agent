/**
 * Process census for the core lane, per platform.
 *
 * Linux reads /proc: a process belongs to a sandbox when its environment names the sandbox
 * HERMES_HOME (orphans reparented to init included). Other platforms have no readable foreign
 * environment, so membership is the process tree rooted at every gateway pid a sandbox
 * `gateway.lock` has ever recorded (remembered per sandbox, so the census still sees a stopped
 * gateway's orphans after its lock is gone). Windows keeps a dead parent's id in
 * ParentProcessId, so the tree walk still reaches orphans there.
 *
 * `HERMES_E2E_PROC_CENSUS=ps` forces the tree census on Linux (how the non-/proc path is tested).
 */

import { spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

export interface ProcInfo {
  pid: number
  ppid: number
  cmdline: string
}

export const procfsCensus = process.env.HERMES_E2E_PROC_CENSUS !== 'ps' && fs.existsSync('/proc/self/environ')

export function readProc(pid: number): null | { environ: string; cmdline: string; ppid: number } {
  try {
    const environ = fs.readFileSync(`/proc/${pid}/environ`, 'utf8')
    const cmdline = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').join(' ').trim()
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8')
    // Field 4 (ppid) follows the parenthesised comm, which may contain spaces.
    const ppid = Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1])

    return { environ, cmdline, ppid }
  } catch {
    return null
  }
}

/** Every live process (pid, ppid, command line); zombies/unreadable entries skipped. */
export function listProcesses(): ProcInfo[] {
  if (procfsCensus) {
    return fs.readdirSync('/proc').flatMap(entry => {
      const pid = Number(entry)
      const info = Number.isInteger(pid) ? readProc(pid) : null

      return info?.cmdline ? [{ pid, ppid: info.ppid, cmdline: info.cmdline }] : []
    })
  }

  const listing =
    process.platform === 'win32'
      ? spawnSync(
          'powershell.exe',
          [
            '-NoProfile',
            '-NonInteractive',
            '-Command',
            'Get-CimInstance Win32_Process | ForEach-Object { "$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)" }'
          ],
          { encoding: 'utf8', timeout: 60_000, windowsHide: true }
        ).stdout
      : spawnSync('ps', ['-axww', '-o', 'pid=,ppid=,args='], { encoding: 'utf8', timeout: 30_000 }).stdout

  return String(listing ?? '')
    .split(/\r?\n/)
    .flatMap(line => {
      const match = process.platform === 'win32' ? /^(\d+)\t(\d+)\t(.*)$/.exec(line) : /^\s*(\d+)\s+(\d+)\s+(.*)$/.exec(line)

      return match && match[3].trim() ? [{ pid: Number(match[1]), ppid: Number(match[2]), cmdline: match[3].trim() }] : []
    })
}

const recordedRoots = new Map<string, Set<number>>()

/** Gateway pids recorded in the sandbox's `gateway.lock` files (launch home + each profile home). */
function lockedGatewayPids(hermesHome: string): number[] {
  const profilesDir = path.join(hermesHome, 'profiles')
  const homes = [hermesHome, ...(fs.existsSync(profilesDir) ? fs.readdirSync(profilesDir).map(name => path.join(profilesDir, name)) : [])]

  return homes.flatMap(home => {
    try {
      const pid = Number(JSON.parse(fs.readFileSync(path.join(home, 'gateway.lock'), 'utf8'))?.pid)

      return Number.isInteger(pid) && pid > 0 ? [pid] : []
    } catch {
      return []
    }
  })
}

/** Every live process belonging to the sandbox whose HERMES_HOME is `hermesHome` (orphans included). */
export function sandboxProcessesOf(hermesHome: string, exclude = process.pid): ProcInfo[] {
  if (procfsCensus) {
    const needle = `HERMES_HOME=${hermesHome}\0`

    return fs.readdirSync('/proc').flatMap(entry => {
      const pid = Number(entry)
      const info = Number.isInteger(pid) && pid !== exclude ? readProc(pid) : null

      // Zombies have an empty cmdline and are already dead for our purposes.
      return info?.cmdline && (info.environ + '\0').includes(needle) ? [{ pid, ppid: info.ppid, cmdline: info.cmdline }] : []
    })
  }

  const roots = recordedRoots.get(hermesHome) ?? new Set<number>()
  recordedRoots.set(hermesHome, roots)
  const all = listProcesses()
  const live = new Set(all.map(proc => proc.pid))

  for (const pid of lockedGatewayPids(hermesHome)) {
    if (live.has(pid)) {
      roots.add(pid)
    }
  }

  const members = new Set([...roots].filter(pid => live.has(pid)))
  const parents = new Set(roots)

  for (let grew = true; grew; ) {
    grew = false

    for (const proc of all) {
      if (!parents.has(proc.pid) && parents.has(proc.ppid)) {
        parents.add(proc.pid)
        members.add(proc.pid)
        grew = true
      }
    }
  }

  return all.filter(proc => members.has(proc.pid) && proc.pid !== exclude)
}

/**
 * Every live process whose executable lives under `dir` (Linux /proc only).
 *
 * The HERMES_HOME census cannot see Electron 44+ processes: Chromium's process-title
 * rewrite zeroes the browser's and every child's /proc/<pid>/environ right after start
 * (Electron 40 kept it readable). A packaged app's install directory names its processes
 * just as precisely, whatever their environment shows.
 */
export function processesRunningFrom(dir: string): ProcInfo[] {
  let root: string

  try {
    root = fs.realpathSync(dir) + path.sep
  } catch {
    return []
  }

  return listProcesses().filter(proc => {
    try {
      return proc.pid !== process.pid && fs.readlinkSync(`/proc/${proc.pid}/exe`).startsWith(root)
    } catch {
      return false
    }
  })
}

/** True while `pid` is still a live member of the sandbox (a recycled pid never matches). */
export function isSandboxProcess(pid: number, hermesHome: string): boolean {
  return sandboxProcessesOf(hermesHome).some(proc => proc.pid === pid)
}

/** Every live process whose command line carries `tag`. */
export function processesTagged(tag: string): ProcInfo[] {
  return listProcesses().filter(proc => proc.pid !== process.pid && proc.cmdline.includes(tag))
}

/** True when a process looks like an Electron main/helper binary. */
export function isElectronCmdline(cmdline: string): boolean {
  const lower = cmdline.toLowerCase()

  return (
    lower.includes('electron.exe')
    || /(^|[\\/])electron(\.exe)?(\s|$)/i.test(cmdline)
    || lower.includes('electron.app')
    || /[\\/]electron[\\/]dist[\\/]/i.test(cmdline)
  )
}

/** Live Electron processes (excludes the current process). */
export function listElectronProcesses(): ProcInfo[] {
  return listProcesses().filter(proc => proc.pid !== process.pid && isElectronCmdline(proc.cmdline))
}

/**
 * Electron processes whose command line mentions `marker` (userData path,
 * HERMES_DESKTOP_APP_NAME, worktree path, etc.).
 */
export function electronProcessesMatching(marker: string): ProcInfo[] {
  if (!marker) {
    return []
  }

  const needles = [
    marker,
    marker.replace(/\//g, '\\'),
    marker.replace(/\\/g, '/'),
  ].map(value => value.toLowerCase())

  return listElectronProcesses().filter(proc => {
    const cmd = proc.cmdline.toLowerCase()

    return needles.some(needle => needle && cmd.includes(needle))
  })
}

/**
 * Roots that host agent Playwright sandboxes (HERMES_HOME under pw-* / hermes-e2e-*).
 * Never includes the live install — only scratch/tmp trees.
 */
export function defaultSandboxReapRoots(): string[] {
  const roots = new Set<string>()

  for (const key of ['HERMES_E2E_RUN_TMP', 'HERMES_E2E_CORE_ROOT', 'TMPDIR', 'TEMP', 'TMP'] as const) {
    const value = process.env[key]

    if (value) {
      roots.add(path.resolve(value))
    }
  }

  // Agent scratch on ILUM (Architect 10/29): leftover sandboxes under S:\ForceAgents\hera\tmp\pw-*
  for (const candidate of [
    'S:\\ForceAgents\\hera\\tmp',
    'S:/ForceAgents/hera/tmp',
    path.join('S:', 'ForceAgents', 'hera', 'tmp'),
  ]) {
    try {
      if (fs.existsSync(candidate)) {
        roots.add(path.resolve(candidate))
      }
    } catch {
      // ignore
    }
  }

  try {
    roots.add(path.resolve(os.tmpdir()))
  } catch {
    // ignore
  }

  return [...roots]
}

function cmdlineMentionsRoot(cmdline: string, root: string): boolean {
  if (!root) {
    return false
  }

  const cmd = cmdline.toLowerCase()

  const variants = [
    root,
    root.replace(/\//g, '\\'),
    root.replace(/\\/g, '/'),
  ].map(value => value.toLowerCase())

  return variants.some(needle => needle.length > 3 && cmd.includes(needle))
}

function looksLikeSandboxCmdline(cmdline: string): boolean {
  const lower = cmdline.toLowerCase()

  // Main Electron launched by Playwright only gets apps/desktop + worktree path —
  // user-data-dir / hermes-e2e markers land on child gpu/renderer processes.
  const forceAgentsWorktree =
    lower.includes('forceagents')
    && (lower.includes('hermes-agent') || lower.includes('apps\\desktop') || lower.includes('apps/desktop'))

  return (
    lower.includes('hermes-e2e-')
    || lower.includes('electron-user-data')
    || /[\\/]core-[a-z0-9_-]+/i.test(cmdline)
    || /[\\/]pw-[a-z0-9]+/i.test(cmdline)
    || (lower.includes('gateway run') && (lower.includes('hermes-e2e') || /[\\/]pw-/i.test(cmdline) || lower.includes('forceagents')))
    || (isElectronCmdline(cmdline) && (
      lower.includes('hermes-e2e')
      || /[\\/]pw-/i.test(cmdline)
      || lower.includes('electron-user-data')
      || forceAgentsWorktree
    ))
  )
}

/**
 * Reap sandbox Electron / gateway-run leftovers whose parent is already dead.
 * Safe against the live gateway: only matches processes whose cmdline points at
 * a known sandbox root (pw- or hermes-e2e under agent tmp) OR an agent worktree
 * Electron launch (ForceAgents + apps/desktop). Never bare "gateway run".
 */
export function orphanSandboxProcesses(roots: string[] = defaultSandboxReapRoots()): ProcInfo[] {
  const all = listProcesses()
  const live = new Set(all.map(proc => proc.pid))
  const normalizedRoots = roots.map(root => path.resolve(root)).filter(Boolean)

  // Also treat agent worktree paths as roots when HERMES_E2E points there.
  for (const key of ['HERMES_DESKTOP_HERMES_ROOT'] as const) {
    const value = process.env[key]

    if (value) {
      try {
        normalizedRoots.push(path.resolve(value))
      } catch {
        // ignore
      }
    }
  }

  // Hard-coded ILUM agent worktree root (Architect 10/29).
  for (const candidate of [
    'S:\\ForceAgents\\hera\\work',
    'S:/ForceAgents/hera/work',
  ]) {
    try {
      if (fs.existsSync(candidate)) {
        normalizedRoots.push(path.resolve(candidate))
      }
    } catch {
      // ignore
    }
  }

  return all.filter(proc => {
    if (proc.pid === process.pid) {
      return false
    }

    // Parent still alive → owned by a running run; leave it alone.
    // (Children of an orphan Electron main still have a live ppid = that main;
    //  taskkill /T on the main reaps them.)
    if (proc.ppid > 0 && live.has(proc.ppid)) {
      return false
    }

    if (!looksLikeSandboxCmdline(proc.cmdline)) {
      return false
    }

    // ForceAgents worktree electron is enough without a tmp root hit.
    const lower = proc.cmdline.toLowerCase()

    if (lower.includes('forceagents') && isElectronCmdline(proc.cmdline)) {
      return true
    }

    // Require a sandbox-root marker so the live install gateway is never matched.
    return normalizedRoots.some(root => cmdlineMentionsRoot(proc.cmdline, root))
  })
}

/** Tree-kill orphan sandbox processes (dead parent + sandbox path). Returns killed pids. */
export function reapOrphanSandboxProcesses(roots: string[] = defaultSandboxReapRoots()): number[] {
  const killed: number[] = []

  for (const proc of orphanSandboxProcesses(roots)) {
    try {
      if (process.platform === 'win32') {
        spawnSync('taskkill', ['/PID', String(proc.pid), '/T', '/F'], {
          stdio: 'ignore',
          windowsHide: true,
          timeout: 15_000,
        })
      } else {
        try {
          process.kill(-proc.pid, 'SIGKILL')
        } catch {
          process.kill(proc.pid, 'SIGKILL')
        }
      }

      killed.push(proc.pid)
    } catch {
      // already gone or access denied
    }
  }

  return killed
}

/** `value` single-quoted for the POSIX shell the terminal tool runs (Git Bash on Windows). */
export function shellQuote(value: string): string {
  const posix = process.platform === 'win32' ? value.replace(/^([A-Za-z]):[\\/]/, (_, d: string) => `/${d.toLowerCase()}/`).replace(/\\/g, '/') : value

  return `'${posix.replace(/'/g, `'\\''`)}'`
}
