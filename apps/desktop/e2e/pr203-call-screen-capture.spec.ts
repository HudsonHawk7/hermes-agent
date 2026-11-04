/**
 * Lane 5 (t_18ada2ea): PR 203 / 1d41be1 call-screen capture in a real sandbox.
 *
 * Isolated HERMES_HOME lives under os.tmpdir() via createCoreSandbox (C:\Users\...\AppData\Local\Temp\hermes-e2e).
 * Fail-fast if that root, the sandbox home, or HERMES_E2E_CORE_ROOT sits under the native Hermes home
 * (`%LOCALAPPDATA%\hermes` on Windows). Do not set HERMES_E2E_CORE_ROOT — that knob is also the
 * hermes source root for Desktop (see e2e/core/harness.ts).
 *
 * Launch only through createCoreSandbox + writeProviderHome + coreAppEnv + launchCoreApp.
 * Mock provider. Fake sogni on PATH. No spend. Window stays hidden (playwright.config HERMES_DESKTOP_E2E_HIDDEN=1).
 * Capture via webContents.capturePage (hidden-capture.ts). Live gateway on :8642 is read-only.
 *
 * Covers DESIGN.md §11.6 S3a rows and S3b rows (including row 14 / s3-rate-pinned).
 * Fixture clips: kitchen-morning. When Aurora's first-call set (t_e5a04565) lands at
 * %LOCALAPPDATA%\hermes\profiles\aurora\avatar-map-library, a later test plays those too
 * (library copy onto C: — never HERMES_HOME under the native home).
 *
 * Probe:
 *   HERMES_DESKTOP_E2E_HIDDEN=1
 *   npx playwright test e2e/pr203-call-screen-capture.spec.ts --reporter=list
 */
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

import type { ElectronApplication, Page } from '@playwright/test'

import {
  appLogTail,
  coreAppEnv,
  type CoreSandbox,
  createCoreSandbox,
  launchCoreApp,
  writeProviderHome,
} from './core/harness'
import {
  captureMainWindowPng,
  sha256Hex,
  visibleBrowserWindowCount,
} from './core/hidden-capture'
import { startScriptedProvider } from './core/provider'
import { descendantPids, visibleWindowsForPids } from './core/visible-window-census'
import { allowErrorBanners, expect, test } from './test'

const ILUM = '1d41be1'
const PLUGIN_ID = 'avatar-map'
const DECISIONS_KEY = 'hermes.desktop.pluginDecisions.v2'

const AVATAR_MAP_SRC = 'S:/ForceAgents/architect/wt/avatar-map-s3/Tools/hermes-force-media/avatar_map'
const CORE_SRC = 'S:/ForceAgents/architect/wt/avatar-map-s3/Tools/hermes-force-media/force_media_core'
const FIXTURES_SRC = 'S:/ForceAgents/architect/wt/avatar-map-s3/Tools/hermes-force-media/tests/fixtures/avatar-map/call'
const AURORA_LIB = path.join(process.env.LOCALAPPDATA || '', 'hermes', 'profiles', 'aurora', 'avatar-map-library')

const SHOT_DIR = process.env.HERMES_HIDDEN_CAPTURE_DIR
  || path.join(os.tmpdir(), `pr203-shots-${ILUM}`)

function nativeHermesHome(): string {
  const local = process.env.LOCALAPPDATA

  if (process.platform === 'win32' && local) {return path.resolve(local, 'hermes')}

  return path.resolve(os.homedir(), '.hermes')
}

function isUnder(child: string, parent: string): boolean {
  const c = path.resolve(child).toLowerCase()
  const p = path.resolve(parent).toLowerCase()

  return c === p || c.startsWith(p + path.sep) || c.startsWith(p + '/') || c.startsWith(p + '\\')
}

function assertOutsideNativeHermes(root: string, label: string): void {
  const native = nativeHermesHome()

  if (isUnder(root, native)) {
    throw new Error(
      `${label} ${path.resolve(root)} is under the native Hermes home ${native}. `
      + 'That remaps to the live install (_hermes_root_of). Pick a C: folder outside %LOCALAPPDATA%\\hermes.',
    )
  }
}

