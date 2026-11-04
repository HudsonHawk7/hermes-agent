import { spawnSync } from 'node:child_process'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import { listProcesses, type ProcInfo } from './process-census'

export interface VisibleWindow {
  hwnd: string
  pid: number
  title: string
}

/** Parse `hwnd<TAB>pid<TAB>title` rows from the Win32 census script. */
export function parseVisibleWindowLines(stdout: string): VisibleWindow[] {
  return String(stdout ?? '')
    .split(/\r?\n/)
    .flatMap(line => {
      const match = /^(\d+)\t(\d+)\t(.*)$/.exec(line)

      return match ? [{ hwnd: match[1], pid: Number(match[2]), title: match[3] }] : []
    })
}

/** Root pid plus every live descendant (Electron spawners included). */
export function descendantPids(rootPid: number, procs?: ProcInfo[]): number[] {
  if (!Number.isInteger(rootPid) || rootPid <= 0) {
    return []
  }

  const all = procs ?? listProcesses()
  const members = new Set<number>([rootPid])

  for (let grew = true; grew; ) {
    grew = false

    for (const proc of all) {
      if (!members.has(proc.pid) && members.has(proc.ppid)) {
        members.add(proc.pid)
        grew = true
      }
    }
  }

  return [...members]
}

const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), 'visible-windows.ps1')

/**
 * Visible, non-minimized top-level windows owned by `pids` in the current
 * interactive session. Hidden BrowserWindows (`show:false`) must not appear.
 */
export function visibleWindowsForPids(pids: number[]): VisibleWindow[] {
  const wanted = [...new Set(pids.filter(pid => Number.isInteger(pid) && pid > 0))]

  if (process.platform !== 'win32' || wanted.length === 0) {
    return []
  }

  const result = spawnSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT, '-ProcessIds', wanted.join(',')],
    { encoding: 'utf8', timeout: 30_000, windowsHide: true },
  )

  if (result.status !== 0) {
    throw new Error(
      `visible-windows census failed (status ${result.status}): ${(result.stderr || result.stdout || '').trim()}`,
    )
  }

  return parseVisibleWindowLines(result.stdout ?? '')
}
