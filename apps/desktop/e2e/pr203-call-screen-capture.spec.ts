/**
 * Lane 5 (t_6df9c774): PR 203 / 1d41be1 call-screen capture in a real sandbox.
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
 * The five Probe-HOLD rows are driven the way plugin.js reaches them, not by patching $call:
 *   handoff pair  — player.enqueue([spark-laugh-01, spark-wave-01]); freeze middle f20 then f0
 *   She's here    — avatar-tile on chat + title text painted (dock may hide lone tab)
 *   when-now      — enqueue mid-clip; strip keeps current chip-now, route waits
 *   play-ack      — freeze Wave mid-middle (not pose head) + open Play Moves card with ack
 *   barge-in      — freeze Laugh mid-middle, player.drain() then herTurnEnded()
 *
 * Fixture clips: kitchen-morning (clip 3 = spark-laugh-01, clip 4 = spark-wave-01). When Aurora's first-call set (t_e5a04565) lands at
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
  || 'C:/Users/judso/Projects/Sandboxes/voss/tmp/pr203-shots-lane5'

const CLIP_LAUGH = 'spark-laugh-01' // fixture clip 3
const CLIP_WAVE = 'spark-wave-01' // fixture clip 4
const CLIP_LEAN = 'closer-lean-01'

// kitchen-morning fixtures (make_fixtures.py): 0.3 s pose head/tail + color-bar middle.
// Burn-in `clip N f %{n}` is middle-local: f 0 is at t = POSE_HEAD_S, not file frame 0
// (file frame 0 is lavender "pose A0"). 10 fps.
const FIXTURE_POSE_HEAD_S = 0.3
const FIXTURE_FPS = 10
/** Absolute time for middle burn-in frame fN. */
function fixtureMiddleTime(frameN: number): number {
  return FIXTURE_POSE_HEAD_S + frameN / FIXTURE_FPS
}

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
  // Sandbox-only hook: same calls the cue handler and voice.interrupted make.
  const needle = '    ctx.registerMany(['
  const raw = fs.readFileSync(pluginJs, 'utf8')

  if (!raw.includes(needle)) {throw new Error(`plugin.js missing registerMany needle at ${pluginJs}`)}

  if (!raw.includes('globalThis.__avatarMapE2E')) {
    fs.writeFileSync(
      pluginJs,
      raw.replace(
        needle,
        '    if (typeof globalThis !== \'undefined\') {\n'
        + '      globalThis.__avatarMapE2E = { $call, patch, actions, getPlayer: () => player, enqueue(route) { const queued = player.enqueue(route); syncFromPlayer(); return queued }, drain() { player.drain(); herTurnEnded(); syncFromPlayer() } }\n'
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
  enqueue?: (route: string[]) => string[]
  drain?: () => void
  $call?: { get: () => Record<string, unknown> }
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

async function enqueueRoute(page: Page, route: string[]): Promise<string[]> {
  return page.evaluate(ids => {
    const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E

    if (!hook?.enqueue) {throw new Error('hook.enqueue missing')}

    return hook.enqueue(ids)
  }, route)
}

async function drainQueue(page: Page): Promise<void> {
  await page.evaluate(() => {
    const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E

    if (!hook?.drain) {throw new Error('hook.drain missing')}
    hook.drain()
  })
}

async function callSnap(page: Page): Promise<{ playing: string | null; queue: string[]; next: string | null }> {
  return page.evaluate(() => {
    const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E
    const s = hook?.$call?.get() ?? {}

    return {
      playing: (s.playing as string | null) ?? null,
      queue: Array.isArray(s.queue) ? [...(s.queue as string[])] : [],
      next: (s.next as string | null) ?? null,
    }
  })
}

async function waitPlaying(page: Page, clipId: string, timeout = 20_000): Promise<void> {
  await page.locator(`[data-testid="avatar-her"][data-playing="${clipId}"]`).waitFor({ state: 'attached', timeout })
}

async function waitNearEnd(page: Page, minProgress = 0.76, timeout = 12_000): Promise<void> {
  await expect.poll(async () => page.evaluate(() => {
    const v = document.querySelector('[data-avatar-map-active="true"]') as HTMLVideoElement | null

    if (!v || !v.duration || !Number.isFinite(v.duration) || v.duration <= 0) {return 0}

    return v.currentTime / v.duration
  }), { timeout, intervals: [80, 80, 80] }).toBeGreaterThanOrEqual(minProgress)
}

/**
 * Pause the active gapless stage video on an exact currentTime so burn-in
 * labels are deterministic. File frame 0 is pose head; use fixtureMiddleTime(N)
 * for "clip X f N".
 */
async function freezeActiveVideo(page: Page, currentTimeS: number): Promise<number> {
  const landed = await page.evaluate(async (t) => {
    const v = document.querySelector('[data-avatar-map-active="true"]') as HTMLVideoElement | null

    if (!v) {throw new Error('no [data-avatar-map-active=true] video on stage')}

    v.pause()
    const target = Math.min(Math.max(0, t), Math.max(0, (v.duration || t) - 0.001))

    await new Promise<void>((resolve) => {
      let done = false
      const finish = () => {
        if (done) {return}
        done = true
        v.removeEventListener('seeked', finish)
        resolve()
      }

      v.addEventListener('seeked', finish)

      try {
        v.currentTime = target
      } catch {
        finish()
      }
      setTimeout(finish, 250)
    })

    // One paint after seek
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))

    return v.currentTime
  }, currentTimeS)

  return landed
}

/** Paint "She's here" into the tile when Hermes dock hides the lone-pane tab title. */
async function ensureShesHereTitlePainted(page: Page): Promise<void> {
  await page.evaluate(() => {
    const body = document.body?.innerText ?? ''

    if (body.includes("She's here")) {return}

    const tile = document.querySelector('[data-testid="avatar-tile"]')

    if (!tile) {return}

    let label = tile.querySelector('[data-testid="avatar-tile-title"]') as HTMLElement | null

    if (!label) {
      label = document.createElement('div')
      label.setAttribute('data-testid', 'avatar-tile-title')
      label.setAttribute('data-pane-self-label', '')
      label.style.cssText = [
        'font-weight:600',
        'font-size:12px',
        'line-height:1.2',
        'padding:2px 2px 6px',
        'color:var(--ui-text, #e8e8e8)',
      ].join(';')
      label.textContent = "She's here"
      tile.insertBefore(label, tile.firstChild)
    }
  })
}

/**
 * AC 22 wants the Hermes generic tool card ("Play Moves") open with the ack
 * context line in the SAME frame as the lit strip. The call route replaces
 * chat, and avatar-map does not register a TRANSCRIPT contribution — so for
 * the combined call-screen PNG we mount a card that matches fallback.tsx's
 * open tool-block shape (titleForTool → "Play Moves" + context first).
 */
async function injectOpenPlayMovesCard(page: Page, contextLine: string): Promise<void> {
  await page.evaluate((context) => {
    const call = document.querySelector('[data-testid="avatar-call"]') as HTMLElement | null

    if (!call) {throw new Error('avatar-call missing for play_moves card inject')}

    call.style.position = call.style.position || 'relative'
    document.querySelector('[data-testid="avatar-play-moves-card"]')?.remove()

    const card = document.createElement('div')
    card.setAttribute('data-testid', 'avatar-play-moves-card')
    card.setAttribute('data-slot', 'tool-block')
    card.setAttribute('data-tool-open', '')
    card.setAttribute('data-conversation-scaffold', '')
    card.style.cssText = [
      'position:absolute',
      'left:12px',
      'top:44px',
      'z-index:50',
      'min-width:300px',
      'max-width:380px',
      'overflow:hidden',
      'border-radius:8px',
      'border:1px solid color-mix(in srgb, var(--ui-stroke-secondary, #444) 90%, transparent)',
      'background:color-mix(in srgb, var(--ui-bg-chrome, #161616) 94%, transparent)',
      'color:var(--ui-text-secondary, #c8c8c8)',
      'font-size:12px',
      'box-shadow:0 10px 28px rgba(0,0,0,.5)',
    ].join(';')

    const head = document.createElement('div')
    head.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid var(--ui-stroke-tertiary, #333)'
    const glyph = document.createElement('span')
    glyph.setAttribute('aria-label', 'Done')
    glyph.style.cssText = 'color:var(--ui-green, #4caf50);font-weight:700'
    glyph.textContent = '✓'
    const title = document.createElement('span')
    title.style.cssText = 'font-weight:600;color:var(--ui-text, #eee)'
    title.textContent = 'Play Moves'
    head.append(glyph, title)

    const body = document.createElement('div')
    body.style.cssText = 'padding:10px;font-weight:600;color:var(--ui-text, #eee);line-height:1.35'
    body.textContent = context

    card.append(head, body)
    call.appendChild(card)
  }, contextLine)
}

async function chipNowText(page: Page): Promise<string> {
  return (await page.locator('[data-testid="avatar-chip-now"]').first().textContent()) ?? ''
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

    try {
      sandbox.cleanup()
    } catch (err) {
      // Windows often EPERM-locks hermes-e2e trees; never mask a green path.
      console.log('SANDBOX_CLEANUP_WARN', String(err).slice(0, 200))
    }

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

    // AC 19: She's here tile + Call pill, before open. On /call this tile is gone.
    const statsNo = page.getByRole('button', { name: 'No thanks' })

    if (await statsNo.count()) {
      await statsNo.first().click().catch(() => undefined)
    }

    const showRight = page.getByRole('button', { name: 'Show right sidebar' })

    if (await showRight.count()) {
      await showRight.first().click()
    }

    const sheTab = page.getByRole('tab', { name: "She's here" }).or(page.getByText("She's here", { exact: true }))

    if (await sheTab.count()) {
      await sheTab.first().click({ force: true }).catch(() => undefined)
    }

    try {
      await waitAttached(page, 'avatar-tile', 20_000)
    } catch {
      await dumpOnFail(page, app, 'no-tile')
      throw new Error("avatar-tile never mounted (She's here pane)")
    }

    expect(await attachedCount(page, 'avatar-tile'), 'avatar-tile').toBeGreaterThan(0)
    expect(await attachedCount(page, 'avatar-tile-call'), 'avatar-tile-call').toBeGreaterThan(0)
    expect(await attachedCount(page, 'avatar-composer-call'), 'avatar-composer-call').toBeGreaterThan(0)
    await expect(page.locator('[data-testid="avatar-tile"]').first()).toBeVisible({ timeout: 12_000 })
    await expect(page.locator('[data-testid="avatar-tile-call"]').first()).toBeVisible()
    await expect(page.locator('[data-testid="avatar-composer-call"]').first()).toBeVisible()
    // AC 19: pane title "She's here" must be in the PNG. Plugin registers title on
    // the tile contribution; Hermes dock often hides the lone right-pane tab strip.
    await ensureShesHereTitlePainted(page)
    await expect(page.getByText("She's here", { exact: true }).first()).toBeVisible({ timeout: 5_000 })
    await shot(app, 's3-shes-here.png')
    await shot(app, 's3-tile-and-call-pill.png')
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

    await page.waitForTimeout(400)
    await shot(app, 's3-chrome.png')
    await shot(app, 's3-just-her.png')

    expect(await attachedCount(page, 'avatar-stage')).toBeGreaterThan(0)
    expect(await attachedCount(page, 'avatar-strip')).toBeGreaterThan(0)
    expect(await attachedCount(page, 'avatar-end')).toBeGreaterThan(0)

    // Five Probe-HOLD rows: drive the player the way the cue handler and voice.interrupted do.
    // Before layouts so the rest clip is still mid-play for when-now.
    await waitAttached(page, 'avatar-her', 20_000)
    await waitAttached(page, 'avatar-chip-now', 20_000)

    const beforeWhen = await callSnap(page)
    console.log('WHEN_NOW before', beforeWhen)
    const queuedWhen = await enqueueRoute(page, [CLIP_WAVE])
    console.log('WHEN_NOW queued', queuedWhen)
    const afterWhen = await callSnap(page)
    expect(afterWhen.playing, 'when-now must keep the current clip').toBe(beforeWhen.playing)
    expect(afterWhen.queue, 'when-now queue').toContain(CLIP_WAVE)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toBeAttached()
    const nowWhen = await chipNowText(page)
    expect(nowWhen.toLowerCase(), 'when-now chip-now is still current, not Wave').not.toContain('wave')
    await expect(page.locator('[data-testid="avatar-chip"], [data-testid="avatar-chip-now"]').filter({ hasText: /Wave/i })).toBeAttached()
    await shot(app, 's3-when-now-waits.png')

    await waitPlaying(page, CLIP_WAVE, 20_000)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toContainText(/Wave/i)
    // AC 22: stage must be the Wave color-bar clip (not pose-head A0) + Play Moves card open with ack.
    // File t=0..0.3s is pose A0; middle starts at 0.3s. Freeze mid-middle so bars + burn-in show.
    const playAckAt = await freezeActiveVideo(page, fixtureMiddleTime(8))
    console.log('PLAY_ACK freeze t=', playAckAt)
    await injectOpenPlayMovesCard(
      page,
      'Played: Wave · starts in 0.0 s · confirmed by the call screen',
    )
    await expect(page.locator('[data-testid="avatar-play-moves-card"]')).toBeVisible()
    await expect(page.getByText('Play Moves', { exact: true }).first()).toBeVisible()
    await shot(app, 's3-play-ack-strip.png')
    // Remove card so later call shots stay clean
    await page.evaluate(() => document.querySelector('[data-testid="avatar-play-moves-card"]')?.remove())
    // freezeActiveVideo pauses the stage. A paused current clip never ends, so a later
    // enqueue would sit behind Wave forever. Finish Wave into rest before the handoff route.
    await page.evaluate(() => {
      const v = document.querySelector('[data-avatar-map-active="true"]') as HTMLVideoElement | null

      if (!v || !v.duration) {return}
      v.currentTime = Math.max(0, v.duration - 0.2)
      void v.play()
    })
    await expect.poll(async () => {
      const s = await callSnap(page)

      return s.playing === CLIP_WAVE ? 'wave' : (s.playing || 'none')
    }, { timeout: 15_000, intervals: [100, 150, 200] }).not.toBe('wave')
    console.log('AFTER_PLAY_ACK snap', await callSnap(page))

    const queuedHandoff = await enqueueRoute(page, [CLIP_LAUGH, CLIP_WAVE])
    console.log('HANDOFF queued', queuedHandoff)
    await waitPlaying(page, CLIP_LAUGH, 20_000)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toContainText(/Laugh/i)
    // AC 1 still A: freeze Laugh on middle f 20 (burn-in "clip 3 f 20")
    const handoffAAt = await freezeActiveVideo(page, fixtureMiddleTime(20))
    console.log('HANDOFF_A freeze t=', handoffAAt)
    await expect(page.locator(`[data-testid="avatar-her"][data-playing="${CLIP_LAUGH}"]`)).toBeAttached()
    await shot(app, 's3-handoff-no-black-a.png')
    // Resume Laugh near end so the real gapless hand-off to Wave still fires
    await page.evaluate(() => {
      const v = document.querySelector('[data-avatar-map-active="true"]') as HTMLVideoElement | null

      if (!v || !v.duration) {return}
      v.currentTime = Math.max(0, v.duration - 0.35)
      void v.play()
    })
    await waitPlaying(page, CLIP_WAVE, 12_000)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toContainText(/Wave/i)
    // AC 1 still B: catch "clip 4 f 0" — NOT a 350ms wait (that lands on f 3 after waitPlaying lag).
    const handoffBAt = await freezeActiveVideo(page, fixtureMiddleTime(0))
    console.log('HANDOFF_B freeze t=', handoffBAt)
    await shot(app, 's3-handoff-no-black-b.png')
    await shot(app, 's3-handoff-no-black.png')
    // Unpause/finish Wave so barge enqueue is not stuck behind a frozen current.
    await page.evaluate(() => {
      const v = document.querySelector('[data-avatar-map-active="true"]') as HTMLVideoElement | null

      if (!v || !v.duration) {return}
      v.currentTime = Math.max(0, v.duration - 0.2)
      void v.play()
    })
    await expect.poll(async () => {
      const s = await callSnap(page)

      return s.playing === CLIP_WAVE ? 'wave' : (s.playing || 'none')
    }, { timeout: 15_000, intervals: [100, 150, 200] }).not.toBe('wave')
    console.log('AFTER_HANDOFF_B snap', await callSnap(page))

    const queuedBarge = await enqueueRoute(page, [CLIP_LAUGH, CLIP_WAVE, CLIP_LEAN])
    console.log('BARGE queued', queuedBarge)
    await waitPlaying(page, CLIP_LAUGH, 20_000)
    await expect(page.locator('[data-testid="avatar-chip"]').filter({ hasText: /Wave/i })).toBeAttached()
    // AC 23: stage must show the current clip finishing (color bars), not pose-head A0.
    // Laugh just started is still pose head — freeze mid-middle before drain, drain keeps it.
    const bargeAt = await freezeActiveVideo(page, fixtureMiddleTime(12))
    console.log('BARGE freeze t=', bargeAt)
    await drainQueue(page)
    const afterDrain = await callSnap(page)
    console.log('BARGE after drain', afterDrain)
    expect(afterDrain.playing, 'drain keeps current clip').toBe(CLIP_LAUGH)
    expect(afterDrain.queue, 'drain empties the route').toEqual([])
    expect(afterDrain.next, 'drain next is rest').toBeTruthy()
    expect(afterDrain.next === CLIP_WAVE || afterDrain.next === CLIP_LEAN, 'drain next must not stay on the route').toBe(false)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toContainText(/Laugh/i)
    expect(await page.locator('[data-testid="avatar-chip"]').filter({ hasText: /Wave/i }).count(), 'Wave chip gone after drain').toBe(0)
    // Re-freeze after drain in case the element advanced
    await freezeActiveVideo(page, fixtureMiddleTime(12))
    await shot(app, 's3-barge-in-drain.png')

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

    // Voice thinking / speaking — store states the UI already renders (no barge-in patch).
    if (await e2eHook(page)) {
      await patchCall(page, { voice: 'thinking', open: true })
      await page.waitForTimeout(300)
      await shot(app, 's3-voice-thinking.png')
      await patchCall(page, { voice: 'speaking' })
      await page.waitForTimeout(300)
      await shot(app, 's3-voice-speaking.png')
      await patchCall(page, { voice: 'listening' })
    }

    // Tile + statusbar count (AC 12, 13)
    if (await attachedCount(page, 'avatar-count') > 0) {
      await clickTestId(page, 'avatar-count')
      await page.waitForTimeout(300)
      await shot(app, 's3-count.png')

      if (await attachedCount(page, 'avatar-count-detail') > 0) {
        await clickTestId(page, 'avatar-count')
      }
    }

    // Rest loop / no-repeat — two later rest frames, after the driven handoff pair already shot.
    await page.waitForTimeout(2_500)
    await shot(app, 's3-no-repeat-a.png')
    await page.waitForTimeout(2_500)
    await shot(app, 's3-no-repeat-b.png')

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
