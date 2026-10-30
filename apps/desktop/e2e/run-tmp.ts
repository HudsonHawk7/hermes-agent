/**
 * One temp root per Playwright run, removed when the runner exits.
 *
 * Playwright (artifact and stack-trace dirs), the Electron app, its backend
 * and every sandbox mkdtemp under os.tmpdir(), and a run that fails or times
 * out in setup leaves all of it behind (a clean run of two specs left eight
 * `playwright-tracing-*` dirs). Workers and the apps they launch inherit
 * TMPDIR, so everything lands here; a SIGKILLed run leaves one dir, not dozens.
 *
 * Config modules load in the runner and again in each worker; only the first
 * (runner) load creates the root.
 *
 * At first load we also reap sandbox Electron/gateway leftovers whose parent
 * is already dead (prior agent runs that outlived their owner).
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { reapOrphanSandboxProcesses } from './core/process-census'

// Prefer a local-disk temp root. Playwright's network/trace copy on Windows
// ENOENTs when TMPDIR lives on a network/subst volume (S:\ForceAgents\…),
// while the orphan reap still scans ForceAgents paths via defaultSandboxReapRoots.
function resolveRunTmpParent(): string {
  const preferred = [
    process.env.HERMES_E2E_CORE_ROOT,
    process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'Temp', 'hermes-e2e') : '',
    process.env.TMP,
    process.env.TEMP,
    os.tmpdir(),
    'S:/ForceAgents/hera/tmp',
    'S:\\ForceAgents\\hera\\tmp',
  ]

  for (const candidate of preferred) {
    if (!candidate) {
      continue
    }

    try {
      fs.mkdirSync(candidate, { recursive: true })

      return candidate
    } catch {
      // try next
    }
  }

  return os.tmpdir()
}

if (!process.env.HERMES_E2E_RUN_TMP) {
  // Prior runs (dead parent, sandbox under pw-*/hermes-e2e) must not pile up.
  try {
    const killed = reapOrphanSandboxProcesses()

    if (killed.length > 0) {
      console.error(`[hermes-desktop-e2e] reaped orphan sandbox pids at run start: ${killed.join(', ')}`)
    }
  } catch (error) {
    console.error('[hermes-desktop-e2e] orphan reap at run start failed:', error)
  }

  const root = fs.mkdtempSync(path.join(resolveRunTmpParent(), 'pw-'))
  process.env.HERMES_E2E_RUN_TMP = root
  process.env.TMPDIR = root
  process.env.TEMP = root
  process.env.TMP = root
  process.on('exit', () => {
    try {
      reapOrphanSandboxProcesses([root, ...((process.env.HERMES_E2E_CORE_ROOT ? [process.env.HERMES_E2E_CORE_ROOT] : []))])
    } catch {
      // best-effort
    }

    try {
      fs.rmSync(root, { recursive: true, force: true })
    } catch {
      // Windows may still hold a handle briefly; leave the dir rather than throw on exit.
    }
  })
}