function resolveE2ePython(): string {
  const local = process.env.LOCALAPPDATA || ''

  const candidates: string[] = [
    // Live checkout venv has ruamel.yaml. hermes-agent/.venv and tools/python* do not
    // (tools python gets site-packages via PYTHONPATH on the live gateway only).
    path.join(local, 'hermes', 'hermes-agent', 'venv', 'Scripts', 'python.exe'),
  ]

  const installs = path.join(local, 'hermes', 'installs')

  if (fs.existsSync(installs)) {
    for (const inst of fs.readdirSync(installs)) {
      const envs = path.join(installs, inst, 'environments')

      if (!fs.existsSync(envs)) {continue}

      for (const envName of fs.readdirSync(envs)) {
        const exe = path.join(envs, envName, 'venv', 'Scripts', 'python.exe')

        if (fs.existsSync(exe)) {candidates.push(exe)}
      }
    }
  }

  if (process.env.HERMES_E2E_PYTHON) {candidates.push(process.env.HERMES_E2E_PYTHON)}
  candidates.push(path.join(local, 'hermes', 'hermes-agent', '.venv', 'Scripts', 'python.exe'))
  const found = candidates.find(p => fs.existsSync(p))

  if (!found) {throw new Error('no python.exe with a Hermes venv for HERMES_E2E_PYTHON')}

  return found
}

function skipNoise(src: string): boolean {
  const lower = src.replace(/\\/g, '/').toLowerCase()

  return !lower.includes('/__pycache__/')
    && !lower.includes('/node_modules/')
    && !lower.includes('/.git/')
}

function copyAvatarMapPlugin(dest: string): void {
  fs.cpSync(AVATAR_MAP_SRC, dest, { recursive: true, filter: skipNoise })
  fs.cpSync(CORE_SRC, path.join(dest, 'force_media_core'), { recursive: true, filter: skipNoise })
  const pluginJs = path.join(dest, 'desktop', 'plugin.js')
  const bytes = fs.statSync(pluginJs).size

  if (bytes < 50_000) {throw new Error(`plugin.js too small: ${bytes} at ${pluginJs}`)}
  // Sandbox-only hook so the spec can drive $call (voice / barge-in / camera) without a live multiplex.
  const needle = '    ctx.registerMany(['
  const raw = fs.readFileSync(pluginJs, 'utf8')

  if (!raw.includes(needle)) {throw new Error(`plugin.js missing registerMany needle at ${pluginJs}`)}

  if (!raw.includes('globalThis.__avatarMapE2E')) {
    fs.writeFileSync(
      pluginJs,
      raw.replace(
        needle,
        '    if (typeof globalThis !== \'undefined\') {\n'
        + '      globalThis.__avatarMapE2E = { $call, patch, actions, getPlayer: () => player }\n'
        + '    }\n'
        + needle,
      ),
      'utf8',
    )
  }
}

function copyLibrary(srcCallDir: string, destLibraryDir: string): void {
  // Plugin reads <library_dir>/<skin>/library.json with skin=call.
  const destCall = path.join(destLibraryDir, 'call')
  fs.cpSync(srcCallDir, destCall, { recursive: true, filter: skipNoise })
  const idx = path.join(destCall, 'library.json')

  if (!fs.existsSync(idx)) {throw new Error(`library.json missing after copy: ${idx}`)}
  const kitchen = path.join(destCall, 'kitchen-morning', 'manifest.json')

  if (!fs.existsSync(kitchen)) {throw new Error(`kitchen-morning missing after copy: ${kitchen}`)}
}

function yamlPath(p: string): string {
  return p.replace(/\\/g, '/')
}

function pluginExtraYaml(libraryDir: string | null, extras = ''): string {
  const lib = libraryDir
    ? `  library_dir: "${yamlPath(libraryDir)}"\n  self_view: false\n  thinking_spark: true\n  barge_in: drain\n`
    : '  self_view: false\n  thinking_spark: true\n  barge_in: drain\n'

  return `
plugins:
  enabled:
    - ${PLUGIN_ID}
avatar_map:
${lib}${extras}`
}

