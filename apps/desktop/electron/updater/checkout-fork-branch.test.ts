/**
 * Desktop Update on this fork must never hand the checkout script a git
 * branch of main or master. That is what checked out upstream main on
 * 2026-11-23 and 2026-11-26. An explicit non-upstream branch is kept.
 */
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import { markerPath } from '../update-marker'
import * as updaterProcess from '../updater-process'

import { type CheckoutStrategyDeps, createCheckoutStrategy } from './checkout'
import type { SourceUpdate } from './checkout-source'

const IS_WINDOWS: boolean = process.platform === 'win32'

function fixture(status: SourceUpdate | null): { root: string; deps: CheckoutStrategyDeps } {
  const root: string = fs.mkdtempSync(path.join(os.tmpdir(), 'checkout-fork-branch-'))
  const home: string = path.join(root, 'profile')
  const scriptDirectory: string = path.join(root, 'scripts', 'desktop-update')
  fs.mkdirSync(home)
  fs.mkdirSync(scriptDirectory, { recursive: true })
  fs.writeFileSync(path.join(scriptDirectory, IS_WINDOWS ? 'windows.ps1' : 'posix.sh'), '')
  fs.writeFileSync(path.join(scriptDirectory, 'runtime.ps1'), '')
  fs.mkdirSync(path.join(root, '.hermes', 'bin'), { recursive: true })
  fs.writeFileSync(path.join(root, '.hermes', 'bin', 'hermes.exe'), '')

  const deps: CheckoutStrategyDeps = {
    readSourceUpdate: async (): Promise<SourceUpdate | null> => status,
    hermesHome: home,
    isWindows: IS_WINDOWS,
    isMac: false,
    defaultUpdateBranch: 'main',
    updateHandoffDwellMs: 0,
    handoffClaimTimeoutMs: 2000,
    resolveUpdateRoot: (): string => root,
    resolveUpdaterBinary: (): null => null,
    remoteGatewayActive: (): boolean => false,
    emitUpdateProgress: vi.fn(),
    rememberLog: vi.fn(),
    startHermes: vi.fn(async (): Promise<void> => {}),
    stopBackendsForUpdate: async (): Promise<void> => {},
    repairMacUpdaterHelper: (): void => {},
    preflightStateDb: (): void => {},
    runningAppBundle: (): null => null,
    markQuittingForHandoff: vi.fn(),
    quit: vi.fn()
  }

  return { root, deps }
}

function branchArg(args: string[]): string | undefined {
  const flag: string = IS_WINDOWS ? '-Branch' : '--branch'
  const index: number = args.indexOf(flag)
  return index === -1 ? undefined : args[index + 1]
}

async function applyAndCapture(status: SourceUpdate | null): Promise<string | undefined> {
  const { root, deps } = fixture(status)
  const spawned: string[][] = []
  vi.spyOn(updaterProcess, 'spawnUpdaterProcess').mockImplementation(
    (
      _command: string,
      args: string[],
      options: Parameters<typeof updaterProcess.spawnUpdaterProcess>[2]
    ): updaterProcess.UpdaterChild => {
      spawned.push(args)
      fs.writeFileSync(markerPath(deps.hermesHome), `${process.ppid}\n${options.env?.HERMES_UPDATE_STARTED_AT}\n`)
      return { unref: (): void => {} }
    }
  )

  try {
    await createCheckoutStrategy(deps).apply()
    return branchArg(spawned[0] ?? [])
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

describe('fork update never checks out upstream main', () => {
  it('rewrites a missing probe that would have fallen back to main', async () => {
    expect(await applyAndCapture(null)).toBe('force/carry')
  })

  it('rewrites a checkout that a previous Update already moved onto main', async () => {
    expect(await applyAndCapture({ supported: true, branch: 'main', updateAvailable: true })).toBe('force/carry')
  })

  it('keeps an explicit branch that is not upstream main', async () => {
    expect(
      await applyAndCapture({
        supported: true,
        branch: 'hera/hermes/desktop/some-fix',
        updateAvailable: false
      })
    ).toBe('hera/hermes/desktop/some-fix')
  })
})
