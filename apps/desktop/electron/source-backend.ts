import { existsSync } from 'node:fs'
import path from 'node:path'

import { buildDesktopBackendEnv } from './backend-env'
import { execProbe, PROBE_TIMEOUT_MS } from './backend-probes'
import { enterRuntimeProbe, noteProbeFailure, probeWithBackoff } from './startup-resilience'
import { resolveInstallationLauncher } from './updater-process'

export interface SourceBackend {
  kind: 'command' | 'python'
  label: string
  command: string
  args: string[]
  env: NodeJS.ProcessEnv
  root: string
  bootstrap: false
  shell: boolean
  local: 'installed'
}

interface SourceOptions {
  isWindows?: boolean
  env?: NodeJS.ProcessEnv
  hermesHome?: string
  log?: (line: string) => void
  sleep?: (ms: number) => Promise<void>
  probe?: () => Promise<void>
}

/** Keep the validated command. PM owns interpreter and generation selection. */
export async function resolveSourceInstallationBackend(
  root: string,
  args: string[],
  options: SourceOptions = {}
): Promise<SourceBackend | null> {
  if (!existsSync(path.join(root, 'hermes_cli', 'main.py'))) {
    return null
  }

  const isWindows: boolean = options.isWindows ?? process.platform === 'win32'
  const launcher: string | null = resolveInstallationLauncher(root, isWindows, options.hermesHome)

  if (!launcher) {
    return null
  }

  const shell: boolean = isWindows && /\.(cmd|bat)$/i.test(launcher)
  const command: string = shell ? `"${launcher}"` : launcher
  const env: NodeJS.ProcessEnv = buildDesktopBackendEnv({ currentEnv: options.env ?? process.env })

  const leaveProbe = enterRuntimeProbe()

  try {
    const probe =
      options.probe ??
      (() =>
        execProbe(
          command,
          ['--version'],
          {
            cwd: root,
            env: { ...process.env, ...options.env, ...env },
            shell,
            stdio: 'ignore',
            timeout: PROBE_TIMEOUT_MS,
            windowsHide: true
          },
          false
        ))

    const probed = await probeWithBackoff(probe, { sleep: options.sleep })

    // strictNullChecks:false (tsconfig.electron) does not narrow `if (!ok)` on
    // a boolean-literal discriminant under tsc 6.0.3 — use === false.
    if (probed.ok === false) {
      noteProbeFailure(probed.failure)
      options.log?.(
        `[bootstrap] hermes --version probe ${probed.failure.reason} (exit ${probed.failure.exitCode ?? 'n/a'}) after ${probed.failure.elapsedMs}ms, ${probed.failure.attempts} tries, at ${root}`
      )

      return null
    }
  } finally {
    leaveProbe()
  }

  return {
    kind: 'command',
    label: `Hermes at ${root}`,
    command,
    args: [...args],
    env,
    root,
    bootstrap: false,
    shell,
    local: 'installed'
  }
}

/** Developer overrides retain their interpreter, even outside the checkout. */
export function createSourcePythonBackend(
  root: string,
  python: string | null,
  args: string[],
  options: SourceOptions = {}
): SourceBackend | null {
  if (!python) {
    return null
  }

  let command: string = python

  if ((options.isWindows ?? process.platform === 'win32') && /[\\/]pythonw\.exe$/i.test(python)) {
    // Use only the console interpreter beside the selected windowless one.
    const consolePython: string = python.replace(/pythonw\.exe$/i, 'python.exe')

    if (existsSync(consolePython)) {
      command = consolePython
    }
  }

  return {
    kind: 'python',
    label: `Hermes source at ${root}`,
    command,
    args: ['-m', 'hermes_cli.main', ...args],
    // The backend runs in the user's workspace cwd, and the selected
    // interpreter need not have this checkout installed: name it explicitly.
    // (The scrubbed inherited value could point at another checkout.)
    env: { ...buildDesktopBackendEnv({ currentEnv: options.env ?? process.env }), PYTHONPATH: root },
    root,
    bootstrap: false,
    shell: false,
    local: 'installed'
  }
}