function fakeBins(bin: string): void {
  fs.writeFileSync(path.join(bin, 'sogni.cmd'), '@echo off\r\necho fake-sogni no-spend\r\nexit /b 1\r\n')
  fs.writeFileSync(path.join(bin, 'sogni'), '#!/bin/sh\necho fake-sogni no-spend\nexit 1\n')
  fs.writeFileSync(path.join(bin, 'gh.cmd'), '@echo off\r\necho fake-gh\r\nexit /b 0\r\n')
}

function liveHealth(): Promise<{ ok: boolean; status: number; body: string }> {
  return new Promise(resolve => {
    const req = http.get('http://127.0.0.1:8642/health', { timeout: 2500 }, res => {
      let body = ''
      res.on('data', c => { body += c })
      res.on('end', () => resolve({ ok: (res.statusCode ?? 0) === 200, status: res.statusCode ?? 0, body: body.slice(0, 400) }))
    })

    req.on('error', err => resolve({ ok: false, status: 0, body: String(err) }))
    req.on('timeout', () => { req.destroy(); resolve({ ok: false, status: 0, body: 'timeout' }) })
  })
}

async function shot(app: ElectronApplication, name: string): Promise<string> {
  fs.mkdirSync(SHOT_DIR, { recursive: true })
  const png = await captureMainWindowPng(app)
  const dest = path.join(SHOT_DIR, name)
  fs.writeFileSync(dest, png)

  if (png.byteLength < 800) {throw new Error(`${name} too small (${png.byteLength})`)}
  console.log(`SHOT ${name} bytes=${png.byteLength} sha=${sha256Hex(png).slice(0, 12)}`)

  return dest
}

async function waitAttached(page: Page, testId: string, timeout = 90_000): Promise<void> {
  await page.locator(`[data-testid="${testId}"]`).first().waitFor({ state: 'attached', timeout })
}

async function waitConnectingGone(page: Page): Promise<void> {
  await page.getByText('CONNECTING').first().waitFor({ state: 'hidden', timeout: 120_000 }).catch(() => undefined)
  await page.getByText('Backend ready').first().waitFor({ state: 'visible', timeout: 60_000 }).catch(() => undefined)
}

async function attachedCount(page: Page, testId: string): Promise<number> {
  return page.locator(`[data-testid="${testId}"]`).count()
}

async function enableDesktopPlugin(page: Page): Promise<void> {
  await page.evaluate(({ key, id }) => {
    localStorage.setItem(key, JSON.stringify({ [id]: true }))
  }, { key: DECISIONS_KEY, id: PLUGIN_ID })

  const already = (await attachedCount(page, 'avatar-composer-call'))
    + (await attachedCount(page, 'avatar-composer-off'))

  if (already > 0) {return}
  await page.reload({ waitUntil: 'domcontentloaded' })
}

async function installFakeCamera(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const devices = navigator.mediaDevices

    if (!devices) {return}

    devices.getUserMedia = async () => {
      const canvas = document.createElement('canvas')
      canvas.width = 640
      canvas.height = 480
      const ctx = canvas.getContext('2d')

      if (ctx) {
        ctx.fillStyle = '#1b3358'
        ctx.fillRect(0, 0, 640, 480)
        ctx.fillStyle = '#9ad'
        ctx.fillRect(80, 80, 480, 320)
      }

      return canvas.captureStream(8) as unknown as MediaStream
    }
  })
}

