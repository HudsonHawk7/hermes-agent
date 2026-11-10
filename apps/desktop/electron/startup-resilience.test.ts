import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, expect, test } from 'vitest'

import { installerUrlForRef } from './bootstrap-runner'
import { createStaleGatewayRestarter } from './local-gateway'
import { resolveSourceInstallationBackend } from './source-backend'
import {
  enterRuntimeProbe,
  installScriptSource,
  probeWithBackoff,
  refuseFirstRunWhenCheckoutPresent,
  resetRuntimeProbeForTests,
  STILL_STARTING_PREFIX
} from './startup-resilience'

afterEach(() => {
  resetRuntimeProbeForTests()
})

test('ignore-existing with a present checkout falls through to bootstrap (first-run choice)', () => {
  const decision = refuseFirstRunWhenCheckoutPresent({
    checkoutPresent: true,
    probeOk: false,
    repair: false,
    ignoreExisting: true,
    elapsedMs: 0,
    reason: 'exit',
    exitCode: null,
    attempts: 0
  })

  expect(decision.action).toBe('bootstrap')
})

test('probe timeout plus an existing checkout does not bootstrap and shows the retry state', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-probe-'))
  const home = path.join(root, 'home')

  fs.mkdirSync(path.join(root, 'hermes_cli'), { recursive: true })
  fs.mkdirSync(path.join(root, '.hermes', 'bin'), { recursive: true })
  fs.writeFileSync(path.join(root, 'hermes_cli', 'main.py'), '# checkout\n')
  fs.writeFileSync(
    path.join(root, '.hermes', 'bin', process.platform === 'win32' ? 'hermes.cmd' : 'hermes'),
    '@echo off\n'
  )

  let calls = 0
  const logs: string[] = []

  const backend = await resolveSourceInstallationBackend(root, [], {
    hermesHome: home,
    isWindows: process.platform === 'win32',
    sleep: async () => undefined,
    log: line => logs.push(line),
    probe: async () => {
      calls += 1
      const err = new Error('timeout') as Error & { killed?: boolean }

      err.killed = true
      throw err
    }
  })

  const decision = refuseFirstRunWhenCheckoutPresent({
    checkoutPresent: true,
    probeOk: false,
    repair: false,
    ignoreExisting: false,
    elapsedMs: 22_000,
    reason: 'timeout',
    exitCode: null,
    attempts: 3
  })

  expect(backend).toBeNull()
  expect(calls).toBe(3)
  expect(logs.join('\n')).toContain('timeout')
  expect(decision.action).toBe('still-starting')

  if (decision.action === 'still-starting') {
    expect(decision.message.startsWith(STILL_STARTING_PREFIX)).toBe(true)
    expect(decision.message).toContain('probe took 22 s')
  }

  fs.rmSync(root, { recursive: true, force: true })
})

test('installer URL for branch force/carry resolves to the fork remote', () => {
  const source = installerUrlForRef(
    'force/carry',
    'install.ps1',
    'https://github.com/HudsonHawk7/hermes-agent.git'
  )

  expect('url' in source).toBe(true)

  if ('url' in source) {
    expect(source.url).toBe(
      'https://raw.githubusercontent.com/HudsonHawk7/hermes-agent/force/carry/scripts/install.ps1'
    )
    expect(source.ownerRepo).toBe('HudsonHawk7/hermes-agent')
    expect(source.url).not.toContain('NousResearch')
  }

  const missing = installScriptSource({ ref: 'force/carry', scriptName: 'install.ps1', remoteUrl: null })

  expect('error' in missing).toBe(true)

  if ('error' in missing) {
    expect(missing.error).toContain('force/carry')
    expect(missing.error).toContain('could not be read')
    expect(missing.error).not.toContain('https://raw.githubusercontent.com/NousResearch')
  }
})

test('stale-sha gateway at launch does not restart while the runtime probe is in flight', async () => {
  const restart = async () => {
    throw new Error('gateway restart must not run while probing')
  }

  const logs: string[] = []
  const restarter = createStaleGatewayRestarter(restart, line => logs.push(line))
  const leave = enterRuntimeProbe()

  const endpoint = {
    profile_id: '/h',
    instance_id: 'old',
    authority_epoch: 1,
    runtime_protocol: 1,
    api_origin: 'http://127.0.0.1:8642',
    capabilities: ['session-authority-v1'],
    supervisor: 'none',
    code_sha: '9a2a4ed'
  }

  const held = await restarter(endpoint, { gateway: '9a2a4ed', client: 'a37ac70' })

  leave()

  expect(held).toBeNull()
  expect(logs.join('\n')).toContain('gateway is on older code; restart it at a quiet moment')
  expect(logs.join('\n')).not.toContain('restarting it')

  let restarted = false

  const after = createStaleGatewayRestarter(async () => {
    restarted = true
  }, () => undefined)

  await after(endpoint, { gateway: '9a2a4ed', client: 'a37ac70' })
  expect(restarted).toBe(true)
})

test('probeWithBackoff stops on the first success and classifies a timeout', async () => {
  let calls = 0

  const ok = await probeWithBackoff(
    async () => {
      calls += 1
    },
    { sleep: async () => undefined }
  )

  expect(ok.ok).toBe(true)
  expect(calls).toBe(1)

  const failed = await probeWithBackoff(
    async () => {
      const err = new Error('slow') as Error & { code?: string }

      err.code = 'ETIMEDOUT'
      throw err
    },
    { sleep: async () => undefined, tries: 3, backoffMs: [1, 1] }
  )

  expect(failed.ok).toBe(false)

  if (!failed.ok) {
    expect(failed.failure.reason).toBe('timeout')
    expect(failed.failure.attempts).toBe(3)
  }
})
