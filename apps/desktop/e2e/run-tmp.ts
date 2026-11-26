/**
 * One temp root per Playwright run, removed when the runner exits.
 *
 * Playwright (artifact and stack-trace dirs), the Electron app, its backend
 * and every sandbox mkdtemp under os.tmpdir(), and a run that fails or times
 * out in setup leaves all of it behind (a clean run of two specs left eight
 * `playwright-tracing-*` dirs). Workers and the apps they launch inherit
 * TMPDIR, so everything lands here; a SIGKILLed run leaves one dir, not dozens.
 *
 * The root also names the run's backends (`HERMES_E2E_RUN_TMP` in their
 * environment): the exit hook stops them before removing it, and the next
 * runner stops the ones a killed run left (./run-tmp-reaper). Canonical, so a
 * backend's resolved HERMES_HOME still lies under it behind a symlinked tmpdir.
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
import { reapAbandonedRuns, reapRunBackends, recordRunOwner } from './run-tmp-reaper'

function resolveRunTmpParent(): string {
  const named = process.env.HERMES_E2E_CORE_ROOT
  if (named) return named
  const local = process.env.LOCALAPPDATA
  if (local) return path.join(local, 'Temp', 'hermes-e2e')
  return process.env.TMP || process.env.TEMP || os.tmpdir()
}

export async function prepareRunTmp(): Promise<string> {
  reapOrphanSandboxProcesses()
  reapAbandonedRuns()
  const root = fs.realpathSync(fs.mkdtempSync(path.join(resolveRunTmpParent(), 'pw-')))
  recordRunOwner(root)
  process.env.HERMES_E2E_RUN_TMP = root
  process.env.TMPDIR = root
  process.env.TEMP = root
  process.env.TMP = root
  process.on('exit', () => {
    try {
      reapOrphanSandboxProcesses([root, ...(process.env.HERMES_E2E_CORE_ROOT ? [process.env.HERMES_E2E_CORE_ROOT] : [])])
    } catch {
      // a dead sandbox must not hide the temp cleanup
    }
    reapRunBackends(candidate => candidate === root)
    try {
      fs.rmSync(root, { recursive: true, force: true })
    } catch {
      // Windows can still hold a handle for a moment
    }
  })
}