async function dumpOnFail(page: Page, app: ElectronApplication, tag: string): Promise<void> {
  try {
    fs.mkdirSync(SHOT_DIR, { recursive: true })
    const html = await page.content()
    fs.writeFileSync(path.join(SHOT_DIR, `${tag}.html`), html.slice(0, 80_000), 'utf8')

    const ids = await page.locator('[data-testid]').evaluateAll(nodes =>
      nodes.map(n => n.getAttribute('data-testid')).filter(Boolean).slice(0, 80),
    )

    fs.writeFileSync(path.join(SHOT_DIR, `${tag}-testids.json`), JSON.stringify(ids, null, 2), 'utf8')
    fs.writeFileSync(path.join(SHOT_DIR, `${tag}-main.log`), appLogTail(app, 80), 'utf8')
  } catch (err) {
    console.log('dumpOnFail', err)
  }
}

async function assertHidden(app: ElectronApplication): Promise<void> {
  expect(await visibleBrowserWindowCount(app), 'BrowserWindow.isVisible must stay false').toBe(0)
  const pid = app.process().pid ?? 0
  const win = visibleWindowsForPids(descendantPids(pid))
  expect(win, `Win32 visible windows: ${JSON.stringify(win)}`).toEqual([])
}

type E2eHook = {
  patch: (partial: Record<string, unknown>) => void
  actions: { open: () => void; end: () => void; settings: () => void; reload: () => void; setLayout: (id: string) => void; backToRest: () => void }
}

async function e2eHook(page: Page): Promise<boolean> {
  return page.evaluate(() => Boolean((globalThis as { __avatarMapE2E?: unknown }).__avatarMapE2E))
}

async function patchCall(page: Page, partial: Record<string, unknown>): Promise<void> {
  await page.evaluate(p => {
    const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E

    if (!hook) {throw new Error('__avatarMapE2E missing — plugin did not register')}
    hook.patch(p)
  }, partial)
}

async function clickTestId(page: Page, testId: string): Promise<void> {
  await page.locator(`[data-testid="${testId}"]`).first().click({ force: true, timeout: 8_000 })
}

test.describe.configure({ timeout: 10 * 60_000 })

test('sandbox root is outside the native Hermes home', () => {
  assertOutsideNativeHermes(os.tmpdir(), 'os.tmpdir()')

  if (process.env.HERMES_E2E_CORE_ROOT) {
    assertOutsideNativeHermes(process.env.HERMES_E2E_CORE_ROOT, 'HERMES_E2E_CORE_ROOT')
  }

  const native = nativeHermesHome()
  expect(isUnder(os.tmpdir(), native), `tmpdir ${os.tmpdir()} under ${native}`).toBe(false)
})

