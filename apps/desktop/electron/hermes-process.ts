import { execFileSync, execSync } from 'node:child_process'

const HERMES_PROCESS_TOKENS = new Set([
  'hermes',
  'hermes.exe',
  'hermes.cmd',
])

/**
 * Match Hermes by executable token, not by substring.
 *
 * A bare `includes('hermes')` check treats any command that merely mentions
 * Hermes — `ps aux | grep hermes`, `tail -f /tmp/hermes.log`,
 * `hermes-dashboard`, a path like `/usr/local/hermes-data/foo` — as a live
 * Hermes process. Those false positives orphan or kill unrelated processes
 * (HX003). Tokenize the command line and accept only an exact executable-name
 * match.
 */
export function commandLineIsHermesRuntime(commandLine: string): boolean {
  if (!commandLine) return false
  const tokens = commandLine.trim().split(/\s+/)
  return tokens.some(token => {
    const base = token.split(/[/\\]/).pop()?.toLowerCase() ?? ''
    return HERMES_PROCESS_TOKENS.has(base)
  })
}

/**
 * Windows has no /proc and `ps` is not a process list. CIM is the read that works.
 * Other platforms return null so the caller uses /proc or ps.
 */
export function hermesProcessProbe(
  platform: NodeJS.Platform,
  pid: number
): { command: string; args: string[] } | null {
  if (!Number.isInteger(pid) || pid <= 0) return null
  if (platform !== 'win32') return null
  return {
    command: 'powershell.exe',
    args: [
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      `Get-CimInstance Win32_Process -Filter "ProcessId=${pid}" | Select-Object -ExpandProperty CommandLine`
    ]
  }
}

export function isHermesProcess(pid: number): boolean {
  try {
    process.kill(pid, 0)
  } catch {
    return false
  }

  if (process.platform === 'win32') {
    const spec = hermesProcessProbe('win32', pid)
    if (!spec) return false
    try {
      const out = execFileSync(spec.command, spec.args, {
        encoding: 'utf8',
        timeout: 2000,
        windowsHide: true
      })
      return commandLineIsHermesRuntime(out)
    } catch {
      return false
    }
  }

  if (process.platform === 'linux') {
    try {
      const cmdline = execFileSync('cat', [`/proc/${pid}/cmdline`], {
        encoding: 'utf8',
        timeout: 1000
      }).replace(/\0/g, ' ')
      return commandLineIsHermesRuntime(cmdline)
    } catch {
      // /proc may be unavailable (e.g. sandboxed); fall through to ps.
    }
  }

  try {
    const out = execSync(`ps -p ${pid} -o args=`, {
      encoding: 'utf8',
      timeout: 2000
    })
    return commandLineIsHermesRuntime(out)
  } catch {
    return false
  }
}
