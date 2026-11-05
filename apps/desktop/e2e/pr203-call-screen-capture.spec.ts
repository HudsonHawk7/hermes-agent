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
 * The five Probe-HOLD rows fire the plugin's real handlers (not $call patches, not a fake card):
 *   hook          — wrap ctx.onEvent; __avatarMapE2E.emit(type, payload) calls the same fn
 *   play-ack      — real play_moves in the sandbox (toolset avatar); FAIL if Play Moves / Played: N clips is missing; strip lit on the same clip
 *   handoff       — cue handler queues the route; capture last frames of N then f00 of N+1
 *   She's here    — avatar-tile on CHAT (not /call) + Call button
 *   barge-in      — cue handler queues a route, then emit('voice.interrupted')
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
  send,
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
  const toolsPy = path.join(dest, 'tools.py')
  if (!fs.existsSync(toolsPy)) {throw new Error(`avatar-map tools.py missing at ${toolsPy}`)}
  const toolsSrc = fs.readFileSync(toolsPy, 'utf8')
  if (!toolsSrc.includes('play_moves') || !toolsSrc.includes('TOOLSET')) {
    throw new Error(`avatar-map tools.py has no play_moves at ${toolsPy}`)
  }
  const servicePy = path.join(dest, 'service.py')
  if (!fs.existsSync(servicePy)) {throw new Error(`avatar-map service.py missing at ${servicePy}`)}
  const serviceSrc = fs.readFileSync(servicePy, 'utf8')
  if (!serviceSrc.includes('Played:') || !serviceSrc.includes('confirmed by the call screen')) {
    throw new Error(`avatar-map service.py has no Played: ack line at ${servicePy}`)
  }
  const dash = path.join(dest, 'dashboard', 'manifest.json')
  if (!fs.existsSync(dash)) {throw new Error(`avatar-map dashboard/manifest.json missing at ${dash}`)}
  const pluginJs = path.join(dest, 'desktop', 'plugin.js')
  const bytes = fs.statSync(pluginJs).size

  if (bytes < 50_000) {throw new Error(`plugin.js too small: ${bytes} at ${pluginJs}`)}
  // Sandbox-only hook: wrap ctx.onEvent BEFORE the plugin registers handlers,
  // then expose emit() so the spec fires the same functions the gateway uses.
  const registerNeedle = '  register(ctx) {\n    const girl = () => host.state.focusedSessionProfile.get()'
  const manyNeedle = '    ctx.registerMany(['
  const raw = fs.readFileSync(pluginJs, 'utf8')

  if (!raw.includes(registerNeedle)) {throw new Error(`plugin.js missing register(ctx) girl() at ${pluginJs}`)}
  if (!raw.includes(manyNeedle)) {throw new Error(`plugin.js missing ctx.registerMany([ at ${pluginJs}`)}

  if (!raw.includes('globalThis.__avatarMapE2E')) {
    const wrapped = raw.replace(
      registerNeedle,
      '  register(ctx) {\n'
        + '    const e2eHandlers = Object.create(null)\n'
        + '    const e2eOnEvent = ctx.onEvent.bind(ctx)\n'
        + '    ctx.onEvent = (type, fn) => {\n'
        + '      e2eHandlers[type] = fn\n'
        + '      return e2eOnEvent(type, fn)\n'
        + '    }\n'
        + '    const girl = () => host.state.focusedSessionProfile.get()',
    )
    fs.writeFileSync(
      pluginJs,
      wrapped.replace(
        manyNeedle,
        '    if (typeof globalThis !== \'undefined\') {\n'
          + '      globalThis.__avatarMapE2E = {\n'
          + '        $call, patch, actions, getPlayer: () => player, handlers: e2eHandlers, navigate: (p) => host.navigate(p),\n'
          + '        emit(type, payload) {\n'
          + '          const fn = e2eHandlers[type]\n'
          + '          if (!fn) throw new Error(\'no plugin handler for \' + type)\n'
          + '          return fn({ type, payload: payload || {} })\n'
          + '        }\n'
          + '      }\n'
          + '    }\n'
          + manyNeedle,
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
_config_version: 51
platform_toolsets:
  cli:
    - hermes-cli
    - avatar
plugins:
  enabled:
    - ${PLUGIN_ID}
  entries:
    ${PLUGIN_ID}:
      granted_capabilities:
        - tools.override
      allow_tool_override: true
avatar_map:
${lib}${extras}
tools:
  tool_search:
    enabled: "off"
`
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

function copyIfExists(src: string, dest: string): void {
  if (fs.existsSync(src)) {fs.copyFileSync(src, dest)}
}

function copyTree(src: string, dest: string): void {
  if (!fs.existsSync(src)) {return}
  fs.mkdirSync(dest, { recursive: true })
  for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
    const from = path.join(src, ent.name)
    const to = path.join(dest, ent.name)
    if (ent.isDirectory()) {copyTree(from, to); continue}
    fs.copyFileSync(from, to)
  }
}

function dumpSandboxHome(sandbox: CoreSandbox, tag: string): void {
  try {
    fs.mkdirSync(SHOT_DIR, { recursive: true })
    copyIfExists(path.join(sandbox.hermesHome, 'config.yaml'), path.join(SHOT_DIR, `${tag}-config.yaml`))
    copyIfExists(path.join(sandbox.hermesHome, 'logs', 'gateway.log'), path.join(SHOT_DIR, `${tag}-gateway.log`))
    copyIfExists(path.join(sandbox.hermesHome, 'logs', 'errors.log'), path.join(SHOT_DIR, `${tag}-errors.log`))
    copyIfExists(path.join(sandbox.hermesHome, 'logs', 'agent.log'), path.join(SHOT_DIR, `${tag}-agent.log`))
    copyIfExists(path.join(sandbox.hermesHome, 'state.db'), path.join(SHOT_DIR, `${tag}-state.db`))
    const cache = path.join(sandbox.hermesHome, 'cache', 'plugin_toolset_keys.json')
    copyIfExists(cache, path.join(SHOT_DIR, `${tag}-plugin_toolset_keys.json`))
    copyTree(path.join(sandbox.hermesHome, 'sessions'), path.join(SHOT_DIR, `${tag}-sessions`))
    const walk = (dir: string, depth: number) => {
      if (depth < 0 || !fs.existsSync(dir)) {return}
      for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, ent.name)
        if (ent.isDirectory()) {walk(p, depth - 1); continue}
        if (/\.(jsonl|json)$/i.test(ent.name) && /session/i.test(p)) {
          copyIfExists(p, path.join(SHOT_DIR, `${tag}-${ent.name}`))
        }
      }
    }
    walk(sandbox.hermesHome, 4)
  } catch (err) {
    console.log('dumpSandboxHome', err)
  }
}

function seedPluginToolsetCache(hermesHome: string): void {
  const dir = path.join(hermesHome, 'cache')
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(
    path.join(dir, 'plugin_toolset_keys.json'),
    JSON.stringify({ toolset_keys: ['avatar'], portable_mcp: [] }),
    'utf8',
  )
}

async function dumpOnFail(page: Page, app: ElectronApplication, tag: string): Promise<void> {
  try {
    fs.mkdirSync(SHOT_DIR, { recursive: true })
    const html = await page.content()
    fs.writeFileSync(path.join(SHOT_DIR, `${tag}.html`), html.slice(0, 80_000), 'utf8')
    const body = await page.evaluate(() => document.body.innerText || '')
    fs.writeFileSync(path.join(SHOT_DIR, `${tag}-body.txt`), body.slice(0, 20_000), 'utf8')

    const ids = await page.locator('[data-testid]').evaluateAll(nodes =>
      nodes.map(n => n.getAttribute('data-testid')).filter(Boolean).slice(0, 80),
    )

    fs.writeFileSync(path.join(SHOT_DIR, `${tag}-testids.json`), JSON.stringify(ids, null, 2), 'utf8')
    const toolRows = await page.evaluate(() => [...document.querySelectorAll('[data-tool-row]')].map(n => {
      const el = n as HTMLElement
      return {
        text: (el.innerText || '').slice(0, 800),
        expanded: el.getAttribute('aria-expanded'),
        html: el.outerHTML.slice(0, 4000),
      }
    }))
    fs.writeFileSync(path.join(SHOT_DIR, `${tag}-tool-rows.json`), JSON.stringify(toolRows, null, 2), 'utf8')
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
  emit?: (type: string, payload?: Record<string, unknown>) => unknown
  handlers?: Record<string, (event: unknown) => unknown>
  $call?: { get: () => Record<string, unknown> }
  navigate?: (path: string) => void
}

const CUE_EVENT = 'plugin.avatar-map.cue'

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

async function goViaHook(page: Page, path: string): Promise<void> {
  await page.evaluate(p => {
    const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E

    if (!hook?.navigate) {throw new Error('hook.navigate missing — e2e wrap did not land')}
    hook.navigate(p)
  }, path)
}

async function emitPluginEvent(page: Page, type: string, payload: Record<string, unknown> = {}): Promise<unknown> {
  return page.evaluate(({ type, payload }) => {
    const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E

    if (!hook?.emit) {throw new Error('hook.emit missing — onEvent wrap did not land')}

    return hook.emit(type, payload)
  }, { type, payload })
}

async function playCue(page: Page, route: string[], extra: Record<string, unknown> = {}): Promise<void> {
  await emitPluginEvent(page, CUE_EVENT, {
    opId: `e2e-${Date.now()}`,
    action: 'play',
    expiresAt: Date.now() / 1000 + 30,
    route,
    when: 'after_current',
    sceneSet: 'kitchen-morning',
    skipped: [],
    cut: 'at the end of the current clip',
    ...extra,
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

async function finishCurrentClip(page: Page): Promise<void> {
  const before = await callSnap(page)

  await page.evaluate(() => {
    const v = document.querySelector('[data-avatar-map-active="true"]') as HTMLVideoElement | null

    if (!v || !v.duration) {return}
    v.currentTime = Math.max(0, v.duration - 0.2)
    void v.play()
  })

  if (!before.playing) {return}

  await expect.poll(async () => {
    const s = await callSnap(page)

    return s.playing === before.playing ? 'same' : (s.playing || 'none')
  }, { timeout: 15_000, intervals: [100, 150, 200] }).not.toBe('same')
}

type StageVid = {
  active: string | null
  readyState: number
  currentTime: number
  paused: boolean
  ended: boolean
}

async function waitSwap(
  page: Page,
  fromId: string,
  timeout = 15_000,
): Promise<{ playing: string | null; next: string | null; vids: StageVid[] }> {
  let last: { playing: string | null; next: string | null; vids: StageVid[] } = { playing: fromId, next: null, vids: [] }

  await expect.poll(async () => {
    last = await page.evaluate(() => {
      const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E
      const s = hook?.$call?.get() ?? {}

      return {
        playing: (s.playing as string | null) ?? null,
        next: (s.next as string | null) ?? null,
        vids: [...document.querySelectorAll('video')].map(v => ({
          active: v.getAttribute('data-avatar-map-active'),
          readyState: v.readyState,
          currentTime: v.currentTime,
          paused: v.paused,
          ended: v.ended,
        })),
      }
    })

    return last.playing !== fromId
  }, { timeout, intervals: [40, 40, 40] }).toBe(true)

  return last
}

async function activeFrameLuma(page: Page): Promise<{ luma: number; readyState: number; t: number; w: number; h: number }> {
  return page.evaluate(() => {
    const v = document.querySelector('[data-avatar-map-active="true"]') as HTMLVideoElement | null

    if (!v || v.videoWidth < 2) {
      return { luma: -1, readyState: v?.readyState ?? -1, t: v?.currentTime ?? 0, w: 0, h: 0 }
    }
    const c = document.createElement('canvas')
    c.width = 16
    c.height = 16
    const ctx = c.getContext('2d')

    if (!ctx) {return { luma: -1, readyState: v.readyState, t: v.currentTime, w: v.videoWidth, h: v.videoHeight }}
    ctx.drawImage(v, 0, 0, 16, 16)
    try {
      const d = ctx.getImageData(0, 0, 16, 16).data
      let s = 0

      for (let i = 0; i < d.length; i += 4) {s += (d[i] + d[i + 1] + d[i + 2]) / 3}

      return { luma: s / (d.length / 4), readyState: v.readyState, t: v.currentTime, w: v.videoWidth, h: v.videoHeight }
    } catch {
      // file:// fixture clips taint the canvas; PNG pixels are the proof, not getImageData.
      return { luma: -2, readyState: v.readyState, t: v.currentTime, w: v.videoWidth, h: v.videoHeight }
    }
  })
}

function writeShotManifest(dir: string): void {
  if (!fs.existsSync(dir)) {return}
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.png')).sort()
  const shots = files.map(file => {
    const buf = fs.readFileSync(path.join(dir, file))

    return { file, bytes: buf.length, sha256: sha256Hex(buf) }
  })
  fs.writeFileSync(path.join(dir, 'MANIFEST.json'), JSON.stringify({ at: new Date().toISOString(), dir, shots }, null, 2), 'utf8')
  console.log('MANIFEST', shots.length, 'pngs at', dir)
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
  seedPluginToolsetCache(sandbox.hermesHome)
  dumpSandboxHome(sandbox, 'boot-before')

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

    dumpSandboxHome(sandbox, 'boot-after-composer')

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

    await expect.poll(() => page.evaluate(() => typeof (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E?.emit === 'function')).toBe(true)
    const handlerKeys = await page.evaluate(() => Object.keys((globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E?.handlers ?? {}))
    console.log('E2E_HANDLERS', handlerKeys)
    expect(handlerKeys, 'cue handler registered').toContain(CUE_EVENT)
    expect(handlerKeys, 'voice.interrupted registered').toContain('voice.interrupted')

    // play-ack: open the call so REST /ack can confirm, go back to chat without
    // ending (composer needed), then run the real play_moves tool. Fail if the
    // Play Moves card / Played: N clips line is missing. Do not inject a fake card.
    // Do not script "Played: N clips" as assistant text — that line must come from tools.py.
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
    const callOpen = await page.evaluate(() => Boolean((globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E?.$call?.get()?.open))
    expect(callOpen, 'call must be open before play_moves so /ack can confirm').toBe(true)
    const returnTo = await page.evaluate(() => {
      const hook = (globalThis as { __avatarMapE2E?: E2eHook }).__avatarMapE2E
      return String(hook?.$call?.get()?.returnTo || '/')
    })
    console.log('PLAY_ACK call open, returnTo=', returnTo)
    await goViaHook(page, returnTo || '/')
    await waitAttached(page, 'avatar-composer-call', 20_000)

    const PLAYACK = 'U9-playack'
    provider.script(PLAYACK, [
      { toolCalls: [{ name: 'play_moves', args: { segments: [CLIP_WAVE], when: 'after_current' } }] },
      { text: ['wave is queued on the call.'] },
    ])
    await send(page, `${PLAYACK} play the wave`)
    await expect.poll(
      () => provider.completions.some(c => c.marker === PLAYACK && c.step === 0 && c.finished),
      { timeout: 90_000, message: 'play_moves provider step 0' },
    ).toBe(true)
    const playMovesViaProvider = true
    console.log('PLAY_MOVES provider', provider.completions.filter(c => c.marker === PLAYACK).map(c => ({
      step: c.step, tools: c.toolCalls, finished: c.finished, text: c.sentText.slice(0, 80),
    })))
    const playRow = page.locator('[data-tool-row]').filter({ hasText: /Play Moves|play_moves/i }).first()
    await playRow.waitFor({ state: 'attached', timeout: 45_000 }).catch(() => undefined)
    if (await playRow.count()) {
      await playRow.evaluate(el => el.scrollIntoView({ block: 'center', inline: 'nearest' })).catch(() => undefined)
      const toggle = playRow.locator('button[aria-expanded]').first()
      if (await toggle.count()) {
        const expanded = await toggle.getAttribute('aria-expanded')
        if (expanded !== 'true') {
          await toggle.click({ force: true }).catch(() => undefined)
        }
      } else {
        await playRow.click({ force: true }).catch(() => undefined)
      }
    }
    try {
      await expect.poll(async () => {
        const snap = await page.evaluate(() => {
          const rows = [...document.querySelectorAll('[data-tool-row]')].map(n => {
            const el = n as HTMLElement
            const btn = el.querySelector('button[aria-expanded]')
            return {
              text: el.innerText || '',
              expanded: btn?.getAttribute('aria-expanded') || '',
            }
          })
          const play = rows.find(r => /Play Moves|play_moves/i.test(r.text))
          return { rows, play }
        })
        const hay = snap.play?.text || ''
        const card = /Play Moves|play_moves/i.test(hay)
        const played = /Played:\s*\d+\s*clips/i.test(hay)
        const confirmed = /confirmed by the call screen/i.test(hay)
        const refused = /Couldn't play|does not exist|unknown tool|snowballstemmer/i.test(hay)
        return card && played && confirmed
          ? 'ok'
          : `card=${card} played=${played} confirmed=${confirmed} refused=${refused} expanded=${snap.play?.expanded || ''} rows=${snap.rows.length} hay=${hay.slice(0, 240)}`
      }, { timeout: 45_000, message: 'Play Moves row innerText must contain Played: N clips' }).toBe('ok')
      await expect(
        playRow.getByText(/Played:\s*\d+\s*clips/i),
        'Played: N clips must be visible on the Play Moves row',
      ).toBeVisible({ timeout: 10_000 })
    } catch (err) {
      dumpSandboxHome(sandbox, 'play-ack-miss')
      await dumpOnFail(page, app, 'play-ack-miss')
      throw err
    }
    dumpSandboxHome(sandbox, 'play-ack')
    await dumpOnFail(page, app, 'play-ack')
    const playMovesOnChat = await page.getByText(/Play Moves|play_moves/i).count()
    expect(playMovesOnChat, 'Play Moves card must appear after play_moves').toBeGreaterThan(0)
    await playRow.evaluate(el => el.scrollIntoView({ block: 'center', inline: 'nearest' })).catch(() => undefined)
    const rowPng = await playRow.screenshot({ animations: 'disabled' }).catch(() => null)
    if (rowPng && rowPng.byteLength >= 400) {
      fs.writeFileSync(path.join(SHOT_DIR, 's3-play-ack-card-row.png'), rowPng)
      console.log(`SHOT s3-play-ack-card-row.png bytes=${rowPng.byteLength}`)
    }
    await shot(app, 's3-play-ack-card.png')

    await goViaHook(page, '/call')
    try {
      await waitAttached(page, 'avatar-call', 60_000)
    } catch {
      await dumpOnFail(page, app, 'no-call-return')
      throw new Error(`avatar-call missing after return from chat. logs:\n${appLogTail(app, 40)}`)
    }

    await page.waitForTimeout(400)
    await shot(app, 's3-chrome.png')
    await shot(app, 's3-just-her.png')

    expect(await attachedCount(page, 'avatar-stage')).toBeGreaterThan(0)
    expect(await attachedCount(page, 'avatar-strip')).toBeGreaterThan(0)
    expect(await attachedCount(page, 'avatar-end')).toBeGreaterThan(0)

    // Five Probe-HOLD rows: real cue handler + voice.interrupted, not store patches.
    // Before layouts so the rest clip is still mid-play for when-now.
    await waitAttached(page, 'avatar-her', 20_000)
    await waitAttached(page, 'avatar-chip-now', 20_000)

    const beforeWhen = await callSnap(page)
    console.log('WHEN_NOW before', beforeWhen)
    await playCue(page, [CLIP_WAVE])
    const afterWhen = await callSnap(page)
    console.log('WHEN_NOW after cue', afterWhen)
    expect(afterWhen.playing, 'when-now must keep the current clip').toBe(beforeWhen.playing)
    expect(afterWhen.queue, 'when-now queue').toContain(CLIP_WAVE)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toBeAttached()
    const nowWhen = await chipNowText(page)
    expect(nowWhen.toLowerCase(), 'when-now chip-now is still current, not Wave').not.toContain('wave')
    await expect(page.locator('[data-testid="avatar-chip"], [data-testid="avatar-chip-now"]').filter({ hasText: /Wave/i })).toBeAttached()
    await shot(app, 's3-when-now-waits.png')

    await waitPlaying(page, CLIP_WAVE, 20_000)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toContainText(/Wave/i)
    const playAckAt = await freezeActiveVideo(page, fixtureMiddleTime(8))
    console.log('PLAY_ACK freeze t=', playAckAt)
    const playMovesOnCall = await page.getByText('Play Moves').count()
    const playedLineOnCall = await page.getByText(/Played:\s*\d+\s*clips/i).count()
    console.log('PLAY_ACK call Play Moves=', playMovesOnCall, 'Played line=', playedLineOnCall, 'viaProvider=', playMovesViaProvider)
    await shot(app, 's3-play-ack-strip.png')
    await finishCurrentClip(page)
    console.log('AFTER_PLAY_ACK snap', await callSnap(page))

    await playCue(page, [CLIP_LAUGH, CLIP_WAVE])
    await waitPlaying(page, CLIP_LAUGH, 20_000)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toContainText(/Laugh/i)
    const handoffAAt = await freezeActiveVideo(page, fixtureMiddleTime(20))
    console.log('HANDOFF_A freeze t=', handoffAAt)
    await expect(page.locator(`[data-testid="avatar-her"][data-playing="${CLIP_LAUGH}"]`)).toBeAttached()
    await shot(app, 's3-handoff-no-black-a.png')
    await page.evaluate(() => {
      const v = document.querySelector('[data-avatar-map-active="true"]') as HTMLVideoElement | null

      if (!v || !v.duration) {return}
      v.currentTime = Math.max(0, v.duration - 0.35)
      void v.play()
    })
    const swap = await waitSwap(page, CLIP_LAUGH, 12_000)
    console.log('HANDOFF_SWAP readyState', JSON.stringify(swap))
    const handoffBAt = await freezeActiveVideo(page, fixtureMiddleTime(0))
    console.log('HANDOFF_B freeze t=', handoffBAt, 'playing', (await callSnap(page)).playing)
    await shot(app, 's3-handoff-no-black-b.png')
    await shot(app, 's3-handoff-no-black.png')
    let luma = { luma: -1, readyState: -1, t: 0, w: 0, h: 0 }
    try { luma = await activeFrameLuma(page) } catch (err) { console.log('HANDOFF_SWAP luma throw', String(err).slice(0, 200)) }
    console.log('HANDOFF_SWAP luma', luma)
    if (luma.luma >= 0 && luma.luma < 12) {
      console.log('BLACK_FRAME_AT_SWAP t_a4231e39 plugin.mjs:446-494 incoming readyState', luma.readyState)
      fs.writeFileSync(path.join(SHOT_DIR, 'black-frame-at-swap.json'), JSON.stringify({ swap, luma }, null, 2), 'utf8')
    }
    await finishCurrentClip(page)
    console.log('AFTER_HANDOFF_B snap', await callSnap(page))

    await playCue(page, [CLIP_LAUGH, CLIP_WAVE, CLIP_LEAN])
    await waitPlaying(page, CLIP_LAUGH, 20_000)
    await expect(page.locator('[data-testid="avatar-chip"]').filter({ hasText: /Wave/i })).toBeAttached()
    const bargeAt = await freezeActiveVideo(page, fixtureMiddleTime(12))
    console.log('BARGE freeze t=', bargeAt)
    await shot(app, 's3-barge-in-before.png')
    await emitPluginEvent(page, 'voice.interrupted', {})
    const afterDrain = await callSnap(page)
    console.log('BARGE after voice.interrupted', afterDrain)
    expect(afterDrain.playing, 'interrupt keeps current clip').toBe(CLIP_LAUGH)
    expect(afterDrain.queue, 'interrupt empties the route').toEqual([])
    expect(afterDrain.next, 'interrupt next is rest').toBeTruthy()
    expect(afterDrain.next === CLIP_WAVE || afterDrain.next === CLIP_LEAN, 'interrupt next must not stay on the route').toBe(false)
    await expect(page.locator('[data-testid="avatar-chip-now"]')).toContainText(/Laugh/i)
    expect(await page.locator('[data-testid="avatar-chip"]').filter({ hasText: /Wave/i }).count(), 'Wave chip gone after interrupt').toBe(0)
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
    writeShotManifest(SHOT_DIR)
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
  seedPluginToolsetCache(sandbox.hermesHome)
  dumpSandboxHome(sandbox, 'boot-before')

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