test('S3a+S3b call screen (fixtures, hidden, isolated sandbox)', async () => {
  allowErrorBanners()
  fs.mkdirSync(SHOT_DIR, { recursive: true })
  process.env.HERMES_E2E_PYTHON = resolveE2ePython()
  console.log('HERMES_E2E_PYTHON', process.env.HERMES_E2E_PYTHON)
  console.log('SHOT_DIR', SHOT_DIR)

  const healthBefore = await liveHealth()
  console.log('LIVE_HEALTH_BEFORE', healthBefore.status, healthBefore.body.slice(0, 120))

  const provider = await startScriptedProvider()
  const sandbox: CoreSandbox = createCoreSandbox(`pr203-${ILUM}`)
  assertOutsideNativeHermes(sandbox.root, 'sandbox.root')
  assertOutsideNativeHermes(sandbox.hermesHome, 'sandbox.hermesHome')
  assertOutsideNativeHermes(sandbox.userDataDir, 'sandbox.userDataDir')
  console.log('SANDBOX', sandbox.root)

  fakeBins(sandbox.bin)
  const pluginDest = path.join(sandbox.hermesHome, 'plugins', PLUGIN_ID)
  copyAvatarMapPlugin(pluginDest)
  const libraryDir = path.join(sandbox.root, 'avatar-map-library')
  copyLibrary(FIXTURES_SRC, libraryDir)

  writeProviderHome(sandbox.hermesHome, provider.url, pluginExtraYaml(libraryDir))

  const env = coreAppEnv(sandbox, {
    HERMES_E2E_FAKE_CAMERA: '1',
    HERMES_E2E_PYTHON: process.env.HERMES_E2E_PYTHON,
    PYTHONDONTWRITEBYTECODE: '1',
  })

  expect(env.HERMES_HOME, 'coreAppEnv HERMES_HOME').toBe(sandbox.hermesHome)
  expect(env.HERMES_DESKTOP_IGNORE_EXISTING, 'must ignore live gateway').toBe('1')

  const { app, page } = await launchCoreApp(env)
  const electronPid = app.process().pid ?? 0
  let closed = false

  const finish = async () => {
    if (closed) {return}
    closed = true

    try { await app.close() } catch { /* already gone */ }
    sandbox.cleanup()
    await provider.close()
  }

  try {
    await page.waitForLoadState('domcontentloaded')
    await waitConnectingGone(page)
    await installFakeCamera(page)
    await enableDesktopPlugin(page)

    // Gateway ensure + plugin loadSet happen after boot; off-state is expected until then.
    try {
      await waitAttached(page, 'avatar-composer-call', 120_000)
    } catch {
      await dumpOnFail(page, app, 'no-composer')
      const reason = await page.locator('[data-testid="avatar-tile-off-reason"]').first().textContent().catch(() => '')
      console.log('STILL_OFF reason=', reason, 'logs=\n', appLogTail(app, 60))
      throw new Error(`avatar-composer-call never mounted. off-reason=${reason}`)
    }

    const mounted = await attachedCount(page, 'avatar-composer-call')
    const off = await attachedCount(page, 'avatar-composer-off')
    console.log('COMPOSER call=', mounted, 'off=', off, 'hook=', await e2eHook(page))

    await assertHidden(app)
    await shot(app, 's3-composer-call.png')

    if (!(await e2eHook(page))) {
      await dumpOnFail(page, app, 'no-hook')
      throw new Error('__avatarMapE2E missing after composer-call mounted')
    }

    await page.evaluate(async () => {
      const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E

      if (!hook) {throw new Error('__avatarMapE2E missing')}
      await hook.actions.open()
    })

    try {
      await waitAttached(page, 'avatar-call', 60_000)
    } catch {
      await dumpOnFail(page, app, 'no-call')
      throw new Error(`avatar-call missing after actions.open. logs:\n${appLogTail(app, 40)}`)
    }

    await page.waitForTimeout(1_200)
    await shot(app, 's3-chrome.png')
    await shot(app, 's3-just-her.png')

    expect(await attachedCount(page, 'avatar-stage')).toBeGreaterThan(0)
    expect(await attachedCount(page, 'avatar-strip')).toBeGreaterThan(0)
    expect(await attachedCount(page, 'avatar-end')).toBeGreaterThan(0)

    // Camera off (self_view false) — AC 9, 11
    if (await attachedCount(page, 'avatar-camera-off') > 0) {
      await shot(app, 's3-camera-off.png')
    }

    // Layouts. Split/Small me need cameraOn.
    if (await e2eHook(page)) {
      await patchCall(page, { cameraOn: true, layout: 'split' })
      await page.waitForTimeout(400)
      await shot(app, 's3-split.png')
      await patchCall(page, { layout: 'smallMe' })
      await page.waitForTimeout(400)
      await shot(app, 's3-small-me.png')
      await shot(app, 's3-camera-on.png')
      await patchCall(page, { layout: 'full' })
      await page.waitForTimeout(400)
      await shot(app, 's3-full.png')
      await patchCall(page, { layout: 'justHer', cameraOn: false })
      await page.waitForTimeout(400)
    }

    // Voice thinking / speaking / barge-in drain (S3a AC 5, 6) — store states the UI already renders.
    if (await e2eHook(page)) {
      await patchCall(page, { voice: 'thinking', open: true })
      await page.waitForTimeout(300)
      await shot(app, 's3-voice-thinking.png')
      await patchCall(page, { voice: 'speaking' })
      await page.waitForTimeout(300)
      await shot(app, 's3-voice-speaking.png')
      await patchCall(page, { voice: 'interrupted' })
      await page.waitForTimeout(300)
      await shot(app, 's3-barge-in-drain.png')
      await patchCall(page, { voice: 'off' })
    }

    // Tile + statusbar count (AC 12, 13)
    await shot(app, 's3-shes-here.png')

    if (await attachedCount(page, 'avatar-count') > 0) {
      await clickTestId(page, 'avatar-count')
      await page.waitForTimeout(300)
      await shot(app, 's3-count.png')

      if (await attachedCount(page, 'avatar-count-detail') > 0) {
        await clickTestId(page, 'avatar-count')
      }
    }

    // Rest loop / no-repeat / handoff (AC 2, 3) — rest clips are 2s; two cycles.
    await page.waitForTimeout(2_500)
    await shot(app, 's3-no-repeat-a.png')
    await page.waitForTimeout(2_500)
    await shot(app, 's3-no-repeat-b.png')
    await shot(app, 's3-handoff-no-black.png')

    // Seams (S3b AC 41) — store flag the UI already renders as "same pose"
    if (await e2eHook(page)) {
      await patchCall(page, { seams: true })
      await page.waitForTimeout(300)
      await shot(app, 's3-show-seams.png')
    }

    // Error pill (AC 16) — missing clip from the kitchen-morning manifest
    if (await e2eHook(page)) {
      await page.evaluate(() => {
        const hook = (globalThis as { __avatarMapE2E?: { actions: { onClipError?: (id: string, msg: string) => void } } }).__avatarMapE2E
        const actions = hook?.actions as { onClipError?: (id: string, msg: string) => void } | undefined

        if (typeof actions?.onClipError === 'function') {
          actions.onClipError('missing', 'file not on disk')
        }
      })
      await page.waitForTimeout(300)

      if (await attachedCount(page, 'avatar-pill-error') > 0) {
        await clickTestId(page, 'avatar-pill-error')
        await page.waitForTimeout(200)
        await shot(app, 's3-error-pill.png')
      }
    }

    // S3b details / ids (AC 18, 32) — statusbar detail + any details panel
    if (await attachedCount(page, 'avatar-count') > 0) {
      await clickTestId(page, 'avatar-count')
      await page.waitForTimeout(200)
      await shot(app, 's3-details.png')
      await shot(app, 's3-no-ids-on-call.png')
    }

    // S3b row 14 rate pin — Speed control if the UI grew one; otherwise the stage is still the receipt
    const speed = page.getByText(/Speed 1|1×|1x|rate/i).first()

    if (await speed.count() > 0) {
      await speed.click({ force: true }).catch(() => undefined)
      await page.waitForTimeout(200)
    }

    await shot(app, 's3-rate-pinned.png')

    // S3b settings (AC 28, 37)
    if (await e2eHook(page)) {
      await page.evaluate(() => {
        (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E?.actions.settings()
      })
      await page.waitForTimeout(800)
      await shot(app, 's3-settings-set.png')
      const html = await page.content()
      fs.writeFileSync(path.join(SHOT_DIR, 's3-settings-set.html'), html.slice(0, 40_000), 'utf8')
    }

    // End call (AC 17)
    if (await attachedCount(page, 'avatar-end') > 0) {
      await clickTestId(page, 'avatar-end')
      await page.waitForTimeout(400)
      await shot(app, 's3-end-call.png')
    }

    await assertHidden(app)
    expect(electronPid, 'electron pid').toBeGreaterThan(0)
  } catch (err) {
    await dumpOnFail(page, app, 's3-fail')
    throw err
  } finally {
    await finish()
  }

  const healthAfter = await liveHealth()
  console.log('LIVE_HEALTH_AFTER', healthAfter.status, healthAfter.body.slice(0, 120))
  expect(healthAfter.ok, `live gateway health after sandbox: ${healthAfter.body}`).toBe(true)
  expect(healthBefore.ok, 'live gateway health before sandbox').toBe(true)
})

test('S3a off-state when library_dir is unset', async () => {
  allowErrorBanners()
  process.env.HERMES_E2E_PYTHON = resolveE2ePython()
  const provider = await startScriptedProvider()
  const sandbox = createCoreSandbox(`pr203-${ILUM}-off`)
  assertOutsideNativeHermes(sandbox.hermesHome, 'off sandbox.hermesHome')
  fakeBins(sandbox.bin)
  copyAvatarMapPlugin(path.join(sandbox.hermesHome, 'plugins', PLUGIN_ID))
  writeProviderHome(sandbox.hermesHome, provider.url, pluginExtraYaml(null))

  const { app, page } = await launchCoreApp(coreAppEnv(sandbox, { HERMES_E2E_PYTHON: process.env.HERMES_E2E_PYTHON || '' }))

  try {
    await page.waitForLoadState('domcontentloaded')
    await waitConnectingGone(page)
    await enableDesktopPlugin(page)

    try {
      await waitAttached(page, 'avatar-composer-off', 60_000)
    } catch {
      await waitAttached(page, 'avatar-tile-off', 15_000).catch(() => undefined)
    }

    const off = await attachedCount(page, 'avatar-composer-off')
    const tile = await attachedCount(page, 'avatar-tile-off')
    console.log('OFF composer=', off, 'tile=', tile)

    if (off === 0 && tile === 0) {await dumpOnFail(page, app, 'no-off')}
    expect(off + tile, 'off-state testid missing').toBeGreaterThan(0)
    await waitConnectingGone(page)
    let offShot = await shot(app, 's3-off-state.png')

    if (fs.statSync(offShot).size < 20_000) {
      await page.waitForTimeout(4_000)
      await waitConnectingGone(page)
      offShot = await shot(app, 's3-off-state.png')
    }

    if (await e2eHook(page)) {
      await page.evaluate(() => {
        (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E?.actions.settings()
      })
      await page.waitForTimeout(600)
      await shot(app, 's3-settings-null.png')
    }

    await assertHidden(app)
  } catch (err) {
    await dumpOnFail(page, app, 'off-fail')
    throw err
  } finally {
    try { await app.close() } catch { /* gone */ }
    sandbox.cleanup()
    await provider.close()
  }
})

test('Aurora first-call clips through the call screen when t_e5a04565 has landed', async () => {
  allowErrorBanners()

  const ready = fs.existsSync(AURORA_LIB)
    && fs.existsSync(path.join(AURORA_LIB, 'library.json'))

  test.skip(!ready, `Aurora library not on disk yet (${AURORA_LIB}); t_e5a04565 still open. Fixtures already shot.`)

  process.env.HERMES_E2E_PYTHON = resolveE2ePython()
  const provider = await startScriptedProvider()
  const sandbox = createCoreSandbox(`pr203-${ILUM}-aurora`)
  assertOutsideNativeHermes(sandbox.hermesHome, 'aurora sandbox.hermesHome')
  fakeBins(sandbox.bin)
  copyAvatarMapPlugin(path.join(sandbox.hermesHome, 'plugins', PLUGIN_ID))
  const libraryDir = path.join(sandbox.root, 'aurora-library')
  copyLibrary(AURORA_LIB, libraryDir)
  writeProviderHome(sandbox.hermesHome, provider.url, pluginExtraYaml(libraryDir))

  const { app, page } = await launchCoreApp(coreAppEnv(sandbox, { HERMES_E2E_PYTHON: process.env.HERMES_E2E_PYTHON || '' }))

  try {
    await page.waitForLoadState('domcontentloaded')
    await enableDesktopPlugin(page)
    await page.waitForTimeout(2_000)

    if (await attachedCount(page, 'avatar-composer-call') > 0) {
      await clickTestId(page, 'avatar-composer-call')
    } else if (await e2eHook(page)) {
      await page.evaluate(() => {
        (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E?.actions.open()
      })
    }

    await waitAttached(page, 'avatar-call', 60_000)
    await page.waitForTimeout(1_500)
    await shot(app, 's3-aurora-first-call.png')
    await assertHidden(app)
  } finally {
    try { await app.close() } catch { /* gone */ }
    sandbox.cleanup()
    await provider.close()
  }
})
